"""One body checksum supplies both record integrity and policy binding."""

from __future__ import annotations

import errno
import hashlib
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nab_index import cache as cache_module
from nab_index.cache import CachePolicy, OnDiskCache

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


_INDEX = "https://pypi.org/simple/"
_PAGE = "https://pypi.org/simple/pkg/"
_BODY = b'{"files": [], "name": "pkg"}'
_MAGIC = b"nabcache1\x00"


@pytest.mark.parametrize("body", [b"", _BODY])
def test_put_simple_hashes_each_body_once(tmp_path: Path, body: bytes) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    with patch.object(cache_module.hashlib, "sha256", wraps=hashlib.sha256) as measured:
        cache.put_simple("pkg", body, CachePolicy(123, 600, None))

    body_calls = [
        call for call in measured.call_args_list if call.args and call.args[0] is body
    ]
    assert len(body_calls) == 1


def test_record_bytes_binding_and_damage_rejection_are_preserved(
    tmp_path: Path,
) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    checksum = hashlib.sha256(_BODY).digest()
    expected_digest = checksum.hex()
    policy = CachePolicy(123, 600, None, _PAGE, body_digest="0" * 64)

    assert cache.put_simple("pkg", _BODY, policy) == expected_digest
    body_path, policy_path = cache._simple_paths("pkg")
    policy_bytes = json.dumps(
        {
            "fetched_at": 123,
            "max_age": 600,
            "etag": None,
            "page_url": _PAGE,
            "body_digest": expected_digest,
        }
    ).encode()
    assert body_path.read_bytes() == _MAGIC + checksum + _BODY
    assert (
        policy_path.read_bytes()
        == _MAGIC + hashlib.sha256(policy_bytes).digest() + policy_bytes
    )
    assert cache.get_simple("pkg") == (
        _BODY,
        CachePolicy(123, 600, None, _PAGE, body_digest=expected_digest),
    )

    damaged = _BODY.replace(b"pkg", b"bad")
    body_path.write_bytes(_MAGIC + checksum + damaged)
    assert cache.get_simple("pkg") is None


def test_failed_directory_recovery_does_not_rehash_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    parent = cache._simple_paths("pkg")[0].parent
    attempted = []

    def deny_mkdir(path: Path, *_args: object, **_kwargs: object) -> None:
        attempted.append(path)
        raise PermissionError(errno.EACCES, "directory creation refused")

    monkeypatch.setattr(cache_module.Path, "mkdir", deny_mkdir)
    with patch.object(cache_module.hashlib, "sha256", wraps=hashlib.sha256) as measured:
        assert cache.put_simple("pkg", _BODY, CachePolicy(123, 600, None)) is None

    assert attempted == [parent]
    assert measured.call_count == 1
    assert measured.call_args.args == (_BODY,)


def test_policy_failure_still_returns_no_digest_after_storing_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    body_path, policy_path = cache._simple_paths("pkg")
    body_path.parent.mkdir(parents=True)
    real_write = cache_module.atomic_write_chunks
    written = []

    def write(path: Path, chunks: Iterable[bytes], *, sync: bool = True) -> None:
        written.append(path.suffix)
        if path.suffix == ".policy":
            raise OSError(errno.ENOSPC, "policy did not fit")
        real_write(path, chunks, sync=sync)

    monkeypatch.setattr(cache_module, "atomic_write_chunks", write)
    assert cache.put_simple("pkg", _BODY, CachePolicy(123, 600, None)) is None

    assert written == [".json", ".policy"]
    assert body_path.read_bytes() == _MAGIC + hashlib.sha256(_BODY).digest() + _BODY
    assert not policy_path.exists()
