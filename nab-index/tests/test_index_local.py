"""Tests for nab_index.local_index PEP 503 directory scanning."""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urljoin

import pytest

from nab_index import local_index
from nab_index._pep503 import read_page
from nab_index.local_index import (
    LocalIndexClient,
    _listing_bases,
    _merged_href,
    _merging_bases,
    _scan_pep503_directory,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path
    from typing import Any

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


def _make_index(tmp_path: Path, body: str) -> Path:
    package_dir = tmp_path / "foo"
    package_dir.mkdir()
    (package_dir / "index.html").write_text(body, encoding="utf-8")
    return package_dir


def test_anchor_with_unknown_attribute_is_parsed(tmp_path: Path) -> None:
    # An attribute the parser does not recognise (here 'rel') is skipped
    # without disturbing the href it accompanies.
    body = '<a href="foo-1.0-py3-none-any.whl" rel="nofollow">foo</a>'
    package_dir = _make_index(tmp_path, body)
    (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
    client = LocalIndexClient(tmp_path.as_uri())
    result = run(client.get_files("foo"))
    assert len(result) == 1


def test_http_href_without_basename_is_skipped(tmp_path: Path) -> None:
    # An http href whose path has no final segment yields no filename, so the
    # link is dropped while a well-formed sibling still lists.
    body = (
        '<a href="https://example.com/">nofile</a>'
        '<a href="foo-1.0-py3-none-any.whl">foo</a>'
    )
    package_dir = _make_index(tmp_path, body)
    (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
    client = LocalIndexClient(tmp_path.as_uri())
    result = run(client.get_files("foo"))
    assert len(result) == 1


def test_scan_directory_without_index_html_returns_empty(tmp_path: Path) -> None:
    # get_files only calls the scanner once index.html exists; the guard is
    # exercised directly here.
    package_dir = tmp_path / "foo"
    package_dir.mkdir()
    scan = _scan_pep503_directory(package_dir, "foo")

    assert scan.files == []
    assert not scan.unreadable
    assert not scan.unreachable
    assert not scan.all_yanked
    assert not scan.named_files
    assert scan.zip_sdists == frozenset()


def _anchor(filename: str, *, yanked: bool = False) -> str:
    """One PEP 503 link, yanked or not."""
    attribute = ' data-yanked=""' if yanked else ""
    return f'<a href="{filename}"{attribute}>{filename}</a>'


_YANKED_WHEEL = _anchor("foo-1.0-py3-none-any.whl", yanked=True)
_YANKED_SDIST = _anchor("foo-2.0.tar.gz", yanked=True)
_MISNAMED = _anchor("foo-1.0.zip")
_READABLE = _anchor("foo-3.0-py3-none-any.whl")
_UNREACHABLE = _anchor("ftp://mirror.example/foo-4.0-py3-none-any.whl")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(_YANKED_WHEEL, True, id="one-yanked-link"),
        pytest.param(_YANKED_WHEEL + _YANKED_SDIST, True, id="every-link-yanked"),
        pytest.param("", False, id="no-links"),
        pytest.param(_MISNAMED, False, id="misnamed-link-alone"),
        pytest.param(_YANKED_WHEEL + _MISNAMED, False, id="one-link-stands"),
        pytest.param(_YANKED_WHEEL + _READABLE, False, id="one-link-admitted"),
        pytest.param(_YANKED_WHEEL + _UNREACHABLE, False, id="one-link-unreachable"),
    ],
)
def test_the_all_yanked_flag_counts_the_yanked_links(
    tmp_path: Path, body: str, expected: bool
) -> None:
    """Every link on the page has to be yanked, not merely one of them.

    A page whose only link nab cannot read also lists no files, and
    reporting that as yanked would name a PEP 592 withdrawal the index
    never declared.
    """
    package_dir = _make_index(tmp_path, body)
    (package_dir / "foo-3.0-py3-none-any.whl").write_bytes(b"")

    scan = _scan_pep503_directory(package_dir, "foo")

    assert scan.all_yanked is expected


def test_a_page_of_yanked_misnamed_links_reads_as_yanked(tmp_path: Path) -> None:
    """The unreadable flag skips yanked links, so only the yank flag answers.

    Nothing can set both flags: the unreadable one needs a link that stands,
    and the yank one needs every link withdrawn.
    """
    package_dir = _make_index(tmp_path, _anchor("foo-1.0.zip", yanked=True))

    scan = _scan_pep503_directory(package_dir, "foo")

    assert scan.files == []
    assert not scan.unreadable
    assert scan.all_yanked


def test_a_page_of_unreachable_links_is_not_an_absent_package(
    tmp_path: Path,
) -> None:
    """An href naming a file nab cannot reach still says the page listed one.

    Dropping it silently makes the listing identical to one for a package
    the index does not carry.  The filename parses, so only the href fails
    and the format flag stays clear.
    """
    package_dir = _make_index(tmp_path, _UNREACHABLE)

    scan = _scan_pep503_directory(package_dir, "foo")

    assert scan.files == []
    assert scan.unreachable
    assert not scan.unreadable

    client = LocalIndexClient(tmp_path.as_uri())
    assert run(client.get_files("foo")) == []
    assert client.served_unreachable_only("foo")
    assert not client.served_unreadable_only("foo")


def test_a_page_of_files_naming_another_project_is_not_absent(tmp_path: Path) -> None:
    """A filename that parses to another project still leaves the page naming one.

    ``cffi-1.0.2-2.tar.gz`` parses as project ``cffi-1-0-2`` at version
    ``2``, so the record is dropped while the anchor still named a release.
    """
    package_dir = tmp_path / "cffi"
    package_dir.mkdir()
    (package_dir / "index.html").write_text(
        _anchor("cffi-1.0.2-2.tar.gz"), encoding="utf-8"
    )

    scan = _scan_pep503_directory(package_dir, "cffi")

    assert scan.files == []
    assert scan.named_files
    assert not scan.unreadable
    assert not scan.unreachable

    client = LocalIndexClient(tmp_path.as_uri())
    assert run(client.get_files("cffi")) == []
    assert client.served_no_usable_file("cffi")


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        pytest.param(
            "http://[bad/foo-1.0-py3-none-any.whl", True, id="wheel-behind-a-bad-host"
        ),
        pytest.param("http://[bad", False, id="bad-host-alone"),
        pytest.param("mailto:admin@example.com", False, id="mailto"),
    ],
)
def test_an_href_marks_the_page_only_when_it_names_a_release(
    tmp_path: Path, href: str, expected: bool
) -> None:
    """All three hrefs are dropped; only the one naming a wheel loses a release.

    An unterminated bracket and a ``mailto:`` offer nothing, so marking them
    would report a package the index does not carry as one whose links nab
    could not reach.
    """
    package_dir = _make_index(tmp_path, _anchor(href))

    scan = _scan_pep503_directory(package_dir, "foo")

    assert scan.files == []
    assert scan.unreachable is expected


