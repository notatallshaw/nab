"""Tests for nab_index.cache."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nab_index.atomic import atomic_write_text
from nab_index.cache import (
    CACHE_VERSION_METADATA,
    CACHE_VERSION_SDIST,
    CACHE_VERSION_SIMPLE,
    CACHE_VERSION_SIMPLE_NEG,
    VCS_BUCKET,
    CachePolicy,
    NullCache,
    OfflineError,
    OnDiskCache,
    _add_owner_mode,
    _atomic_write,
    _encode_policy,
    _index_dirname,
)
from nab_index.serialization import SimpleSerialization
from nab_index.vcs import VcsRequest, prepare_clone

# Derived so a bucket-version bump does not need every path updated.
SIMPLE_BUCKET = f"simple-{CACHE_VERSION_SIMPLE}"
NEG_BUCKET = f"simple-neg-{CACHE_VERSION_SIMPLE_NEG}"
METADATA_BUCKET = f"metadata-{CACHE_VERSION_METADATA}"
SDIST_BUCKET = f"sdist-{CACHE_VERSION_SDIST}"

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


class TestOnDiskCache:
    def _make(self, tmp_path: Path) -> OnDiskCache:
        return OnDiskCache(tmp_path, "https://pypi.org/simple/")

    def test_simple_round_trip(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        policy = CachePolicy(fetched_at=1000, max_age=600, etag="abc")
        cache.put_simple("foo", b'{"files": []}', policy)
        result = cache.get_simple("foo")
        assert result is not None
        body, got_policy = result
        assert body == b'{"files": []}'
        assert got_policy == policy

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

    def test_sdist_corrupt_record_is_a_miss(self, tmp_path: Path) -> None:
        cache = self._make(tmp_path)
        path = cache._sdist_path("foo", "1.0")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")
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
        assert list(tmp_path.rglob("*.json")) == []

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


class TestNullCache:
    def test_get_returns_none_and_put_is_noop(self) -> None:
        cache = NullCache()
        assert cache.get_simple("foo") is None
        assert cache.get_metadata("foo", METADATA_URLS[0]) is None
        assert cache.get_sdist_files("foo", "1.0") is None
        # Puts must be no-ops with no return value.
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
        path.write_text("not-json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="nab_index.cache"):
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
        path.write_bytes(b"\xff\xfe not utf-8")
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
        policy = CachePolicy(fetched_at=10, max_age=20, etag="x")
        decoded = json.loads(_encode_policy(policy))
        assert decoded == {"fetched_at": 10, "max_age": 20, "etag": "x"}

    def test_round_trip_without_etag(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag=None)
        decoded = json.loads(_encode_policy(policy))
        assert decoded == {"fetched_at": 10, "max_age": 20, "etag": None}


class TestReadCacheEntry:
    def _cache(self, tmp_path: Path) -> OnDiskCache:
        return OnDiskCache(tmp_path, "https://pypi.org/simple")

    def test_corrupt_policy(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.policy"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not json")
        assert cache.read_cache_entry(path) is not None

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

    def test_valid_simple_json_is_clean(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_simple("foo", b'{"files": []}', _FRESH)
        path = tmp_path / SIMPLE_BUCKET / "pypi" / "foo.json"
        assert cache.read_cache_entry(path) is None

    def test_sdist_record_missing_fields(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / "sdist-v1" / "pypi" / "foo" / "1.0.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"pkg_info": "x"}')
        assert cache.read_cache_entry(path) is not None

    def test_valid_sdist_record(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_sdist_files("foo", "1.0", "info", None)
        path = tmp_path / "sdist-v1" / "pypi" / "foo" / "1.0.json"
        assert cache.read_cache_entry(path) is None

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
            "sdist-v1",
        }
        assert not (tmp_path / SIMPLE_BUCKET).exists()
        assert not (tmp_path / "sdist-v1").exists()

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

    def test_clear_removes_the_clone_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cache"
        cache = _populate(root)
        clone = self._clone_into(root / "vcs", monkeypatch)
        assert (clone / "pyproject.toml").is_file()
        assert "vcs" in cache.clear_cache()
        assert not clone.exists()
        assert not (root / "vcs").exists()

    def test_clear_removes_the_archive_tree(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        cache = _populate(root)
        tree = self._extract_into(root / "archive")
        assert "archive" in cache.clear_cache()
        assert not tree.exists()
        assert not (root / "archive").exists()

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
