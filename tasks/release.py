"""Cut a lockstep release of the nab workspace packages.

``make X.Y.Z`` branches off main, writes two commits (the release version, then a
return to development), tags the release commit, and pushes the branch and tag.
You open the PR from that branch yourself. Publishing happens later: merge the PR
with a merge commit, then publish the tag's release in the GitHub UI, which
triggers the publish workflow. ``check`` verifies a tag matches the manifests and
is what that workflow runs before building.

Run through hatch::

    hatch run release:make 0.0.3
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Any

import tomlkit
import tyro
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from tyro.extras import SubcommandApp

REPO_ROOT = Path(__file__).resolve().parent.parent

WORKSPACE_PACKAGES = ("nab-resolver", "nab-python", "nab-index")
_WORKSPACE = {canonicalize_name(name) for name in WORKSPACE_PACKAGES}

PYPROJECT_PATHS = (
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "nab-resolver" / "pyproject.toml",
    REPO_ROOT / "nab-python" / "pyproject.toml",
    REPO_ROOT / "nab-index" / "pyproject.toml",
)


def cross_pin(version: str) -> str:
    """Return the cross-pin specifier for a workspace dependency."""
    return f"=={version}"


def is_dev_version(version: str) -> bool:
    """Return whether the version is a PEP 440 development release."""
    try:
        return Version(version).is_devrelease
    except InvalidVersion:
        return False


def is_release_version(version: str) -> bool:
    """Return whether the version parses and is neither a dev nor a local version."""
    try:
        parsed = Version(version)
    except InvalidVersion:
        return False
    return not parsed.is_devrelease and parsed.local is None


def next_dev_version(release: str) -> str:
    """Return the development version after a release, bumping the last component."""
    version = Version(release)
    bumped = (*version.release[:-1], version.release[-1] + 1)
    return str(
        version.__replace__(release=bumped, dev=0, pre=None, post=None, local=None)
    )


def _rewrite_requirement(dependency: str, version: str) -> str:
    """Rewrite a workspace cross-pin to ``==version``; leave others unchanged."""
    requirement = Requirement(dependency)
    if canonicalize_name(requirement.name) not in _WORKSPACE:
        return dependency
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    return f"{requirement.name}{extras}{cross_pin(version)}"


def _dependency_arrays(project: Any) -> list[Any]:
    """Return every dependency array in a parsed ``[project]`` table."""
    arrays: list[Any] = []
    if "dependencies" in project:
        arrays.append(project["dependencies"])
    if "optional-dependencies" in project:
        arrays.extend(project["optional-dependencies"].values())
    return arrays


def apply_version(version: str) -> None:
    """Write the version and rewrite workspace cross-pins in every pyproject."""
    for path in PYPROJECT_PATHS:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        document["project"]["version"] = version
        for array in _dependency_arrays(document["project"]):
            for index, dependency in enumerate(array):
                array[index] = _rewrite_requirement(str(dependency), version)
        path.write_text(tomlkit.dumps(document), encoding="utf-8")


def read_current_version() -> str:
    """Return the lockstep version, requiring every package to agree."""
    versions: set[str] = set()
    for path in PYPROJECT_PATHS:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        versions.add(str(document["project"]["version"]))
    if len(versions) != 1:
        msg = f"workspace versions are not in lockstep: {sorted(versions)}"
        raise ValueError(msg)
    return versions.pop()


def check_release(tag: str) -> None:
    """Verify the working tree is a clean release matching ``tag``.

    Confirms the ``vX.Y.Z`` tag matches every package version, that the version
    is not a development version, and that every cross-pin is exactly
    ``==X.Y.Z``. The publish workflow runs this before it builds.
    """
    if not tag.startswith("v"):
        msg = f"release tag {tag!r} must start with 'v'"
        raise SystemExit(msg)
    expected = tag[1:]
    version = read_current_version()
    if version != expected:
        msg = f"tag {tag!r} does not match package version {version!r}"
        raise SystemExit(msg)
    if is_dev_version(version):
        msg = f"refusing to release a dev version {version!r}"
        raise SystemExit(msg)
    wanted = cross_pin(version)
    for path in PYPROJECT_PATHS:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        for array in _dependency_arrays(document["project"]):
            for dependency in array:
                requirement = Requirement(str(dependency))
                specifier = str(requirement.specifier)
                if (
                    canonicalize_name(requirement.name) in _WORKSPACE
                    and specifier != wanted
                ):
                    msg = (
                        f"{path}: {requirement.name} is pinned "
                        f"{specifier!r}, expected {wanted!r}"
                    )
                    raise SystemExit(msg)
    print(f"{tag}: all packages at {version} with consistent cross-pins.")


def _plan(
    release: str, current: str, next_dev: str | None
) -> tuple[str, str, str, str]:
    """Validate inputs and return ``(release, dev, branch, tag)``."""
    if not is_release_version(release):
        msg = f"{release!r} is not a release version"
        raise SystemExit(msg)
    if not is_dev_version(current):
        msg = f"current version is {current!r}, expected a .dev version"
        raise SystemExit(msg)
    dev = next_dev or next_dev_version(release)
    if not is_dev_version(dev):
        msg = f"{dev!r} is not a dev version"
        raise SystemExit(msg)
    return release, dev, f"release/{release}", f"v{release}"


def _require_tools(*tools: str) -> None:
    """Exit early if any required external tool is missing from PATH."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        msg = f"required tools not found on PATH: {', '.join(missing)}"
        raise SystemExit(msg)


