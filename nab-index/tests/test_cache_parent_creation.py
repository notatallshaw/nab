"""Cache writes reuse existing directories and recreate removed buckets."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from nab_index.cache import OnDiskCache

if TYPE_CHECKING:
    from pathlib import Path


INDEX = "https://pypi.org/simple/"
FIRST = "https://files.example/pkg-1-py3-none-any.whl.metadata"
SECOND = "https://files.example/pkg-1-cp312-none-any.whl.metadata"
TEXT = "Metadata-Version: 2.1\nName: pkg\nVersion: 1\n\n"


def test_existing_parent_needs_no_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_metadata("pkg", FIRST, TEXT)

    def unexpected_mkdir(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an existing cache parent must not be created again")

    monkeypatch.setattr(type(tmp_path), "mkdir", unexpected_mkdir)
    cache.put_metadata("pkg", SECOND, TEXT)

    assert cache.get_metadata("pkg", FIRST) == TEXT
    assert cache.get_metadata("pkg", SECOND) == TEXT


def test_cleared_cache_bucket_is_recreated(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_metadata("pkg", FIRST, TEXT)
    assert cache.get_metadata("pkg", FIRST) == TEXT

    assert cache.clear_cache()
    assert cache.get_metadata("pkg", FIRST) is None
    cache.put_metadata("pkg", FIRST, TEXT)

    assert cache.get_metadata("pkg", FIRST) == TEXT
    target = cache._metadata_path("pkg", FIRST)
    assert list(target.parent.iterdir()) == [target]


def test_other_write_failures_do_not_retry_or_replace_existing_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = OnDiskCache(tmp_path, INDEX)
    cache.put_metadata("pkg", FIRST, TEXT)
    calls = []

    def refused_replace(source: Path, target: Path) -> None:
        calls.append((source, target))
        raise PermissionError("replacement refused")

    monkeypatch.setattr(os, "replace", refused_replace)
    cache.put_metadata("pkg", FIRST, "different metadata")

    assert len(calls) == 1
    assert cache.get_metadata("pkg", FIRST) == TEXT
    target = cache._metadata_path("pkg", FIRST)
    assert list(target.parent.iterdir()) == [target]
