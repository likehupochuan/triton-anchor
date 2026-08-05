"""Tests for adapter registry diagnostics."""

import pytest

from triton_anchor.adapters.base import ITritonToLinalgAdapter
from triton_anchor.adapters.registry import AdapterNotFoundError, AdapterRegistry
from triton_anchor.anchor_ir import AnchorIRTrack
from triton_anchor.hw_capability import ComputeParadigm, HWCapability, TensorCapability


class _DummyAdapter(ITritonToLinalgAdapter):
    def __init__(self, adapter_name):
        self._adapter_name = adapter_name

    def name(self):
        return self._adapter_name

    def convert(self, ttir_module, metadata, context=None):
        return ttir_module


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setattr(AdapterRegistry, "discover", classmethod(lambda cls: None))
    AdapterRegistry.reset()
    yield
    AdapterRegistry.reset()


def _tensor_hw(preferred_adapter=None):
    return HWCapability(
        name="test-tpu",
        arch_family="tpu",
        compute_paradigm=ComputeParadigm.TENSOR_PROCESSOR,
        anchor_ir_track=AnchorIRTrack.LINALG,
        ptr_model="axis_info",
        tensor_cap=TensorCapability(),
        preferred_adapter=preferred_adapter,
    )


def test_missing_preferred_adapter_lists_available_adapters_sorted():
    AdapterRegistry.register(_DummyAdapter("zeta"))
    AdapterRegistry.register(_DummyAdapter("alpha"))

    with pytest.raises(AdapterNotFoundError) as excinfo:
        AdapterRegistry.get_adapter(_tensor_hw(preferred_adapter="missing"))

    assert "Preferred adapter 'missing' not found" in str(excinfo.value)
    assert "Available: ['alpha', 'zeta']" in str(excinfo.value)


def test_list_adapters_returns_stable_name_order():
    AdapterRegistry.register(_DummyAdapter("zeta"))
    AdapterRegistry.register(_DummyAdapter("alpha"))

    adapters = AdapterRegistry.list_adapters()

    assert list(adapters) == ["alpha", "zeta"]
    assert adapters == {
        "alpha": "_DummyAdapter",
        "zeta": "_DummyAdapter",
    }
