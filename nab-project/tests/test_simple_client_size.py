"""Tests for ``size`` parsing in nab_index's Simple API JSON parser.

bool is an int subclass, so a plain ``isinstance(value, int)`` check would
accept a JSON boolean and read ``True`` as a size of 1; the entry parse drops it.
"""

from __future__ import annotations

from nab_index.client import _parse_files


def _size(value: object) -> int | None:
    """The ``size`` the record carries for an entry declaring ``value``."""
    entry: dict[str, object] = {
        "filename": "foo-1.0-py3-none-any.whl",
        "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
        "hashes": {"sha256": "a" * 64},
        "size": value,
    }
    files = _parse_files({"files": [entry]}, "https://example.com/", "foo")
    return files[0].size


def test_integer_size_preserved() -> None:
    assert _size(4096) == 4096


def test_boolean_size_dropped() -> None:
    for value in (True, False):
        assert _size(value) is None


def test_non_integer_size_dropped() -> None:
    assert _size(1.5) is None
    assert _size("9") is None


def test_negative_size_dropped() -> None:
    assert _size(-1) is None
