"""Check saved pins using standard Pylock selections and current declarations."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from nab_index.file_urls import is_file_url, parse_file_url
from nab_provider._vendor.packaging.markers import Marker, prepare_environment
from nab_provider._vendor.packaging.pylock import (
    Pylock,
    PylockSelectError,
    PylockValidationError,
)
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.conflict_kind import EMPTY_MEMBERSHIP_SETS
from nab_provider.environment import classify_dependency
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.pep508 import parse_requirement
from nab_provider.records import select_artifact_hash
from nab_provider.target import micro_boundary_points, slices_from_points

from . import toml_io
from .build_backend import project_metadata
from .lock_validation import (
    VALIDATION_VERSION,
    content_digest,
    selection_inputs_digest,
    validation_digest,
)
from .resolve import plan_project_inputs

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from pathlib import Path

    from nab_provider._vendor.packaging.markers import Environment
    from nab_provider._vendor.packaging.pylock import (
        PackageArchive,
        PackageSdist,
        PackageWheel,
    )
    from nab_provider._vendor.packaging.requirements import Requirement
    from nab_provider._vendor.packaging.version import Version
    from nab_provider.metadata import WheelMetadata
    from nab_provider.target import ResolveTarget

    from .inputs import ResolveInputs
    from .resolve import ResolveFork

__all__ = ["LockConsistencyError", "check_saved_lock"]


class LockConsistencyError(ValueError):
    """A saved solution cannot satisfy or verify the current inputs."""


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
            if artifact.size is not None and path.stat().st_size != artifact.size:
                message = f"local artifact has unexpected size: {path}"
                raise LockConsistencyError(message)
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


def _record(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """Check that supported fingerprints belong to this standard Pylock document."""
    tool = document.get("tool", {})
    nab: Mapping[str, Any] = tool.get("nab", {}) if isinstance(tool, dict) else {}
    record = nab.get("validation") if isinstance(nab, dict) else None
    if not isinstance(record, dict) or record.get("version") != VALIDATION_VERSION:
        message = "no supported validation record; regenerate the lock"
        raise LockConsistencyError(message)
    if content_digest(document) != record["contents-sha256"]:
        message = "validation record does not match the lock contents"
        raise LockConsistencyError(message)
    return record


def _current_local_metadata(
    current: Mapping[str, Any], inputs: ResolveInputs
) -> dict[str, WheelMetadata]:
    """Read local declarations that can be checked without provider overrides."""
    metadata = {}
    for name, project in current["local"].items():
        if any(
            override.name == name
            and (
                override.dependencies is not None
                or override.requires_python is not None
                or override.provides_extra is not None
            )
            for override in inputs.package_overrides
        ):
            message = f"local metadata overrides for {name} require a lock update"
            raise LockConsistencyError(message)
        parsed = project_metadata(dict(project))
        if parsed is None:
            message = f"cannot read static local metadata for {name}"
            raise LockConsistencyError(message)
        metadata[name] = parsed
    return metadata


def _fork_selections(
    fork: ResolveFork, inputs: ResolveInputs
) -> tuple[set[str], set[str]]:
    """Select the contexts a fork carries in one Pylock environment."""
    contexts = fork.contexts
    members = (
        contexts.selectors if contexts is not None else dict.fromkeys(fork.selection)
    )
    extras = {name for kind, name in members if kind == "extra"}
    groups = {name for kind, name in members if kind == "group"}
    if contexts is not None and contexts.project and inputs.base_group is not None:
        groups.add(inputs.base_group)
    return extras, groups


def _check_target_slices(
    lock: Pylock, target: ResolveTarget, requirements: Sequence[Requirement]
) -> Iterator[ResolveTarget]:
    """Split a target at lock and requirement marker boundaries."""
    markers = [
        *(lock.environments or ()),
        *(package.marker for package in lock.packages if package.marker is not None),
        *(
            requirement.marker
            for requirement in requirements
            if requirement.marker is not None
        ),
    ]
    if lock.requires_python:
        markers.append(
            Marker(
                " and ".join(
                    f'python_full_version {specifier.operator} "{specifier.version}"'
                    for specifier in sorted(lock.requires_python, key=str)
                )
            )
        )
    slices = (
        [target]
        if target.concrete_micro
        else slices_from_points(target, sorted(micro_boundary_points(target, markers)))
    )
    for sliced in slices:
        if lock.requires_python is not None and not lock.requires_python.contains(
            sliced.marker_env["python_full_version"], prereleases=True
        ):
            continue
        yield sliced


def _require_version(name: str, versions: Mapping[str, Version | None]) -> Version:
    """Refuse bounds we cannot verify from a stable pin or fresh local metadata."""
    version = versions[name]
    if version is None:
        message = f"cannot verify the version of {name}; regenerate the lock"
        raise LockConsistencyError(message)
    return version


def _check_fixed_solution(
    versions: dict[str, Version | None],
    local: Mapping[str, WheelMetadata],
    roots: Sequence[Requirement],
    constraints: Sequence[Requirement],
    excluded: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    """Validate current declarations against saved versions."""
    prepared = prepare_environment({**environment, **EMPTY_MEMBERSHIP_SETS})
    reached: dict[str, frozenset[str]] = {}
    versions.update(
        (name, metadata.version) for name, metadata in local.items() if name in versions
    )
    pending = [
        *roots,
        *(
            parse_requirement(name)
            for name in local
            if name in versions or name in excluded
        ),
    ]
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not dependency_marker_holds(
            requirement.marker, environment
        ):
            continue
        name = canonicalize_name(requirement.name)
        metadata = local.get(name)
        if name not in versions:
            if metadata is None or name not in excluded:
                message = f"locked solution is missing {name}"
                raise LockConsistencyError(message)
            versions[name] = metadata.version
        if metadata is not None:
            versions[name] = metadata.version
        if requirement.specifier and not requirement.specifier.contains(
            _require_version(name, versions), prereleases=True
        ):
            message = f"{name}=={versions[name]} does not satisfy {requirement}"
            raise LockConsistencyError(message)
        if metadata is None:
            continue
        extras = reached.get(name, frozenset()) | (
            frozenset(canonicalize_name(extra) for extra in requirement.extras)
            & frozenset(canonicalize_name(extra) for extra in metadata.provides_extra)
        )
        if name in reached and reached[name] == extras:
            continue
        reached[name] = extras
        pending.extend(_active_local_dependencies(metadata, extras, prepared))
    _check_constraints(versions, constraints, environment)


def _active_local_dependencies(
    metadata: WheelMetadata,
    extras: AbstractSet[str],
    environment: dict[str, str | AbstractSet[str]],
) -> Iterator[Requirement]:
    """Evaluate parent extras without carrying their markers onto the child."""
    for dependency in metadata.requires_dist:
        if classify_dependency(dependency.marker, environment, extras) is not None:
            child = parse_requirement(str(dependency))
            child.marker = None
            yield child


def _check_constraints(
    versions: Mapping[str, Version | None],
    constraints: Sequence[Requirement],
    environment: Mapping[str, str],
) -> None:
    """Apply active constraints to the saved selection without adding packages."""
    for constraint in constraints:
        name = canonicalize_name(constraint.name)
        if name not in versions or (
            constraint.marker is not None
            and not dependency_marker_holds(constraint.marker, environment)
        ):
            continue
        if constraint.url is not None:
            message = "URL constraints require a lock update"
            raise LockConsistencyError(message)
        version = _require_version(name, versions)
        if not constraint.specifier.contains(version, prereleases=True):
            message = f"{name}=={version} does not satisfy constraint {constraint}"
            raise LockConsistencyError(message)


def _pylock_environment(environment: Mapping[str, str]) -> Environment:
    """Project a target onto the standard marker environment."""
    return {
        "implementation_name": environment["implementation_name"],
        "implementation_version": environment["implementation_version"],
        "os_name": environment["os_name"],
        "platform_machine": environment["platform_machine"],
        "platform_release": environment["platform_release"],
        "platform_system": environment["platform_system"],
        "platform_version": environment["platform_version"],
        "platform_python_implementation": environment["platform_python_implementation"],
        "python_full_version": environment["python_full_version"],
        "python_version": environment["python_version"],
        "sys_platform": environment["sys_platform"],
    }


def _check_changed_bounds(
    lock: Pylock,
    path: Path,
    current: Mapping[str, Any],
    inputs: ResolveInputs,
    targets: Sequence[ResolveTarget],
    *,
    groups: Sequence[str],
    extras: Sequence[str],
    build_requirements: bool,
) -> None:
    """Validate compatible input edits using the standard package selections."""
    local = _current_local_metadata(current, inputs)
    forks, _ = plan_project_inputs(
        path,
        inputs,
        targets,
        groups=groups,
        extras=extras,
        build_requirements=build_requirements,
    )
    constraints = [parse_requirement(text) for text in current["constraints"]]
    local_requirements = [
        requirement
        for metadata in local.values()
        for requirement in metadata.requires_dist
    ]
    for fork in forks:
        selected_extras, selected_groups = _fork_selections(fork, inputs)
        for target in targets:
            for sliced in _check_target_slices(
                lock, target, [*fork.requirements, *constraints, *local_requirements]
            ):
                versions = {
                    str(package.name): package.version
                    for package, _ in lock.select(
                        environment=_pylock_environment(sliced.marker_env),
                        tags=sliced.tags.ordered,
                        extras=selected_extras,
                        dependency_groups=selected_groups,
                    )
                }
                _check_fixed_solution(
                    versions,
                    local,
                    fork.requirements,
                    constraints,
                    current["excluded"],
                    sliced.marker_env,
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
    """Validate saved pins without version discovery or a resolver transport."""
    if not lockfile.exists():
        message = f"no lockfile at {lockfile} to check"
        raise LockConsistencyError(message)
    document = toml_io.load_path(lockfile)
    try:
        lock = Pylock.from_dict(document)
        _check_local_artifacts(lock, lockfile.resolve().parent)
        record = _record(document)
        if any(project.get("dynamic") for project in current["local"].values()):
            message = "dynamic local metadata requires a lock update"
            raise LockConsistencyError(message)
        if record["all-inputs-sha256"] == validation_digest(current):
            return
        if record["selection-inputs-sha256"] != selection_inputs_digest(current):
            message = "dependency selections or policy changed; update the lock"
            raise LockConsistencyError(message)
        _check_changed_bounds(
            lock,
            path,
            current,
            inputs,
            targets,
            groups=groups,
            extras=extras,
            build_requirements=build_requirements,
        )
    except (
        PylockValidationError,
        PylockSelectError,
        KeyError,
        TypeError,
        RecursionError,
    ) as exc:
        message = f"cannot verify saved lock: {exc}"
        raise LockConsistencyError(message) from exc
