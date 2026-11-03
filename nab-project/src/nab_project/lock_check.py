"""Check the saved solution without discovering replacement package versions."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from nab_index.file_urls import is_file_url, parse_file_url
from nab_provider._vendor.packaging.markers import Marker, prepare_environment
from nab_provider._vendor.packaging.pylock import (
    Pylock,
    PylockValidationError,
)
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.conflict_kind import EMPTY_MEMBERSHIP_SETS
from nab_provider.environment import classify_dependency
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.pep508 import parse_requirement
from nab_provider.records import select_artifact_hash
from nab_provider.tags import PlatformSpec
from nab_provider.target import micro_boundary_points, slices_from_points

from . import toml_io
from .build_backend import project_metadata
from .lock_validation import (
    VALIDATION_VERSION,
    capture_metadata,
    content_digest,
    validation_digest,
)
from .resolve import plan_project_inputs

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from nab_provider._vendor.packaging.pylock import (
        PackageArchive,
        PackageSdist,
        PackageWheel,
    )
    from nab_provider._vendor.packaging.requirements import Requirement
    from nab_provider.target import ResolveTarget

    from .inputs import ResolveInputs

__all__ = ["LockConsistencyError", "check_saved_lock"]


class LockConsistencyError(ValueError):
    """A committed solution cannot satisfy or verify the current inputs."""


def _identity(text: str) -> tuple[object, ...]:
    """Keep selection topology separate from a requirement's version bounds."""
    req = parse_requirement(text)
    return (
        canonicalize_name(req.name),
        tuple(sorted(req.extras)),
        str(req.marker or ""),
        req.url,
    )


def _same_declarations(old: Sequence[str], new: Sequence[str]) -> bool:
    return {_identity(text) for text in old} == {_identity(text) for text in new}