def test_a_page_of_navigation_links_reads_as_an_empty_page(tmp_path: Path) -> None:
    """An autoindex's own links name no file, so they mark nothing."""
    body = '<a href="../">parent</a><a href="?C=N;O=D">sort</a>'
    package_dir = _make_index(tmp_path, body)

    scan = _scan_pep503_directory(package_dir, "foo")

    assert scan.files == []
    assert not scan.unreachable
    assert not scan.unreadable


def test_get_sdist_archive_returns_file_bytes(tmp_path: Path) -> None:
    sdist = tmp_path / "foo-1.0.tar.gz"
    sdist.write_bytes(b"SDIST-BYTES")
    client = LocalIndexClient(tmp_path.as_uri())
    data = run(client.get_sdist_archive("foo", "1.0", sdist.as_uri()))
    assert data == b"SDIST-BYTES"


def test_get_range_metadata_returns_no_source_result(tmp_path: Path) -> None:
    from packaging.utils import canonicalize_name

    from nab_index.lazy_wheel import RangeOutcome

    client = LocalIndexClient(tmp_path.as_uri())
    result = run(
        client.get_range_metadata(
            "foo", "1.0", "https://x/foo-1.0-py3-none-any.whl", canonicalize_name("foo")
        )
    )
    assert result.text is None
    assert result.outcome is RangeOutcome.UNSUPPORTED


