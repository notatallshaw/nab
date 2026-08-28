"""Tests for the parsed-listing cache storage layer and write path.

Covers the ``body_digest`` policy field and its encode/decode, the
policy-only ``get_simple_policy`` read, the opaque-bytes
``get_simple_parsed``/``put_simple_parsed`` pair, and the ``.parsed`` branch
of ``read_cache_entry``, plus the write path that binds a parsed blob to
the body just stored: :meth:`OnDiskCache.put_simple` computes the digest,
and every :class:`CachedAsyncSimpleClient` write point emits a blob bound
to it (fetch and 200 revalidation) or carries the digest forward (304).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Coroutine, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, TypeVar

import pytest

from nab_index.cache import (
    CACHE_VERSION_SIMPLE,
    CACHE_VERSION_SIMPLE_PARSED,
    CachePolicy,
    NullCache,
    OfflineError,
    OnDiskCache,
    _decode_policy,
    _encode_policy,
    is_recognized_bucket,
)
from nab_index.cached_client import CachedAsyncSimpleClient, ParsedCacheStats
from nab_index.client import SdistFile, WheelFile, _parse_files
from nab_index.parsed_listing import corruption_reason, decode, encode

# Derived so a bucket-version bump does not need every path updated.
_SIMPLE_BUCKET = f"simple-{CACHE_VERSION_SIMPLE}"

_FRESH = CachePolicy(fetched_at=0, max_age=600, etag=None)

# Stands in for a body nested past the decoder's guard (``refuse_over_nested``).
_OVER_NESTED = b"[[[]]]"

_T = TypeVar("_T")

# Constructor form and the trailing-slash form the client normalizes to and
# feeds _parse_files; the cache maps both to the "pypi" index dir.
_INDEX = "https://pypi.org/simple"
_INDEX_NORM = "https://pypi.org/simple/"
# The page a warm entry was fetched from, which its policy records.
_PAGE_URL = f"{_INDEX_NORM}pkg/"

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

# A replacement listing at a new version, so a swapped body shows in the records.
_LISTING_V2_BYTES = json.dumps(
    {
        "meta": {"api-version": "1.0"},
        "name": "pkg",
        "files": [
            {
                "filename": "pkg-2.0-py3-none-any.whl",
                "url": "https://files.example.com/pkg-2.0-py3-none-any.whl",
                "hashes": {"sha256": "beef"},
            }
        ],
    }
).encode()


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
    return OnDiskCache(root, "https://pypi.org/simple")


def _decoded(blob: bytes | None, policy: CachePolicy) -> list[WheelFile | SdistFile]:
    """The records ``blob`` rehydrates to, asserting it decoded at all."""
    assert blob is not None
    parsed = decode(blob, policy)
    assert parsed is not None
    return parsed.files


class TestBodyDigestPolicy:
    def test_default_is_none(self) -> None:
        assert CachePolicy(fetched_at=0, max_age=1, etag=None).body_digest is None

    def test_round_trip_with_body_digest(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag="x", body_digest="abc123")
        assert _decode_policy(_encode_policy(policy)) == policy

    def test_encode_omits_absent_body_digest(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag="x")
        assert "body_digest" not in json.loads(_encode_policy(policy))

    def test_encode_includes_present_body_digest(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag=None, body_digest="ff00")
        assert json.loads(_encode_policy(policy))["body_digest"] == "ff00"

    def test_decode_absent_body_digest_is_none(self) -> None:
        raw = json.dumps({"fetched_at": 1, "max_age": 2, "etag": None}).encode("utf-8")
        decoded = _decode_policy(raw)
        assert decoded is not None
        assert decoded.body_digest is None


class TestGetSimpleParsedBucket:
    def test_bucket_is_recognized(self) -> None:
        assert is_recognized_bucket(f"simple-parsed-{CACHE_VERSION_SIMPLE_PARSED}")

    def test_round_trip(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple_parsed("foo", b"\x00blob\x01")
        assert cache.get_simple_parsed("foo") == b"\x00blob\x01"

    def test_written_under_parsed_bucket(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple_parsed("foo", b"blob")
        expected = (
            tmp_path
            / f"simple-parsed-{CACHE_VERSION_SIMPLE_PARSED}"
            / "pypi"
            / "foo.parsed"
        )
        assert expected.read_bytes() == b"blob"

    def test_miss_returns_none(self, tmp_path: Path) -> None:
        assert _cache(tmp_path).get_simple_parsed("absent") is None

    def test_reput_replaces(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple_parsed("foo", b"old")
        cache.put_simple_parsed("foo", b"new")
        assert cache.get_simple_parsed("foo") == b"new"

    def test_partial_write_leaves_prior_blob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _cache(tmp_path)
        cache.put_simple_parsed("foo", b"good")

        def fail_replace(*_a: object, **_k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("nab_index.atomic.os.replace", fail_replace)
        with pytest.raises(OSError, match="disk full"):
            cache.put_simple_parsed("foo", b"partial")
        assert cache.get_simple_parsed("foo") == b"good"
        parent = tmp_path / f"simple-parsed-{CACHE_VERSION_SIMPLE_PARSED}" / "pypi"
        assert [p.name for p in parent.iterdir()] == ["foo.parsed"]


class TestGetSimplePolicy:
    def test_hit_without_reading_body(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        body = b'{"files": []}'
        cache.put_simple("foo", body, CachePolicy(fetched_at=5, max_age=9, etag="t"))
        # The body is gone; a policy-only read must still hit, carrying the
        # digest put_simple stamped from the body.
        (tmp_path / _SIMPLE_BUCKET / "pypi" / "foo.json").unlink()
        got = cache.get_simple_policy("foo")
        assert got == CachePolicy(
            fetched_at=5,
            max_age=9,
            etag="t",
            body_digest=hashlib.sha256(body).hexdigest(),
        )

    def test_miss_returns_none(self, tmp_path: Path) -> None:
        assert _cache(tmp_path).get_simple_policy("absent") is None

    def test_corrupt_policy_misses_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _cache(tmp_path)
        path = tmp_path / _SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not json")
        with caplog.at_level(logging.WARNING):
            assert cache.get_simple_policy("foo") is None
        assert "Corrupt cache policy" in caplog.text


class TestReadCacheEntryParsed:
    def test_valid_parsed_is_clean(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple_parsed("foo", encode(_PARSED, "a" * 64))
        path = (
            tmp_path
            / f"simple-parsed-{CACHE_VERSION_SIMPLE_PARSED}"
            / "pypi"
            / "foo.parsed"
        )
        assert cache.read_cache_entry(path) is None

    def test_structurally_corrupt_parsed(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        path = (
            tmp_path
            / f"simple-parsed-{CACHE_VERSION_SIMPLE_PARSED}"
            / "pypi"
            / "foo.parsed"
        )
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe not json")
        assert cache.read_cache_entry(path) is not None


class TestNullCacheParsed:
    def test_get_simple_policy(self) -> None:
        assert NullCache().get_simple_policy("foo") is None

    def test_get_simple_parsed(self) -> None:
        assert NullCache().get_simple_parsed("foo") is None

    def test_get_simple_parsed_size(self) -> None:
        assert NullCache().get_simple_parsed_size("foo") is None

    def test_put_simple_parsed_noop(self) -> None:
        assert NullCache().put_simple_parsed("foo", b"blob") is None


class _CountingNullCache(NullCache):
    """A disabled cache that records whether a parsed blob was offered to it."""

    def __init__(self) -> None:
        self.parsed_writes = 0

    def put_simple_parsed(self, package: str, blob: bytes) -> None:
        """Count the discarded write."""
        del package, blob
        self.parsed_writes += 1


class TestPutSimpleDigest:
    def test_returns_and_stores_body_digest(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        body = b'{"files": []}'
        digest = cache.put_simple("foo", body, _FRESH)
        assert digest == hashlib.sha256(body).hexdigest()
        result = cache.get_simple("foo")
        assert result is not None
        _, policy = result
        assert policy.body_digest == digest

    def test_overrides_any_passed_digest(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        body = b'{"files": []}'
        stale = CachePolicy(fetched_at=0, max_age=600, etag=None, body_digest="stale")
        digest = cache.put_simple("foo", body, stale)
        assert digest == hashlib.sha256(body).hexdigest()

    def test_null_cache_returns_none(self) -> None:
        # Nothing was stored, so there is no body for a parsed blob to describe.
        assert NullCache().put_simple("foo", b"abc", _FRESH) is None

    def test_null_cache_fetch_writes_no_parsed_blob(self) -> None:
        cache = _CountingNullCache()
        transport = _FakeTransport([_FakeResponse(_LISTING_BYTES)])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        _run(client.get_files("pkg"))

        assert cache.parsed_writes == 0


class _DroppingCache(OnDiskCache):
    """A store whose body write is refused, so ``put_simple`` hands back nothing.

    Models a cache root that cannot take the write (read-only, full disk).
    Reads keep answering from whatever landed before.
    """

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> None:
        """Drop the write and report that nothing was stored."""
        del package, body, policy


class TestDroppedBodyWrite:
    """A body the store refused must leave no parsed blob claiming to describe it."""

    def test_cold_fetch_writes_no_parsed_blob(self, tmp_path: Path) -> None:
        cache = _DroppingCache(tmp_path, _INDEX)
        transport = _FakeTransport([_FakeResponse(_LISTING_BYTES)])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        files = _run(client.get_files("pkg"))

        assert files == _PARSED
        assert cache.get_simple_parsed("pkg") is None

    def test_revalidation_keeps_the_blob_bound_to_the_stored_body(
        self, tmp_path: Path
    ) -> None:
        warm = _cache(tmp_path)
        _warm_bound(warm, fresh=False)
        before = warm.get_simple_parsed("pkg")
        new_body = json.dumps({**_LISTING, "files": []}).encode()
        cache = _DroppingCache(tmp_path, _INDEX)
        transport = _FakeTransport([_FakeResponse(new_body, headers={"etag": "v2"})])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        _run(client.get_files("pkg"))

        # The refused body never landed, so the old body, its policy, and the
        # blob bound to it stay coherent instead of being retired for a body
        # that is not there.
        assert cache.get_simple_parsed("pkg") == before
        result = cache.get_simple("pkg")
        assert result is not None
        body, policy = result
        assert body == _LISTING_BYTES
        assert decode(before, policy) is not None


_UNREADABLE_LISTING_BYTES = json.dumps(
    {
        "meta": {"api-version": "1.0"},
        "name": "pkg",
        "files": [
            {
                "filename": "pkg-1.0.exe",
                "url": "https://files.example.com/pkg-1.0.exe",
                "hashes": {"sha256": "aa"},
            }
        ],
    }
).encode()


class TestWritePathParsedBlob:
    """The resolve-path write points bind a parsed blob to the stored body."""

    def test_listing_with_no_readable_files_writes_no_blob(
        self, tmp_path: Path
    ) -> None:
        # A blob holding no records is declined on read, so writing one would
        # rebuild and rewrite it on every warm resolve without ever serving it.
        cache = _cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(_UNREADABLE_LISTING_BYTES)])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        assert _run(client.get_files("pkg")) == []
        assert cache.get_simple_parsed("pkg") is None

        stats = ParsedCacheStats()
        warm = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )
        assert _run(warm.get_files("pkg")) == []
        assert cache.get_simple_parsed("pkg") is None
        assert (stats.hit, stats.miss, stats.rebuild) == (0, 1, 0)

    def test_fetch_writes_bound_parsed_blob(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        transport = _FakeTransport(
            [_FakeResponse(_LISTING_BYTES, headers={"etag": "v1"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        files = _run(client.get_files("pkg"))

        result = cache.get_simple("pkg")
        assert result is not None
        body, policy = result
        assert body == _LISTING_BYTES
        assert policy.body_digest == hashlib.sha256(_LISTING_BYTES).hexdigest()
        blob = cache.get_simple_parsed("pkg")
        assert blob is not None
        decoded = _decoded(blob, policy)
        assert decoded == files
        assert decoded == _parse_files(json.loads(body), _INDEX_NORM, "pkg")

    def test_revalidate_200_writes_bound_parsed_blob(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg", b'{"files": []}', CachePolicy(fetched_at=0, max_age=0, etag="old")
        )
        transport = _FakeTransport(
            [_FakeResponse(_LISTING_BYTES, status=200, headers={"etag": "v2"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        files = _run(client.get_files("pkg"))

        result = cache.get_simple("pkg")
        assert result is not None
        body, policy = result
        assert body == _LISTING_BYTES
        assert policy.body_digest == hashlib.sha256(_LISTING_BYTES).hexdigest()
        blob = cache.get_simple_parsed("pkg")
        assert blob is not None
        assert _decoded(blob, policy) == files

    def test_revalidate_304_preserves_body_parsed_and_digest(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        body = _LISTING_BYTES
        digest = cache.put_simple(
            "pkg",
            body,
            CachePolicy(fetched_at=0, max_age=0, etag="e1", page_url=_PAGE_URL),
        )
        old_files = _parse_files(json.loads(body), _INDEX_NORM, "pkg")
        old_blob = encode(old_files, digest)
        cache.put_simple_parsed("pkg", old_blob)
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "e1"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        files = _run(client.get_files("pkg"))

        result = cache.get_simple("pkg")
        assert result is not None
        new_body, policy = result
        assert new_body == body
        assert policy.body_digest == digest
        assert cache.get_simple_parsed("pkg") == old_blob
        assert _decoded(old_blob, policy) == files

    def test_revalidate_304_from_a_new_page_retires_the_blob(
        self, tmp_path: Path
    ) -> None:
        """A moved page re-resolves relative entries, so the old blob is retired."""
        cache = _cache(tmp_path)
        digest = cache.put_simple(
            "pkg",
            _LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=0, etag="e1", page_url=_PAGE_URL),
        )
        old_blob = encode(_parse_files(_LISTING, _INDEX_NORM, "pkg"), digest)
        cache.put_simple_parsed("pkg", old_blob)
        moved = "https://mirror.example/simple/pkg/"
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "e1"}, url=moved)]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        _run(client.get_files("pkg"))

        result = cache.get_simple("pkg")
        assert result is not None
        new_body, policy = result
        assert new_body == _LISTING_BYTES
        assert policy.page_url == moved
        assert policy.body_digest is None
        # The blob is still on disk but no longer binds, so the next read
        # rebuilds it against the new page rather than serving stale URLs.
        assert cache.get_simple_parsed("pkg") == old_blob
        assert decode(old_blob, policy) is None


_SURROGATE_REQUIRES_PYTHON = ">=3.8\ud800"

# The escape is plain ASCII on the wire; json.loads is what makes it a surrogate.
_SURROGATE_LISTING_BYTES = json.dumps(
    {
        "meta": {"api-version": "1.0"},
        "name": "pkg",
        "files": [
            {
                "filename": "pkg-1.0-py3-none-any.whl",
                "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
                "requires-python": _SURROGATE_REQUIRES_PYTHON,
                "hashes": {"sha256": "cafef00d"},
            }
        ],
    }
).encode()


class TestSurrogateInListingString:
    def test_field_with_no_utf8_form_is_served_and_cached(self, tmp_path: Path) -> None:
        """A field with no UTF-8 form must not abort the fetch or the blob write.

        ``requires-python`` is kept verbatim, so a surrogate in it reaches the
        blob; the warm read proves the blob was written rather than rebuilt.
        """
        cache = _cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(_SURROGATE_LISTING_BYTES)])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        files = _run(client.get_files("pkg"))

        assert [record.requires_python for record in files] == [
            _SURROGATE_REQUIRES_PYTHON
        ]

        stats = ParsedCacheStats()
        warm = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )

        assert _run(warm.get_files("pkg")) == files
        assert (stats.hit, stats.miss, stats.rebuild) == (1, 0, 0)


_PARSED = _parse_files(json.loads(_LISTING_BYTES), _INDEX_NORM, "pkg")

_JSON_PATH_PARTS = (_SIMPLE_BUCKET, "pypi", "pkg.json")


def _warm_bound(
    cache: OnDiskCache, *, fresh: bool = True, body: bytes = _LISTING_BYTES
) -> tuple[list, str]:
    """Warm a package's body, policy, and a parsed blob bound to that body."""
    policy = CachePolicy(
        fetched_at=2_000_000_000 if fresh else 0,
        max_age=99999 if fresh else 0,
        etag="e",
        page_url=_PAGE_URL,
    )
    digest = cache.put_simple("pkg", body, policy)
    files = _parse_files(json.loads(body), _INDEX_NORM, "pkg")
    cache.put_simple_parsed("pkg", encode(files, digest))
    return files, digest


