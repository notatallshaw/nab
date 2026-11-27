"""Saved selection and local artifact evidence for native project locks."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlsplit

from packaging.markers import Marker
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from nab_index.atomic import atomic_write_text
from nab_index.file_urls import is_file_url, parse_file_url

from .. import toml_io, toml_writer
from ..lock_validation import (
    VALIDATION_VERSION,
    content_digest,
    make_validation_record,
    validation_digest,
)
from ..paths import relative_lock_path
from .pylock import LockValidationError

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from nab_provider.target import ResolveTarget

    from ..lockfile import LockInput


class _RowSelection(NamedTuple):
    """Native row numbers selected by a fork, grouped by source mutability."""

    members: tuple[tuple[str, str], ...]
    indices: tuple[int, ...]
    archives: tuple[int, ...]
    mutable: tuple[int, ...]


def _saved_artifacts(lock: LockInput, base: Path) -> tuple[dict[str, str], bool]:
    """Retain local file digests and identify locks eligible for remote-index reuse."""
    from ..lockfile import ArchivePin, IndexPin

    files: dict[str, str] = {}
    reusable = True
    seen: set[int] = set()
    for target in lock.targets.values():
        for pin in target.pins.values():
            if isinstance(pin, IndexPin):
                artifacts = (*pin.wheels, *((pin.sdist,) if pin.sdist else ()))
            else:
                reusable = False
                if isinstance(pin, ArchivePin) and is_file_url(pin.url):
                    algorithm, digest = pin.primary_digest
                    files[relative_lock_path(parse_file_url(pin.url), base)] = (
                        f"{algorithm}:{digest}"
                    )
                continue

            for artifact in artifacts:
                # Resolved targets can share the same artifact instance.
                if id(artifact) in seen:
                    continue
                seen.add(id(artifact))
                path = artifact.local_path
                if path is None and is_file_url(artifact.url):
                    path = parse_file_url(artifact.url)
                if path is not None:
                    algorithm, digest = artifact.primary_digest
                    files[relative_lock_path(path, base)] = f"{algorithm}:{digest}"
                if path is not None or urlsplit(artifact.url).scheme not in (
                    "https",
                    "http",
                ):
                    reusable = False
    return files, reusable


def _solution(lock: LockInput, data: Mapping[str, Any], base: Path) -> dict[str, Any]:
    """Refer to native package rows and retain each distinct selection once."""
    from ..lockfile import ArchivePin, IndexPin

    rows = {
        (p["name"], Version(p["version"])): i for i, p in enumerate(data["package"])
    }
    contexts: dict[_RowSelection, set[str]] = defaultdict(set)
    declarations = lock.target_environments or {}
    for label, result in lock.targets.items():
        indices: list[int] = []
        archives: list[int] = []
        mutable: list[int] = []
        for name, pin in result.pins.items():
            index = rows[canonicalize_name(name), Version(pin.version)]
            if isinstance(pin, IndexPin):
                indices.append(index)
            elif isinstance(pin, ArchivePin):
                archives.append(index)
            else:
                mutable.append(index)

        selection = _RowSelection(
            result.target.selection,
            tuple(sorted(indices)),
            tuple(sorted(archives)),
            tuple(sorted(mutable)),
        )
        contexts[selection].add(
            declarations.get(label) or result.target.environment_marker_string
        )

    targets = [
        {
            "environments": sorted(markers),
            "selection": selection.members,
            "index": selection.indices,
            "archives": selection.archives,
            "mutable": selection.mutable,
        }
        for selection, markers in sorted(contexts.items())
    ]
    files, reusable = _saved_artifacts(lock, base)
    return {
        "requires-python": lock.requires_python or "",
        "targets": targets,
        "local-files": files,
        "reusable": reusable,
    }


def uv_project_identity(document: Mapping[str, Any]) -> str:
    """Fingerprint the project fields that define the native root and its inputs."""
    project = document.get("project", {})
    return validation_digest(
        {
            "project": {
                key: project[key]
                for key in (
                    "name",
                    "version",
                    "dynamic",
                    "requires-python",
                    "dependencies",
                    "optional-dependencies",
                )
                if key in project
            },
            "dependency-groups": document.get("dependency-groups", {}),
            "build-system": document.get("build-system"),
            "uv": document.get("tool", {}).get("uv", {}),
        }
    )


def write_native_document(
    data: dict[str, Any],
    lock: LockInput,
    *,
    format_name: str,
    project_identity: str,
    output_path: Path | None,
    reproduce: bool,
) -> str:
    """Bind nab's checks to the native fields the consumer will install."""
    if reproduce:
        if output_path is None:
            msg = "--reproduce requires a lockfile path"
            raise LockValidationError(msg)
        try:
            committed = toml_io.load_path(output_path)
        except (OSError, ValueError) as exc:
            msg = f"--reproduce: cannot read {output_path}: {exc}"
            raise LockValidationError(msg) from exc
        if content_digest(data) != content_digest(committed):
            msg = f"--reproduce: lockfile {output_path} is out of date"
            raise LockValidationError(msg)
        return ""

    if lock.provenance is not None or lock.validation_inputs is not None:
        producer = lock.provenance.to_block() if lock.provenance is not None else {}
        if lock.validation_inputs is not None:
            base = output_path.parent if output_path is not None else Path.cwd()
            solution = _solution(lock, data, base)
            record = make_validation_record(data, lock.validation_inputs)
            record.update(
                {
                    "format": format_name,
                    "project": project_identity,
                    "solution-sha256": validation_digest(solution),
                }
            )
            producer.update(validation=record, solution=solution)
        data["tool"] = {"nab": producer}
    text = toml_writer.dumps(data)
    if output_path is not None:
        atomic_write_text(output_path, text)
    return text


