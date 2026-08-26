"""Tests for nab_index.local_index PEP 503 directory scanning."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

import pytest

from nab_index.local_index import LocalIndexClient, _scan_pep503_directory

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
    assert _scan_pep503_directory(package_dir, "foo") == ([], False, False)


def _anchor(filename: str, *, yanked: bool = False) -> str:
    """One PEP 503 link, yanked or not."""
    attribute = ' data-yanked=""' if yanked else ""
    return f'<a href="{filename}"{attribute}>{filename}</a>'


_YANKED_WHEEL = _anchor("foo-1.0-py3-none-any.whl", yanked=True)
_YANKED_SDIST = _anchor("foo-2.0.tar.gz", yanked=True)
_MISNAMED = _anchor("foo-1.0.zip")
_READABLE = _anchor("foo-3.0-py3-none-any.whl")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(_YANKED_WHEEL, True, id="one-yanked-link"),
        pytest.param(_YANKED_WHEEL + _YANKED_SDIST, True, id="every-link-yanked"),
        pytest.param("", False, id="no-links"),
        pytest.param(_MISNAMED, False, id="misnamed-link-alone"),
        pytest.param(_YANKED_WHEEL + _MISNAMED, False, id="one-link-stands"),
        pytest.param(_YANKED_WHEEL + _READABLE, False, id="one-link-admitted"),
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

    _files, _unreadable, all_yanked = _scan_pep503_directory(package_dir, "foo")

    assert all_yanked is expected


def test_a_page_of_yanked_misnamed_links_reads_as_yanked(tmp_path: Path) -> None:
    """The unreadable flag skips yanked links, so only the yank flag answers.

    Nothing can set both flags: the unreadable one needs a link that stands,
    and the yank one needs every link withdrawn.
    """
    package_dir = _make_index(tmp_path, _anchor("foo-1.0.zip", yanked=True))

    files, unreadable, all_yanked = _scan_pep503_directory(package_dir, "foo")

    assert files == []
    assert not unreadable
    assert all_yanked


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