def _tamper_header(blob: bytes, index: int, value: object) -> bytes:
    header, rows = json.loads(blob)
    header[index] = value
    return json.dumps([header, rows]).encode()


def _tamper_rows(blob: bytes, value: object) -> bytes:
    header, _rows = json.loads(blob)
    return json.dumps([header, value]).encode()


class TestReadPathParsedHit:
    def test_hit_returns_without_reading_body_or_network(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        # Delete the raw body: a hit must serve the parsed blob alone.
        tmp_path.joinpath(*_JSON_PATH_PARTS).unlink()
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert got == files
        assert transport.calls == []

    def test_hit_does_not_rewrite_the_blob(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        before = cache.get_simple_parsed("pkg")
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        _run(client.get_files("pkg"))

        assert cache.get_simple_parsed("pkg") == before


class TestReadPathRebuild:
    def test_cold_parsed_miss_reparses_and_rebuilds(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        digest = cache.put_simple(
            "pkg",
            _LISTING_BYTES,
            CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None),
        )
        assert cache.get_simple_parsed("pkg") is None
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert got == _PARSED
        assert transport.calls == []
        blob = cache.get_simple_parsed("pkg")
        assert blob is not None
        result = cache.get_simple("pkg")
        assert result is not None
        _, policy = result
        assert policy.body_digest == digest
        assert _decoded(blob, policy) == got

    def test_rebuild_on_digest_mismatch_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        # Rebind the blob to a foreign body digest so the gate fails.
        cache.put_simple_parsed("pkg", encode(files, "f" * 64))
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert "Corrupt parsed-listing" not in caplog.text
        result = cache.get_simple("pkg")
        assert result is not None
        _, policy = result
        assert _decoded(cache.get_simple_parsed("pkg"), policy) == files

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("format", 99),
            ("codec", 99),
            ("key_scheme", 99),
        ],
    )
    def test_rebuild_on_header_mismatch_no_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        field: str,
        value: object,
    ) -> None:
        cache = _cache(tmp_path)
        files, digest = _warm_bound(cache)
        index = ["format", "codec", "key_scheme"].index(field)
        tampered = _tamper_header(encode(files, digest), index, value)
        cache.put_simple_parsed("pkg", tampered)
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert "Corrupt parsed-listing" not in caplog.text
        result = cache.get_simple("pkg")
        assert result is not None
        _, policy = result
        # The rebuilt blob is now well-formed and hits.
        assert _decoded(cache.get_simple_parsed("pkg"), policy) == files

    @pytest.mark.parametrize("blob", [b"\xff\xfe not json", b"", b"\x00\x01\x02"])
    def test_garbage_blob_warns_and_reparses(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, blob: bytes
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        cache.put_simple_parsed("pkg", blob)
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert "Corrupt parsed-listing" in caplog.text
        result = cache.get_simple("pkg")
        assert result is not None
        _, policy = result
        assert _decoded(cache.get_simple_parsed("pkg"), policy) == files

    def test_over_nested_blob_warns_and_reparses(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        cache.put_simple_parsed("pkg", _OVER_NESTED)
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with refuse_over_nested(_OVER_NESTED), caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert "Corrupt parsed-listing" in caplog.text
        assert "nested too deeply to decode" in caplog.text

    def test_truncated_blob_warns_and_reparses(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _cache(tmp_path)
        files, digest = _warm_bound(cache)
        good = encode(files, digest)
        cache.put_simple_parsed("pkg", good[: len(good) // 2])
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert "Corrupt parsed-listing" in caplog.text

    @pytest.mark.parametrize(
        "rows",
        ["not-a-list", [[]], [["short"]], [[0, "only-a-filename"]]],
    )
    def test_row_corrupt_blob_warns_and_reparses(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, rows: object
    ) -> None:
        # A blob whose header matches this build and digest but whose rows were
        # mangled on disk self-heals to a logged rebuild, never crashing.
        cache = _cache(tmp_path)
        files, digest = _warm_bound(cache)
        cache.put_simple_parsed("pkg", _tamper_rows(encode(files, digest), rows))
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert "Corrupt parsed-listing" in caplog.text
        result = cache.get_simple("pkg")
        assert result is not None
        _, policy = result
        assert _decoded(cache.get_simple_parsed("pkg"), policy) == files

    def test_pre_c1_policy_without_digest_self_heals(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        digest = cache.put_simple(
            "pkg",
            _LISTING_BYTES,
            CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None),
        )
        # Simulate an entry written before body_digest existed: strip the digest.
        cache.refresh_simple_policy(
            "pkg",
            CachePolicy(
                fetched_at=2_000_000_000, max_age=99999, etag=None, body_digest=None
            ),
        )
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert got == _PARSED
        # The rebuild stamped the digest into the policy and bound a blob to it.
        got_policy = cache.get_simple_policy("pkg")
        assert got_policy is not None
        assert got_policy.body_digest == digest
        assert _decoded(cache.get_simple_parsed("pkg"), got_policy) == _PARSED


class TestReadPathCorruptBody:
    def test_fresh_corrupt_body_refetches_online(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg",
            b"<html>not json",
            CachePolicy(fetched_at=2_000_000_000, max_age=1, etag=None),
        )
        transport = _FakeTransport(
            [_FakeResponse(_LISTING_BYTES, headers={"etag": "v1"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert got == _PARSED
        assert len(transport.calls) == 1

    def test_fresh_corrupt_body_offline_raises(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg", b"<html>not json", CachePolicy(fetched_at=0, max_age=1, etag=None)
        )
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX, offline=True)

        with pytest.raises(OfflineError, match="pkg"):
            _run(client.get_files("pkg"))
        assert transport.calls == []


class TestReadPathOffline:
    def test_offline_parsed_hit_without_body_or_network(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache, fresh=False)
        tmp_path.joinpath(*_JSON_PATH_PARTS).unlink()
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX, offline=True)

        got = _run(client.get_files("pkg"))

        assert got == files
        assert transport.calls == []

    def test_offline_reparse_fallback_on_corrupt_blob(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache, fresh=False)
        cache.put_simple_parsed("pkg", b"\xff garbage")
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX, offline=True)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert transport.calls == []
        assert "Corrupt parsed-listing" in caplog.text

    def test_offline_policy_present_body_absent_raises(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg", _LISTING_BYTES, CachePolicy(fetched_at=0, max_age=1, etag=None)
        )
        # Body torn away, policy left behind and no parsed blob.
        tmp_path.joinpath(*_JSON_PATH_PARTS).unlink()
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX, offline=True)

        with pytest.raises(OfflineError, match="pkg"):
            _run(client.get_files("pkg"))
        assert transport.calls == []

    def test_offline_both_absent_raises(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX, offline=True)

        with pytest.raises(OfflineError, match="pkg"):
            _run(client.get_files("pkg"))


class TestReadPathRevalidate:
    def test_stale_online_200_replaces_body_and_blob(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache, fresh=False)
        transport = _FakeTransport(
            [_FakeResponse(_LISTING_V2_BYTES, status=200, headers={"etag": "v2"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert [f.version for f in got] == ["2.0"]
        result = cache.get_simple("pkg")
        assert result is not None
        body, policy = result
        assert body == _LISTING_V2_BYTES
        assert _decoded(cache.get_simple_parsed("pkg"), policy) == got

    def test_stale_online_200_revalidates_without_reading_the_body(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache, fresh=False)
        # The body is gone, so a read of it before revalidating would fetch
        # unconditionally instead of sending the ETag.
        tmp_path.joinpath(*_JSON_PATH_PARTS).unlink()
        transport = _FakeTransport(
            [_FakeResponse(_LISTING_V2_BYTES, status=200, headers={"etag": "v2"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert [f.version for f in got] == ["2.0"]

        assert len(transport.calls) == 1
        _, headers = transport.calls[0]
        assert headers is not None
        assert headers["If-None-Match"] == "e"

    def test_stale_online_304_reuses_blob(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache, fresh=False)
        before = cache.get_simple_parsed("pkg")
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "e"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert got == files
        assert cache.get_simple_parsed("pkg") == before
        result = cache.get_simple("pkg")
        assert result is not None
        _, policy = result
        assert _decoded(before, policy) == files

    def test_stale_online_304_serves_the_blob_without_the_body(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache, fresh=False)
        # The body is gone: a 304 must be answered from the blob alone.
        tmp_path.joinpath(*_JSON_PATH_PARTS).unlink()
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "e"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert got == files
        assert len(transport.calls) == 1

    def test_stale_online_304_without_body_or_blob_refetches(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg",
            _LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=0, etag="e", page_url=_PAGE_URL),
        )
        tmp_path.joinpath(*_JSON_PATH_PARTS).unlink()
        transport = _FakeTransport(
            [
                _FakeResponse(b"", status=304, headers={"etag": "e"}),
                _FakeResponse(_LISTING_BYTES, headers={"etag": "e2"}),
            ]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert [f.filename for f in got] == [f.filename for f in _PARSED]

        assert len(transport.calls) == 2

        # The second request is a full copy, not another conditional one.
        _, headers = transport.calls[1]
        assert headers is not None
        assert "If-None-Match" not in headers

        result = cache.get_simple("pkg")
        assert result is not None
        assert result[0] == _LISTING_BYTES

    def test_stale_online_304_with_corrupt_body_refetches(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg",
            b"<html>not json",
            CachePolicy(fetched_at=0, max_age=0, etag="e", page_url=_PAGE_URL),
        )
        transport = _FakeTransport(
            [
                _FakeResponse(b"", status=304, headers={"etag": "e"}),
                _FakeResponse(_LISTING_BYTES, headers={"etag": "e2"}),
            ]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert [f.filename for f in got] == [f.filename for f in _PARSED]
        assert len(transport.calls) == 2
        assert "Corrupt cached Simple-API body" in caplog.text

        result = cache.get_simple("pkg")
        assert result is not None
        assert result[0] == _LISTING_BYTES


class TestParsedCorruptionReason:
    def test_garbage_is_corrupt(self) -> None:
        assert corruption_reason(b"not json") is not None

    def test_truncated_is_corrupt(self) -> None:
        blob = encode(_PARSED, "a" * 64)
        assert corruption_reason(blob[: len(blob) // 2]) is not None

    def test_wrong_top_shape_is_corrupt(self) -> None:
        assert corruption_reason(json.dumps([1, 2, 3]).encode()) is not None

    def test_wrong_header_shape_is_corrupt(self) -> None:
        assert corruption_reason(json.dumps(["bad", "rows"]).encode()) is not None

    @pytest.mark.parametrize(
        "rows",
        ["not-a-list", [[]], [["short"]], [[0, "only-a-filename"]]],
    )
    def test_wrong_row_shape_is_corrupt(self, rows: object) -> None:
        tampered = _tamper_rows(encode(_PARSED, "a" * 64), rows)
        assert corruption_reason(tampered) is not None

    def test_well_formed_blob_is_clean(self) -> None:
        assert corruption_reason(encode(_PARSED, "a" * 64)) is None

    def test_header_value_mismatch_is_clean(self) -> None:
        # A build/digest mismatch is a benign self-heal, not corruption.
        tampered = _tamper_header(encode(_PARSED, "a" * 64), 0, 99)
        assert corruption_reason(tampered) is None

    def test_foreign_build_with_wrong_rows_is_clean(self) -> None:
        # A future build may bump the codec and write a row shape this build
        # never wrote; that is benign version skew, not on-disk corruption.
        tampered = _tamper_rows(_tamper_header(encode(_PARSED, "a" * 64), 1, 99), [])
        assert corruption_reason(tampered) is None


class TestParsedCacheStats:
    def test_default_is_zeroed(self) -> None:
        stats = ParsedCacheStats()
        assert (stats.hit, stats.miss, stats.rebuild) == (0, 0, 0)

    def test_warm_hit_counts_a_hit(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        stats = ParsedCacheStats()
        client = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )

        _run(client.get_files("pkg"))

        assert (stats.hit, stats.miss, stats.rebuild) == (1, 0, 0)

    def test_cold_parsed_miss_counts_a_miss(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple(
            "pkg",
            _LISTING_BYTES,
            CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None),
        )
        stats = ParsedCacheStats()
        client = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )

        _run(client.get_files("pkg"))

        assert (stats.hit, stats.miss, stats.rebuild) == (0, 1, 0)

    def test_digest_mismatch_counts_a_rebuild(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        files, _ = _warm_bound(cache)
        cache.put_simple_parsed("pkg", encode(files, "f" * 64))
        stats = ParsedCacheStats()
        client = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )

        _run(client.get_files("pkg"))

        assert (stats.hit, stats.miss, stats.rebuild) == (0, 0, 1)

    def test_corrupt_blob_counts_a_rebuild(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        cache.put_simple_parsed("pkg", b"not json")
        stats = ParsedCacheStats()
        client = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )

        _run(client.get_files("pkg"))

        assert (stats.hit, stats.miss, stats.rebuild) == (0, 0, 1)

    def test_passed_instance_is_the_one_incremented(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        stats = ParsedCacheStats(hit=5)
        client = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )

        _run(client.get_files("pkg"))

        assert stats.hit == 6

    def test_default_instance_when_none_passed(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache)
        client = CachedAsyncSimpleClient(_FakeTransport([]), cache, _INDEX)

        # No stats injected: the client owns a private sink and still serves.
        got = _run(client.get_files("pkg"))

        assert [f.filename for f in got] == [
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0.tar.gz",
        ]


_ZIP_ONLY_LISTING = {
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
_ZIP_ONLY_BYTES = json.dumps(_ZIP_ONLY_LISTING).encode()


class TestReadPathEmptyListing:
    """A blob holding no records declines, so the raw body reclassifies it.

    A page of formats nab does not read parses to zero files, and only the raw
    body says whether that emptiness means "no such package" or "nothing nab
    reads". The blob carries records alone, so an empty rehydration goes back to
    the body rather than answering from the blob.
    """

    def test_empty_blob_reparses_and_keeps_the_format_report(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache, body=_ZIP_ONLY_BYTES)
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert got == []
        assert transport.calls == []
        assert client.served_unreadable_only("pkg")

    def test_empty_blob_counts_a_rebuild(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache, body=_ZIP_ONLY_BYTES)
        stats = ParsedCacheStats()
        client = CachedAsyncSimpleClient(
            _FakeTransport([]), cache, _INDEX, parsed_stats=stats
        )

        _run(client.get_files("pkg"))

        assert (stats.hit, stats.miss, stats.rebuild) == (0, 0, 1)

    def test_empty_blob_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _cache(tmp_path)
        _warm_bound(cache, body=_ZIP_ONLY_BYTES)
        client = CachedAsyncSimpleClient(_FakeTransport([]), cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            _run(client.get_files("pkg"))

        assert "Corrupt parsed-listing" not in caplog.text
