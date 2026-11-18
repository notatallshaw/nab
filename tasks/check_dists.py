"""Build the release distributions and prove that they install.

Builds an sdist and a wheel for every released distribution, installs every
sdist and the full wheel set into throwaway venvs, and checks package imports,
typing markers and artifact licenses. Building alone does not catch a broken sdist:
``build`` extracts one with a permissive tar filter, while ``pip install``
applies the data filter and rejects a link that points outside the archive. A
symlinked license drops out of the wheel silently.

Run through nox::

    nox -s dists
"""

from __future__ import annotations

import email
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import build_dists

# The checked artifacts are the set the release publishes.
PACKAGES = build_dists.PACKAGES

# Every nab-provider artifact ships the vendored packaging tree, so its license
# files have to travel with it. Matched by suffix because the wheel roots the
# package at nab_provider/ while the sdist roots it at <root>/src/nab_provider/.
VENDOR_LICENSES = (
    "nab_provider/_vendor/packaging/LICENSE",
    "nab_provider/_vendor/packaging/LICENSE.APACHE",
    "nab_provider/_vendor/packaging/LICENSE.BSD",
)


def _run(command: list[str]) -> None:
    """Run a command, streaming its output, and stop the check if it fails."""
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        msg = f"command failed: {' '.join(command)}"
        raise SystemExit(msg)


def _find_links(dist_root: Path) -> list[str]:
    """Return --find-links args covering every built package directory.

    They point the nab-* cross-dependencies at the in-tree build rather than an
    older release on PyPI.
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


# A standalone marker installation selects its packaging backend with an extra.
SDIST_EXTRAS = {"nab-markersets": "[packaging]"}


def install_each_sdist(dist_root: Path, scratch: Path) -> None:
    """Install every sdist into its own venv, resolving siblings locally."""
    links = _find_links(dist_root)
    for package in PACKAGES:
        sdist = _sdist(dist_root, package)
        python = build_dists.create_install_env(scratch / f"sdist-{package}")
        target = f"{sdist}{SDIST_EXTRAS.get(package, '')}"
        _run([str(python), "-m", "pip", "install", *links, target])
        build_dists.check_installed(python, package)


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
    """Fail unless both nab-provider artifacts carry the vendored licenses."""
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
    """Build every distribution, then install and check each artifact."""
    source_date_epoch = build_dists.source_date_epoch()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        dist_root = workdir / "dist"
        build_dists.build_all(dist_root, source_date_epoch)

        install_each_sdist(dist_root, workdir)
        build_dists.smoke_test(dist_root)

        for package in PACKAGES:
            check_wheel_license(build_dists.wheel_file(dist_root, package))
            check_sdist_license(_sdist(dist_root, package))
        check_vendored_licenses(
            build_dists.wheel_file(dist_root, "nab-provider"),
            _sdist(dist_root, "nab-provider"),
        )
        build_dists.report_sizes(dist_root)

    print("dists built, installed from sdists and wheels, and license-checked.")


if __name__ == "__main__":
    main()