def _run(program: str, *args: str) -> str:
    """Run a command in the repo root, exiting cleanly on failure."""
    try:
        result = subprocess.run(
            [program, *args],
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
    except FileNotFoundError:
        msg = f"command not found: {program}"
        raise SystemExit(msg) from None
    except subprocess.CalledProcessError as error:
        details = (error.stderr or error.stdout or "").strip()
        msg = f"command failed ({program} {' '.join(args)}):\n{details}"
        raise SystemExit(msg) from error
    return result.stdout.strip()


def _github_slug(remote_url: str) -> str | None:
    """Return ``owner/repo`` from a GitHub remote URL, or None if not GitHub."""
    if "github.com" not in remote_url:
        return None
    return remote_url.removesuffix(".git").split("github.com", 1)[-1].strip(":/")


def _pr_url(branch: str) -> str | None:
    """Return the GitHub create-PR URL for a pushed branch, if origin is GitHub."""
    slug = _github_slug(_run("git", "remote", "get-url", "origin"))
    if slug is None:
        return None
    return f"https://github.com/{slug}/pull/new/{branch}"


def _make(release: str, next_dev: str | None, *, assume_yes: bool, push: bool) -> None:
    """Make the release branch: two commits, the tag, and push it."""
    _require_tools("git")
    if _run("git", "rev-parse", "--abbrev-ref", "HEAD") != "main":
        msg = "releases are cut from main"
        raise SystemExit(msg)
    if _run("git", "status", "--porcelain"):
        msg = "working tree is not clean"
        raise SystemExit(msg)
    current = read_current_version()
    release, dev, branch, tag = _plan(release, current, next_dev)
    if _run("git", "tag", "--list", tag):
        msg = f"tag {tag} already exists; delete it to redo"
        raise SystemExit(msg)

    print(f"Release {release} on {branch}, tag {tag}, then main to {dev}.")
    if not assume_yes and input("Proceed? [y/N] ").strip().lower() != "y":
        msg = "aborted"
        raise SystemExit(msg)

    _run("git", "switch", "--create", branch)
    apply_version(release)
    _run("git", "add", "--all")
    _run("git", "commit", "--message", f"Release {release}")
    _run("git", "tag", "--annotate", tag, "--message", tag)
    apply_version(dev)
    _run("git", "add", "--all")
    _run("git", "commit", "--message", "Back to development")

    if push:
        _run("git", "push", "--set-upstream", "origin", branch)
        _run("git", "push", "origin", tag)
    _run("git", "switch", "main")

    if not push:
        print(f"Built {branch} and {tag} locally (not pushed).")
        return
    location = _pr_url(branch) or f"branch {branch}"
    print(f"Pushed {branch} and {tag}.")
    print(f"Open a PR into main ({location}); merge with a merge commit, not squash.")


app = SubcommandApp()


@app.command
def make(
    version: Annotated[str, tyro.conf.Positional],
    next_dev: str | None = None,
    *,
    yes: bool = False,
    no_push: bool = False,
) -> None:
    """Branch, bump, tag, and push a release, then open the PR yourself."""
    _make(version, next_dev, assume_yes=yes, push=not no_push)


@app.command
def check(tag: Annotated[str, tyro.conf.Positional]) -> None:
    """Verify the working tree matches a release tag (run by the publish workflow)."""
    check_release(tag)


def main() -> None:
    """Entry point for the release tooling."""
    app.cli(prog="release")


if __name__ == "__main__":
    main()
