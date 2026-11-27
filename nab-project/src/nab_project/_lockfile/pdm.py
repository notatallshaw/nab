"""Write PDM project locks with resolved artifacts and install groups."""

from __future__ import annotations

import hashlib
import json
import sysconfig
from typing import TYPE_CHECKING, Any

from packaging.tags import sys_tags
from packaging.utils import canonicalize_name

from nab_provider.marker_holds import intractable_as_error
from nab_provider.value import replace

from .native import distribution_files, file_digest, install_contexts
from .native_saved import write_native_document
from .pylock import LockValidationError, project_packages

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from packaging.pylock import Package

    from nab_provider.target import ResolveTarget

    from ..lockfile import LockInput
    from .pylock import ProjectedPackage


def pdm_content_hash(document: Mapping[str, Any]) -> str:
    """Match the project inputs that PDM hashes for its native lock format."""
    project = document.get("project", {})
    settings = document.get("tool", {}).get("pdm", {})

    # PDM combines PEP 735 groups and its legacy dev-dependencies before hashing.
    groups = {
        canonicalize_name(key): list(values)
        for key, values in document.get("dependency-groups", {}).items()
    }
    for key, values in settings.get("dev-dependencies", {}).items():
        groups.setdefault(canonicalize_name(key), []).extend(values)

    inputs = {
        "sources": settings.get("source", []),
        "dependencies": project.get("dependencies", []),
        "dev-dependencies": groups,
        "optional-dependencies": project.get("optional-dependencies", {}),
        "requires-python": project.get("requires-python", ""),
        "resolution": settings.get("resolution", {}),
    }

    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


def _source(package: Package, project_dir: Path) -> dict[str, Any]:
    """Record the download files or the direct source chosen by nab."""
    if package.directory is not None:
        source = package.directory
        result: dict[str, Any] = {
            "path": source.path,
            "editable": bool(source.editable),
        }
        subdirectory = source.subdirectory
    elif package.vcs is not None:
        vcs = package.vcs
        result = {vcs.type: vcs.url, "revision": vcs.commit_id}
        if vcs.requested_revision is not None:
            result["ref"] = vcs.requested_revision
        subdirectory = vcs.subdirectory
    elif package.archive is not None and package.archive.url is not None:
        archive = package.archive
        result = {
            "url": package.archive.url,
            "files": [
                {
                    "url": package.archive.url,
                    "hash": file_digest(package.archive.url, archive.hashes),
                }
            ],
        }
        subdirectory = archive.subdirectory
    else:
        files = []
        # Static URLs let PDM fetch the exact files chosen by nab.
        for artifact in distribution_files(package):
            url = (
                (project_dir / artifact.path).resolve().as_uri()
                if artifact.path is not None
                else artifact.url
            )
            if url is None or url == "":
                msg = f"{artifact.filename} has no download URL or path"
                raise LockValidationError(msg)

            files.append(
                {"url": url, "hash": file_digest(artifact.filename, artifact.hashes)}
            )

        result = {"files": files}
        if package.requires_python is not None:
            result["requires_python"] = str(package.requires_python)
        return result

    if subdirectory is not None:
        result["subdirectory"] = subdirectory
    return result


def _membership(
    package: ProjectedPackage, groups: tuple[str, ...], extras: tuple[str, ...]
) -> dict[str, Any]:
    """Bind nab's group and extra markers to PDM's single group dimension."""
    contexts = list(
        install_contexts(package.marker, base=groups[0], groups=groups, extras=extras)
    )
    membership = {name for _, name, _ in contexts}
    markers = {marker for _, _, marker in contexts}

    # PDM applies a package's environment marker to every group listed on that row.
    if len(markers) != 1:
        msg = f"PDM cannot express distinct group environments for {package.name}"
        raise LockValidationError(msg)

    marker = markers.pop()
    result: dict[str, Any] = {"groups": sorted(membership)}
    if marker is not None:
        result["marker"] = marker
    return result


def _pdm_target(target: ResolveTarget) -> dict[str, str | bool]:
    """Translate the resolved Python, platform and ABI into PDM's target table."""
    spec = target.platform_spec
    if spec is None:
        # A native tag supplies the host libc or macOS deployment version.
        tags = sys_tags()
        platform = next(tags).platform
        if platform.startswith("linux_"):
            platform = next(
                (
                    tag.platform
                    for tag in tags
                    if tag.platform.startswith(("manylinux_", "musllinux_"))
                ),
                platform,
            )

        platform = platform.replace("macosx_", "macos_").replace("win_", "windows_")
        if platform == "win32":
            platform = "windows_x86"
        free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    else:
        platform = spec.platform_id
        if platform.startswith("linux_"):
            arch = platform.removeprefix("linux_")
            family = "musllinux" if spec.libc == "musl" else "manylinux"
            # A missing deployment version admits the whole libc major series.
            major, minor = spec.runs_on_libc or (
                (1, 0) if spec.libc == "musl" else (2, 0)
            )
            platform = f"{family}_{major}_{minor}_{arch}"
        elif platform.startswith("macos_") and spec.runs_on_macos:
            major, minor = spec.runs_on_macos
            platform = f"macos_{major}_{minor}_{platform.removeprefix('macos_')}"
        free_threaded = spec.free_threaded

    version = (
        target.python_full_version
        if target.concrete_micro
        else f"{target.python_version}.*"
    )
    result: dict[str, str | bool] = {
        "requires_python": f"=={version}",
        "platform": platform,
    }
    if not target.multi_implementation:
        result.update(implementation=target.implementation, gil_disabled=free_threaded)
    return result


def write_pdm_lock(
    lock_input: LockInput,
    document: Mapping[str, Any],
    *,
    project_dir: Path,
    output_path: Path | None = None,
    reproduce: bool = False,
) -> str:
    """Render a PDM lock from resolved pins and the consuming pyproject."""
    # Keep runtime dependencies addressable by PDM's default group.
    base_group = lock_input.base_group or "default"
    groups = tuple(dict.fromkeys((base_group, *lock_input.active_groups)))
    if set(groups).intersection(lock_input.extras):
        msg = "PDM requires distinct names for extras and dependency groups"
        raise LockValidationError(msg)

    packages = project_packages(replace(lock_input, base_group=base_group), project_dir)
    rows = []
    with intractable_as_error():
        for package in packages:
            row = {
                "name": package.name,
                "version": str(package.version),
                **_source(package.source, project_dir),
                **_membership(package, groups, lock_input.extras),
            }
            if package.source.dependencies is not None:
                row["dependencies"] = sorted(
                    {dep["name"] for dep in package.source.dependencies}
                )
            rows.append(row)

    # Conflict forks can share a single PDM target declaration.
    targets = {}
    for lock in lock_input.targets.values():
        target = _pdm_target(lock.target)
        targets[tuple(target.items())] = target

    content_hash = pdm_content_hash(document)
    metadata = {
        "groups": sorted({"default", *groups, *lock_input.extras}),
        "strategy": ["inherit_metadata", "static_urls"],
        "lock_version": "4.5.1",
        "content_hash": content_hash,
        "targets": list(targets.values()),
    }
    return write_native_document(
        {"metadata": metadata, "package": rows},
        lock_input,
        format_name="pdm",
        project_identity=content_hash,
        output_path=output_path,
        reproduce=reproduce,
    )
