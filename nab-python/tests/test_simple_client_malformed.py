"""Tests for non-conformant Simple API JSON shapes in nab_index's parser.

A malformed *body* (not a JSON object, or a ``files`` value that is not
a list) raises rather than returning an empty result, since an empty
result means "package absent" to the multi-index router.  A malformed
*entry* is skipped so the usable entries in the same listing are kept.
"""

from __future__ import annotations

import pytest

from nab_index.client import (
    MalformedSimpleResponseError,
    WheelFile,
    _parse_files,
)
from nab_index.transport import HttpError

_INDEX = "https://example.com/"


def _good_file() -> dict[str, object]:
    return {
        "filename": "foo-1.0-py3-none-any.whl",
        "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
        "hashes": {"sha256": "ab"},
    }


def test_valid_body_parses() -> None:
    files = _parse_files({"files": [_good_file()]}, _INDEX, "foo")
    assert len(files) == 1
    assert isinstance(files[0], WheelFile)
    assert files[0].filename == "foo-1.0-py3-none-any.whl"


def test_malformed_body_is_index_error() -> None:
    """A malformed body raises ``HttpError``, not just the subclass."""
    with pytest.raises(HttpError, match="malformed Simple-API"):
        _parse_files("oops", _INDEX, "foo")


def test_non_dict_body_raises() -> None:
    with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
        _parse_files(["foo"], _INDEX, "foo")


def test_non_string_body_raises() -> None:
    with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
        _parse_files("oops", _INDEX, "foo")


def test_non_list_files_value_raises() -> None:
    with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
        _parse_files({"files": "oops"}, _INDEX, "foo")
    with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
        _parse_files({"files": {"a": 1}}, _INDEX, "foo")


def test_missing_files_key_raises() -> None:
    with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
        _parse_files({}, _INDEX, "foo")


def test_non_dict_entry_is_skipped() -> None:
    assert _parse_files({"files": ["not-a-dict"]}, _INDEX, "foo") == []
    assert _parse_files({"files": [42]}, _INDEX, "foo") == []


def test_entry_missing_filename_is_skipped() -> None:
    data = {"files": [{"url": "https://example.com/foo/foo-1.0.whl"}]}
    assert _parse_files(data, _INDEX, "foo") == []


def test_entry_missing_url_is_skipped() -> None:
    data = {"files": [{"filename": "foo-1.0-py3-none-any.whl"}]}
    assert _parse_files(data, _INDEX, "foo") == []


def test_entry_non_string_filename_is_skipped() -> None:
    data = {"files": [{"filename": 7, "url": "https://example.com/foo/x.whl"}]}
    assert _parse_files(data, _INDEX, "foo") == []


def test_entry_non_string_url_is_skipped() -> None:
    data = {"files": [{"filename": "foo-1.0-py3-none-any.whl", "url": 7}]}
    assert _parse_files(data, _INDEX, "foo") == []


@pytest.mark.parametrize(
    "bad_entry",
    [
        "not-a-dict",
        {"url": "https://example.com/foo/foo-1.0.whl"},
        {"filename": "foo-2.0-py3-none-any.whl"},
    ],
)
def test_bad_entry_dropped_good_entry_kept(bad_entry: object) -> None:
    data = {"files": [bad_entry, _good_file()]}
    files = _parse_files(data, _INDEX, "foo")
    assert len(files) == 1
    assert files[0].filename == "foo-1.0-py3-none-any.whl"


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://[2001:db8::1/foo-2.0-py3-none-any.whl",
        "http://2001:db8::1]/foo-2.0-py3-none-any.whl",
        "//[2001:db8::1/foo-2.0-py3-none-any.whl",
    ],
)
def test_entry_with_unsplittable_url_is_skipped(bad_url: str) -> None:
    entry = {"filename": "foo-2.0-py3-none-any.whl", "url": bad_url}
    files = _parse_files({"files": [entry, _good_file()]}, _INDEX, "foo")
    assert [f.filename for f in files] == ["foo-1.0-py3-none-any.whl"]


def test_sdist_entry_with_unsplittable_url_is_skipped() -> None:
    entry = {
        "filename": "foo-2.0.tar.gz",
        "url": "https://[2001:db8::1/foo-2.0.tar.gz",
    }
    files = _parse_files({"files": [entry, _good_file()]}, _INDEX, "foo")
    assert [f.filename for f in files] == ["foo-1.0-py3-none-any.whl"]
