"""Tests for HWCapability and ComputeParadigm."""

import pytest

from triton_anchor.anchor_ir import AnchorIRTrack
from triton_anchor.hw_capability import (
    ComputeParadigm,
    GPGPUCapability,
    HWCapability,
    MatrixCapability,
    TensorCapability,
)


class TestComputeParadigm:
    def test_enum_values(self):
        assert ComputeParadigm.AME_MATRIX.value == "ame_matrix"
        assert ComputeParadigm.TENSOR_PROCESSOR.value == "tensor"
        assert ComputeParadigm.GPGPU.value == "gpgpu"


class TestHWCapability:
    def test_sophgo_capability(self):
        hw = HWCapability(
            name="sophgo-bm1684x",
            arch_family="tpu",
            compute_paradigm=ComputeParadigm.TENSOR_PROCESSOR,
            anchor_ir_track=AnchorIRTrack.LINALG,
            ptr_model="axis_info",
            tensor_cap=TensorCapability(num_cores=8),
        )
        assert hw.name == "sophgo-bm1684x"
        assert hw.compute_paradigm == ComputeParadigm.TENSOR_PROCESSOR
        assert hw.lowering_path == "linalg"

    def test_spacemit_capability(self):
        hw = HWCapability(
            name="spacemit-x60",
            arch_family="riscv",
            compute_paradigm=ComputeParadigm.AME_MATRIX,
            anchor_ir_track=AnchorIRTrack.LINALG,
            ptr_model="structured",
            matrix_cap=MatrixCapability(
                num_matrix_registers=8,
                tile_shape=(8, 8),
            ),
        )
        assert hw.arch_family == "riscv"
        assert hw.matrix_cap.num_matrix_registers == 8

    def test_gpu_capability(self):
        hw = HWCapability(
            name="usc-gpu",
            arch_family="gpu",
            compute_paradigm=ComputeParadigm.GPGPU,
            anchor_ir_track=AnchorIRTrack.TRITON_GPU,
            ptr_model="gpu",
            gpgpu_cap=GPGPUCapability(num_warps=4, warp_size=32),
        )
        assert hw.gpgpu_cap.num_warps == 4

    def test_to_gpu_target(self):
        hw = HWCapability(
            name="sophgo-bm1684x",
            arch_family="tpu",
            compute_paradigm=ComputeParadigm.TENSOR_PROCESSOR,
            anchor_ir_track=AnchorIRTrack.LINALG,
            ptr_model="axis_info",
            tensor_cap=TensorCapability(),
        )
        target = hw.to_gpu_target()
        # When triton is not installed, returns a dict
        if isinstance(target, dict):
            assert target["backend"] == "sophgo"
            assert target["warp_size"] == 0
        else:
            assert target.backend == "sophgo"

    def test_validation_missing_matrix_cap(self):
        with pytest.raises(ValueError, match="matrix_cap"):
            HWCapability(
                name="bad",
                arch_family="riscv",
                compute_paradigm=ComputeParadigm.AME_MATRIX,
                anchor_ir_track=AnchorIRTrack.LINALG,
                ptr_model="structured",
                # Missing matrix_cap!
            )


def _tensor_hw(**overrides) -> HWCapability:
    values = {
        "name": "test-tpu",
        "arch_family": "tpu",
        "compute_paradigm": ComputeParadigm.TENSOR_PROCESSOR,
        "anchor_ir_track": AnchorIRTrack.LINALG,
        "ptr_model": "axis_info",
        "tensor_cap": TensorCapability(),
    }
    values.update(overrides)
    return HWCapability(**values)


def test_compute_paradigm_string_is_resolved():
    hw = _tensor_hw(compute_paradigm="tensor")

    assert hw.compute_paradigm is ComputeParadigm.TENSOR_PROCESSOR


def test_unknown_compute_paradigm_string_is_rejected():
    with pytest.raises(ValueError, match="Unknown compute paradigm"):
        _tensor_hw(compute_paradigm="quantum")


