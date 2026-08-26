"""Tests for nab_index.client helpers."""

from __future__ import annotations

import json
import sys

import pytest
from packaging.utils import parse_sdist_filename

from nab_index.client import (
    _parse_files,
    _parse_sdist_filename,
    _sdist_member_top_level,
    holds_only_yanked,
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


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("foo-1.0-1\ud800-py3-none-any.whl", id="wheel-build-tag"),
        pytest.param("foo-1.0-py3-\ud800-any.whl", id="wheel-abi"),
        pytest.param("foo-1.0-py3-none-\ud800.whl", id="wheel-platform"),
        pytest.param("foo\ud800-1.0.tar.gz", id="sdist-name"),
        # POSIX carries this range through surrogateescape, so a file on
        # disk can be named that; a UTF-8 lockfile cannot record it.
        pytest.param("foo-1.0-1\udc80-py3-none-any.whl", id="wheel-surrogateescape"),
    ],
)
def test_filename_with_no_utf8_form_is_unreadable(filename: str) -> None:
    assert not is_readable_filename(filename)


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("foo-1.0-1\x00-py3-none-any.whl", id="wheel"),
        pytest.param("foo\x00-1.0.tar.gz", id="sdist"),
    ],
)
def test_filename_with_an_embedded_nul_is_unreadable(filename: str) -> None:
    assert not is_readable_filename(filename)


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("foo-1.0-1é-py3-none-any.whl", id="wheel"),
        pytest.param("fooé-1.0.tar.gz", id="sdist"),
    ],
)
def test_non_ascii_filename_stays_readable(filename: str) -> None:
    assert is_readable_filename(filename)


def test_listing_drops_a_file_with_no_utf8_form() -> None:
    # An ASCII PEP 691 body still carries this name: json.loads turns the
    # \ud800 escape into a lone surrogate.
    body = json.loads(
        '{"files": [{"filename": "foo-1.0-1\\ud800-py3-none-any.whl",'
        ' "url": "https://e.example/foo-1.0.whl"}]}'
    )

    assert _parse_files(body, "https://e.example/simple/", "foo") == []
    assert holds_unreadable_format(body)


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


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            {"files": [{"filename": "foo-1.0-py3-none-any.whl", "yanked": True}]},
            id="one-yanked-wheel",
        ),
        pytest.param(
            {
                "files": [
                    {"filename": "foo-1.0-py3-none-any.whl", "yanked": True},
                    {"filename": "foo-2.0.tar.gz", "yanked": "withdrawn"},
                ]
            },
            id="every-entry-yanked",
        ),
    ],
)
def test_holds_only_yanked_true(data: object) -> None:
    assert holds_only_yanked(data)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(["files"], id="body-not-an-object"),
        pytest.param({"files": "nope"}, id="files-not-a-list"),
        pytest.param({"files": []}, id="no-entries"),
        pytest.param({"files": ["foo-1.0.whl"]}, id="no-entry-is-an-object"),
        pytest.param(
            {
                "files": [
                    {"filename": "foo-1.0-py3-none-any.whl", "yanked": True},
                    {"filename": "foo-2.0-py3-none-any.whl"},
                ]
            },
            id="one-entry-stands",
        ),
    ],
)
def test_holds_only_yanked_false(data: object) -> None:
    assert not holds_only_yanked(data)


def test_the_two_empty_listing_flags_are_exclusive() -> None:
    """A page of yanked .zip sdists is yanked rather than unreadable.

    ``holds_unreadable_format`` skips a yanked entry before reading its
    name, and ``holds_only_yanked`` needs every entry withdrawn, so no body
    sets both.
    """
    data = {"files": [{"filename": "foo-1.0.zip", "yanked": True}]}
    assert not holds_unreadable_format(data)
    assert holds_only_yanked(data)


@pytest.mark.parametrize(
    "filename",
    [
        "foo-1.0.tar.gz",
        "Foo_Bar.baz-1.0rc1.tar.gz",
        "foo-1.0.post1.dev2+local.tag.tar.gz",
        "foo-1!2.0.tar.gz",
        "cffi-1.0.2-2.tar.gz",
        "foo-1.0.zip",
        "foo-1.0.tar.bz2",
        "foo-1.5.win32.exe",
        "foo.tar.gz",
        "-1.0.tar.gz",
        "foo-.tar.gz",
        "foo-v1.0.tar.gz",
        "foo--1.0.tar.gz",
        "foo-1.0.TAR.GZ",
        f"foo-{OVERSIZED}.tar.gz",
    ],
)
def test_parse_sdist_filename_parity(filename: str) -> None:
    """The inline parse matches the vendored parser on everything but ``.zip``.

    Released ``parse_sdist_filename`` is the oracle, corrected on the two
    known divergences: ``.zip`` sdists, which nab rejects, and an empty
    project name, which packaging releases before 26.3 accept.
    """
    if filename.endswith(".zip"):
        expected = None
    else:
        try:
            name, version = parse_sdist_filename(filename)
        except ValueError:
            expected = None
        else:
            expected = (name, str(version)) if name else None

    assert _parse_sdist_filename(filename) == expected


def test_sdist_member_top_level_normal() -> None:
    assert _sdist_member_top_level("foo-1.0/PKG-INFO") == (1, "foo-1.0", "PKG-INFO")


@pytest.mark.parametrize("name", ["", "./", "/abs", "/"])
def test_sdist_member_top_level_rejects_empty_or_absolute(name: str) -> None:
    # A member that strips to nothing, or points at an absolute path, is
    # reported at depth -1 so callers skip it rather than treat it as a root.
    assert _sdist_member_top_level(name) == (-1, "", "")
