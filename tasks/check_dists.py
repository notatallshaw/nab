"""Build the release distributions and prove that they install.

Builds an sdist and a wheel for all four workspace packages, installs every
sdist and the full wheel set into throwaway venvs, and checks that each
artifact ships its LICENSE. Building alone does not catch a broken sdist:
``build`` extracts an sdist with a permissive tar filter, while ``pip install``
applies the data filter and rejects a link that points outside the archive, so
an sdist that builds cleanly can still fail for anyone who installs it. A
symlinked license also drops out of the wheel silently, which the artifact
checks below catch.

Run through nox::

    nox -s dists
"""

from __future__ import annotations

import email
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import build_dists

REPO_ROOT = build_dists.REPO_ROOT

# Build order and package directories come from the release build so the
# checked artifacts are exactly the set the release publishes.
PACKAGES = build_dists.PACKAGES

# Every nab-python artifact ships the vendored packaging tree, so its license
# files have to travel with it. Matched by suffix because the wheel roots the
# package at nab_python/ while the sdist roots it at <root>/src/nab_python/.
VENDOR_LICENSES = (
    "nab_python/_vendor/packaging/LICENSE",
    "nab_python/_vendor/packaging/LICENSE.APACHE",
    "nab_python/_vendor/packaging/LICENSE.BSD",
)


def _source_date_epoch() -> str:
    """Return HEAD's committer timestamp, matching the release build."""
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=%ct"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _run(command: list[str]) -> None:
    """Run a command, streaming its output, and stop the check if it fails."""
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        msg = f"command failed: {' '.join(command)}"
        raise SystemExit(msg)


def _capture(command: list[str]) -> str:
    """Run a command, echo its output, and return its stdout."""
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        msg = f"command failed: {' '.join(command)}"
        raise SystemExit(msg)
    return result.stdout


def _make_venv(path: Path) -> Path:
    """Create a fresh venv with a current pip and return its interpreter."""
    _run([sys.executable, "-m", "venv", str(path)])
    bindir = path / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / "python"
    _run([str(python), "-m", "pip", "install", "--upgrade", "pip>=26.1"])
    return python


def _find_links(dist_root: Path) -> list[str]:
    """Return --find-links args covering every built package directory.

    With these, the nab-* cross-dependencies resolve to the in-tree build
    rather than an older release on PyPI.
    """
    links: list[str] = []
    for package in PACKAGES:
        links += ["--find-links", str(dist_root / package)]
    return links


def _only(paths: list[Path], what: str, package: str) -> Path:
    """Return the sole path, or fail if the build produced anything else."""
    match paths:
        case [single]:
            return single
    found = [path.name for path in paths]
    msg = f"expected exactly one {what} for {package}, found {found}"
    raise SystemExit(msg)


def _wheel(dist_root: Path, package: str) -> Path:
    """Return the built wheel for a package."""
    return _only(sorted((dist_root / package).glob("*.whl")), "wheel", package)


def _sdist(dist_root: Path, package: str) -> Path:
    """Return the built sdist for a package."""
    return _only(sorted((dist_root / package).glob("*.tar.gz")), "sdist", package)


def _dist_info(names: list[str]) -> str:
    """Return the .dist-info directory name within a wheel."""
    for name in names:
        top = name.split("/", 1)[0]
        if top.endswith(".dist-info"):
            return top
    msg = "wheel has no .dist-info directory"
    raise SystemExit(msg)


def install_each_sdist(dist_root: Path, scratch: Path) -> None:
    """Install every sdist into its own venv, resolving siblings locally."""
    links = _find_links(dist_root)
    for package in PACKAGES:
        sdist = _sdist(dist_root, package)
        python = _make_venv(scratch / f"sdist-{package}")
        _run([str(python), "-m", "pip", "install", *links, str(sdist)])


def install_wheels(dist_root: Path, scratch: Path) -> None:
    """Install all four wheels together, then import and run the CLI."""
    wheels = [str(_wheel(dist_root, package)) for package in PACKAGES]
    version = _wheel_version(_wheel(dist_root, "nab"))
    python = _make_venv(scratch / "wheels")
    _run([str(python), "-m", "pip", "install", *wheels])
    _run([str(python), "-c", "import nab, nab_resolver, nab_python, nab_index"])
    output = _capture([str(python), "-m", "nab", "--version"])
    expected = f"nab {version}"
    if expected not in output:
        msg = f"`nab --version` reported {output.strip()!r}, expected {expected!r}"
        raise SystemExit(msg)


def _wheel_version(wheel: Path) -> str:
    """Return the Version recorded in a wheel's METADATA."""
    with zipfile.ZipFile(wheel) as archive:
        info = _dist_info(archive.namelist())
        metadata = email.message_from_string(archive.read(f"{info}/METADATA").decode())
    version = metadata["Version"]
    if not version:
        msg = f"{wheel.name}: METADATA has no Version"
        raise SystemExit(msg)
    return version


def check_wheel_license(wheel: Path) -> None:
    """Fail unless the wheel carries its LICENSE and records License-File."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        info = _dist_info(names)
        member = f"{info}/licenses/LICENSE"
        if member not in names:
            msg = f"{wheel.name}: wheel is missing {member}"
            raise SystemExit(msg)
        metadata = email.message_from_string(archive.read(f"{info}/METADATA").decode())
    if not metadata.get_all("License-File"):
        msg = f"{wheel.name}: METADATA has no License-File header"
        raise SystemExit(msg)


def check_sdist_license(sdist: Path) -> None:
    """Fail unless the sdist roots a regular-file LICENSE (never a symlink)."""
    root = sdist.name[: -len(".tar.gz")]
    member_name = f"{root}/LICENSE"
    with tarfile.open(sdist) as archive:
        try:
            member = archive.getmember(member_name)
        except KeyError:
            msg = f"{sdist.name}: sdist is missing {member_name}"
            raise SystemExit(msg) from None
        if not member.isreg():
            msg = (
                f"{sdist.name}: {member_name} is not a regular file "
                f"(tar type {member.type!r}); a dangling symlink here fails "
                f"pip's install-time extraction"
            )
            raise SystemExit(msg)


def check_vendored_licenses(wheel: Path, sdist: Path) -> None:
    """Fail unless both nab-python artifacts carry the vendored licenses."""
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(sdist) as archive:
        sdist_names = archive.getnames()
    for suffix in VENDOR_LICENSES:
        if not any(name.endswith(suffix) for name in wheel_names):
            msg = f"{wheel.name}: missing vendored license {suffix}"
            raise SystemExit(msg)
        if not any(name.endswith(suffix) for name in sdist_names):
            msg = f"{sdist.name}: missing vendored license {suffix}"
            raise SystemExit(msg)


def main() -> None:
    """Build the four distributions, then install and check every artifact."""
    source_date_epoch = _source_date_epoch()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        dist_root = workdir / "dist"
        build_dists.build_all(dist_root, source_date_epoch)

        install_each_sdist(dist_root, workdir)
        install_wheels(dist_root, workdir)

        for package in PACKAGES:
            check_wheel_license(_wheel(dist_root, package))
            check_sdist_license(_sdist(dist_root, package))
        check_vendored_licenses(
            _wheel(dist_root, "nab-python"),
            _sdist(dist_root, "nab-python"),
        )

    print("dists built, installed from sdists and wheels, and license-checked.")


if __name__ == "__main__":
    main()
