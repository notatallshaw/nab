"""Fixtures for the index distribution's tests."""

# ruff: noqa: INP001 - Pytest discovers this module at the distribution root.
import json
from types import SimpleNamespace

import pytest

from nab_index import _json_decode


def pytest_configure(config: pytest.Config) -> None:
    """Register the property tests when the workspace config is absent."""
    if not any(
        mark.partition(":")[0] == "property" for mark in config.getini("markers")
    ):
        config.addinivalue_line("markers", "property: Hypothesis property tests")


@pytest.fixture
def over_nested_json(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Simulate the index decoder's recursion limit for one small input."""
    payload = b"[[[]]]"

    def loads(raw: bytes) -> _json_decode.JsonValue:
        if raw == payload:
            raise RecursionError
        decoded: _json_decode.JsonValue = json.loads(raw)
        return decoded

    monkeypatch.setattr(_json_decode, "json", SimpleNamespace(loads=loads))
    return payload
