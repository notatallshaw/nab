"""Incomplete checked cache records miss without weakening output writes."""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from nab_index.atomic import atomic_write, atomic_write_chunks, atomic_write_text
from nab_index.cache import (
    CachePolicy,
    OnDiskCache,
    _encode_sdist_record,
)
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.httpx_async_transport import HttpxAsyncTransport

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _write_legacy(path: Path, raw: bytes) -> None:
    """Create an older synchronized entry without the new record envelope."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, raw)


INDEX = "https://pypi.org/simple/"
URL = "https://files.example/demo-1-py3-none-any.whl.metadata"
TEXT = "Metadata-Version: 2.1\nName: demo\nVersion: 1\nRequires-Dist: first\nRequires-Dist: second\n\n"


def test_cache_roundtrips_do_not_synchronize_output_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced = []
    real_fsync = os.fsync

    def sync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    cache = OnDiskCache(tmp_path / "cache", INDEX)
    policy = CachePolicy(1, 600, '"tag"')
    body = b'{"files": []}'
    cache.put_simple("demo", body, policy)
    cache.put_simple_parsed("demo", b"opaque parsed data")
    cache.put_metadata("demo", URL, TEXT)
    cache.put_sdist_files("demo", "1", TEXT, None)
    cache.put_negative("absent", policy)

    stored = cache.get_simple("demo")
    assert stored is not None
    assert stored[0] == body
    assert cache.get_simple_parsed("demo") == b"opaque parsed data"
    assert cache.get_simple_parsed_size("demo") == len(b"opaque parsed data")
    assert cache.get_metadata("demo", URL) == TEXT
    assert cache.get_sdist_files("demo", "1") == (TEXT, None)
    assert cache.get_negative("absent") == policy
    assert not synced

    atomic_write_text(tmp_path / "pylock.toml", "version = '1.0'\n")
    assert len(synced) == 1


@pytest.mark.parametrize("damage", ["truncate", "replace", "append", "header"])
def test_valid_utf8_corruption_is_not_returned(tmp_path: Path, damage: str) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_metadata("demo", URL, TEXT)
    path = cache._metadata_path("demo", URL)
    original = path.read_bytes()
    if damage == "truncate":
        changed = original[: -len("Requires-Dist: second\n\n")]
    elif damage == "replace":
        changed = original.replace(b"first", b"other")
    elif damage == "append":
        changed = original + b"\n"
    else:
        changed = b"?" + original[1:]
    assert changed != original
    path.write_bytes(changed)

    assert cache.get_metadata("demo", URL) is None
    assert cache.read_cache_entry(path) is not None


def test_empty_metadata_roundtrips(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_metadata("demo", URL, "")
    assert cache.get_metadata("demo", URL) == ""


def test_valid_but_mismatched_listing_and_policy_miss(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_simple("demo", b'{"files": [1]}', CachePolicy(1, 600, '"old"'))
    body_path, _ = cache._simple_paths("demo")
    old_body_record = body_path.read_bytes()
    cache.put_simple("demo", b'{"files": [2]}', CachePolicy(2, 600, '"new"'))
    body_path.write_bytes(old_body_record)

    assert cache.get_simple_policy("demo") is not None
    assert cache.get_simple("demo") is None


def test_revalidation_retains_the_body_binding(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_simple("demo", b'{"files": []}', CachePolicy(1, 600, '"old"'))
    cache.refresh_simple_policy("demo", CachePolicy(2, 700, '"new"'))
    stored = cache.get_simple("demo")
    assert stored is not None
    assert stored[0] == b'{"files": []}'
    assert stored[1].fetched_at == 2
    assert stored[1].etag == '"new"'


def test_legacy_sdist_is_carried_forward_without_changing_it(tmp_path: Path) -> None:
    legacy = tmp_path / "sdist-v2/pypi/demo/1.record"
    original = _encode_sdist_record(TEXT, None)
    _write_legacy(legacy, original)
    cache = OnDiskCache(tmp_path, INDEX)

    assert cache.get_sdist_files("demo", "1") == (TEXT, None)
    assert legacy.read_bytes() == original
    assert cache._sdist_path("demo", "1").is_file()
    assert cache.read_cache_entry(cache._sdist_path("demo", "1")) is None


def test_streaming_failure_preserves_the_previous_file(tmp_path: Path) -> None:
    target = tmp_path / "cache-record"
    target.write_bytes(b"old")

    def chunks() -> Iterator[bytes]:
        yield b"new prefix"
        raise OSError("write failed")

    with pytest.raises(OSError, match="write failed"):
        atomic_write_chunks(target, chunks(), sync=False)
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("kind", ["body", "policy", "parsed", "sdist", "negative"])
def test_damaged_records_are_misses_for_each_reader(tmp_path: Path, kind: str) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    policy = CachePolicy(1, 600, None)
    cache.put_simple("demo", b'{"files": []}', policy)
    cache.put_sdist_files("demo", "1", TEXT, None)
    cache.put_simple_parsed("demo", b"parsed payload")
    cache.put_negative("demo", policy)
    paths = {
        "body": cache._simple_paths("demo")[0],
        "parsed": cache._parsed_path("demo"),
        "policy": cache._simple_paths("demo")[1],
        "sdist": cache._sdist_path("demo", "1"),
        "negative": cache._neg_path("demo"),
    }
    path = paths[kind]
    path.write_bytes(path.read_bytes()[:5])

    if kind in {"body", "policy"}:
        assert cache.get_simple("demo") is None
    elif kind == "parsed":
        assert cache.get_simple_parsed("demo") is None
    elif kind == "sdist":
        assert cache.get_sdist_files("demo", "1") is None
    else:
        assert cache.get_negative("demo") is None


def test_corrupt_legacy_sdist_does_not_fall_back_to_older_json(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    legacy = tmp_path / "sdist-v2/pypi/demo/1.record"
    _write_legacy(legacy, b"nabsdist1 partial")
    _write_legacy(
        cache._sdist_json_path("demo", "1"), b'{"pkg_info":"older", "pyproject":null}'
    )

    assert cache.get_sdist_files("demo", "1") is None
    assert not cache._sdist_path("demo", "1").exists()


def test_truncated_metadata_is_refetched_before_use(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_metadata("demo", URL, TEXT)
    path = cache._metadata_path("demo", URL)
    path.write_bytes(path.read_bytes()[: -len("Requires-Dist: second\n\n")])

    async def read() -> str:
        async with CachedAsyncSimpleClient(
            HttpxAsyncTransport(), cache, INDEX
        ) as client:
            return await client.get_metadata_text(
                "demo", "1", URL, ("sha256", hashlib.sha256(TEXT.encode()).hexdigest())
            )

    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(
            return_value=httpx.Response(200, content=TEXT.encode())
        )
        assert asyncio.run(read()) == TEXT
        assert route.call_count == 1
    assert cache.get_metadata("demo", URL) == TEXT
