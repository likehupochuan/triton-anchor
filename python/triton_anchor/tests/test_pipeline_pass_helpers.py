import sys
from types import ModuleType, SimpleNamespace

import pytest

from triton_anchor.hw_capability import ComputeParadigm
from triton_anchor.pipeline import _require_pass, _try_add_pass, build_ttir_pipeline


class RecordingPassModule:
    __name__ = "fake.ttir"

    def __init__(self):
        self.calls = []

    def add_known_pass(self, pm, **kwargs):
        self.calls.append((pm, kwargs))


class StrictPassModule:
    __name__ = "fake.strict"

    def add_strict_pass(self, pm):
        return pm


def test_try_add_pass_calls_available_optional_pass_without_kwargs():
    module = RecordingPassModule()
    pm = object()

    assert _try_add_pass(module, "add_known_pass", pm)

    assert module.calls == [(pm, {})]


def test_try_add_pass_calls_available_optional_pass_with_kwargs():
    module = RecordingPassModule()
    pm = object()

    assert _try_add_pass(module, "add_known_pass", pm, threshold=4, enabled=True)

    assert module.calls == [(pm, {"enabled": True, "threshold": 4})]


def test_try_add_pass_returns_false_for_missing_optional_pass():
    module = RecordingPassModule()

    assert not _try_add_pass(module, "add_missing_pass", object())
    assert module.calls == []


def test_require_pass_calls_available_required_pass():
    module = RecordingPassModule()
    pm = object()

    assert _require_pass(module, "add_known_pass", pm, num_warps=8)

    assert module.calls == [(pm, {"num_warps": 8})]


def test_require_pass_raises_for_missing_required_pass_with_module_name():
    module = SimpleNamespace(__name__="fake.ttir")

    with pytest.raises(RuntimeError) as exc_info:
        _require_pass(module, "add_rewrite_tensor_pointer", object())

    message = str(exc_info.value)
    assert "add_rewrite_tensor_pointer" in message
    assert "fake.ttir" in message
    assert "critical" in message


def test_pass_lookup_rejects_non_callable_attribute():
    module = SimpleNamespace(__name__="fake.ttir", add_bad_pass="not-callable")

    with pytest.raises(TypeError, match="not callable"):
        _try_add_pass(module, "add_bad_pass", object())


def test_pass_type_error_includes_pass_module_and_kwarg_context():
    module = StrictPassModule()

    with pytest.raises(TypeError) as exc_info:
        _try_add_pass(module, "add_strict_pass", object(), unexpected=True)

    message = str(exc_info.value)
    assert "add_strict_pass" in message
    assert "fake.strict" in message
    assert "unexpected" in message


def _recording_pass(name, calls):
    def add_pass(pm, **kwargs):
        calls.append((name, pm, kwargs))

    return add_pass


def _install_fake_libtriton(
    monkeypatch,
    calls,
    *,
    include_gpu_rewrite=True,
    include_loop_unroll=True,
    include_expression_restructing=True,
):
    common = SimpleNamespace(
        add_inliner=_recording_pass("common.add_inliner", calls),
        add_canonicalizer=_recording_pass("common.add_canonicalizer", calls),
        add_cse=_recording_pass("common.add_cse", calls),
        add_licm=_recording_pass("common.add_licm", calls),
        add_symbol_dce=_recording_pass("common.add_symbol_dce", calls),
    )
    ttir_passes = {
        "add_combine": _recording_pass("ttir.add_combine", calls),
        "add_reorder_broadcast": _recording_pass("ttir.add_reorder_broadcast", calls),
    }
    if include_loop_unroll:
        ttir_passes["add_loop_unroll"] = _recording_pass("ttir.add_loop_unroll", calls)
    if include_expression_restructing:
        ttir_passes["add_expression_restructing"] = _recording_pass(
            "ttir.add_expression_restructing", calls
        )
    if include_gpu_rewrite:
        ttir_passes["add_rewrite_tensor_pointer"] = _recording_pass(
            "ttir.add_rewrite_tensor_pointer", calls
        )

    libtriton = ModuleType("triton._C.libtriton")
    libtriton.passes = SimpleNamespace(
        common=common,
        ttir=SimpleNamespace(**ttir_passes),
    )

    triton_module = ModuleType("triton")
    triton_c_module = ModuleType("triton._C")
    triton_module._C = triton_c_module
    triton_c_module.libtriton = libtriton

    monkeypatch.setitem(sys.modules, "triton", triton_module)
    monkeypatch.setitem(sys.modules, "triton._C", triton_c_module)
    monkeypatch.setitem(sys.modules, "triton._C.libtriton", libtriton)


