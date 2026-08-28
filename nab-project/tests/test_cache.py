"""Tests for nab_index.cache."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from nab_index.atomic import atomic_write, atomic_write_text
from nab_index.cache import (
    ARCHIVE_BUCKET,
    CACHE_VERSION_METADATA,
    CACHE_VERSION_SDIST,
    CACHE_VERSION_SIMPLE,
    CACHE_VERSION_SIMPLE_NEG,
    CACHE_VERSION_SIMPLE_PARSED,
    SOURCE_BUCKETS,
    VCS_BUCKET,
    CachePolicy,
    NullCache,
    OfflineError,
    OnDiskCache,
    _add_owner_mode,
    _atomic_write,
    _encode_policy,
    _index_dirname,
    _require_single_segment,
)
from nab_index.vcs import VcsRequest, prepare_clone
from nab_provider.serialization import SimpleSerialization

# Derived so a bucket-version bump does not need every path updated.
SIMPLE_BUCKET = f"simple-{CACHE_VERSION_SIMPLE}"
NEG_BUCKET = f"simple-neg-{CACHE_VERSION_SIMPLE_NEG}"
METADATA_BUCKET = f"metadata-{CACHE_VERSION_METADATA}"
SDIST_BUCKET = f"sdist-{CACHE_VERSION_SDIST}"
PARSED_BUCKET = f"simple-parsed-{CACHE_VERSION_SIMPLE_PARSED}"

# Stands in for a body nested past the decoder's guard (``refuse_over_nested``).
OVER_NESTED = b"[[[]]]"

# Two wheels of one version, each with its own PEP 658 sidecar.
METADATA_URLS = (
    "https://f.example/foo-1.0-cp311-manylinux_2_17_x86_64.whl.metadata",
    "https://f.example/foo-1.0-cp311-win_amd64.whl.metadata",
)

_FRESH = CachePolicy(fetched_at=0, max_age=600, etag=None)


def _symlink_or_skip(
    link: Path, target: Path, *, target_is_directory: bool = False
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")


def _populate(root: Path) -> OnDiskCache:
    """Write one valid entry of each kind under ``root`` and return the cache."""
    cache = OnDiskCache(root, "https://pypi.org/simple")
    cache.put_simple("foo", b'{"files": []}', _FRESH)
    cache.put_negative("bar", _FRESH)
    cache.put_metadata("foo", "https://example.com/foo.whl", "Name: foo\n")
    cache.put_sdist_files("foo", "1.0", "Name: foo\n", None)
    return cache


class TestCachePolicy:
    def test_is_fresh_within_window(self) -> None:
        policy = CachePolicy(fetched_at=1000, max_age=600, etag=None)
        assert policy.is_fresh(now=1100) is True

    def test_is_fresh_at_boundary(self) -> None:
        policy = CachePolicy(fetched_at=1000, max_age=600, etag=None)
        assert policy.is_fresh(now=1600) is False

    def test_is_fresh_past_window(self) -> None:
        policy = CachePolicy(fetched_at=1000, max_age=600, etag="x")
        assert policy.is_fresh(now=2000) is False

    def test_is_fresh_uses_real_clock_when_now_omitted(self) -> None:
        with patch("nab_index.cache.time.time", return_value=1500.0):
            policy = CachePolicy(fetched_at=1000, max_age=600, etag=None)
            assert policy.is_fresh() is True


class TestIndexDirname:
    def test_default_pypi_https(self) -> None:
        assert _index_dirname("https://pypi.org/simple/") == "pypi"

    def test_default_pypi_http(self) -> None:
        assert _index_dirname("http://pypi.org/simple/") == "pypi"

    def test_alt_index_hashed(self) -> None:
        name = _index_dirname("https://example.com/simple/")
        assert name != "pypi"
        assert len(name) == 16
        assert all(c in "0123456789abcdef" for c in name)

    def test_alt_index_stable(self) -> None:
        a = _index_dirname("https://example.com/simple/")
        b = _index_dirname("https://example.com/simple/")
        assert a == b


class TestOfflineError:
    def test_is_exception(self) -> None:
        with pytest.raises(OfflineError, match="boom"):
            raise OfflineError("boom")


class TestAtomicWrite:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.txt"
        _atomic_write(target, b"hello")
        assert target.read_bytes() == b"hello"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "x.txt"
        target.write_bytes(b"old")
        _atomic_write(target, b"new")
        assert target.read_bytes() == b"new"

    def test_no_temp_left_after_success(self, tmp_path: Path) -> None:
        target = tmp_path / "x.txt"
        _atomic_write(target, b"data")
        leftovers = [p for p in tmp_path.iterdir() if p.name != "x.txt"]
        assert leftovers == []

    def test_cleans_up_temp_on_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "x.txt"

        def fail_replace(_src: str, _dst: str) -> None:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr("nab_index.atomic.os.replace", fail_replace)
        with pytest.raises(OSError, match="boom"):
            _atomic_write(target, b"data")
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_swallows_unlink_error_during_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "x.txt"
        monkeypatch.setattr(
            "nab_index.atomic.os.replace",
            lambda _s, _d: (_ for _ in ()).throw(RuntimeError("rep")),
        )

        def fail_unlink(_path: str) -> None:
            msg = "unlink-fail"
            raise OSError(msg)

        monkeypatch.setattr("nab_index.atomic.os.unlink", fail_unlink)
        with pytest.raises(RuntimeError, match="rep"):
            _atomic_write(target, b"data")

    def test_new_file_gets_the_mode_open_would_have_given_it(
        self, tmp_path: Path
    ) -> None:
        reference = tmp_path / "reference.txt"
        reference.write_bytes(b"x")
        target = tmp_path / "x.txt"
        _atomic_write(target, b"data")
        assert target.stat().st_mode == reference.stat().st_mode

    def test_mode_of_the_replaced_file_is_kept(self, tmp_path: Path) -> None:
        target = tmp_path / "x.txt"
        target.write_bytes(b"old")
        target.chmod(0o640)
        mode = target.stat().st_mode
        _atomic_write(target, b"new")
        assert target.stat().st_mode == mode

    def test_content_is_synced_before_the_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd: int) -> None:
            calls.append("fsync")
            real_fsync(fd)

        def spy_replace(src: Path, dst: Path) -> None:
            calls.append("replace")
            real_replace(src, dst)

        monkeypatch.setattr("nab_index.atomic.os.fsync", spy_fsync)
        monkeypatch.setattr("nab_index.atomic.os.replace", spy_replace)
        atomic_write_text(tmp_path / "x.txt", "data")
        assert calls == ["fsync", "replace"]


class TestRequireSingleSegment:
    def test_single_segment_component_is_returned(self) -> None:
        assert _require_single_segment("foo-1.0") == "foo-1.0"

    @pytest.mark.parametrize("component", ["", ".", "..", "foo/", "foo/bar"])
    def test_component_that_is_not_one_segment_is_rejected(
        self, component: str
    ) -> None:
        with pytest.raises(ValueError, match="not a single path segment"):
            _require_single_segment(component)

    @pytest.mark.parametrize("component", ["foo\\bar", "C:", "C:x"])
    def test_windows_path_syntax_follows_the_running_platform(
        self, component: str
    ) -> None:
        """Windows reads a backslash as a separator and ``C:`` as a drive prefix.

        POSIX reads both as ordinary filename characters.
        """
        if os.sep == "\\":
            with pytest.raises(ValueError, match="not a single path segment"):
                _require_single_segment(component)
        else:
            assert _require_single_segment(component) == component


class TestOnDiskCache:
    def _make(self, tmp_path: Path) -> OnDiskCache:
        return OnDiskCache(tmp_path, "https://pypi.org/simple/")

    def test_simple_round_trip(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        body = b'{"files": []}'
        policy = CachePolicy(fetched_at=1000, max_age=600, etag="abc")
        cache.put_simple("foo", body, policy)
        result = cache.get_simple("foo")
        assert result is not None
        got_body, got_policy = result
        assert got_body == body
        # put_simple stamps the body digest into the stored policy.
        assert got_policy == replace(
            policy, body_digest=hashlib.sha256(body).hexdigest()
        )

    def test_simple_layout_uses_pypi_dirname(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_simple(
            "foo",
            b"{}",
            CachePolicy(fetched_at=1, max_age=1, etag=None),
        )
        assert (tmp_path / SIMPLE_BUCKET / "pypi" / "foo.json").exists()
        assert (tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy").exists()

    def test_simple_alt_index_uses_hash_dirname(self, tmp_path: Path) -> None:
        cache = OnDiskCache(tmp_path, "https://alt.example/simple/")
        cache.put_simple(
            "foo",
            b"{}",
            CachePolicy(fetched_at=1, max_age=1, etag=None),
        )
        sub = tmp_path / SIMPLE_BUCKET
        children = list(sub.iterdir())
        assert len(children) == 1
        assert children[0].name != "pypi"

    def test_simple_miss_when_neither_file(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        assert cache.get_simple("none") is None

    def test_simple_miss_when_only_body(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        body_path, _ = cache._simple_paths("foo")
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(b"{}")
        assert cache.get_simple("foo") is None

    def test_simple_miss_when_only_policy(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        _, policy_path = cache._simple_paths("foo")
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_bytes(b'{"fetched_at":1,"max_age":1,"etag":null}')
        assert cache.get_simple("foo") is None

    def test_simple_miss_on_corrupt_policy_json(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        body_path, policy_path = cache._simple_paths("foo")
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(b"{}")
        policy_path.write_bytes(b"not-json")
        assert cache.get_simple("foo") is None

    def test_simple_miss_on_policy_missing_keys(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        body_path, policy_path = cache._simple_paths("foo")
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(b"{}")
        policy_path.write_bytes(b'{"fetched_at":1}')
        assert cache.get_simple("foo") is None

    def test_simple_miss_on_policy_wrong_types(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        body_path, policy_path = cache._simple_paths("foo")
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(b"{}")
        policy_path.write_bytes(b'{"fetched_at":"x","max_age":"y","etag":null}')
        assert cache.get_simple("foo") is None

    def test_refresh_policy_keeps_body(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        old = CachePolicy(fetched_at=1000, max_age=600, etag="old")
        cache.put_simple("foo", b"BODY", old)
        new = CachePolicy(fetched_at=2000, max_age=600, etag="new")
        cache.refresh_simple_policy("foo", new)
        result = cache.get_simple("foo")
        assert result is not None
        body, got = result
        assert body == b"BODY"
        assert got == new

    def test_metadata_round_trip(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_metadata(
            "foo", METADATA_URLS[0], "Metadata-Version: 2.1\nName: foo\n"
        )
        assert cache.get_metadata("foo", METADATA_URLS[0]) == (
            "Metadata-Version: 2.1\nName: foo\n"
        )

    def test_metadata_miss(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        assert cache.get_metadata("foo", METADATA_URLS[0]) is None

    def test_metadata_per_artifact(self, tmp_path: Path) -> None:
        """Each wheel of a version gets its own entry, keyed by sidecar URL."""
        cache = self._make(tmp_path)
        linux_url, win_url = METADATA_URLS
        cache.put_metadata("foo", linux_url, "linux")
        cache.put_metadata("foo", win_url, "win")
        assert cache.get_metadata("foo", linux_url) == "linux"
        assert cache.get_metadata("foo", win_url) == "win"
        assert len(list((tmp_path / "metadata-v1" / "pypi" / "foo").iterdir())) == 2

    def test_sdist_round_trip(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_sdist_files("foo", "1.0", "Name: foo\n", "[project]\n")
        assert cache.get_sdist_files("foo", "1.0") == ("Name: foo\n", "[project]\n")

    def test_sdist_miss(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        assert cache.get_sdist_files("foo", "1.0") is None

    def test_sdist_no_pyproject_is_a_hit_not_a_miss(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_sdist_files("foo", "1.0", "Name: foo\n", None)
        assert cache.get_sdist_files("foo", "1.0") == ("Name: foo\n", None)

    def test_sdist_no_pkg_info_is_a_hit_not_a_miss(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_sdist_files("foo", "1.0", None, "[project]\n")
        assert cache.get_sdist_files("foo", "1.0") == (None, "[project]\n")

    def test_sdist_empty_field_is_not_an_absent_one(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_sdist_files("foo", "1.0", "", "")
        assert cache.get_sdist_files("foo", "1.0") == ("", "")

    def test_sdist_lone_surrogate_round_trips(self, tmp_path: Path) -> None:
        """A text field holds any ``str``, including one with no UTF-8 form."""
        cache = self._make(tmp_path)
        cache.put_sdist_files("foo", "1.0", "Name: \ud800\n", None)
        assert cache.get_sdist_files("foo", "1.0") == ("Name: \ud800\n", None)

    def test_sdist_json_record_is_carried_over(self, tmp_path: Path) -> None:
        """A record the retired bucket holds is a hit, and is rewritten here."""
        cache = self._make(tmp_path)
        legacy = cache._sdist_json_path("foo", "1.0")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            json.dumps({"pkg_info": "Name: foo\n", "pyproject": None}),
            encoding="utf-8",
        )

        assert cache.get_sdist_files("foo", "1.0") == ("Name: foo\n", None)

        assert cache._sdist_path("foo", "1.0").exists()
        assert legacy.exists()

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(b"nabsdist1 10 -1", id="no-header-end"),
            pytest.param(b"nabsdist1 x -1\n", id="length-not-a-number"),
            pytest.param(b"nabsdist1 1\n", id="one-length"),
            pytest.param(b"nabsdist1 -2 -1\nA", id="length-below-absent"),
            pytest.param(b"nabsdist1 1 -1\nAB", id="body-longer-than-declared"),
            pytest.param(b"nabsdist1 1 -1\n\xff", id="field-not-utf-8"),
        ],
    )
    def test_sdist_malformed_record_is_a_miss(self, tmp_path: Path, raw: bytes) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        assert cache.get_sdist_files("foo", "1.0") is None

    def test_sdist_record_without_magic_is_a_miss(self, tmp_path: Path) -> None:
        """Bytes that would parse as a record, but do not carry the magic."""
        cache = self._make(tmp_path)
        path = cache._sdist_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"otherfmt1 5 -1\nhello")
        assert cache.get_sdist_files("foo", "1.0") is None

    def test_sdist_json_record_not_json_is_a_miss(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_json_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")
        assert cache.get_sdist_files("foo", "1.0") is None

    @pytest.mark.parametrize(
        "record",
        [
            {"pkg_info": 5, "pyproject": None},
            {"pkg_info": [], "pyproject": None},
            {"pkg_info": "Name: foo\n", "pyproject": 5},
        ],
    )
    def test_sdist_json_record_non_text_field_is_a_miss(
        self, tmp_path: Path, record: dict[str, object]
    ) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_json_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        assert cache.get_sdist_files("foo", "1.0") is None

    def test_sdist_json_record_not_an_object_is_a_miss(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_json_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["Name: foo\\n", null]', encoding="utf-8")
        assert cache.get_sdist_files("foo", "1.0") is None

    def test_put_metadata_rejects_multi_segment_package(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        with pytest.raises(ValueError, match="not a single path segment"):
            cache.put_metadata("foo/../../elsewhere", METADATA_URLS[0], "text")
        assert list(tmp_path.rglob("*.metadata")) == []

    def test_put_sdist_rejects_multi_segment_version(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        with pytest.raises(ValueError, match="not a single path segment"):
            cache.put_sdist_files("foo", "1.0/../../elsewhere", "Name: foo\n", None)
        assert list(tmp_path.rglob("*.record")) == []

    def test_metadata_url_cannot_escape_the_package_dir(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_metadata("foo", "https://f.example/../../../etc/passwd.metadata", "T")
        written = list(tmp_path.rglob("*.metadata"))
        assert [p.parent for p in written] == [
            tmp_path / "metadata-v1" / "pypi" / "foo"
        ]

    def test_negative_round_trip(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        policy = CachePolicy(fetched_at=1000, max_age=600, etag=None)
        cache.put_negative("foo", policy)
        assert cache.get_negative("foo") == policy

    def test_negative_layout_uses_neg_bucket(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_negative("foo", CachePolicy(fetched_at=1, max_age=1, etag=None))
        assert (tmp_path / "simple-neg-v0" / "pypi" / "foo.neg").exists()

    def test_negative_miss_when_absent(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        assert cache.get_negative("none") is None

    def test_negative_drop_then_get_is_none(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.put_negative("foo", CachePolicy(fetched_at=1, max_age=1, etag=None))
        cache.drop_negative("foo")
        assert cache.get_negative("foo") is None

    def test_negative_drop_missing_is_noop(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        cache.drop_negative("foo")
        assert cache.get_negative("foo") is None

    def test_negative_miss_on_corrupt_neg(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        path = cache._neg_path("foo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-json")
        assert cache.get_negative("foo") is None

    def test_put_negative_rejects_multi_segment_package(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        with pytest.raises(ValueError, match="not a single path segment"):
            cache.put_negative(
                "foo/../../elsewhere",
                CachePolicy(fetched_at=1, max_age=1, etag=None),
            )
        assert list(tmp_path.rglob("*.neg")) == []


class TestUnwritableRoot:
    """A root the process cannot write to degrades to no caching."""

    def _cache(self, tmp_path: Path) -> OnDiskCache:
        """Return a cache whose root is a regular file, so every store fails."""
        root = tmp_path / "cache"
        root.write_bytes(b"not a directory")
        return OnDiskCache(root, "https://pypi.org/simple/")

    def test_put_simple_does_not_raise(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_simple("foo", b"{}", CachePolicy(fetched_at=1, max_age=1, etag=None))
        assert cache.get_simple("foo") is None

    def test_put_metadata_does_not_raise(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_metadata("foo", METADATA_URLS[0], "Name: foo\n")
        assert cache.get_metadata("foo", METADATA_URLS[0]) is None

    def test_put_sdist_files_does_not_raise(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_sdist_files("foo", "1.0", "Name: foo\n", None)
        assert cache.get_sdist_files("foo", "1.0") is None

    def test_refresh_simple_policy_does_not_raise(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.refresh_simple_policy(
            "foo", CachePolicy(fetched_at=2, max_age=1, etag=None)
        )
        assert cache.get_simple("foo") is None

    def test_permission_denied_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def denied(_path: Path, _data: bytes) -> None:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("nab_index.cache.atomic_write", denied)
        cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")
        cache.put_metadata("foo", METADATA_URLS[0], "Name: foo\n")
        assert cache.get_metadata("foo", METADATA_URLS[0]) is None

    def test_warns_once_per_cache(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._cache(tmp_path)
        policy = CachePolicy(fetched_at=1, max_age=1, etag=None)
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            cache.put_simple("foo", b"{}", policy)
            cache.put_metadata("foo", METADATA_URLS[0], "Name: foo\n")
            cache.put_sdist_files("foo", "1.0", "Name: foo\n", None)
        assert len(caplog.records) == 1
        assert str(tmp_path / "cache") in caplog.records[0].getMessage()

    def test_writable_root_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            cache.put_simple(
                "foo", b"{}", CachePolicy(fetched_at=1, max_age=1, etag=None)
            )
        assert caplog.records == []
        assert cache.get_simple("foo") is not None

    def test_dropped_body_write_keeps_the_old_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body lost to ENOSPC must not be stamped with the new policy.

        The small sidecar can land when the much larger body does not,
        which would leave the old listing looking fresh under the new
        body's ETag.
        """
        cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")
        old_policy = CachePolicy(fetched_at=0, max_age=600, etag="E1")
        old_digest = cache.put_simple("foo", b"OLD-LISTING", old_policy)

        def fail_body(path: Path, data: bytes) -> None:
            if path.suffix == ".json":
                raise OSError(28, "No space left on device")
            atomic_write(path, data)

        monkeypatch.setattr("nab_index.cache.atomic_write", fail_body)
        cache.put_simple(
            "foo",
            b"NEW-LISTING",
            CachePolicy(fetched_at=int(time.time()), max_age=600, etag="E2"),
        )

        stored = replace(old_policy, body_digest=old_digest)
        assert cache.get_simple("foo") == (b"OLD-LISTING", stored)

    def test_dropped_policy_write_hands_back_no_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body that lands without its sidecar binds no parsed blob.

        The caller keys its parsed blob on the returned digest, so a stored
        body whose policy never landed has to come back as no digest at all.
        """
        cache = OnDiskCache(tmp_path, "https://pypi.org/simple/")

        def fail_policy(path: Path, data: bytes) -> None:
            if path.suffix == ".policy":
                raise OSError(28, "No space left on device")
            atomic_write(path, data)

        monkeypatch.setattr("nab_index.cache.atomic_write", fail_policy)
        digest = cache.put_simple(
            "foo",
            b"LISTING",
            CachePolicy(fetched_at=int(time.time()), max_age=600, etag="E1"),
        )

        assert digest is None
        assert cache.get_simple("foo") is None

    def test_bad_key_still_raises(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        with pytest.raises(ValueError, match="not a single path segment"):
            cache.put_metadata("foo/../elsewhere", METADATA_URLS[0], "text")


class TestNullCache:
    def test_get_returns_none_and_put_is_noop(self) -> None:
        cache = NullCache()
        assert cache.get_simple("foo") is None
        assert cache.get_metadata("foo", METADATA_URLS[0]) is None
        assert cache.get_sdist_files("foo", "1.0") is None
        # Puts store nothing, so put_simple hands back no digest and the caller
        # builds no parsed blob for a body this backend does not hold.
        policy = CachePolicy(fetched_at=0, max_age=0, etag=None)
        assert cache.put_simple("foo", b"", policy) is None
        assert cache.refresh_simple_policy("foo", policy) is None
        assert cache.put_metadata("foo", METADATA_URLS[0], "x") is None
        assert cache.put_sdist_files("foo", "1.0", "x", None) is None
        # And subsequent gets still miss.
        assert cache.get_simple("foo") is None

    def test_negative_get_none_and_put_drop_noop(self) -> None:
        cache = NullCache()
        assert cache.get_negative("foo") is None
        policy = CachePolicy(fetched_at=0, max_age=0, etag=None)
        assert cache.put_negative("foo", policy) is None
        assert cache.drop_negative("foo") is None
        assert cache.get_negative("foo") is None


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


class TestCorruptEntryLogging:
    """A present-but-unparseable entry is a miss named in one WARNING line.

    An absent file is a silent miss.
    """

    def _make(self, tmp_path: Path) -> OnDiskCache:
        return OnDiskCache(tmp_path, "https://pypi.org/simple/")

    def test_corrupt_policy_logs_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        body_path, policy_path = cache._simple_paths("foo")
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(b"{}")
        policy_path.write_bytes(b"not-json")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_simple("foo") is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(policy_path) in warnings[0].getMessage()

    def test_corrupt_neg_logs_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        path = cache._neg_path("foo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-json")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_negative("foo") is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()

    def test_corrupt_sdist_record_logs_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a record")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_sdist_files("foo", "1.0") is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()

    def test_non_text_sdist_json_field_logs_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_json_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"pkg_info": 5, "pyproject": null}', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_sdist_files("foo", "1.0") is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()

    def test_over_nested_policy_logs_one_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = self._make(tmp_path)
        body_path, policy_path = cache._simple_paths("foo")
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(b"{}")
        policy_path.write_bytes(OVER_NESTED)
        with (
            refuse_over_nested(OVER_NESTED),
            caplog.at_level(logging.WARNING, logger="nab_index.cache"),
        ):
            assert cache.get_simple("foo") is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(policy_path) in warnings[0].getMessage()

    def test_over_nested_sdist_json_record_logs_one_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_json_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(OVER_NESTED)
        with (
            refuse_over_nested(OVER_NESTED),
            caplog.at_level(logging.WARNING, logger="nab_index.cache"),
        ):
            assert cache.get_sdist_files("foo", "1.0") is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()

    def test_non_utf8_sdist_record_is_logged_miss(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"nabsdist1 1 -1\n\xff")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_sdist_files("foo", "1.0") is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()

    def test_non_utf8_metadata_is_logged_miss(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        path = cache._metadata_path("foo", METADATA_URLS[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not utf-8")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_metadata("foo", METADATA_URLS[0]) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()

    def test_absent_policy_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_simple("none") is None
        assert _warnings(caplog) == []

    def test_absent_neg_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_negative("none") is None
        assert _warnings(caplog) == []

    def test_absent_sdist_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_sdist_files("foo", "1.0") is None
        assert _warnings(caplog) == []

    def test_absent_metadata_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = self._make(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
            assert cache.get_metadata("foo", METADATA_URLS[0]) is None
        assert _warnings(caplog) == []


class TestEncodePolicy:
    def test_round_trip_with_etag(self) -> None:
        policy = CachePolicy(
            fetched_at=10, max_age=20, etag="x", page_url="https://e.test/simple/foo/"
        )
        decoded = json.loads(_encode_policy(policy))
        assert decoded == {
            "fetched_at": 10,
            "max_age": 20,
            "etag": "x",
            "page_url": "https://e.test/simple/foo/",
        }

    def test_round_trip_without_etag(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag=None)
        decoded = json.loads(_encode_policy(policy))
        assert decoded == {
            "fetched_at": 10,
            "max_age": 20,
            "etag": None,
            "page_url": None,
        }

    def test_policy_without_a_page_url_decodes(self, tmp_path: Path) -> None:
        cache = OnDiskCache(tmp_path, "https://pypi.org/simple")
        cache.put_simple("foo", b"{}", _FRESH)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.write_bytes(b'{"fetched_at":1,"max_age":600,"etag":"x"}')

        entry = cache.get_simple("foo")

        assert entry is not None
        assert entry[1].page_url is None

    @pytest.mark.parametrize("page_url", [123, "", []])
    def test_unusable_page_url_is_dropped(
        self, tmp_path: Path, page_url: object
    ) -> None:
        cache = OnDiskCache(tmp_path, "https://pypi.org/simple")
        cache.put_simple("foo", b"{}", _FRESH)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.write_bytes(
            json.dumps(
                {"fetched_at": 1, "max_age": 600, "etag": "x", "page_url": page_url}
            ).encode()
        )

        entry = cache.get_simple("foo")

        assert entry is not None
        assert entry[1].page_url is None

    def test_non_ascii_etag_is_not_stored(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag='"é"')
        assert json.loads(_encode_policy(policy))["etag"] is None

    @pytest.mark.parametrize("etag", ['"é"', '"abc\r\n def"', 123, []])
    def test_unusable_etag_is_dropped(self, tmp_path: Path, etag: object) -> None:
        cache = OnDiskCache(tmp_path, "https://pypi.org/simple")
        cache.put_simple("foo", b"{}", _FRESH)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.write_bytes(
            json.dumps({"fetched_at": 1, "max_age": 600, "etag": etag}).encode()
        )

        entry = cache.get_simple("foo")

        assert entry is not None
        assert entry[1].etag is None


class TestReadCacheEntry:
    def _cache(self, tmp_path: Path) -> OnDiskCache:
        return OnDiskCache(tmp_path, "https://pypi.org/simple")

    def test_corrupt_policy(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not json")
        assert cache.read_cache_entry(path) is not None

    @pytest.mark.parametrize(
        "raw",
        [
            b'{"fetched_at": Infinity, "max_age": 600, "etag": null}',
            b'{"fetched_at": -Infinity, "max_age": 600, "etag": null}',
            b'{"fetched_at": 1, "max_age": 1e400, "etag": null}',
        ],
    )
    def test_out_of_range_policy_number(self, tmp_path: Path, raw: bytes) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        assert cache.read_cache_entry(path) == "policy not decodable"

    def test_valid_policy(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_simple("foo", b"{}", _FRESH)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        assert cache.read_cache_entry(path) is None

    def test_corrupt_negative(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / "simple-neg-v0" / "pypi" / "bar.neg"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"nope")
        assert cache.read_cache_entry(path) is not None

    def test_non_utf8_metadata(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / "metadata-v1" / "pypi" / "foo" / "abc.metadata"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe")
        assert cache.read_cache_entry(path) == "not valid UTF-8"

    def test_valid_metadata(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_metadata("foo", "https://example.com/f.whl", "ok")
        path = next((tmp_path / "metadata-v1").rglob("*.metadata"))
        assert cache.read_cache_entry(path) is None

    def test_invalid_simple_json(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"{not json")
        assert cache.read_cache_entry(path) == "not valid JSON"

    def test_over_nested_simple_json(
        self,
        tmp_path: Path,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(OVER_NESTED)
        with refuse_over_nested(OVER_NESTED):
            assert cache.read_cache_entry(path) == "nested too deeply to decode"

    def test_over_nested_policy(
        self,
        tmp_path: Path,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.parent.mkdir(parents=True)
        path.write_bytes(OVER_NESTED)
        with refuse_over_nested(OVER_NESTED):
            assert cache.read_cache_entry(path) == "policy not decodable"

    def test_over_nested_parsed_blob(
        self,
        tmp_path: Path,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / PARSED_BUCKET / "pypi" / "foo.parsed"
        path.parent.mkdir(parents=True)
        path.write_bytes(OVER_NESTED)
        with refuse_over_nested(OVER_NESTED):
            assert cache.read_cache_entry(path) == "nested too deeply to decode"

    def test_valid_simple_json_is_clean(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_simple("foo", b'{"files": []}', _FRESH)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.json"
        assert cache.read_cache_entry(path) is None

    def test_sdist_json_record_missing_fields(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = cache._sdist_json_path("foo", "1.0")
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"pkg_info": "x"}')
        assert cache.read_cache_entry(path) is not None

    def test_valid_sdist_record(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_sdist_files("foo", "1.0", "info", None)
        path = tmp_path / SDIST_BUCKET / "pypi" / "foo" / "1.0.record"
        assert cache.read_cache_entry(path) is None

    def test_undecodable_sdist_record(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SDIST_BUCKET / "pypi" / "foo" / "1.0.record"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"nabsdist1 4 -1\nab")
        assert cache.read_cache_entry(path) == "sdist record not decodable"

    def test_unknown_suffix_is_clean(self, tmp_path: Path) -> None:
        # A leftover atomic-write temp file is not a nab entry and is ignored.
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.json.abc.tmp"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"partial")
        assert cache.read_cache_entry(path) is None

    def test_unreadable_entry(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "adir.policy"
        path.mkdir(parents=True)
        assert cache.read_cache_entry(path) is not None

    def test_root_itself_has_no_bucket(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        assert cache._bucket_of(tmp_path) == ""


class TestIterCacheEntries:
    def test_yields_files_and_skips_symlinks(self, tmp_path: Path) -> None:
        cache = _populate(tmp_path)
        outside = tmp_path.parent / "iter-out"
        outside.mkdir(exist_ok=True)
        (outside / "x.json").write_text("{}")
        _symlink_or_skip(
            tmp_path / "linked-simple-v0", outside, target_is_directory=True
        )
        (tmp_path / "sdist-v1-file").write_text("junk")
        _symlink_or_skip(
            tmp_path / SIMPLE_BUCKET / "pypi" / "link.json", outside / "x.json"
        )
        names = {e.name for e in cache.iter_cache_entries()}
        assert "foo.json" in names
        assert "foo.policy" in names
        assert "bar.neg" in names
        assert "x.json" not in names
        assert "link.json" not in names

    def test_skips_recognized_symlinked_bucket(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.json").write_text("{}")
        root = tmp_path / "cache"
        root.mkdir()
        _symlink_or_skip(root / SIMPLE_BUCKET, outside, target_is_directory=True)
        cache = OnDiskCache(root, "https://pypi.org/simple")
        assert list(cache.iter_cache_entries()) == []

    def test_empty_root_yields_nothing(self, tmp_path: Path) -> None:
        cache = OnDiskCache(tmp_path / "gone", "https://pypi.org/simple")
        assert list(cache.iter_cache_entries()) == []


class TestClearCache:
    def test_removes_dir_buckets_and_returns_names(self, tmp_path: Path) -> None:
        cache = _populate(tmp_path)
        removed = cache.clear_cache()
        assert set(removed) == {
            SIMPLE_BUCKET,
            "simple-neg-v0",
            "metadata-v1",
            SDIST_BUCKET,
        }
        assert not (tmp_path / SIMPLE_BUCKET).exists()
        assert not (tmp_path / SDIST_BUCKET).exists()

    def test_unlinks_symlinked_bucket_without_following(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")
        root = tmp_path / "cache"
        root.mkdir()
        _symlink_or_skip(root / SIMPLE_BUCKET, outside, target_is_directory=True)
        cache = OnDiskCache(root, "https://pypi.org/simple")
        assert cache.clear_cache() == [SIMPLE_BUCKET]
        assert (outside / "keep.txt").read_text() == "precious"
        assert not (root / SIMPLE_BUCKET).exists()

    def test_leaves_bucket_named_file(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        root.mkdir()
        foreign = root / "metadata-notes.txt"
        foreign.write_text("mine")
        cache = OnDiskCache(root, "https://pypi.org/simple")
        assert cache.clear_cache() == []
        assert foreign.read_text() == "mine"


class TestSourceBuckets:
    """The clone and archive trees a resolve leaves under the cache root."""

    @staticmethod
    def _clone_into(cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Return the tree ``prepare_clone`` writes under ``cache_root``."""

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            if cmd[:2] == ["git", "checkout"]:
                (cwd / "pyproject.toml").write_text("[project]\n")
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        request = VcsRequest("git", "https://example/repo.git", "a" * 40, "")
        return prepare_clone(cache_root, request, require_pin=True).path

    @staticmethod
    def _extract_into(cache_root: Path) -> Path:
        tree = cache_root / ("b" * 64)
        tree.mkdir(parents=True)
        (tree / "pyproject.toml").write_text("[project]\n")
        return tree

    def test_clear_removes_the_legacy_clone_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cache"
        cache = _populate(root)
        clone = self._clone_into(root / "vcs", monkeypatch)
        assert (clone / "pyproject.toml").is_file()
        assert "vcs" in cache.clear_cache()
        assert not clone.exists()
        assert not (root / "vcs").exists()

    def test_clear_removes_the_legacy_archive_tree(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        cache = _populate(root)
        tree = self._extract_into(root / "archive")
        assert "archive" in cache.clear_cache()
        assert not tree.exists()
        assert not (root / "archive").exists()

    def test_clear_removes_current_source_buckets(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        cache = OnDiskCache(root, "https://pypi.org/simple")
        (root / VCS_BUCKET).mkdir(parents=True)
        (root / ARCHIVE_BUCKET).mkdir()

        assert set(cache.clear_cache()) == {VCS_BUCKET, ARCHIVE_BUCKET}
        assert not (root / VCS_BUCKET).exists()
        assert not (root / ARCHIVE_BUCKET).exists()

    def test_clear_removes_a_read_only_clone(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        cache = _populate(root)
        tree = root / VCS_BUCKET / "vcs" / ("0" * 16) / ("a" * 40)
        tree.mkdir(parents=True)
        packfile = tree / "pack-0.pack"
        packfile.write_bytes(b"PACK")

        outside = tmp_path / "outside.txt"
        outside.write_text("upstream")
        outside.chmod(0o444)
        _symlink_or_skip(tree / "link", outside)

        # A read-only file stops rmtree on Windows, a read-only directory on POSIX.
        packfile.chmod(0o444)
        tree.chmod(0o500)

        assert VCS_BUCKET in cache.clear_cache()
        assert not (root / VCS_BUCKET).exists()
        assert not outside.stat().st_mode & stat.S_IWUSR

    def test_verify_does_not_parse_source_trees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cache"
        cache = _populate(root)
        clone = self._clone_into(root / "vcs", monkeypatch)
        (clone / "fixture.json").write_text("not json")
        (self._extract_into(root / "archive") / "data.json").write_text("not json")
        names = {entry.name for entry in cache.iter_cache_entries()}
        assert "fixture.json" not in names
        assert "data.json" not in names
        assert "foo.json" in names


class TestCacheReferenceLayout:
    """The cache reference's Layout tables name every bucket the root holds."""

    def _documented_buckets(self) -> set[str]:
        """Return the bucket names the Layout section's tables list."""
        doc = Path(__file__).resolve().parents[2] / "docs" / "reference" / "cache.md"
        text = doc.read_text(encoding="utf-8")

        start = text.index("## Layout")
        end = text.index("\n## ", start + 1)
        section = text[start:end]

        return set(re.findall(r"^\| `([^`]+)/` \|", section, flags=re.MULTILINE))

    def _record_buckets(self, cache: OnDiskCache) -> set[str]:
        """Return the root-level directories ``cache`` writes its records under.

        Read off the cache's own paths rather than a populated root, so a
        bucket added to ``OnDiskCache`` fails this test until the reference
        names it too.
        """
        root = cache._root
        return {
            path.relative_to(root).parts[0]
            for path in vars(cache).values()
            if isinstance(path, Path) and path != root
        }

    def test_tables_name_every_bucket(self, tmp_path: Path) -> None:
        cache = OnDiskCache(tmp_path / "cache", "https://pypi.org/simple")

        expected = self._record_buckets(cache) | set(SOURCE_BUCKETS)
        assert self._documented_buckets() == expected


class TestAddOwnerMode:
    """``_add_owner_mode`` grants the owner bit but never through a symlink."""

    def test_grants_the_bit_on_a_real_file(self, tmp_path: Path) -> None:
        target = tmp_path / "packfile"
        target.write_bytes(b"PACK")
        target.chmod(0o444)
        _add_owner_mode(target, stat.S_IWUSR)
        assert target.stat().st_mode & stat.S_IWUSR

    def test_leaves_a_symlink_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A symlink is skipped so no chmod lands outside the cache root.

        ``is_symlink`` is faked rather than making a real link: creating
        one needs a privilege Windows CI does not have, and the guard
        must be covered on every platform.
        """
        target = tmp_path / "looks-like-a-link"
        target.write_bytes(b"x")
        target.chmod(0o444)
        monkeypatch.setattr(Path, "is_symlink", lambda _self: True)
        _add_owner_mode(target, stat.S_IWUSR)
        assert not target.stat().st_mode & stat.S_IWUSR


class TestSerializationPartition:
    """A pinned index gets its own Simple buckets, sharing the rest."""

    _URL = "https://pypi.org/simple/"

    def test_unpinned_layout_is_unchanged(self, tmp_path: Path) -> None:
        cache = OnDiskCache(tmp_path, self._URL)
        cache.put_simple("foo", b"{}", _FRESH)
        cache.put_negative("bar", _FRESH)
        assert (tmp_path / SIMPLE_BUCKET / "pypi" / "foo.json").exists()
        assert (tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy").exists()
        assert (tmp_path / NEG_BUCKET / "pypi" / "bar.neg").exists()

    @pytest.mark.parametrize(
        ("serialization", "dirname"),
        [
            (SimpleSerialization.JSON, "pypi-json"),
            (SimpleSerialization.HTML, "pypi-html"),
        ],
    )
    def test_pin_gets_its_own_listing_directory(
        self, tmp_path: Path, serialization: SimpleSerialization, dirname: str
    ) -> None:
        cache = OnDiskCache(tmp_path, self._URL, serialization=serialization)
        cache.put_simple("foo", b"{}", _FRESH)
        cache.put_negative("bar", _FRESH)
        assert (tmp_path / SIMPLE_BUCKET / dirname / "foo.json").exists()
        assert (tmp_path / NEG_BUCKET / dirname / "bar.neg").exists()
        assert not (tmp_path / SIMPLE_BUCKET / "pypi").exists()

    def test_metadata_and_sdist_records_are_shared(self, tmp_path: Path) -> None:
        # Same bytes at the same URL either way, so a flip must not refetch.
        unpinned = OnDiskCache(tmp_path, self._URL)
        unpinned.put_metadata("foo", METADATA_URLS[0], "Name: foo\n")
        unpinned.put_sdist_files("foo", "1.0", "Name: foo\n", None)

        pinned = OnDiskCache(
            tmp_path, self._URL, serialization=SimpleSerialization.HTML
        )
        pinned.put_metadata("foo", METADATA_URLS[1], "Name: foo\n")
        assert pinned.get_metadata("foo", METADATA_URLS[0]) == "Name: foo\n"
        assert pinned.get_sdist_files("foo", "1.0") == ("Name: foo\n", None)
        assert [p.name for p in (tmp_path / METADATA_BUCKET).iterdir()] == ["pypi"]
        assert [p.name for p in (tmp_path / SDIST_BUCKET).iterdir()] == ["pypi"]


class TestNullCacheEnumeration:
    def test_helpers_are_trivial(self) -> None:
        nc = NullCache()
        assert list(nc.iter_cache_entries()) == []
        assert nc.read_cache_entry(Path("x")) is None
        assert nc.clear_cache() == []