def _check_changed_inputs(old: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    """Admit bound edits while retaining the recorded selection topology."""
    for field in ("policy-sha256", "targets", "excluded", "build-requirements"):
        if old[field] != current[field]:
            msg = f"{field} changed; update the lock"
            raise LockConsistencyError(msg)
    if old["roots"].keys() != current["roots"].keys() or any(
        not _same_declarations(old["roots"][scope], current["roots"][scope])
        for scope in old["roots"]
    ):
        msg = "dependency selections changed; update the lock"
        raise LockConsistencyError(msg)
    if old["local"].keys() != current["local"].keys():
        msg = "local source declarations changed; update the lock"
        raise LockConsistencyError(msg)
    for name, previous in old["local"].items():
        now = current["local"][name]
        for field in ("name", "dynamic", "requires-python"):
            if previous.get(field) != now.get(field):
                msg = f"local metadata for {name} changed; update the lock"
                raise LockConsistencyError(msg)
        if not _same_declarations(
            previous.get("dependencies", []), now.get("dependencies", [])
        ):
            msg = f"local dependencies for {name} changed; update the lock"
            raise LockConsistencyError(msg)
        before = previous.get("optional-dependencies", {})
        after = now.get("optional-dependencies", {})
        if before.keys() != after.keys() or any(
            not _same_declarations(before[k], after[k]) for k in before
        ):
            msg = f"local extras for {name} changed; update the lock"
            raise LockConsistencyError(msg)


def _check_local_artifacts(lock: Pylock, base: Path) -> None:
    """Mutable files must still match the hashes reviewed in the lock."""
    checked: dict[tuple[Path, str], str] = {}
    for package in lock.packages:
        artifacts: list[PackageWheel | PackageSdist | PackageArchive] = list(
            package.wheels or ()
        )
        artifacts.extend(
            item for item in (package.sdist, package.archive) if item is not None
        )
        for artifact in artifacts:
            path = base / artifact.path if artifact.path is not None else None
            if path is None and artifact.url is not None and is_file_url(artifact.url):
                path = parse_file_url(artifact.url)
            if path is None:
                continue
            selected_hash = select_artifact_hash(tuple(artifact.hashes.items()))
            if selected_hash is None:
                msg = f"no supported hash for local artifact {path}"
                raise LockConsistencyError(msg)
            algorithm, expected = selected_hash
            key = (path, algorithm)
            actual = checked.get(key)
            if actual is None:
                digest = hashlib.new(algorithm)
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                actual = checked[key] = digest.hexdigest()
            if actual != expected:
                msg = f"local artifact changed: {path}"
                raise LockConsistencyError(msg)


def _record(document: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Accept evidence only when both the record and install content are intact."""
    tool = document.get("tool", {})
    nab: Mapping[str, Any] = tool.get("nab", {}) if isinstance(tool, dict) else {}
    record = nab.get("validation") if isinstance(nab, dict) else None
    if not isinstance(record, dict) or record.get("version") != VALIDATION_VERSION:
        return None
    content = {key: value for key, value in record.items() if key != "record-sha256"}
    if validation_digest(content) != record.get("record-sha256"):
        msg = "validation record changed; update the lock"
        raise LockConsistencyError(msg)
    if content_digest(document) != record.get("contents-sha256"):
        msg = "validation record does not match the lock contents"
        raise LockConsistencyError(msg)
    if validation_digest(record["inputs"]) != record["inputs-sha256"]:
        msg = "validation inputs do not match their digest"
        raise LockConsistencyError(msg)
    return record


def _local_metadata(
    metadata: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
    inputs: ResolveInputs,
) -> dict[str, Mapping[str, Any]]:
    """Refresh static local declarations while retaining immutable package metadata."""
    result = dict(metadata)
    for name, project in current["local"].items():
        if project.get("dynamic"):
            msg = f"{name} has dynamic local metadata; update the lock"
            raise LockConsistencyError(msg)
        if name not in result or project == previous["local"].get(name):
            continue
        if any(
            override.name == name
            and (
                override.dependencies is not None
                or override.requires_python is not None
                or override.provides_extra is not None
            )
            for override in inputs.package_overrides
        ):
            msg = f"local metadata overrides for {name} require a lock update"
            raise LockConsistencyError(msg)
        parsed = project_metadata(dict(project))
        if parsed is None:
            msg = f"cannot read static local metadata for {name}"
            raise LockConsistencyError(msg)
        result[name] = capture_metadata(
            {(name, parsed.version): parsed}, {name: parsed.version}
        )[name]
    return result


def _validate_graph(
    metadata: Mapping[str, Mapping[str, Any]],
    roots: Sequence[Requirement],
    constraints: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    """Propagate extras through fixed versions; no candidate selection is required."""
    reached: dict[str, frozenset[str]] = {}
    pending = list(roots)
    prepared = prepare_environment({**environment, **EMPTY_MEMBERSHIP_SETS})
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not dependency_marker_holds(
            requirement.marker, environment
        ):
            continue
        name = canonicalize_name(requirement.name)
        package = metadata.get(name)
        if package is None:
            msg = f"locked solution is missing {name}"
            raise LockConsistencyError(msg)
        version = Version(package["version"])
        if requirement.specifier and not requirement.specifier.contains(
            version, prereleases=True
        ):
            msg = f"{name}=={version} does not satisfy {requirement}"
            raise LockConsistencyError(msg)
        extras = reached.get(name, frozenset()) | (
            frozenset(canonicalize_name(extra) for extra in requirement.extras)
            & frozenset(package["provides-extra"])
        )
        if name in reached and extras == reached[name]:
            continue
        reached[name] = extras
        for text in package["requires-dist"]:
            dependency = parse_requirement(text)
            if classify_dependency(dependency.marker, prepared, extras) is not None:
                # The parent marker was evaluated; the child has its own extras.
                dependency = parse_requirement(str(dependency))
                dependency.marker = None
                pending.append(dependency)
    _check_constraints(metadata, reached, constraints, environment)


def _check_constraints(
    metadata: Mapping[str, Mapping[str, Any]],
    reached: Mapping[str, frozenset[str]],
    constraints: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    """Constraints restrict reached packages without adding dependencies."""
    for text in constraints:
        constraint = parse_requirement(text)
        name = canonicalize_name(constraint.name)
        if name not in reached or (
            constraint.marker is not None
            and not dependency_marker_holds(constraint.marker, environment)
        ):
            continue
        version = Version(metadata[name]["version"])
        if constraint.url is not None:
            msg = "URL constraints require a lock update"
            raise LockConsistencyError(msg)
        if not constraint.specifier.contains(version, prereleases=True):
            msg = f"{name}=={version} does not satisfy constraint {constraint}"
            raise LockConsistencyError(msg)


def _case_targets(
    case: Mapping[str, Any],
    targets: Sequence[ResolveTarget],
    constraints: Sequence[str],
) -> list[ResolveTarget]:
    """Check new constraint boundaries inside the already locked micro intervals."""
    env = case["environment"]
    target = next(
        (
            target
            for target in targets
            if all(
                target.marker_env.get(key) == value
                for key, value in env.items()
                if key not in {"python_full_version", "implementation_version"}
            )
            and target.platform_spec
            == (PlatformSpec(**case["platform"]) if case["platform"] else None)
        ),
        None,
    )
    if target is None:
        msg = "locked target is no longer declared"
        raise LockConsistencyError(msg)
    if case["concrete-micro"]:
        return [target]
    points = {
        point
        for text in constraints
        for marker in (parse_requirement(text).marker,)
        if marker is not None
        for point in micro_boundary_points(target, [marker])
    }
    original = Version(env["python_full_version"])
    if original > Version(target.python_version + ".0"):
        points.add(original)
    result = []
    for sliced in slices_from_points(target, sorted(points)):
        if case["micro-clauses"] and not dependency_marker_holds(
            Marker(" and ".join(case["micro-clauses"])), sliced.marker_env
        ):
            continue
        result.append(sliced)
    return result


def _check_record(
    record: Mapping[str, Any],
    current: Mapping[str, Any],
    path: Path,
    inputs: ResolveInputs,
    targets: Sequence[ResolveTarget],
    *,
    groups: Sequence[str],
    extras: Sequence[str],
    build_requirements: bool,
) -> None:
    """Replay each recorded fork with the current bounds and static local metadata."""
    _check_changed_inputs(record["inputs"], current)
    forks, _ = plan_project_inputs(
        path,
        inputs,
        targets,
        groups=groups,
        extras=extras,
        build_requirements=build_requirements,
    )
    by_selection = {tuple(fork.selection): fork for fork in forks}
    for case in record["targets"]:
        selection = tuple(tuple(member) for member in case["selection"])
        fork = by_selection.get(selection)
        if fork is None:
            msg = "locked selections are no longer declared"
            raise LockConsistencyError(msg)
        selected = record["metadata-sets"][case["metadata-set"]]
        metadata = {name: record["metadata"][key] for name, key in selected.items()}
        for key in selected.values():
            if validation_digest(record["metadata"][key]) != key:
                msg = "recorded dependency metadata changed"
                raise LockConsistencyError(msg)
        metadata = _local_metadata(metadata, current, record["inputs"], inputs)
        for target in _case_targets(case, targets, current["constraints"]):
            _validate_graph(
                metadata, fork.requirements, current["constraints"], target.marker_env
            )


def check_saved_lock(
    lockfile: Path,
    path: Path,
    current: Mapping[str, Any],
    inputs: ResolveInputs,
    targets: Sequence[ResolveTarget],
    *,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    build_requirements: bool = False,
) -> None:
    """Validate a saved solution using artifact-bound dependency metadata."""
    if not lockfile.exists():
        msg = f"no lockfile at {lockfile} to check"
        raise LockConsistencyError(msg)
    document = toml_io.load_path(lockfile)
    try:
        lock = Pylock.from_dict(document)
        _check_local_artifacts(lock, lockfile.resolve().parent)
        record = _record(document)
        if record is not None:
            if record["cacheable"] and record["inputs"] == current:
                return
            _check_record(
                record,
                current,
                path,
                inputs,
                targets,
                groups=groups,
                extras=extras,
                build_requirements=build_requirements,
            )
            return
        msg = "no supported validation record; regenerate the lock"
        raise LockConsistencyError(msg)
    except (
        PylockValidationError,
        KeyError,
        TypeError,
        RecursionError,
    ) as exc:
        msg = f"cannot verify saved lock: {exc}"
        raise LockConsistencyError(msg) from exc
