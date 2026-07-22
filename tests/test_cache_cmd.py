"""Tests for the ``nab cache`` subcommand and its enumeration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab import cli as nab_cli
from nab.cli import app
from nab_index.cache import CachePolicy, NullCache, OnDiskCache

_FRESH = CachePolicy(fetched_at=0, max_age=600, etag=None)


def _run_cache(args: list[str]) -> None:
    app.cli(args=["cache", *args], prog="nab")


def _populate(root: Path) -> OnDiskCache:
    """Write one valid entry of each kind under ``root`` and return the cache."""
    cache = OnDiskCache(root, "https://pypi.org/simple")
    cache.put_simple("foo", b'{"files": []}', _FRESH)
    cache.put_negative("bar", _FRESH)
    cache.put_metadata("foo", "https://example.com/foo.whl", "Name: foo\n")
    cache.put_sdist_files("foo", "1.0", "Name: foo\n", None)
    return cache


class TestCacheDir:
    def test_default_uses_xdg(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xc"))
        _run_cache(["dir"])
        captured = capsys.readouterr()
        assert captured.out == f"{tmp_path / 'xc' / 'nab'}\n"
        assert captured.err == ""

    def test_default_falls_back_to_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(nab_cli.Path, "home", lambda: tmp_path)
        _run_cache(["dir"])
        captured = capsys.readouterr()
        assert captured.out == f"{tmp_path / '.cache' / 'nab'}\n"

    def test_explicit_cache_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "cc"
        _run_cache(["dir", "--cache-dir", str(target)])
        captured = capsys.readouterr()
        assert captured.out == f"{target}\n"
        assert captured.err == ""

    def test_prints_even_when_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "never-created"
        _run_cache(["dir", "--cache-dir", str(target)])
        assert capsys.readouterr().out == f"{target}\n"


class TestCacheVerify:
    def test_reports_corrupt_entry(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "cache"
        _populate(root)
        policy_path = root / "simple-v0" / "pypi" / "foo.policy"
        policy_path.write_bytes(b"not json")
        _run_cache(["verify", "--cache-dir", str(root)])
        err = capsys.readouterr().err
        assert str(policy_path) in err
        assert "decodable" in err

    def test_silent_on_clean_cache(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "cache"
        _populate(root)
        _run_cache(["verify", "--cache-dir", str(root)])
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_refuses_file_root(self, tmp_path: Path) -> None:
        target = tmp_path / "file"
        target.write_text("x")
        with pytest.raises(SystemExit) as exc:
            _run_cache(["verify", "--cache-dir", str(target)])
        assert exc.value.code == 1

    def test_refuses_non_cache_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "home"
        root.mkdir()
        (root / "important.txt").write_text("data")
        with pytest.raises(SystemExit) as exc:
            _run_cache(["verify", "--cache-dir", str(root)])
        assert exc.value.code == 1
        assert "important.txt" in {p.name for p in root.iterdir()}
        assert "cache" in capsys.readouterr().err


class TestCacheClear:
    def test_empties_buckets_keeps_sibling(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "cache"
        _populate(root)
        sibling = root / "unrelated.txt"
        sibling.write_text("keep me")
        _run_cache(["clear", "--cache-dir", str(root)])
        assert not (root / "simple-v0").exists()
        assert not (root / "simple-neg-v0").exists()
        assert not (root / "metadata-v1").exists()
        assert not (root / "sdist-v1").exists()
        assert sibling.read_text() == "keep me"
        assert str(root) in capsys.readouterr().err

    def test_refuses_non_cache_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "home"
        root.mkdir()
        (root / "important.txt").write_text("data")
        with pytest.raises(SystemExit) as exc:
            _run_cache(["clear", "--cache-dir", str(root)])
        assert exc.value.code == 1
        assert (root / "important.txt").read_text() == "data"

    def test_does_not_follow_symlink_out(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")
        root = tmp_path / "cache"
        root.mkdir()
        (root / "simple-v0").mkdir()
        (root / "metadata-v1").symlink_to(outside, target_is_directory=True)
        _run_cache(["clear", "--cache-dir", str(root)])
        assert (outside / "keep.txt").read_text() == "precious"
        assert not (root / "simple-v0").exists()
        assert not (root / "metadata-v1").exists()

    def test_nonexistent_root_is_noop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "never"
        _run_cache(["clear", "--cache-dir", str(root)])
        assert str(root) in capsys.readouterr().err
        assert not root.exists()

    def test_leaves_bucket_named_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "cache"
        _populate(root)
        foreign = root / "metadata-notes.txt"
        foreign.write_text("mine")
        _run_cache(["clear", "--cache-dir", str(root)])
        assert not (root / "simple-v0").exists()
        assert not (root / "metadata-v1").exists()
        assert foreign.read_text() == "mine"
        assert str(root) in capsys.readouterr().err

    def test_refuses_root_whose_only_bucket_named_child_is_a_file(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "home"
        root.mkdir()
        (root / "metadata-notes.txt").write_text("data")
        with pytest.raises(SystemExit) as exc:
            _run_cache(["clear", "--cache-dir", str(root)])
        assert exc.value.code == 1
        assert (root / "metadata-notes.txt").read_text() == "data"


class TestCacheUnknownAction:
    def test_unknown_action_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run_cache(["frobnicate", "--cache-dir", str(tmp_path)])
        assert exc.value.code == 1
        assert "unknown cache action" in capsys.readouterr().err


class TestReadCacheEntry:
    def _cache(self, tmp_path: Path) -> OnDiskCache:
        return OnDiskCache(tmp_path, "https://pypi.org/simple")

    def test_corrupt_policy(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / "simple-v0" / "pypi" / "foo.policy"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not json")
        assert cache.read_cache_entry(path) is not None

    def test_valid_policy(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        cache.put_simple("foo", b"{}", _FRESH)
        path = tmp_path / "simple-v0" / "pypi" / "foo.policy"
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
        path = tmp_path / "simple-v0" / "pypi" / "foo.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"{not json")
        assert cache.read_cache_entry(path) == "not valid JSON"

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
        path = tmp_path / "simple-v0" / "pypi" / "foo.json.abc.tmp"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"partial")
        assert cache.read_cache_entry(path) is None

    def test_unreadable_entry(self, tmp_path: Path) -> None:
        cache = self._cache(tmp_path)
        path = tmp_path / "simple-v0" / "pypi" / "adir.policy"
        path.mkdir(parents=True)
        assert cache.read_cache_entry(path) is not None


class TestIterCacheEntries:
    def test_yields_files_and_skips_symlinks(self, tmp_path: Path) -> None:
        cache = _populate(tmp_path)
        outside = tmp_path.parent / "iter-out"
        outside.mkdir()
        (outside / "x.json").write_text("{}")
        (tmp_path / "linked-simple-v0").symlink_to(outside, target_is_directory=True)
        (tmp_path / "sdist-v1-file").write_text("junk")
        (tmp_path / "simple-v0" / "pypi" / "link.json").symlink_to(outside / "x.json")
        names = {e.name for e in cache.iter_cache_entries()}
        assert "foo.json" in names
        assert "foo.policy" in names
        assert "bar.neg" in names
        assert "x.json" not in names
        assert "link.json" not in names

    def test_empty_root_yields_nothing(self, tmp_path: Path) -> None:
        cache = OnDiskCache(tmp_path / "gone", "https://pypi.org/simple")
        assert list(cache.iter_cache_entries()) == []


class TestNullCacheEnumeration:
    def test_helpers_are_trivial(self) -> None:
        nc = NullCache()
        assert list(nc.iter_cache_entries()) == []
        assert nc.read_cache_entry(Path("x")) is None
        assert nc.clear_cache() == []