# Base URLs crossed with every generated href below. The list mixes the shapes
# a page URL really takes (a package directory, a UNC share, a Windows drive)
# with the ones _listing_bases has to decline.
_JOIN_BASES = [
    "file:///simple/foo/index.html",
    "file:///simple/foo/",
    "file:///index.html",
    "file:///",
    "file://server/share/foo/index.html",
    "file:///C:/simple/foo/index.html",
    "file:////server/share/foo/index.html",
    "file:///a/b/c/d/e/index.html",
    "file:///simple/./foo/index.html",
    "file:///simple//foo/index.html",
    "file:foo/bar/index.html",
    "https://mirror.example/simple/foo/",
]

# One path segment each. The product covers a scheme, a query, a fragment, a
# leading space, a tab, a NUL, dot segments, and the names that only look like
# one. The dot-run-plus-delimiter entries are what make the corpus
# discriminating: "a/b?x" resolves the same either way, "a/..?x" does not.
_HREF_SEGMENTS = [
    "",
    ".",
    "..",
    "...",
    "a",
    "b.whl",
    "a:b",
    "a;b",
    "a?b",
    "a#b",
    "a\tb",
    "a\x00b",
    "?q",
    "#f",
    ".a",
    "a.",
    "%2e",
    " a",
    "a ",
    "..?q",
    ".?q",
    "..#f",
    ".#f",
]


def _generated_hrefs() -> list[str]:
    """Every join of up to three ``_HREF_SEGMENTS``, deduplicated.

    Each join also appears with a leading and with a trailing slash.
    """
    hrefs: list[str] = []
    for count in (1, 2, 3):
        for combo in itertools.product(_HREF_SEGMENTS, repeat=count):
            body = "/".join(combo)
            hrefs += (body, "/" + body, body + "/")
    return list(dict.fromkeys(hrefs))


def test_merged_href_answers_what_urljoin_answers() -> None:
    # The whole risk in merging by string is that RFC 3986 reference
    # resolution does something else, so every accepted href is compared
    # against urljoin over the generated corpus. The floor on the accepted
    # count is there because a guard that declined everything would pass an
    # equality check over an empty set.
    hrefs = _generated_hrefs()
    accepted = 0
    divergent: list[tuple[str, str, str, str]] = []
    for base in _JOIN_BASES:
        bases = _listing_bases(base)
        if bases is None:
            continue
        for href in hrefs:
            merged = _merged_href(bases, href)
            if merged is None:
                continue
            accepted += 1
            if merged != urljoin(base, href):
                divergent.append((base, href, merged, urljoin(base, href)))

    assert divergent == []
    assert accepted > 8000


@pytest.mark.parametrize(
    "href",
    [
        "foo-1.0-py3-none-any.whl",
        "packages/foo-1.0-py3-none-any.whl",
        "../packages/foo-1.0-py3-none-any.whl",
        "../../packages/ab/cd/foo-1.0-py3-none-any.whl",
        "foo%00bar.whl",
        "f\u00f6o.whl",
        "foo bar.whl",
        "..foo/bar.whl",
    ],
)
def test_merged_href_takes_a_relative_artifact_href(href: str) -> None:
    # The shapes a PEP 503 mirror really emits have to reach the fast path,
    # or the change costs a guard and buys nothing.
    base = "file:///simple/foo/index.html"
    bases = _listing_bases(base)
    assert bases is not None
    assert _merged_href(bases, href) == urljoin(base, href)


@pytest.mark.parametrize(
    "href",
    [
        "",
        "/absolute/foo.whl",
        "//host/foo.whl",
        "https://example.com/foo.whl",
        "file:///other/foo.whl",
        "foo.whl?rev=7",
        "foo.whl#frag",
        "?q=1",
        "#frag",
        "a//b.whl",
        "sub/",
        ".",
        "..",
        "./foo.whl",
        "a/../b.whl",
        "\x00foo.whl",
        " foo.whl",
        "\tfoo.whl",
        "foo\tbar.whl",
        "foo\rbar.whl",
        "foo\nbar.whl",
        "../../../foo.whl",
    ],
)
def test_merged_href_declines_what_urljoin_has_to_resolve(href: str) -> None:
    # Most merge to something urljoin would not produce: a different base, a
    # normalised path, a rewritten reference, a climb above the root. "sub/",
    # "foo.whl?rev=7" and "foo.whl#frag" would merge correctly and are declined
    # anyway: a dot segment can end at a "?" or "#" ("a/..?q"), which the
    # segment check does not look past, and a trailing slash names no artefact.
    bases = _listing_bases("file:///simple/foo/index.html")
    assert bases is not None
    assert _merged_href(bases, href) is None


