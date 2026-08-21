"""Tests for the module-level ``read_fresh_parsed_listing`` read helper.

The helper is the synchronous, write-free subset of ``get_files``' fresh-hit
branch used by the warm-sync listing path: it serves records only on a clean
fresh (or offline) parsed-blob hit and declines to ``None`` on every other
reason, never writing, revalidating, or rebuilding. These tests pin its
serve/decline taxonomy and, on a shared warmed fixture, pin the served records
to ``get_files`` so a future edit to either cannot drift them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, TypeVar

import pytest

from nab_index.cache import CACHE_VERSION_SIMPLE, CachePolicy, OnDiskCache
from nab_index.cached_client import (
    CachedAsyncSimpleClient,
    read_fresh_parsed_listing,
)
from nab_index.client import _parse_files
from nab_index.parsed_listing import encode

_T = TypeVar("_T")

_INDEX = "https://pypi.org/simple"
_INDEX_NORM = "https://pypi.org/simple/"

_LISTING = {
    "meta": {"api-version": "1.0"},
    "name": "pkg",
    "files": [
        {
            "filename": "pkg-1.0-py3-none-any.whl",
            "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
            "requires-python": ">=3.8",
            "core-metadata": {"sha256": "abc"},
            "hashes": {"sha256": "deadbeef"},
        },
        {
            "filename": "pkg-1.0.tar.gz",
            "url": "https://files.example.com/pkg-1.0.tar.gz",
            "hashes": {"sha256": "cafef00d"},
        },
    ],
}
_LISTING_BYTES = json.dumps(_LISTING).encode()
_PARSED = _parse_files(json.loads(_LISTING_BYTES), _INDEX_NORM, "pkg")

# Stands in for a body nested past the decoder's guard (``refuse_over_nested``).
_OVER_NESTED = b"[[[]]]"

# A page of only formats nab does not read, so it parses to zero files.
_ZIP_ONLY_BYTES = json.dumps(
    {
        "meta": {"api-version": "1.0"},
        "name": "pkg",
        "files": [
            {
                "filename": "pkg-1.0.zip",
                "url": "https://files.example.com/pkg-1.0.zip",
                "hashes": {"sha256": "deadbeef"},
            }
        ],
    }
).encode()

# Derived so a bucket-version bump does not need every path updated.
_JSON_PATH_PARTS = (f"simple-{CACHE_VERSION_SIMPLE}", "pypi", "pkg.json")
_POLICY_PATH_PARTS = (f"simple-{CACHE_VERSION_SIMPLE}", "pypi", "pkg.policy")


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        url: str = "",
    ) -> None:
        self.content = body
        self.status_code = status
        self.headers = headers or {}
        # Empty means the transport fills in the requested URL. Set it to
        # stand in for a page the index redirected to.
        self.url = url

    def raise_for_status(self) -> None:
        return None


class _FakeTransport:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls.append((url, headers))
        response = self._responses.pop(0)
        if not response.url:
            response.url = url
        return response

    async def aclose(self) -> None:
        return None


def _cache(root: Path) -> OnDiskCache:
    return OnDiskCache(root, _INDEX)


def _warm_bound(
    cache: OnDiskCache, *, fresh: bool = True, body: bytes = _LISTING_BYTES
) -> tuple[list, str]:
    """Warm a package's body, policy, and a parsed blob bound to that body."""
    policy = CachePolicy(
        fetched_at=2_000_000_000 if fresh else 0,
        max_age=99999 if fresh else 0,
        etag="e",
    )
    digest = cache.put_simple("pkg", body, policy)
    files = _parse_files(json.loads(body), _INDEX_NORM, "pkg")
    cache.put_simple_parsed("pkg", encode(files, digest))
    return files, digest


def _read_files(cache: OnDiskCache, *, offline: bool) -> list | None:
    """The records the helper serves for ``pkg``, or ``None`` on a decline."""
    parsed = read_fresh_parsed_listing(cache, "pkg", offline=offline)
    return None if parsed is None else parsed.files


