"""Tests for AnchorIR validator."""

import pytest
import triton_anchor
from triton_anchor.anchor_ir import (
    AnchorIRDialectStatus,
    AnchorIRValidationReport,
    AnchorIRValidator,
    AnchorIRError,
)


VALID_LINALG_IR = """
module attributes {hw.name = "test"} {
  func.func @kernel(%arg0: memref<128xf32>, %arg1: memref<128xf32>) {
    %c0 = arith.constant 0 : index
    %c128 = arith.constant 128 : index
    %c1 = arith.constant 1 : index
    scf.for %i = %c0 to %c128 step %c1 {
      %val = memref.load %arg0[%i] : memref<128xf32>
      %result = math.exp %val : f32
      memref.store %result, %arg1[%i] : memref<128xf32>
    }
    return
  }
}
"""

INVALID_IR_WITH_TT = """
module {
  func.func @kernel(%arg0: !tt.ptr<f32>) {
    %0 = tt.load %arg0 : !tt.ptr<f32>
    %1 = arith.addf %0, %0 : f32
    tt.store %arg0, %1 : !tt.ptr<f32>
    return
  }
}
"""

MIXED_IR = """
module {
  func.func @kernel(%arg0: memref<128xf32>) {
    %0 = linalg.generic {indexing_maps = [], iterator_types = []}
         ins(%arg0 : memref<128xf32>) {
      ^bb0(%in: f32):
        linalg.yield %in : f32
    } -> tensor<128xf32>
    %1 = tt.splat %0 : tensor<128xf32>
    return
  }
}
"""


