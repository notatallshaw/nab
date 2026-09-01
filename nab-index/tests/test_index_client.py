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
    holds_named_files,
    holds_only_yanked,
    holds_unreachable_link,
    holds_unreadable_format,
    is_readable_filename,
    zip_sdist_version,
    zip_sdist_versions,
)

# A version digit run past CPython's int-from-string limit.
OVERSIZED = "1" * (sys.get_int_max_str_digits() + 1)

INDEX_URL = "https://e.example/simple/"


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


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            {
                "files": [
                    {
                        "filename": "cffi-1.0.2-2.tar.gz",
                        "url": "https://e.example/cffi-1.0.2-2.tar.gz",
                    }
                ]
            },
            id="filename-of-another-project",
        ),
        pytest.param({"files": [{"filename": "foo-1.0.zip"}]}, id="unreadable-format"),
        pytest.param(
            {"files": [{"filename": "foo-1.0-py3-none-any.whl"}]}, id="readable-entry"
        ),
    ],
)
def test_holds_named_files_true(data: object) -> None:
    assert holds_named_files(data)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(["files"], id="body-not-an-object"),
        pytest.param({"files": "nope"}, id="files-not-a-list"),
        pytest.param({"files": []}, id="no-entries"),
        pytest.param({"files": ["foo-1.0.whl"]}, id="entry-not-an-object"),
        pytest.param(
            {"files": [{"url": "https://e.example/f"}]}, id="entry-with-no-filename"
        ),
        pytest.param(
            {"files": [{"filename": "foo-1.0-py3-none-any.whl", "yanked": True}]},
            id="yanked-entry",
        ),
    ],
)
def test_holds_named_files_false(data: object) -> None:
    assert not holds_named_files(data)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(
            "https://[::1/foo-1.0-py3-none-any.whl", id="unbalanced-ipv6-bracket"
        ),
        pytest.param(
            "https://exa\u2100mple.com/foo-1.0-py3-none-any.whl",
            id="netloc-that-changes-under-nfkc",
        ),
    ],
)
def test_holds_unreachable_link_finds_an_unusable_url(url: str) -> None:
    """The entry is dropped, so the page it is alone on parses to nothing."""
    data = {"files": [{"filename": "foo-1.0-py3-none-any.whl", "url": url}]}

    assert _parse_files(data, INDEX_URL, "foo") == []
    assert holds_unreachable_link(data, INDEX_URL, "foo")


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"files": []}, id="no-entries"),
        pytest.param(
            {
                "files": [
                    {
                        "filename": "foo-1.0-py3-none-any.whl",
                        "url": "foo-1.0-py3-none-any.whl",
                    }
                ]
            },
            id="relative-url-the-page-resolves",
        ),
        pytest.param(
            {"files": [{"filename": "foo-1.0-py3-none-any.whl"}]},
            id="entry-with-no-url",
        ),
        pytest.param(
            {"files": [{"url": "https://[::1/foo-1.0-py3-none-any.whl"}]},
            id="entry-with-no-filename",
        ),
    ],
)
def test_holds_unreachable_link_false(data: object) -> None:
    """A page marks only where an entry naming a file has a URL nothing can use."""
    assert not holds_unreachable_link(data, INDEX_URL, "foo")


def test_a_yanked_zip_sdist_is_yanked_rather_than_unreadable() -> None:
    """A page of yanked .zip sdists is yanked rather than unreadable.

    ``holds_unreadable_format`` skips a yanked entry before reading its
    name, and ``holds_only_yanked`` needs every entry withdrawn, so no body
    sets both.
    """
    data = {"files": [{"filename": "foo-1.0.zip", "yanked": True}]}
    assert not holds_unreadable_format(data)
    assert holds_only_yanked(data)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        pytest.param("foo-1.0.zip", "1.0", id="canonical"),
        pytest.param("Foo_Bar-1.0.zip", None, id="another-project"),
        pytest.param("foo-1.0.0.zip", "1.0.0", id="version-spelled-out"),
        pytest.param("foo-1.0RC1.zip", "1.0rc1", id="version-normalized"),
        pytest.param("foo-1.0.tar.gz", None, id="readable-sdist"),
        pytest.param("foo-1.0.tar.bz2", None, id="other-dropped-sdist-format"),
        pytest.param("foo-1.5.win32.exe", None, id="installer"),
        pytest.param("foo.zip", None, id="no-version"),
        pytest.param("-1.0.zip", None, id="no-name"),
        pytest.param("foo-notaversion.zip", None, id="unparseable-version"),
        pytest.param(f"foo-{OVERSIZED}.zip", None, id="oversized-version"),
        pytest.param("foo\x00-1.0.zip", None, id="nul-does-not-name-the-package"),
    ],
)
def test_zip_sdist_version(filename: str, expected: str | None) -> None:
    assert zip_sdist_version(filename, "foo") == expected


def test_zip_sdist_versions_collects_every_release() -> None:
    data = {
        "files": [
            {"filename": "foo-1.0.zip"},
            {"filename": "foo-2.0.zip"},
            {"filename": "foo-2.0-py3-none-any.whl"},
        ]
    }
    assert zip_sdist_versions(data, "Foo") == frozenset({"1.0", "2.0"})


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(["files"], id="body-not-an-object"),
        pytest.param({"files": [{"filename": "bar-1.0.zip"}]}, id="another-project"),
        pytest.param(
            {"files": [{"filename": "foo-1.0.zip", "yanked": True}]},
            id="yanked-entry",
        ),
    ],
)
def test_zip_sdist_versions_empty(data: object) -> None:
    assert zip_sdist_versions(data, "foo") == frozenset()


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