class TestParsedBlobSize:
    def test_size_matches_written_blob(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        blob = cache.get_simple_parsed("pkg")
        assert blob is not None
        assert cache.get_simple_parsed_size("pkg") == len(blob)

    def test_size_none_when_absent(self, tmp_path: Path) -> None:
        assert _cache(tmp_path).get_simple_parsed_size("pkg") is None


class TestReadFreshParsedListing:
    def test_fresh_hit_returns_records(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        assert _read_files(cache, offline=False) == files

    def test_fresh_offline_returns_records(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        assert _read_files(cache, offline=True) == files

    def test_stale_offline_returns_records(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache, fresh=False)
        assert _read_files(cache, offline=True) == files

    def test_absent_policy_returns_none(self, tmp_path: Path) -> None:
        assert read_fresh_parsed_listing(_cache(tmp_path), "pkg", offline=False) is None

    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b'{"fetched_at": Infinity, "max_age": 99999, "etag": null}',
            b'{"fetched_at": 2000000000, "max_age": 1e400, "etag": null}',
        ],
    )
    def test_corrupt_policy_returns_none(self, tmp_path: Path, raw: bytes) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        tmp_path.joinpath(*_POLICY_PATH_PARTS).write_bytes(raw)
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    def test_over_nested_policy_returns_none(
        self,
        tmp_path: Path,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        tmp_path.joinpath(*_POLICY_PATH_PARTS).write_bytes(_OVER_NESTED)
        with refuse_over_nested(_OVER_NESTED):
            assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    def test_stale_online_returns_none(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache, fresh=False)
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    def test_absent_blob_returns_none(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg",
            _LISTING_BYTES,
            CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None),
        )
        assert cache.get_simple_parsed("pkg") is None
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    def test_digest_mismatch_returns_none(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        cache.put_simple_parsed("pkg", encode(files, "f" * 64))
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    @pytest.mark.parametrize("blob", [b"\xff\xfe not json", b"", b"\x00\x01\x02"])
    def test_corrupt_blob_returns_none(self, tmp_path: Path, blob: bytes) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        cache.put_simple_parsed("pkg", blob)
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    def test_over_nested_blob_returns_none(
        self,
        tmp_path: Path,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        cache.put_simple_parsed("pkg", _OVER_NESTED)
        with refuse_over_nested(_OVER_NESTED):
            assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    def test_empty_listing_returns_none(self, tmp_path: Path) -> None:
        # A page of formats nab does not read parses to zero files; the blob
        # cannot say so, so the helper declines and the async path reclassifies.
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache, body=_ZIP_ONLY_BYTES)
        assert files == []
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None

    def test_hit_does_not_write(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        before = cache.get_simple_parsed("pkg")
        read_fresh_parsed_listing(cache, "pkg", offline=False)
        assert cache.get_simple_parsed("pkg") == before

    def test_decline_does_not_write(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        mismatched = encode(files, "f" * 64)
        cache.put_simple_parsed("pkg", mismatched)
        read_fresh_parsed_listing(cache, "pkg", offline=False)
        # No rebuild: the mismatched blob is left exactly as it was.
        assert cache.get_simple_parsed("pkg") == mismatched


class TestIdenticalByConstruction:
    def test_hit_records_equal_get_files(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        # Serve get_files from the blob alone: drop the raw body first.
        tmp_path.joinpath(*_JSON_PATH_PARTS).unlink()
        client = CachedAsyncSimpleClient(_FakeTransport([]), cache, _INDEX)

        served = _run(client.get_files("pkg"))
        helper = _read_files(cache, offline=False)

        assert helper == served == files

    def test_absent_blob_declines_while_get_files_rebuilds(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg",
            _LISTING_BYTES,
            CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None),
        )
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None
        client = CachedAsyncSimpleClient(_FakeTransport([]), cache, _INDEX)
        assert _run(client.get_files("pkg")) == _PARSED

    def test_digest_mismatch_declines_while_get_files_rebuilds(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        cache.put_simple_parsed("pkg", encode(files, "f" * 64))
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None
        client = CachedAsyncSimpleClient(_FakeTransport([]), cache, _INDEX)
        assert _run(client.get_files("pkg")) == files

    def test_corrupt_blob_declines_while_get_files_rebuilds(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        cache.put_simple_parsed("pkg", b"not json")
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None
        client = CachedAsyncSimpleClient(_FakeTransport([]), cache, _INDEX)
        assert _run(client.get_files("pkg")) == files

    def test_empty_listing_declines_while_get_files_reports_the_format(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache, body=_ZIP_ONLY_BYTES)
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None
        client = CachedAsyncSimpleClient(_FakeTransport([]), cache, _INDEX)
        assert _run(client.get_files("pkg")) == []
        assert client.served_unreadable_only("pkg")

    def test_stale_online_declines_while_get_files_revalidates(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache, fresh=False)
        assert read_fresh_parsed_listing(cache, "pkg", offline=False) is None
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "e"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)
        assert _run(client.get_files("pkg")) == files
