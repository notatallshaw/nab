"""Build, verify, and smoke-test the nab distributions for a release.

Builds all four packages into ``dist/<package>`` with a pinned timestamp, proves
the build is reproducible by building a second time and comparing hashes, runs
``twine check --strict``, and installs the built ``nab`` into a throwaway venv to
confirm the CLI runs. The publish workflow calls this; nothing here uploads.

Run through hatch::

    hatch run release:build
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The umbrella package lives at the repo root; the rest are subdirectories.
PACKAGES = ("nab-resolver", "nab-index", "nab-python", "nab")


def _require_tools(*tools: str) -> None:
    """Exit early if any required external tool is missing from PATH."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        msg = f"required tools not found on PATH: {', '.join(missing)}"
        raise SystemExit(msg)


def _run(
    command: list[str], *, env: dict[str, str] | None = None, capture: bool = False
) -> str:
    """Run a command in the repo root, exiting cleanly if it is missing or fails."""
    try:
        result = subprocess.run(
            command,
            check=True,
            cwd=REPO_ROOT,
            env=env,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError:
        msg = f"command not found: {command[0]}"
        raise SystemExit(msg) from None
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip()
        msg = f"command failed ({' '.join(command)}):\n{details}".rstrip()
        raise SystemExit(msg) from error
    return (result.stdout or "").strip() if capture else ""


def source_dir(package: str) -> Path:
    """Return the source directory for a workspace package."""
    return REPO_ROOT if package == "nab" else REPO_ROOT / package


def _source_date_epoch() -> str:
    """Return the committer timestamp of HEAD for reproducible builds."""
    return _run(["git", "log", "-1", "--pretty=%ct"], capture=True)


def build_all(outdir: Path, source_date_epoch: str) -> None:
    """Build every package into ``outdir/<package>`` with a fixed timestamp."""
    env = {**os.environ, "SOURCE_DATE_EPOCH": source_date_epoch}
    for package in PACKAGES:
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(outdir / package),
                str(source_dir(package)),
            ],
            env=env,
        )


def _artifact_hashes(root: Path) -> dict[str, str]:
    """Map each built sdist/wheel name under ``root`` to its sha256."""
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name.endswith((".whl", ".tar.gz")):
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def verify_reproducible(dist: Path, source_date_epoch: str) -> None:
    """Build into ``dist``, build again into a temp dir, and compare hashes."""
    build_all(dist, source_date_epoch)
    first = _artifact_hashes(dist)
    with tempfile.TemporaryDirectory() as tmp:
        build_all(Path(tmp), source_date_epoch)
        second = _artifact_hashes(Path(tmp))
    if first != second:
        msg = "build is not reproducible: artifact hashes differ between builds"
        raise SystemExit(msg)


def twine_check(dist: Path) -> None:
    """Run ``twine check --strict`` over every built artifact."""
    artifacts = [
        str(path)
        for path in sorted(dist.rglob("*"))
        if path.is_file() and path.name.endswith((".whl", ".tar.gz"))
    ]
    _run([sys.executable, "-m", "twine", "check", "--strict", *artifacts])


def smoke_test(dist: Path) -> None:
    """Install the built nab into a throwaway venv and run ``nab --help``."""
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        _run([sys.executable, "-m", "venv", str(venv)])
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = str(bindir / "python")
        _run([python, "-m", "pip", "install", "--upgrade", "pip"])
        find_links: list[str] = []
        for package in PACKAGES:
            find_links += ["--find-links", str(dist / package)]
        _run([python, "-m", "pip", "install", *find_links, "nab"])
        _run([str(bindir / "nab"), "--help"])


def main() -> None:
    """Build, verify reproducibility, check metadata, and smoke-test."""
    _require_tools("git")
    dist = REPO_ROOT / "dist"
    source_date_epoch = _source_date_epoch()
    verify_reproducible(dist, source_date_epoch)
    twine_check(dist)
    smoke_test(dist)
    print("dist/ built, reproducible, metadata-checked, and smoke-tested.")


if __name__ == "__main__":
    main()
