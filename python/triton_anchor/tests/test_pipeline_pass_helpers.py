from types import SimpleNamespace

import pytest

from triton_anchor.pipeline import _require_pass, _try_add_pass


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