def test_compute_paradigm_object_type_is_rejected():
    with pytest.raises(TypeError, match="must be a ComputeParadigm"):
        _tensor_hw(compute_paradigm=object())


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("name", ""),
        ("name", "   "),
        ("arch_family", ""),
        ("arch_family", None),
    ],
)
def test_identity_fields_must_be_non_empty(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        _tensor_hw(**{field_name: value})


@pytest.mark.parametrize("ptr_model", ["legacy", None, []])
def test_ptr_model_must_be_supported(ptr_model):
    with pytest.raises(ValueError, match="Unsupported ptr_model"):
        _tensor_hw(ptr_model=ptr_model)


@pytest.mark.parametrize("preferred_adapter", ["", "   ", 42])
def test_preferred_adapter_must_be_a_non_empty_string(preferred_adapter):
    with pytest.raises(ValueError, match="preferred_adapter"):
        _tensor_hw(preferred_adapter=preferred_adapter)


@pytest.mark.parametrize("num_cores", [0, -1, True])
def test_hw_num_cores_must_be_positive(num_cores):
    with pytest.raises(ValueError, match="num_cores"):
        _tensor_hw(num_cores=num_cores)


def test_paradigm_rejects_wrong_capability_type():
    with pytest.raises(TypeError, match="must be a TensorCapability"):
        _tensor_hw(tensor_cap=object())


def test_paradigm_rejects_additional_capability_descriptors():
    with pytest.raises(ValueError, match="does not allow: matrix_cap"):
        _tensor_hw(matrix_cap=MatrixCapability())


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        pytest.param(
            lambda: MatrixCapability(num_matrix_registers=0),
            "num_matrix_registers",
            id="matrix-register-count",
        ),
        pytest.param(
            lambda: MatrixCapability(tile_shape=(8, 0)),
            "tile_shape",
            id="matrix-tile-dimension",
        ),
        pytest.param(
            lambda: MatrixCapability(tile_shape=(8,)),
            "exactly 2 dimensions",
            id="matrix-tile-rank",
        ),
        pytest.param(
            lambda: MatrixCapability(tile_shape=None),
            "exactly 2 dimensions",
            id="matrix-tile-type",
        ),
        pytest.param(
            lambda: MatrixCapability(vector_length=0),
            "vector_length",
            id="matrix-vector-length",
        ),
        pytest.param(
            lambda: MatrixCapability(supported_dtypes=set()),
            "supported_dtypes",
            id="matrix-dtypes",
        ),
        pytest.param(
            lambda: TensorCapability(num_cores=0),
            "num_cores",
            id="tensor-core-count",
        ),
        pytest.param(
            lambda: TensorCapability(local_mem_size=-1),
            "local_mem_size",
            id="tensor-local-memory",
        ),
        pytest.param(
            lambda: TensorCapability(global_mem_size=-1),
            "global_mem_size",
            id="tensor-global-memory",
        ),
        pytest.param(
            lambda: TensorCapability(dma_channels=0),
            "dma_channels",
            id="tensor-dma-channels",
        ),
        pytest.param(
            lambda: TensorCapability(max_tensor_dims=0),
            "max_tensor_dims",
            id="tensor-rank",
        ),
        pytest.param(
            lambda: TensorCapability(supported_dtypes={""}),
            "supported_dtypes",
            id="tensor-dtypes",
        ),
        pytest.param(
            lambda: GPGPUCapability(num_warps=0),
            "num_warps",
            id="gpu-warp-count",
        ),
        pytest.param(
            lambda: GPGPUCapability(warp_size=0),
            "warp_size",
            id="gpu-warp-size",
        ),
        pytest.param(
            lambda: GPGPUCapability(shared_mem_size=-1),
            "shared_mem_size",
            id="gpu-shared-memory",
        ),
        pytest.param(
            lambda: GPGPUCapability(num_stages=0),
            "num_stages",
            id="gpu-stage-count",
        ),
        pytest.param(
            lambda: GPGPUCapability(num_ctas=0),
            "num_ctas",
            id="gpu-cta-count",
        ),
        pytest.param(
            lambda: GPGPUCapability(cluster_dims=(1, 1)),
            "exactly 3 dimensions",
            id="gpu-cluster-rank",
        ),
        pytest.param(
            lambda: GPGPUCapability(cluster_dims=(1, 0, 1)),
            "cluster_dims",
            id="gpu-cluster-dimension",
        ),
        pytest.param(
            lambda: GPGPUCapability(supported_dtypes=set()),
            "supported_dtypes",
            id="gpu-dtypes",
        ),
    ],
)
def test_capability_descriptors_reject_invalid_values(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()
