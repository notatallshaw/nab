"""Tests for the parsed-listing cache storage layer and write path.

Covers the ``body_digest`` policy field and its encode/decode, the
policy-only ``get_simple_policy`` read, the opaque-bytes
``get_simple_parsed``/``put_simple_parsed`` pair, and the ``.parsed`` arm
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
import marshal
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any, TypeVar

import pytest

from nab_index.cache import (
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
from nab_index.client import _parse_files
from nab_index.parsed_listing import corruption_reason, decode, encode

_FRESH = CachePolicy(fetched_at=0, max_age=600, etag=None)

_T = TypeVar("_T")

# Constructor form and the trailing-slash form the client normalizes to and
# feeds _parse_files; the cache maps both to the "pypi" index dir.
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
        (tmp_path / "simple-v0" / "pypi" / "foo.json").unlink()
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
        path = tmp_path / "simple-v0" / "pypi" / "foo.policy"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not json")
        with caplog.at_level(logging.WARNING):
            assert cache.get_simple_policy("foo") is None
        assert "Corrupt cache policy" in caplog.text


class TestReadCacheEntryParsed:
    def test_valid_parsed_is_clean(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        cache.put_simple_parsed("foo", marshal.dumps((1, 0, (3, 14), 0)))
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
        path.write_bytes(b"\xff\xfe not marshal")
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

    def test_null_cache_returns_body_digest(self) -> None:
        assert NullCache().put_simple("foo", b"abc", _FRESH) == (
            hashlib.sha256(b"abc").hexdigest()
        )


class TestWritePathParsedBlob:
    """The resolve-path write points bind a parsed blob to the stored body."""

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
        decoded = decode(blob, policy)
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
        assert decode(blob, policy) == files

    def test_revalidate_304_preserves_body_parsed_and_digest(
        self, tmp_path: Path
    ) -> None:
        cache = _cache(tmp_path)
        body = _LISTING_BYTES
        digest = cache.put_simple(
            "pkg", body, CachePolicy(fetched_at=0, max_age=0, etag="e1")
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
        assert decode(old_blob, policy) == files


_PARSED = _parse_files(json.loads(_LISTING_BYTES), _INDEX_NORM, "pkg")

_JSON_PATH_PARTS = ("simple-v0", "pypi", "pkg.json")


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


def _tamper_header(blob: bytes, index: int, value: object) -> bytes:
    header, body = marshal.loads(blob)  # noqa: S302
    header = list(header)
    header[index] = value
    return marshal.dumps((tuple(header), body))


def _tamper_body(blob: bytes, value: object) -> bytes:
    header, _body = marshal.loads(blob)  # noqa: S302
    return marshal.dumps((header, value))


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
        assert decode(blob, policy) == got

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
        assert decode(cache.get_simple_parsed("pkg"), policy) == files

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("format", 99),
            ("codec", 99),
            ("interp", (2, 0)),
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
        index = ["format", "codec", "interp", "key_scheme"].index(field)
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
        assert decode(cache.get_simple_parsed("pkg"), policy) == files

    @pytest.mark.parametrize("blob", [b"\xff\xfe not marshal", b"", b"\x00\x01\x02"])
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
        assert decode(cache.get_simple_parsed("pkg"), policy) == files

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
        "body",
        [(), ([], [], [0]), ([("short",)], [], [0])],
    )
    def test_body_corrupt_blob_warns_and_reparses(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, body: object
    ) -> None:
        # A blob whose header matches this build and digest but whose body was
        # mangled on disk self-heals to a logged rebuild, never crashing.
        cache = _cache(tmp_path)
        files, digest = _warm_bound(cache)
        cache.put_simple_parsed("pkg", _tamper_body(encode(files, digest), body))
        transport = _FakeTransport([])
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        with caplog.at_level(logging.WARNING):
            got = _run(client.get_files("pkg"))

        assert got == files
        assert "Corrupt parsed-listing" in caplog.text
        result = cache.get_simple("pkg")
        assert result is not None
        _, policy = result
        assert decode(cache.get_simple_parsed("pkg"), policy) == files

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
        assert decode(cache.get_simple_parsed("pkg"), got_policy) == _PARSED


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
        new_listing = json.dumps(
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
        transport = _FakeTransport(
            [_FakeResponse(new_listing, status=200, headers={"etag": "v2"})]
        )
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)

        got = _run(client.get_files("pkg"))

        assert [f.version for f in got] == ["2.0"]
        result = cache.get_simple("pkg")
        assert result is not None
        body, policy = result
        assert body == new_listing
        assert decode(cache.get_simple_parsed("pkg"), policy) == got

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
        assert decode(before, policy) == files


class TestParsedCorruptionReason:
    def test_garbage_is_corrupt(self) -> None:
        assert corruption_reason(b"not marshal") is not None

    def test_truncated_is_corrupt(self) -> None:
        blob = encode(_PARSED, "a" * 64)
        assert corruption_reason(blob[: len(blob) // 2]) is not None

    def test_wrong_top_shape_is_corrupt(self) -> None:
        assert corruption_reason(marshal.dumps((1, 2, 3))) is not None

    def test_wrong_header_shape_is_corrupt(self) -> None:
        assert corruption_reason(marshal.dumps(("bad", "body"))) is not None

    @pytest.mark.parametrize(
        "body",
        [(), ([], [], [0]), ([("short",)], [], [0])],
    )
    def test_wrong_body_shape_is_corrupt(self, body: object) -> None:
        tampered = _tamper_body(encode(_PARSED, "a" * 64), body)
        assert corruption_reason(tampered) is not None

    def test_well_formed_blob_is_clean(self) -> None:
        assert corruption_reason(encode(_PARSED, "a" * 64)) is None

    def test_header_value_mismatch_is_clean(self) -> None:
        # A build/digest mismatch is a benign self-heal, not corruption.
        tampered = _tamper_header(encode(_PARSED, "a" * 64), 0, 99)
        assert corruption_reason(tampered) is None

    def test_foreign_build_with_wrong_body_is_clean(self) -> None:
        # A future build may bump the codec and write a body shape this build
        # never wrote; that is benign version skew, not on-disk corruption.
        tampered = _tamper_body(_tamper_header(encode(_PARSED, "a" * 64), 1, 99), ())
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
        cache.put_simple_parsed("pkg", b"not marshal")
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
