"""Tests for the ``nab cache`` subcommand and its enumeration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab import cli as nab_cli
from nab.cli import app
from nab_index.cache import CachePolicy, OnDiskCache

_FRESH = CachePolicy(fetched_at=0, max_age=600, etag=None)


def _symlink_or_skip(
    link: Path, target: Path, *, target_is_directory: bool = False
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")


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

    def test_does_not_read_through_symlinked_bucket(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "foo.policy").write_bytes(b"not json")
        root = tmp_path / "cache"
        root.mkdir()
        _symlink_or_skip(root / "simple-v0", outside, target_is_directory=True)
        _run_cache(["verify", "--cache-dir", str(root)])
        assert capsys.readouterr().err == ""

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
        _symlink_or_skip(root / "metadata-v1", outside, target_is_directory=True)
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


class TestCacheSourceTrees:
    """The vcs and archive trees a resolve writes under the same root."""

    @staticmethod
    def _populate_source_trees(root: Path) -> None:
        clone = root / "vcs" / "vcs" / "0123456789abcdef" / ("a" * 40)
        clone.mkdir(parents=True)
        (clone / "pyproject.toml").write_text("[project]\n")
        extracted = root / "archive" / ("b" * 64)
        extracted.mkdir(parents=True)
        (extracted / "pyproject.toml").write_text("[project]\n")

    def test_clear_returns_the_cache_to_cold(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "cache"
        _populate(root)
        self._populate_source_trees(root)
        _run_cache(["clear", "--cache-dir", str(root)])
        assert list(root.iterdir()) == []
        assert str(root) in capsys.readouterr().err

    def test_verify_and_clear_accept_a_source_only_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "cache"
        self._populate_source_trees(root)
        _run_cache(["verify", "--cache-dir", str(root)])
        assert capsys.readouterr().err == ""
        _run_cache(["clear", "--cache-dir", str(root)])
        assert list(root.iterdir()) == []
        _run_cache(["clear", "--cache-dir", str(root)])
        assert str(root) in capsys.readouterr().err


class TestCacheUnknownAction:
    def test_unknown_action_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run_cache(["frobnicate", "--cache-dir", str(tmp_path)])
        assert exc.value.code == 1
        assert "unknown cache action" in capsys.readouterr().err
