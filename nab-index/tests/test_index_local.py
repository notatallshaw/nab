"""Tests for nab_index.local_index PEP 503 directory scanning."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

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
    assert _scan_pep503_directory(package_dir, "foo") == ([], False)


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