def test_build_ttir_pipeline_adds_mandatory_passes_in_order(monkeypatch):
    calls = []
    _install_fake_libtriton(monkeypatch, calls)
    pm = object()

    build_ttir_pipeline(pm)

    assert [name for name, _, _ in calls] == [
        "common.add_inliner",
        "ttir.add_combine",
        "common.add_canonicalizer",
        "ttir.add_reorder_broadcast",
        "common.add_cse",
        "common.add_licm",
        "common.add_symbol_dce",
    ]
    assert all(call_pm is pm for _, call_pm, _ in calls)


def test_build_ttir_pipeline_adds_gpu_and_optional_passes(monkeypatch):
    calls = []
    _install_fake_libtriton(monkeypatch, calls)
    pm = object()
    hw = SimpleNamespace(
        compute_paradigm=ComputeParadigm.GPGPU,
        enable_loop_unroll=True,
    )

    build_ttir_pipeline(pm, hw=hw)

    assert [name for name, _, _ in calls] == [
        "common.add_inliner",
        "ttir.add_combine",
        "common.add_canonicalizer",
        "ttir.add_reorder_broadcast",
        "common.add_cse",
        "common.add_licm",
        "common.add_symbol_dce",
        "ttir.add_rewrite_tensor_pointer",
        "ttir.add_loop_unroll",
        "ttir.add_expression_restructing",
    ]


def test_build_ttir_pipeline_requires_gpu_rewrite_pass(monkeypatch):
    calls = []
    _install_fake_libtriton(monkeypatch, calls, include_gpu_rewrite=False)
    hw = SimpleNamespace(
        compute_paradigm=ComputeParadigm.GPGPU,
        enable_loop_unroll=False,
    )

    with pytest.raises(RuntimeError, match="add_rewrite_tensor_pointer"):
        build_ttir_pipeline(object(), hw=hw)


def test_build_ttir_pipeline_skips_gpu_rewrite_for_non_gpu_hw(monkeypatch):
    calls = []
    _install_fake_libtriton(monkeypatch, calls, include_gpu_rewrite=False)
    hw = SimpleNamespace(
        compute_paradigm=ComputeParadigm.AME_MATRIX,
        enable_loop_unroll=False,
    )

    build_ttir_pipeline(object(), hw=hw)

    assert "ttir.add_rewrite_tensor_pointer" not in [name for name, _, _ in calls]
    assert [name for name, _, _ in calls][-1] == "ttir.add_expression_restructing"


def test_build_ttir_pipeline_skips_missing_optional_passes(monkeypatch):
    calls = []
    _install_fake_libtriton(
        monkeypatch,
        calls,
        include_loop_unroll=False,
        include_expression_restructing=False,
    )
    hw = SimpleNamespace(
        compute_paradigm=ComputeParadigm.AME_MATRIX,
        enable_loop_unroll=True,
    )

    build_ttir_pipeline(object(), hw=hw)

    call_names = [name for name, _, _ in calls]
    assert "ttir.add_loop_unroll" not in call_names
    assert "ttir.add_expression_restructing" not in call_names
    assert call_names == [
        "common.add_inliner",
        "ttir.add_combine",
        "common.add_canonicalizer",
        "ttir.add_reorder_broadcast",
        "common.add_cse",
        "common.add_licm",
        "common.add_symbol_dce",
    ]
