"""Tests for the typing stand-ins that keep nab-resolver dependency-free."""

from __future__ import annotations

from nab_resolver import _compat


def test_override_stand_in_returns_the_decorated_object() -> None:
    def sample() -> None: ...

    assert _compat._override(sample) is sample
