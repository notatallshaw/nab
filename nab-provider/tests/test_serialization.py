"""Tests for nab_provider.serialization."""

from __future__ import annotations

from nab_provider.serialization import SimpleSerialization


def test_values_are_the_config_vocabulary() -> None:
    assert [m.value for m in SimpleSerialization] == ["negotiate", "json", "html"]
