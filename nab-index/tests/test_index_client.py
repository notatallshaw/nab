"""Tests for nab_index.client helpers."""

from __future__ import annotations

import sys

import pytest

from nab_index.client import (
    _sdist_member_top_level,
    holds_unreadable_format,
    is_readable_filename,
)

# A version digit run past CPython's int-from-string limit.
OVERSIZED = "1" * (sys.get_int_max_str_digits() + 1)


@pytest.mark.parametrize(
    "filename",
    ["foo-1.0-py3-none-any.whl", "foo-1.0.tar.gz"],
)
def test_readable_filenames(filename: str) -> None:
    assert is_readable_filename(filename)


@pytest.mark.parametrize(
    "filename",
    ["foo-1.0.zip", "foo-1.5.win32.exe", "foo-1.0.tar.bz2", "foo.egg"],
)
def test_unreadable_filenames(filename: str) -> None:
    assert not is_readable_filename(filename)


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param(f"foo-{OVERSIZED}-py3-none-any.whl", id="wheel"),
        pytest.param(f"foo-{OVERSIZED}.tar.gz", id="sdist"),
        pytest.param(f"foo-{OVERSIZED}!1.0-py3-none-any.whl", id="epoch"),
        pytest.param(f"foo-1.0.dev{OVERSIZED}-py3-none-any.whl", id="dev"),
    ],
)
def test_oversized_version_is_unreadable(filename: str) -> None:
    assert not is_readable_filename(filename)


def test_holds_unreadable_format_finds_zip_sdist() -> None:
    data = {"files": [{"filename": "foo-1.0.zip", "url": "https://e.example/f"}]}
    assert holds_unreadable_format(data)


def test_holds_unreadable_format_finds_oversized_version() -> None:
    filename = f"foo-{OVERSIZED}-py3-none-any.whl"
    data = {"files": [{"filename": filename, "url": "https://e.example/f"}]}
    assert holds_unreadable_format(data)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(["files"], id="body-not-an-object"),
        pytest.param({"files": "nope"}, id="files-not-a-list"),
        pytest.param({"files": []}, id="no-entries"),
        pytest.param({"files": ["foo-1.0.zip"]}, id="entry-not-an-object"),
        pytest.param({"files": [{"filename": 3}]}, id="filename-not-a-string"),
        pytest.param(
            {"files": [{"filename": "foo-1.0.zip", "yanked": True}]},
            id="yanked-entry",
        ),
        pytest.param(
            {"files": [{"filename": "foo-1.0-py3-none-any.whl"}]},
            id="readable-entry",
        ),
    ],
)
def test_holds_unreadable_format_false(data: object) -> None:
    assert not holds_unreadable_format(data)


def test_sdist_member_top_level_normal() -> None:
    assert _sdist_member_top_level("foo-1.0/PKG-INFO") == (1, "foo-1.0", "PKG-INFO")


@pytest.mark.parametrize("name", ["", "./", "/abs", "/"])
def test_sdist_member_top_level_rejects_empty_or_absolute(name: str) -> None:
    # A member that strips to nothing, or points at an absolute path, is
    # reported at depth -1 so callers skip it rather than treat it as a root.
    assert _sdist_member_top_level(name) == (-1, "", "")