class _SavedTarget(NamedTuple):
    """Environment coverage and the pins recorded for one install selection."""

    markers: tuple[Marker, ...]
    members: frozenset[tuple[str, str]]
    selected: Mapping[str, Version | None]
    preferred: Mapping[str, Version]


class NativeSolution:
    """Look up resolved versions by environment and conflict selection."""

    def __init__(
        self, data: Mapping[str, Any], format_name: str, exclude: Collection[str] = ()
    ) -> None:
        producer = data["tool"]["nab"]
        record = producer["validation"]
        solution = producer["solution"]
        if (
            type(record["version"]) is not int
            or record["version"] != VALIDATION_VERSION
            or record["format"] != format_name
            or record["contents-sha256"] != content_digest(data)
            or record["solution-sha256"] != validation_digest(solution)
        ):
            msg = (
                "native lock contents or saved selections changed; regenerate the lock"
            )
            raise ValueError(msg)
        self.validation: Mapping[str, Any] = record
        self.requires_python = SpecifierSet(solution["requires-python"])
        self.project_identity = record["project"]
        excluded = {canonicalize_name(name) for name in exclude}
        self.reusable: bool = solution["reusable"]
        self.local_files: Mapping[str, str] = solution["local-files"]
        packages = [
            (canonicalize_name(p["name"]), Version(p["version"]))
            for p in data["package"]
        ]
        self.targets: list[_SavedTarget] = []
        self.known_packages: frozenset[str]
        known: set[str] = set()
        for row in solution["targets"]:
            indexed = dict(packages[i] for i in row["index"])
            stable = {**indexed, **dict(packages[i] for i in row["archives"])}
            known.update(stable)
            selected: dict[str, Version | None] = dict(stable)
            selected.update((packages[i][0], None) for i in row["mutable"])
            self.targets.append(
                _SavedTarget(
                    tuple(Marker(m) for m in row["environments"]),
                    frozenset((kind, name) for kind, name in row["selection"]),
                    selected,
                    {k: v for k, v in indexed.items() if k not in excluded},
                )
            )
        self.known_packages = frozenset(known)

    def verify_files(self, directory: Path) -> None:
        for name, saved in self.local_files.items():
            algorithm, expected = saved.split(":", 1)
            digest = hashlib.new(algorithm)
            with (directory / name).open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                msg = f"local artifact changed: {name}"
                raise ValueError(msg)

    def _matching(
        self, target: ResolveTarget, members: Collection[tuple[str, str]]
    ) -> list[_SavedTarget]:
        environment = target.marker_env
        return [
            row
            for row in self.targets
            if row.members.issubset(members)
            and any(marker.evaluate(environment) for marker in row.markers)
        ]

    def versions_for(
        self,
        target: ResolveTarget,
        *,
        selection: Collection[tuple[str, str]] | None = None,
        default_groups: Collection[str] | None = None,
    ) -> dict[str, Version]:
        """Return unambiguous index choices for a fresh resolve."""
        members = set(target.selection if selection is None else selection)
        members.update(("group", group) for group in default_groups or ())
        rows = self._matching(target, members)
        return dict(rows[0].preferred) if len(rows) == 1 else {}

    def fixed_versions(
        self, target: ResolveTarget, members: Collection[tuple[str, str]]
    ) -> dict[str, Version | None]:
        """Copy the selected versions for validation of changed requirement bounds."""
        rows = self._matching(target, members)
        if len(rows) != 1:
            msg = f"cannot verify saved pins for {target.label}"
            raise ValueError(msg)
        return dict(rows[0].selected)

    def summary_versions(self) -> dict[str, Version] | None:
        """Summarize packages with stable versions across the whole lock."""
        versions: dict[str, Version] = {}
        for row in self.targets:
            for name, version in row.selected.items():
                if (
                    version is not None
                    and versions.setdefault(name, version) != version
                ):
                    return None
        return versions