@pytest.mark.parametrize(
    "base",
    [
        "https://mirror.example/simple/foo/",
        "http://[bad",
        "file:relative/index.html",
        "file:///simple//foo/index.html",
        "file:///simple/./foo/index.html",
        "file:///simple/../foo/index.html",
        "file:////server/share/foo/index.html",
    ],
)
def test_listing_bases_declines_a_base_it_cannot_merge_against(base: str) -> None:
    # A declined base puts the whole page back on urljoin, which is the only
    # thing that reads these correctly.
    assert _listing_bases(base) is None


def test_listing_bases_climbs_to_the_root() -> None:
    bases = _listing_bases("file:///a/b/index.html")
    assert bases == ["file:///a/b/", "file:///a/", "file:///"]


def test_listing_bases_stops_climbing_at_the_fourth_level() -> None:
    # Building a base per level costs the page whatever its directory is
    # deep, and a mirror href climbs two.
    bases = _listing_bases("file:///a/b/c/d/e/index.html")

    assert bases == [
        "file:///a/b/c/d/e/",
        "file:///a/b/c/d/",
        "file:///a/b/c/",
        "file:///a/b/",
    ]
    assert _merged_href(bases, "../../../../foo.whl") is None


def test_a_leading_navigation_link_does_not_settle_the_page() -> None:
    # An autoindex opens with links to its parent and its own sort orders,
    # none of which merge, and the files below them all do.
    anchors, _ = read_page(
        '<a href="?C=N;O=D">name</a>'
        '<a href="../">parent</a>'
        '<a href="foo-1.0-py3-none-any.whl">foo</a>'
        '<a href="foo-2.0-py3-none-any.whl">foo</a>'
    )

    assert _merging_bases("file:///simple/foo/index.html", anchors) == [
        "file:///simple/foo/",
        "file:///simple/",
        "file:///",
    ]


def test_a_page_of_absolute_hrefs_does_not_merge() -> None:
    anchors, _ = read_page(
        '<a href="https://files.example/packages/foo-1.0-py3-none-any.whl">foo</a>'
    )

    assert _merging_bases("file:///simple/foo/index.html", anchors) is None


def test_a_declining_listing_is_settled_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An index built by saving PyPI's own pages carries absolute hrefs that
    # no anchor can merge, and offering each one would cost the guards on top
    # of the urljoin it still has to run.
    offered: list[str] = []
    merge = local_index._merged_href

    def _record(bases: list[str], href: str) -> str | None:
        offered.append(href)
        return merge(bases, href)

    urls = [
        f"https://files.example/packages/foo-{n}.0-py3-none-any.whl"
        for n in range(1, 9)
    ]
    body = "".join(f'<a href="{url}">{url}</a>' for url in urls)
    package_dir = _make_index(tmp_path, body)

    monkeypatch.setattr(local_index, "_merged_href", _record)
    scan = _scan_pep503_directory(package_dir, "foo")

    assert len(offered) == 2
    assert [file.url for file in scan.files] == urls


def test_a_plain_listing_never_reaches_urljoin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fast path that stopped firing would leave every assertion above
    # green, so the three href shapes a mirror emits are resolved with
    # urljoin taken away.
    def _refuse(base: str, href: str) -> str:
        msg = f"urljoin resolved {href!r}"
        raise AssertionError(msg)

    body = (
        '<a href="foo-1.0-py3-none-any.whl">1</a>'
        '<a href="packages/foo-2.0-py3-none-any.whl">2</a>'
        '<a href="../packages/foo-3.0-py3-none-any.whl">3</a>'
    )
    package_dir = _make_index(tmp_path, body)
    wheels = [
        package_dir / "foo-1.0-py3-none-any.whl",
        package_dir / "packages" / "foo-2.0-py3-none-any.whl",
        tmp_path / "packages" / "foo-3.0-py3-none-any.whl",
    ]
    for wheel in wheels:
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_bytes(b"")

    monkeypatch.setattr(local_index, "urljoin", _refuse)
    scan = _scan_pep503_directory(package_dir, "foo")

    assert not scan.unreadable
    assert [file.url for file in scan.files] == [wheel.as_uri() for wheel in wheels]