class TestAnchorIRValidator:
    def test_valid_ir(self):
        v = AnchorIRValidator()
        assert v.is_valid(VALID_LINALG_IR)
        assert v.validate(VALID_LINALG_IR) == []

    def test_invalid_ir_forbidden_dialect(self):
        v = AnchorIRValidator()
        violations = v.validate(INVALID_IR_WITH_TT)
        assert len(violations) > 0
        assert any(viol.dialect == "tt" for viol in violations)
        assert all(
            viol.status == AnchorIRDialectStatus.FORBIDDEN
            for viol in violations
            if viol.dialect == "tt"
        )

    def test_mixed_ir(self):
        v = AnchorIRValidator()
        violations = v.validate(MIXED_IR)
        assert len(violations) > 0
        tt_violations = [v for v in violations if v.dialect == "tt"]
        assert len(tt_violations) > 0

    def test_forbidden_type_reference_detected(self):
        v = AnchorIRValidator()
        ir_with_tt_type = """
        module {
          func.func @kernel(%arg0: !tt.ptr<f32>) {
            return
          }
        }
        """
        violations = v.validate(ir_with_tt_type)
        assert any(
            viol.dialect == "tt"
            and viol.kind == "type"
            and viol.op_name == "!tt.ptr"
            for viol in violations
        )

    def test_forbidden_attribute_reference_detected(self):
        v = AnchorIRValidator()
        ir_with_gpu_attr = """
        #layout = #triton_gpu.blocked_layout<{sizePerThread = [1]}>
        module {
          func.func @kernel(%arg0: tensor<128xf32, #layout>) {
            return
          }
        }
        """
        violations = v.validate(ir_with_gpu_attr)
        assert any(
            viol.dialect == "triton_gpu"
            and viol.kind == "attribute"
            and viol.op_name == "#triton_gpu.blocked_layout"
            for viol in violations
        )

    def test_post_hook_allows_declared_extension_attribute(self):
        v = AnchorIRValidator()
        ir_with_ext_attr = """
        #layout = #xsmt.private_layout<{bank = 0}>
        module {
          func.func @kernel(%arg0: memref<128xf32, #layout>) {
            return
          }
        }
        """
        pre_violations = v.validate_pre_hook(ir_with_ext_attr)
        assert any(
            viol.dialect == "xsmt" and viol.kind == "attribute"
            for viol in pre_violations
        )
        assert v.validate_post_hook(ir_with_ext_attr, ext_allowed={"xsmt"}) == []

    def test_post_hook_extension_cannot_override_forbidden_type(self):
        v = AnchorIRValidator()
        ir_with_tt_type = """
        module {
          func.func @kernel(%arg0: !tt.ptr<f32>) {
            return
          }
        }
        """
        violations = v.validate_post_hook(ir_with_tt_type, ext_allowed={"tt"})
        assert any(
            viol.dialect == "tt" and viol.kind == "type" for viol in violations
        )

    def test_validation_report_summarizes_dialects_and_kinds(self):
        v = AnchorIRValidator()
        ir_with_mixed_violations = """
        #layout = #triton_gpu.blocked_layout<{sizePerThread = [1]}>
        module {
          func.func @kernel(%arg0: !tt.ptr<f32>) {
            %0 = unknown.foo %arg0 : !tt.ptr<f32>
            return
          }
        }
        """
        report = v.validate_report(ir_with_mixed_violations)
        assert isinstance(report, AnchorIRValidationReport)
        assert not report.is_valid
        assert report.count_by_dialect()["tt"] == 2
        assert report.count_by_dialect()["triton_gpu"] == 1
        assert report.count_by_dialect()["unknown"] == 1
        assert report.count_by_kind()["operation"] == 1
        assert report.count_by_kind()["type"] == 2
        assert report.count_by_kind()["attribute"] == 1
        assert report.count_by_status()["forbidden"] == 3
        assert report.count_by_status()["unknown"] == 1
        assert "4 violation(s)" in report.summary()
        assert "by status: forbidden=3, unknown=1" in report.summary()

    def test_post_hook_report_uses_extension_dialects(self):
        v = AnchorIRValidator()
        ir_with_ext_type = """
        module {
          func.func @kernel(%arg0: !xsmt.private_ptr<f32>) {
            return
          }
        }
        """
        pre_report = v.validate_pre_hook_report(ir_with_ext_type)
        post_report = v.validate_post_hook_report(
            ir_with_ext_type,
            ext_allowed={"xsmt"},
        )
        assert not pre_report.is_valid
        assert post_report.is_valid

    def test_pre_hook_validate_and_raise_rejects_extensions(self):
        v = AnchorIRValidator()
        ir_with_ext_type = """
        module {
          func.func @kernel(%arg0: !xsmt.private_ptr<f32>) {
            return
          }
        }
        """
        with pytest.raises(
            AnchorIRError,
            match=r"AnchorIR pre-hook validation failed.*unknown=1",
        ):
            v.validate_pre_hook_and_raise(ir_with_ext_type, context="kernel")

    def test_post_hook_validate_and_raise_allows_declared_extensions(self):
        v = AnchorIRValidator()
        ir_with_ext_type = """
        module {
          func.func @kernel(%arg0: !xsmt.private_ptr<f32>) {
            return
          }
        }
        """
        v.validate_post_hook_and_raise(
            ir_with_ext_type,
            ext_allowed={"xsmt"},
            context="kernel",
        )

    def test_post_hook_validate_and_raise_rejects_forbidden(self):
        v = AnchorIRValidator()
        ir_with_tt_type = """
        module {
          func.func @kernel(%arg0: !tt.ptr<f32>) {
            return
          }
        }
        """
        with pytest.raises(
            AnchorIRError,
            match=r"AnchorIR post-hook validation failed.*forbidden=1",
        ):
            v.validate_post_hook_and_raise(
                ir_with_tt_type,
                ext_allowed={"tt"},
                context="kernel",
            )

    def test_public_report_types_are_exported(self):
        assert triton_anchor.AnchorIRDialectStatus is AnchorIRDialectStatus
        assert triton_anchor.AnchorIRValidationReport is AnchorIRValidationReport
        assert hasattr(triton_anchor, "AnchorIRViolation")

    def test_validate_and_raise(self):
        v = AnchorIRValidator()
        with pytest.raises(AnchorIRError, match=r"AnchorIR validation failed.*tt="):
            v.validate_and_raise(INVALID_IR_WITH_TT, context="test_kernel")

    def test_extra_allowed_dialects(self):
        v = AnchorIRValidator(extra_allowed={"xsmt", "xsmt_async"})
        ir_with_ext = """
        module {
          func.func @kernel() {
            %0 = xsmt.alloc : memref<128xf32>
            return
          }
        }
        """
        assert v.is_valid(ir_with_ext)

    def test_extra_forbidden_dialects(self):
        v = AnchorIRValidator(extra_forbidden={"custom_bad"})
        ir_with_custom = """
        module {
          func.func @kernel() {
            %0 = custom_bad.evil_op : f32
            return
          }
        }
        """
        violations = v.validate(ir_with_custom)
        assert any(viol.dialect == "custom_bad" for viol in violations)

    def test_comments_ignored(self):
        v = AnchorIRValidator()
        ir_with_comments = """
        // tt.load should be ignored in comments
        # tt.store also ignored
        module {
          func.func @kernel(%arg0: memref<128xf32>) {
            return // !tt.ptr<f32> and #triton_gpu.layout are ignored
          }
        }
        """
        assert v.is_valid(ir_with_comments)
