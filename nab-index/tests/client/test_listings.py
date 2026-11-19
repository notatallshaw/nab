"""Malformed bodies fail; malformed entries leave usable neighbors intact."""

from __future__ import annotations

import pytest

from nab_index.client import (
    MalformedSimpleResponseError,
    SdistFile,
    WheelFile,
    _parse_files,
)
from nab_index.transport import HttpError
from nab_provider.errors import IndexAccessError

INDEX = "https://example.com/"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(["foo"], id="array-body"),
        pytest.param("oops", id="string-body"),
        pytest.param({}, id="missing-files"),
        pytest.param({"files": "oops"}, id="string-files"),
        pytest.param({"files": {"a": 1}}, id="object-files"),
    ],
)
def test_malformed_body_raises_index_error(body: object) -> None:
    with pytest.raises(
        MalformedSimpleResponseError, match="malformed Simple-API"
    ) as caught:
        _parse_files(body, INDEX, "foo")

    assert isinstance(caught.value, HttpError)


@pytest.mark.parametrize("version", ["2.0", "010.2"])
def test_newer_repository_major_version_is_refused(version: str) -> None:
    with pytest.raises(
        IndexAccessError, match=f"requires Simple API version {version}"
    ):
        _parse_files({"meta": {"api-version": version}, "files": []}, INDEX, "foo")


@pytest.mark.parametrize("version", ["1", "1.0rc1", 2])
def test_invalid_repository_versions_are_reported(version: str | int) -> None:
    with pytest.raises(IndexAccessError, match="invalid Simple API version"):
        _parse_files({"meta": {"api-version": version}, "files": []}, INDEX, "foo")


def test_newer_minor_version_warns_and_reads_the_listing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        _parse_files({"meta": {"api-version": "01.05"}, "files": []}, INDEX, "foo")
        == []
    )
    assert "Simple API version 01.05" in caplog.text


def test_missing_version_in_metadata_keeps_the_files() -> None:
    filename = "foo-1.0-py3-none-any.whl"
    data = {"meta": {}, "files": [{"filename": filename, "url": filename}]}
    (wheel,) = _parse_files(data, INDEX, "foo")
    assert wheel.filename == filename


def test_empty_listing() -> None:
    assert _parse_files({"files": []}, INDEX, "foo") == []


def test_valid_entry(file_entry: dict[str, object]) -> None:
    files = _parse_files({"files": [file_entry]}, INDEX, "foo")

    assert len(files) == 1
    expected_type = (
        WheelFile if str(file_entry["filename"]).endswith(".whl") else SdistFile
    )
    assert isinstance(files[0], expected_type)
    assert files[0].filename == file_entry["filename"]
    assert files[0].url == file_entry["url"]
    assert files[0].version == "1.0"


@pytest.mark.parametrize(
    "bad_entry",
    [
        pytest.param("not-a-dict", id="string"),
        pytest.param(42, id="integer"),
        pytest.param(
            {"url": "https://example.com/foo/foo-1.0.whl"}, id="missing-filename"
        ),
        pytest.param({"filename": "foo-2.0-py3-none-any.whl"}, id="missing-url"),
        pytest.param(
            {"filename": 7, "url": "https://example.com/foo/x.whl"},
            id="non-string-filename",
        ),
        pytest.param(
            {"filename": "foo-2.0-py3-none-any.whl", "url": 7}, id="non-string-url"
        ),
    ],
)
@pytest.mark.parametrize("bad_first", [True, False], ids=["bad-first", "bad-last"])
def test_malformed_entry_is_skipped(
    wheel_entry: dict[str, object], bad_entry: object, bad_first: bool
) -> None:
    entries = [bad_entry, wheel_entry] if bad_first else [wheel_entry, bad_entry]
    files = _parse_files({"files": entries}, INDEX, "foo")

    assert [file.filename for file in files] == [wheel_entry["filename"]]


@pytest.mark.parametrize(
    "bad_url",
    [
        pytest.param("https://[2001:db8::1/foo-2.0.whl", id="open-ipv6"),
        pytest.param("http://2001:db8::1]/foo-2.0.whl", id="closed-ipv6"),
        pytest.param("//[2001:db8::1/foo-2.0.whl", id="relative-ipv6"),
        pytest.param(
            "https://example.com/foo/foo-2.0.whl?sig=\ud800", id="absolute-surrogate"
        ),
        pytest.param("../pool/foo-2.0.whl?sig=\ud800", id="relative-surrogate"),
    ],
)
def test_unusable_url_is_skipped(file_entry: dict[str, object], bad_url: str) -> None:
    entries = [file_entry | {"url": bad_url}, file_entry]
    files = _parse_files({"files": entries}, INDEX, "foo")

    assert [file.url for file in files] == [file_entry["url"]]
