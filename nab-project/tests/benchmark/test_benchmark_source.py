"""Check source identities and run labels against a real temporary checkout."""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.benchmark, pytest.mark.usefixtures("benchmark_import_path")]


def test_source_state_tracks_the_script_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = importlib.import_module("benchmark_source")
    checkout = tmp_path / "nab"
    scripts = checkout / "nab-project" / "benchmarks"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(source, "BENCHMARKS_DIR", scripts)
    monkeypatch.chdir(tmp_path)
    # Fixture commits should not use the contributor's signing key or ignore rules.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    executable = shutil.which("git")
    assert executable is not None

    def git(*args: str) -> bytes:
        return subprocess.check_output([executable, *args], cwd=checkout)  # noqa: S603

    git("init", "-q")
    tracked = scripts / "tracked"
    tracked.write_bytes(b"original\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "initial",
    )

    commit = git("rev-parse", "HEAD").decode().strip()
    assert (
        source.get_git_commit(short=True)
        == git("rev-parse", "--short", "HEAD").decode().strip()
    )
    assert source.get_git_source_state() == {
        "commit": commit,
        "dirty": False,
        "diff_hash": None,
    }

    tracked.write_bytes(b"modified\n")
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    diff = git("diff", "--binary", "HEAD", "--")
    digest = hashlib.sha256(status + b"\0" + diff + b"\0").hexdigest()
    assert source.get_git_source_state() == {
        "commit": commit,
        "dirty": True,
        "diff_hash": digest,
    }

    # A sibling source file is outside the scripts directory but inside the checkout.
    untracked = checkout / "source.py"
    untracked.write_bytes(b"first\n")
    first = source.get_git_source_state()
    untracked.write_bytes(b"second\n")
    second = source.get_git_source_state()
    assert first["commit"] == second["commit"] == commit
    assert first["dirty"] is second["dirty"] is True
    assert first["diff_hash"] != second["diff_hash"]


def test_source_export_has_no_clean_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = importlib.import_module("benchmark_source")
    monkeypatch.setattr(source, "BENCHMARKS_DIR", tmp_path)
    # Stop Git finding a real checkout if the test's temporary directory is inside one.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert source.get_git_source_state() == {
        "commit": None,
        "dirty": True,
        "diff_hash": None,
    }


def test_failed_status_preserves_known_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    source = importlib.import_module("benchmark_source")
    commit = "a" * 40

    def git(_directory: Path, *args: str) -> bytes:
        if args == ("rev-parse", "HEAD"):
            return commit.encode()
        if args == ("rev-parse", "--show-toplevel"):
            return b"/repo\n"
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr(source, "_git", git)
    assert source.get_git_source_state() == {
        "commit": commit,
        "dirty": True,
        "diff_hash": None,
    }
