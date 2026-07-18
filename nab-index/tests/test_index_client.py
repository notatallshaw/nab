"""Tests for nab_index.client helpers."""

from __future__ import annotations

import pytest

from nab_index.client import _sdist_member_top_level


def test_sdist_member_top_level_normal() -> None:
    assert _sdist_member_top_level("foo-1.0/PKG-INFO") == (1, "foo-1.0", "PKG-INFO")


@pytest.mark.parametrize("name", ["", "./", "/abs", "/"])
def test_sdist_member_top_level_rejects_empty_or_absolute(name: str) -> None:
    # A member that strips to nothing, or points at an absolute path, is
    # reported at depth -1 so callers skip it rather than treat it as a root.
    assert _sdist_member_top_level(name) == (-1, "", "")
