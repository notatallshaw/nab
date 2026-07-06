"""Tests for ``size`` parsing in nab_index's Simple API JSON parser.

bool is an int subclass, so a plain ``isinstance(value, int)`` check would
accept a JSON boolean and read ``True`` as a size of 1; ``_parse_size`` drops it.
"""

from __future__ import annotations

from nab_index.client import _parse_files, _parse_size


def _file(extra: dict[str, object]) -> dict[str, object]:
    base: dict[str, object] = {
        "filename": "foo-1.0-py3-none-any.whl",
        "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
        "hashes": {"sha256": "a" * 64},
    }
    base.update(extra)
    return base


def test_integer_size_preserved() -> None:
    assert _parse_size(123) == 123


def test_boolean_size_dropped() -> None:
    for value in (True, False):
        assert _parse_size(value) is None


def test_non_integer_size_dropped() -> None:
    assert _parse_size(1.5) is None
    assert _parse_size("9") is None
    assert _parse_size(-1) is None


def test_boolean_size_does_not_reach_wheel_file() -> None:
    data = {"files": [_file({"size": True})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert files[0].size is None


def test_integer_size_reaches_wheel_file() -> None:
    data = {"files": [_file({"size": 4096})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert files[0].size == 4096
