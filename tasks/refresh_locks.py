"""Refresh the development and build locks, forwarding options to ``nab lock``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import tomli

from nab_provider._vendor.packaging.pylock import Pylock

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def check_build_systems(root: Path) -> None:
    """Refuse mismatched build inputs before either lock can be rewritten."""
    paths = [root / "pyproject.toml", *sorted(root.glob("nab-*/pyproject.toml"))]
    expected = _build_system(paths[0])
    different = [path for path in paths[1:] if _build_system(path) != expected]
    if different:
        names = ", ".join(str(path.relative_to(root)) for path in different)
        message = f"build systems differ in {names}; align them or lock each separately"
        raise ValueError(message)


def _pins(path: Path) -> dict[str, set[str | None]]:
    """Collect version choices across the lock's declared environments."""
    with path.open("rb") as stream:
        lock = Pylock.from_dict(tomli.load(stream))
    versions: dict[str, set[str | None]] = {}
    for package in lock.packages:
        version = str(package.version) if package.version is not None else None
        versions.setdefault(str(package.name), set()).add(version)
    return versions


def check_shared_pins(root: Path) -> None:
    """Prevent sequential installs from silently moving a shared package's version."""
    build, dev = (_pins(root / path) for path in (BUILD_LOCK, DEV_LOCK))
    different = sorted(
        name for name in build.keys() & dev.keys() if build[name] != dev[name]
    )
    if different:
        message = "build and development locks disagree on: " + ", ".join(different)
        raise ValueError(message)


def main(args: Sequence[str], root: Path = ROOT) -> int:
    """Refresh both locks with this interpreter and the forwarded Nab options."""
    try:
        check_build_systems(root)
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
