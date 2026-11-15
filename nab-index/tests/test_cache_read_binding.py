"""Read-side policy binding reuses integrity checks without masking corruption."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from nab_index.cache import (
    CachePolicy,
    OnDiskCache,
    _atomic_write_record,
    _encode_policy,
    _read_cache_record,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_expected_digest_checks_a_validated_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "entry"
    body = b"payload"
    _atomic_write_record(path, body)
    digest = hashlib.sha256(body).hexdigest()

    assert _read_cache_record(path, expected_digest=digest) == body
    with caplog.at_level(logging.WARNING):
        assert _read_cache_record(path, expected_digest="0" * 64) is None
    assert not caplog.records


def test_corruption_is_reported_before_a_policy_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "entry"
    _atomic_write_record(path, b"payload")
    path.write_bytes(path.read_bytes()[:-1])

    with caplog.at_level(logging.WARNING):
        assert _read_cache_record(path, expected_digest="0" * 64) is None
    assert len(caplog.records) == 1
    assert "checksum invalid" in caplog.records[0].message


def test_unbound_policy_still_misses(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")
    cache.put_simple("pkg", b'{"files": []}', CachePolicy(1, 600, None))
    _, policy_path = cache._simple_paths("pkg")
    _atomic_write_record(policy_path, _encode_policy(CachePolicy(1, 600, None)))

    with caplog.at_level(logging.WARNING):
        assert cache.get_simple("pkg") is None
    assert not caplog.records


def test_empty_body_keeps_its_digest_binding(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")
    digest = cache.put_simple("pkg", b"", CachePolicy(1, 600, None))

    stored = cache.get_simple("pkg")
    assert stored is not None
    assert stored[0] == b""
    assert stored[1].body_digest == digest


def test_uppercase_policy_digest_keeps_the_existing_miss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")
    digest = cache.put_simple("pkg", b'{"files": []}', CachePolicy(1, 600, None))
    assert digest is not None
    _, policy_path = cache._simple_paths("pkg")
    policy = CachePolicy(1, 600, None, body_digest=digest.upper())
    _atomic_write_record(policy_path, _encode_policy(policy))

    with caplog.at_level(logging.WARNING):
        assert cache.get_simple("pkg") is None
    assert not caplog.records


def test_bad_policy_keeps_the_existing_corrupt_body_report(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")
    cache.put_simple("pkg", b'{"files": []}', CachePolicy(1, 600, None))
    body_path, policy_path = cache._simple_paths("pkg")
    _atomic_write_record(policy_path, b"not JSON")
    body_path.write_bytes(body_path.read_bytes()[:-1])

    with caplog.at_level(logging.WARNING):
        assert cache.get_simple("pkg") is None
    assert len(caplog.records) == 2
    assert "Corrupt cache record" in caplog.records[0].message
    assert "Corrupt cache policy" in caplog.records[1].message
