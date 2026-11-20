"""Identify the checkout that supplied the benchmark scripts."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

BENCHMARKS_DIR = Path(__file__).resolve().parent


def _git(directory: Path, *arguments: str) -> bytes:
    """Read Git output as bytes so filenames and binary diffs round-trip."""
    return subprocess.check_output(  # noqa: S603 - paths follow -- in Git arguments
        ["git", *arguments], cwd=directory, stderr=subprocess.PIPE
    )


def get_git_commit(*, short: bool = False) -> str:
    """Return the benchmark checkout's full revision or its short result label."""
    options = ("--short",) if short else ()
    return _git(BENCHMARKS_DIR, "rev-parse", *options, "HEAD").decode("ascii").strip()


def get_git_source_state() -> dict[str, str | bool | None]:
    """Record the revision and changes to tracked and untracked files."""
    try:
        commit = get_git_commit()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": True, "diff_hash": None}

    try:
        root_bytes = _git(BENCHMARKS_DIR, "rev-parse", "--show-toplevel")
        root = Path(os.fsdecode(root_bytes.removesuffix(b"\n")))
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if not status:
            return {"commit": commit, "dirty": False, "diff_hash": None}

        diff = _git(root, "diff", "--binary", "HEAD", "--")
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        digest = hashlib.sha256(status + b"\0" + diff + b"\0")
        for path in untracked.split(b"\0"):
            if path:
                digest.update(
                    path + b"\0" + _git(root, "hash-object", "--", os.fsdecode(path))
                )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"commit": commit, "dirty": True, "diff_hash": None}

    return {"commit": commit, "dirty": True, "diff_hash": digest.hexdigest()}
