"""Tests that a malformed file entry is skipped, not fatal, in the JSON parser."""

from __future__ import annotations

from nab_index.client import _parse_files

_VALID = {
    "filename": "foo-1.0-py3-none-any.whl",
    "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
    "hashes": {"sha256": "a" * 64},
}


def test_entry_missing_filename_skipped_keeps_valid() -> None:
    data = {"files": [{"url": "https://example.com/foo/x.whl"}, _VALID]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert [f.filename for f in files] == ["foo-1.0-py3-none-any.whl"]


def test_entry_missing_url_skipped_keeps_valid() -> None:
    data = {"files": [{"filename": "foo-2.0-py3-none-any.whl"}, _VALID]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert [f.filename for f in files] == ["foo-1.0-py3-none-any.whl"]
