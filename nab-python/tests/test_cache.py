"""Tests for nab_index.cache."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from nab_index.atomic import atomic_write_text
from nab_index.cache import (
    CachePolicy,
    NullCache,
    OfflineError,
    OnDiskCache,
    _atomic_write,
    _encode_policy,
    _index_dirname,
)

# Two wheels of one version, each with its own PEP 658 sidecar.
METADATA_URLS = (
    "https://f.example/foo-1.0-cp311-manylinux_2_17_x86_64.whl.metadata",
    "https://f.example/foo-1.0-cp311-win_amd64.whl.metadata",
)


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
        assert (tmp_path / "simple-v0" / "pypi" / "foo.json").exists()
        assert (tmp_path / "simple-v0" / "pypi" / "foo.policy").exists()

    def test_simple_alt_index_uses_hash_dirname(self, tmp_path: Path) -> None:
        cache = OnDiskCache(tmp_path, "https://alt.example/simple/")
        cache.put_simple(
            "foo",
            b"{}",
            CachePolicy(fetched_at=1, max_age=1, etag=None),
        )
        sub = tmp_path / "simple-v0"
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


class TestEncodePolicy:
    def test_round_trip_with_etag(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag="x")
        decoded = json.loads(_encode_policy(policy))
        assert decoded == {"fetched_at": 10, "max_age": 20, "etag": "x"}

    def test_round_trip_without_etag(self) -> None:
        policy = CachePolicy(fetched_at=10, max_age=20, etag=None)
        decoded = json.loads(_encode_policy(policy))
        assert decoded == {"fetched_at": 10, "max_age": 20, "etag": None}
