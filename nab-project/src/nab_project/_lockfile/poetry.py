"""Produce Poetry's project lock from a finished resolution."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from nab_index.atomic import atomic_write_text
from nab_index.file_urls import is_file_url, parse_file_url
from nab_markersets.markersets import MarkerSet
from nab_provider.marker_holds import intractable_as_error
from nab_provider.requirements_file import expand_extra_requirements
from nab_provider.value import replace

from .. import toml_writer
from ..paths import relative_lock_path
from ..pyproject_files import project_name, project_optional_dependencies
from .native import distribution_files, file_digest, install_contexts
from .pylock import LockValidationError, project_packages

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from packaging.pylock import Package

    from ..lockfile import LockInput
    from .pylock import ProjectedPackage


def _content_hash(document: Mapping[str, Any]) -> str:
    """Compute Poetry's freshness hash, including legacy Poetry declarations."""
    project = document.get("project", {})
    relevant = {
        key: project[key]
        for key in ("requires-python", "dependencies", "optional-dependencies")
        if key in project
    }
    groups = document.get("dependency-groups", {})

    poetry = document.get("tool", {}).get("poetry", {})
    legacy = ("dependencies", "source", "extras", "dev-dependencies")
    settings = {key: poetry[key] for key in (*legacy, "group") if key in poetry}

    # Poetry hashes legacy-only dependencies without a containing tool.poetry table.
    if relevant or groups:
        content: dict[str, Any] = {"tool": {"poetry": settings}}
        if relevant:
            content["project"] = relevant
        if groups:
            content["dependency-groups"] = groups
    else:
        content = {**dict.fromkeys(legacy), **settings}

    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def _source(
    package: Package, repositories: Mapping[str, str], lock_dir: Path
) -> dict[str, Any]:
    """Select the source that Poetry's installer will open for this release."""
    if package.directory is not None:
        directory = package.directory
        source = {"type": "directory", "url": directory.path}
        fields: dict[str, Any] = {"develop": bool(directory.editable), "files": []}
        subdirectory = directory.subdirectory
    elif package.vcs is not None:
        vcs = package.vcs
        if vcs.type != "git":
            msg = (
                f"Poetry cannot install {package.name} from {vcs.type}. "
                "Use --format pdm or a Git source."
            )
            raise LockValidationError(msg)
        if vcs.url is None or vcs.url == "":
            msg = f"Poetry needs a Git repository URL for {package.name}"
            raise LockValidationError(msg)

        source = {
            "type": "git",
            "url": vcs.url,
            "reference": vcs.requested_revision or vcs.commit_id,
            "resolved_reference": vcs.commit_id,
        }
        fields = {"files": []}
        subdirectory = vcs.subdirectory
    elif package.archive is not None:
        archive = package.archive
        if (
            archive.subdirectory is not None
            and PurePosixPath(archive.subdirectory).parts
        ):
            msg = (
                f"Poetry builds {package.name} from the archive root and cannot select "
                f"archive subdirectory {archive.subdirectory!r}. Use --format pdm "
                "or publish an archive rooted at the package."
            )
            raise LockValidationError(msg)

        url = archive.url or ""
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        if is_file_url(url):
            path = relative_lock_path(parse_file_url(url), lock_dir)
            source = {"type": "file", "url": path}
        else:
            source = {"type": "url", "url": url}

        return {
            "source": source,
            "files": [
                {"file": filename, "hash": file_digest(filename, archive.hashes)}
            ],
        }
    else:
        return _registry_source(package, repositories)

    if subdirectory is not None:
        source["subdirectory"] = subdirectory
    return {**fields, "source": source}


def _registry_source(
    package: Package, repositories: Mapping[str, str]
) -> dict[str, Any]:
    """Select the configured Poetry repository or the only available artifact."""
    artifacts = list(distribution_files(package))
    result: dict[str, Any] = {
        "files": [
            {
                "file": artifact.filename,
                "hash": file_digest(artifact.filename, artifact.hashes),
            }
            for artifact in artifacts
        ]
    }

    index = (package.index or "").rstrip("/")
    if index in repositories:
        result["source"] = {
            "type": "legacy",
            "url": index,
            "reference": repositories[index],
        }
    elif index != "https://pypi.org/simple":
        if len(artifacts) != 1:
            msg = f"declare {index!r} in tool.poetry.source for {package.name}"
            raise LockValidationError(msg)

        # A single artifact can be installed without a named Poetry repository.
        artifact = artifacts[0]
        result["source"] = (
            {"type": "file", "url": artifact.path}
            if artifact.path is not None
            else {"type": "url", "url": artifact.url}
        )
    return result


def _selection(
    package: ProjectedPackage, groups: tuple[str, ...], extras: tuple[str, ...]
) -> dict[str, Any]:
    """Translate the package's reachability into one marker per Poetry group."""
    markers: dict[str, MarkerSet] = {}
    optional = True
    for kind, name, condition in install_contexts(
        package.marker, base=groups[0], groups=groups, extras=extras
    ):
        region = (
            MarkerSet.from_marker(condition)
            if condition is not None
            else MarkerSet.full()
        )
        # Poetry represents project extras as markers within its main group.
        if kind == "extra":
            group = "main"
            region &= MarkerSet.from_marker(f'extra == "{name}"')
        else:
            optional = False
            group = name

        markers[group] = markers[group] | region if group in markers else region

    result: dict[str, Any] = {"optional": optional, "groups": sorted(markers)}
    conditions = {
        group: text
        for group, region in markers.items()
        if (text := region.to_marker_string()) is not None
    }
    if conditions:
        result["markers"] = conditions
    return result


def write_poetry_lock(
    lock_input: LockInput,
    document: Mapping[str, Any],
    *,
    project_dir: Path,
    output_path: Path | None = None,
) -> str:
    """Write Poetry 2.1 installation groups, source references and input hash."""
    base = lock_input.base_group or "main"
    groups = tuple(dict.fromkeys((base, *lock_input.active_groups)))

    repositories = {
        source["url"].rstrip("/"): source["name"]
        for source in document.get("tool", {}).get("poetry", {}).get("source", ())
        if "url" in source
    }

    directory = output_path.parent if output_path else project_dir
    packages = project_packages(replace(lock_input, base_group=base), directory)
    rows = []
    with intractable_as_error():
        for package in packages:
            row = {
                "name": package.name,
                "version": str(package.version),
                "description": "",
                "python-versions": str(package.source.requires_python or "*"),
                **_selection(package, groups, lock_input.extras),
                **_source(package.source, repositories, directory),
            }
            if package.source.dependencies is not None:
                row["dependencies"] = {
                    dep["name"]: "*" for dep in package.source.dependencies
                }
            rows.append(row)

    data: dict[str, Any] = {
        "package": rows,
        "metadata": {
            "lock-version": "2.1",
            "python-versions": lock_input.requires_python or "*",
            "content-hash": _content_hash(document),
        },
    }

    if lock_input.extras:
        optional = project_optional_dependencies(document)
        name = project_name(document)
        # Poetry validates requested extras against the names in this table.
        extra_roots = {}
        for extra in lock_input.extras:
            requirements = expand_extra_requirements(optional, name, [extra])
            extra_roots[extra] = sorted(
                {requirement.name for requirement in requirements}
            )
        data["extras"] = extra_roots

    text = toml_writer.dumps(data)
    if output_path is not None:
        atomic_write_text(output_path, text)
    return text
