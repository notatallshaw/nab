"""Refresh the development and build locks, forwarding options to ``nab lock``."""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import tomli

from nab_markersets.markersets import MarkerSet
from nab_provider._vendor.packaging.pylock import Pylock

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider._vendor.packaging.pylock import Package

ROOT = Path(__file__).resolve().parents[1]
BUILD_LOCK = Path(".github/requirements/pylock.build.toml")
DEV_LOCK = Path("pylock.dev.toml")
BUILD_KEYS = ("requires", "build-backend", "backend-path")


def _build_system(path: Path) -> dict[str, object]:
    """Read the build declaration shared by every workspace distribution."""
    with path.open("rb") as stream:
        table = tomli.load(stream).get("build-system")
    if not isinstance(table, dict):
        message = f"{path} does not declare a build system"
        raise TypeError(message)
    return {key: table.get(key) for key in BUILD_KEYS}


def _check_build_systems(root: Path) -> None:
    """Refuse mismatched build inputs before either lock can be rewritten."""
    paths = [root / "pyproject.toml", *sorted(root.glob("nab-*/pyproject.toml"))]
    expected = _build_system(paths[0])
    different = [path for path in paths[1:] if _build_system(path) != expected]
    if different:
        names = ", ".join(str(path.relative_to(root)) for path in different)
        message = f"build systems differ in {names}; align them or lock each separately"
        raise ValueError(message)


def _read_lock(path: Path) -> Pylock:
    with path.open("rb") as stream:
        return Pylock.from_dict(tomli.load(stream))


def _lock_environments(lock: Pylock) -> MarkerSet:
    """Combine each declared environment with the lock's Python requirement."""
    python = MarkerSet.full()
    for specifier in lock.requires_python or ():
        python &= MarkerSet.from_marker(
            f'python_full_version {specifier.operator} "{specifier.version}"'
        )

    if lock.environments is None:
        return python

    targets = MarkerSet.empty()
    for marker in lock.environments:
        targets |= python & MarkerSet.from_marker(marker)
    return targets


def _package_environments(package: Package) -> MarkerSet:
    if package.marker is None:
        return MarkerSet.full()
    return MarkerSet.from_marker(package.marker)


def check_shared_pins(root: Path) -> None:
    """Reject versions that would replace each other in the same environment."""
    build = _read_lock(root / BUILD_LOCK)
    dev = _read_lock(root / DEV_LOCK)
    dev_packages: dict[str, list[Package]] = defaultdict(list)
    for package in dev.packages:
        dev_packages[package.name].append(package)

    differing_versions = [
        (build_pin, dev_pin)
        for build_pin in build.packages
        for dev_pin in dev_packages.get(build_pin.name, ())
        if build_pin.version != dev_pin.version
    ]
    if not differing_versions:
        return

    build_targets = _lock_environments(build)
    dev_targets = _lock_environments(dev)
    conflicts = set()
    for build_pin, dev_pin in differing_versions:
        overlap = (
            build_targets
            & _package_environments(build_pin)
            & _package_environments(dev_pin)
        )
        # Compare one development target at a time so unrelated platform and
        # group conditions do not multiply the marker algebra's search space.
        if not overlap.equivalent_within(MarkerSet.empty(), dev_targets):
            conflicts.add(
                f"{build_pin.name} (build {build_pin.version}, "
                f"development {dev_pin.version})"
            )

    if conflicts:
        message = "build and development pins disagree in the same environment: "
        raise ValueError(message + ", ".join(sorted(conflicts)))


def main(args: Sequence[str], root: Path = ROOT) -> int:
    """Refresh both locks with this interpreter and the forwarded Nab options."""
    try:
        _check_build_systems(root)
        for selection, output in (
            (("--all-groups",), DEV_LOCK),
            (("--build-requirements",), BUILD_LOCK),
        ):
            command = (
                sys.executable,
                "-m",
                "nab",
                "lock",
                *selection,
                "--no-emit-workspace",
                "--output",
                str(output),
                *args,
            )
            result = subprocess.run(command, cwd=root, check=False)
            if result.returncode:
                return result.returncode
        check_shared_pins(root)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
