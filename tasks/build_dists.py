"""Build, verify, and smoke-test the nab distributions for a release.

Builds all six packages into ``dist/<package>`` with a pinned timestamp, proves
the build is reproducible by building a second time and comparing hashes, runs
``twine check --strict``, and installs the built ``nab`` into a throwaway venv to
confirm the CLI runs. The publish workflow calls this; nothing here uploads.

Run through hatch::

    hatch run release:build
"""

from __future__ import annotations

import email
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The umbrella package lives at the repo root; the rest are subdirectories.
PACKAGES = (
    "nab-resolver",
    "nab-markersets",
    "nab-provider",
    "nab-index",
    "nab-project",
    "nab",
)


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


def source_date_epoch() -> str:
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


def _artifact_hashes(root: Path) -> dict[Path, str]:
    """Map each archive's path relative to the build directory to its SHA-256."""
    hashes: dict[Path, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name.endswith((".whl", ".tar.gz")):
            relative = path.relative_to(root)
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _verify_reproducible(dist: Path, source_date_epoch: str) -> None:
    """Build into ``dist``, build again into a temp dir, and compare hashes."""
    build_all(dist, source_date_epoch)
    first = _artifact_hashes(dist)
    with tempfile.TemporaryDirectory() as tmp:
        build_all(Path(tmp), source_date_epoch)
        second = _artifact_hashes(Path(tmp))
    if first != second:
        msg = "build is not reproducible: artifact hashes differ between builds"
        raise SystemExit(msg)


def _twine_check(dist: Path) -> None:
    """Run ``twine check --strict`` over every built artifact."""
    artifacts = [
        str(path)
        for path in sorted(dist.rglob("*"))
        if path.is_file() and path.name.endswith((".whl", ".tar.gz"))
    ]
    _run([sys.executable, "-m", "twine", "check", "--strict", *artifacts])


def wheel_file(dist: Path, package: str) -> Path:
    """Return the sole built wheel for one release package."""
    wheels = sorted((dist / package).glob("*.whl"))
    if len(wheels) != 1:
        found = [path.name for path in wheels]
        msg = f"expected exactly one wheel for {package}, found {found}"
        raise SystemExit(msg)
    return wheels[0]


def _wheel_version(wheel: Path) -> str:
    """Read the version of an artifact before checking its installed CLI."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            msg = f"{wheel.name}: expected exactly one METADATA file"
            raise SystemExit(msg)
        metadata = email.message_from_bytes(archive.read(metadata_files[0]))
    version = metadata["Version"]
    if not version:
        msg = f"{wheel.name}: METADATA has no Version"
        raise SystemExit(msg)
    return version


def create_install_env(path: Path) -> Path:
    """Return the interpreter of a fresh environment for artifact checks."""
    _run([sys.executable, "-m", "venv", str(path)])
    bindir = path / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / "python"
    _run([str(python), "-m", "pip", "install", "--upgrade", "pip>=26.1"])
    return python


def check_installed(python: Path, *packages: str) -> None:
    """Run the import checks in the installed packages' own environment."""
    _run([str(python), "-I", str(REPO_ROOT / "tasks/check_installed.py"), *packages])


def smoke_test(dist: Path) -> None:
    """Check wheel imports, typing markers, CLI help, and both entry versions."""
    wheels = [wheel_file(dist, package) for package in PACKAGES]
    version = _wheel_version(wheel_file(dist, "nab"))
    with tempfile.TemporaryDirectory() as tmp:
        python = create_install_env(Path(tmp) / "venv")
        _run([str(python), "-m", "pip", "install", *(str(wheel) for wheel in wheels)])
        check_installed(python, *PACKAGES)
        console = shutil.which("nab", path=str(python.parent))
        if console is None:
            msg = f"the built wheels installed no nab command in {python.parent}"
            raise SystemExit(msg)
        _run([console, "--help"])
        for command in ([str(python), "-m", "nab"], [console]):
            output = _run([*command, "--version"], capture=True)
            if output.strip() != f"nab {version}":
                shown = " ".join(command)
                msg = f"{shown} --version reported {output!r}, expected nab {version}"
                raise SystemExit(msg)


class ArchiveSize:
    """A built archive's file size and total unpacked member bytes."""

    __slots__ = ("compressed_bytes", "filename", "unpacked_bytes")

    def __init__(
        self, filename: str, compressed_bytes: int, unpacked_bytes: int
    ) -> None:
        """Store one archive's measured byte counts."""
        self.filename = filename
        self.compressed_bytes = compressed_bytes
        self.unpacked_bytes = unpacked_bytes


def archive_size(path: Path) -> ArchiveSize:
    """Read archive sizes without extracting their contents."""
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as wheel:
            unpacked = sum(member.file_size for member in wheel.infolist())
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as sdist:
            unpacked = sum(member.size for member in sdist)
    else:
        msg = f"unsupported distribution archive: {path.name}"
        raise SystemExit(msg)
    return ArchiveSize(path.name, path.stat().st_size, unpacked)


def report_sizes(dist: Path) -> None:
    """Report artifact bytes in the build log and optional Actions summary."""
    sizes = [
        archive_size(path)
        for path in sorted(dist.rglob("*"))
        if path.is_file() and path.name.endswith((".whl", ".tar.gz"))
    ]
    compressed = sum(item.compressed_bytes for item in sizes)
    unpacked = sum(item.unpacked_bytes for item in sizes)
    lines = [
        "| Artifact | Archive bytes | Unpacked bytes |",
        "| --- | ---: | ---: |",
        *(
            f"| {item.filename} | {item.compressed_bytes} | {item.unpacked_bytes} |"
            for item in sizes
        ),
        f"| Total | {compressed} | {unpacked} |",
    ]
    table = "\n".join(lines) + "\n"
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("\n" + table)


def main() -> None:
    """Build, verify reproducibility, check metadata, and smoke-test."""
    _require_tools("git")
    dist = REPO_ROOT / "dist"
    epoch = source_date_epoch()
    _verify_reproducible(dist, epoch)
    _twine_check(dist)
    smoke_test(dist)
    report_sizes(dist)
    print("dist/ built, reproducible, metadata-checked, and smoke-tested.")


if __name__ == "__main__":
    main()
