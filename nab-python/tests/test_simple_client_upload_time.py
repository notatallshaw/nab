"""A non-string upload-time from a non-conformant index must not crash."""

from __future__ import annotations

from nab_index.client import _parse_files
from nab_python._lockfile.builder import _parse_upload_time


def _file(extra: dict[str, object]) -> dict[str, object]:
    base: dict[str, object] = {
        "filename": "foo-1.0-py3-none-any.whl",
        "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
    }
    base.update(extra)
    return base


def test_string_upload_time_preserved() -> None:
    data = {"files": [_file({"upload-time": "2024-06-21T00:00:00Z"})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert files[0].upload_time == "2024-06-21T00:00:00Z"


def test_non_string_upload_time_coerced_to_none() -> None:
    data = {"files": [_file({"upload-time": 1719000000})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert files[0].upload_time is None


def test_non_string_upload_time_does_not_crash_lock_emit() -> None:
    data = {"files": [_file({"upload-time": 1719000000})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert _parse_upload_time(files[0].upload_time) is None
