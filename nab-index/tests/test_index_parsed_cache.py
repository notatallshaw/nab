"""Tests for the C1 parsed-listing cache storage layer in nab_index.cache.

Covers the ``body_digest`` policy field and its encode/decode, the
policy-only ``get_simple_policy`` read, the opaque-bytes
``get_simple_parsed``/``put_simple_parsed`` pair, and the ``.parsed`` arm
of ``read_cache_entry``. No resolve-path behaviour is exercised here.
"""

from __future__ import annotations

import json
import logging
import marshal
from pathlib import Path

import pytest

from nab_index.cache import (
    CACHE_VERSION_SIMPLE_PARSED,
    CachePolicy,
    NullCache,
    OnDiskCache,
    _decode_policy,
    _encode_policy,
    is_recognized_bucket,
)

_FRESH = CachePolicy(fetched_at=0, max_age=600, etag=None)


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
        policy = CachePolicy(fetched_at=5, max_age=9, etag="t", body_digest="d1")
        cache.put_simple("foo", b'{"files": []}', policy)
        # The body is gone; a policy-only read must still hit.
        (tmp_path / "simple-v0" / "pypi" / "foo.json").unlink()
        assert cache.get_simple_policy("foo") == policy

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

    def test_put_simple_parsed_noop(self) -> None:
        assert NullCache().put_simple_parsed("foo", b"blob") is None
