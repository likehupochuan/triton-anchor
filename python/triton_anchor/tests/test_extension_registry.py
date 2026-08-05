"""Tests for DSL extension registry validation."""

import pytest

from triton_anchor.extensions.base import (
    BuiltinSpec,
    DSLExtensionPlugin,
    IncompatibleExtensionError,
)
from triton_anchor.extensions.registry import DSLExtensionRegistry


class _DummyExtension(DSLExtensionPlugin):
    @property
    def name(self):
        return "SMT test extension"

    @property
    def namespace(self):
        return "smt"

    @property
    def target_backend(self):
        return "spacemit"

    def get_builtins(self):
        return {"dot": BuiltinSpec(name="dot")}


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setattr(
        DSLExtensionRegistry,
        "discover",
        classmethod(lambda cls: None),
    )
    DSLExtensionRegistry.reset()
    yield
    DSLExtensionRegistry.reset()


def test_validate_kernel_ignores_extension_names_in_inline_comments():
    DSLExtensionRegistry.register(_DummyExtension())
    kernel_ir = (
        '%0 = "arith.constant"() {value = 0 : i32} : () -> i32 '
        '// "smt.dot" is documented here'
    )

    DSLExtensionRegistry.validate_kernel(kernel_ir, target_backend="sophgo")


def test_validate_kernel_still_checks_actual_extension_operations():
    DSLExtensionRegistry.register(_DummyExtension())
    kernel_ir = '%0 = "smt.dot"() : () -> i32'

    with pytest.raises(
        IncompatibleExtensionError,
        match="requires backend 'spacemit'",
    ):
        DSLExtensionRegistry.validate_kernel(kernel_ir, target_backend="sophgo")
