"""Orchestrate dependency resolution from a pyproject.toml file."""

from __future__ import annotations

import itertools
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nab_resolver.resolver import (
    Incompatibility,
    IncompatibilityCause,
    ResolutionError,
    Resolver,
)

from ._conflict_kind import dependency_marker_holds, membership_set_in_marker
from ._vcs_admission import admit_vcs_url
from ._vendor.packaging.markers import default_environment
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.specifiers import SpecifierSet
from ._vendor.packaging.utils import canonicalize_name
from ._vendor.packaging.version import Version
from .config import (
    ConfigError,
    ConflictFork,
    ConflictKind,
    ConflictSelectionError,
    ConflictSet,
    NabProjectConfig,
    ResolveMode,
    conflict_forks,
    index_routes_from_config,
    read_pyproject_config,
    validate_conflict_exclusions,
    validate_conflict_minimums,
)
from .fetch import FetchCoordinator
from .lockfile import LockInput, build_lock_input_from_provider
from .provider import (
    Provider,
    ResolutionStrategy,
    join_extra,
    python_axis_environment,
    split_extra,
)
from .requirements_file import (
    expand_extra_requirements,
    expand_group_includes,
    expand_self_extras,
    raise_for_unsatisfiable,
    read_pyproject_dependencies,
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
    resolve_groups_to_requirements,
    select_optional_dependencies,
)
from .universal.matrix import Matrix
from .universal.resolve import (
    ResolveFork,
    UniversalResult,
    resolve_universal,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from nab_index.transport import AsyncHttpTransport

    from .universal.matrix import MatrixTuple


__all__ = [
    "ResolutionResult",
    "UnsupportedModeError",
    "resolve_pyproject",
    "resolve_universal_pyproject",
]


_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """A finished single-environment resolution and its lock input.

    ``pins`` is the canonical-name -> :class:`Version` mapping.
    ``lock_input`` carries everything needed to write a PEP 751
    ``pylock.toml`` or a hashed ``requirements.txt`` and to download
    the chosen artefacts.
    """

    pins: dict[str, Version]
    lock_input: LockInput


class UnsupportedModeError(NotImplementedError):
    """Resolve mode requested is not handled by this entry point."""


def resolve_pyproject(  # noqa: PLR0913 - the surface mirrors the CLI; bundling into a config object would hide it
    path: Path,
    transport: AsyncHttpTransport,
    *,
    config: NabProjectConfig | None = None,
    cache_dir: Path | None = None,
    offline: bool = False,
    python_version: str | None = None,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    resolution_strategy: ResolutionStrategy | None = None,
) -> ResolutionResult:
    """Resolve a project's dependencies for a single environment.

    ``config`` defaults to :func:`read_pyproject_config(path)`. The
    caller supplies ``transport`` so the HTTP library choice stays
    outside nab-python. ``cache_dir``, ``offline`` and
    ``python_version`` are runtime overrides from the CLI.

    ``groups`` and ``extras`` name PEP 735 groups and
    ``[project.optional-dependencies]`` keys to fold in.
    ``resolution_strategy`` overrides ``config.resolution`` when set.

    Use :func:`resolve_universal_pyproject` when
    ``config.mode is ResolveMode.UNIVERSAL``. Returns a
    :class:`ResolutionResult` with ``pins`` and ``lock_input``.
    """
    if config is None:
        config = read_pyproject_config(path)

    if config.mode is not ResolveMode.SPECIFIC:
        msg = (
            f"resolve_pyproject only handles ResolveMode.SPECIFIC; got"
            f" {config.mode.value}.  Use resolve_universal_pyproject for"
            " mode = 'universal'."
        )
        raise UnsupportedModeError(msg)

    # ``default-groups`` is project policy: every default install
    # activates them, so the conflict checks and the resolve fold them
    # into the active group set alongside the CLI selection.
    effective_groups = tuple(dict.fromkeys((*groups, *config.default_groups)))

    if python_version is not None:
        effective_python = python_version
    elif config.requires_python is not None:
        effective_python = _resolve_target_python(config.requires_python)
    else:
        vi = sys.version_info
        effective_python = f"{vi.major}.{vi.minor}.{vi.micro}"

    marker_environment = _build_marker_environment(
        python_version=effective_python,
        overrides=config.marker_environment,
    )

    if config.conflicts:
        # Read each table once and reuse it across the existence check
        # and the umbrella expansion, so a conflict the selection only
        # reaches transitively is still caught without re-parsing the
        # file.  The expansion takes the target environment so a self
        # reference gated by a PEP 508 marker counts toward the exclusion
        # check only when its marker holds here; members reached through
        # disjoint markers never co-select.
        optional = read_pyproject_optional_dependencies(path)
        groups_table = read_pyproject_groups(path)
        project_name = read_pyproject_name(path)
        _validate_conflict_members_exist(config.conflicts, optional, groups_table)
        active_extras = expand_self_extras(
            optional, project_name, extras, marker_environment
        )
        active_groups = expand_group_includes(groups_table, effective_groups)
        validate_conflict_exclusions(config.conflicts, active_extras, active_groups)
        validate_conflict_minimums(config.conflicts, active_extras, active_groups)

    effective_strategy = (
        resolution_strategy if resolution_strategy is not None else config.resolution
    )

    requirements = read_pyproject_dependencies(path)
    requirements.extend(_load_group_requirements(path, effective_groups))
    requirements.extend(_load_extra_requirements(path, extras, marker_environment))
    if len(effective_groups) > 1:
        _check_group_disjointness(
            _load_group_requirements_by_group(path, effective_groups),
            environment=marker_environment,
        )
    resolver_requirements, root_extras = _build_resolver_inputs(
        requirements, config, environment=marker_environment
    )
    resolver_constraints = _build_constraints(config, environment=marker_environment)
    direct_packages = frozenset(
        name for name in resolver_requirements if split_extra(name)[1] is None
    )

    with FetchCoordinator(
        transport,
        indexes=list(config.indexes),
        cache_dir=cache_dir,
        offline=offline,
        index_routes=index_routes_from_config(config),
    ) as coordinator:
        provider = Provider(
            coordinator,
            python_version=marker_environment["python_full_version"],
            root_requirements=resolver_requirements,
            uploaded_prior_to=config.uploaded_prior_to,
            root_extras=root_extras,
            dist_policy=config.dist_policy,
            build_policy=config.build_policy,
            package_overrides=config.package_overrides,
            index_overrides=config.index_overrides,
            trust_unverified_sdist_deps=config.trust_unverified_sdist_deps,
            vcs_config=config.vcs,
            marker_environment=dict(config.marker_environment) or None,
            local_sources=list(config.local_sources) or None,
            vcs_sources=list(config.vcs_sources) or None,
            vcs_cache_dir=cache_dir / "vcs" if cache_dir is not None else None,
            build_config=config,
            resolution_strategy=effective_strategy,
            direct_packages=direct_packages,
        )

        resolver: Resolver[str, Version] = Resolver(
            provider, range_type=VersionRange, root_version="0"
        )
        try:
            raw = resolver.resolve(
                resolver_requirements, constraints=resolver_constraints
            )
        except ResolutionError as exc:
            _augment_resolution_error(exc, provider)
            raise
        pins = {k: v for k, v in raw.items() if split_extra(k)[1] is None}
        if config.local_sources or config.vcs_sources:
            _raise_for_local_vcs_python(
                provider, pins, Version(marker_environment["python_full_version"])
            )
        lock_input = build_lock_input_from_provider(
            provider,
            pins,
            requires_python=config.requires_python,
            extras=tuple(extras),
            dependency_groups=tuple(groups),
            default_groups=config.default_groups,
            indexes=config.indexes,
            resolved_keys=raw,
        )
        return ResolutionResult(pins=pins, lock_input=lock_input)


def _load_group_requirements(path: Path, selected: Sequence[str]) -> list[Requirement]:
    """Read [dependency-groups] from ``path`` and expand ``selected``."""
    if not selected:
        return []
    return _group_requirements_from_table(
        read_pyproject_groups(path), selected, path=path
    )


def _group_requirements_from_table(
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    selected: Sequence[str],
    *,
    path: Path,
) -> list[Requirement]:
    """Expand ``selected`` from an already-read group table.

    ``path`` is used only for the missing-table error message.
    """
    if not selected:
        return []
    if not groups:
        msg = (
            "groups requested but [dependency-groups] is missing from"
            f" {path}: {sorted(selected)!r}"
        )
        raise LookupError(msg)
    return resolve_groups_to_requirements(groups, selected)


def _load_group_requirements_by_group(
    path: Path, selected: Sequence[str]
) -> dict[str, list[Requirement]]:
    """Expand ``selected`` groups, keyed by group name.

    Like :func:`_load_group_requirements`, but keeps each group's
    requirements separate so a caller can name the source group.
    """
    return _group_requirements_by_group_from_table(
        read_pyproject_groups(path), selected, path=path
    )


def _group_requirements_by_group_from_table(
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    selected: Sequence[str],
    *,
    path: Path,
) -> dict[str, list[Requirement]]:
    """Per-group expansion from an already-read group table."""
    if not selected:
        return {}
    if not groups:
        msg = (
            "groups requested but [dependency-groups] is missing from"
            f" {path}: {sorted(selected)!r}"
        )
        raise LookupError(msg)
    return {
        group: resolve_groups_to_requirements(groups, [group]) for group in selected
    }


def _group_package_ranges(
    requirements: list[Requirement], environment: dict[str, str]
) -> tuple[dict[str, VersionRange], dict[str, list[str]]]:
    """Fold one group's direct requirements into per-package ranges.

    Mirrors :func:`_build_resolver_inputs` (marker filtering,
    canonicalisation, intersection); URL requirements are skipped. Also
    returns the requirement strings per package, for the conflict message.
    """
    ranges: dict[str, VersionRange] = {}
    sources: dict[str, list[str]] = defaultdict(list)
    for req in requirements:
        if req.marker is not None and not dependency_marker_holds(
            req.marker, environment
        ):
            continue
        if req.url is not None:
            continue
        name = str(canonicalize_name(req.name))
        previous = ranges.get(name, VersionRange.full())
        ranges[name] = previous & req.specifier.to_range()
        sources[name].append(str(req))
    return ranges, sources


@dataclass(frozen=True, slots=True)
class _GroupConflict:
    """One direct group-vs-group conflict on a single package.

    Group names are stored sorted so the conflict has a stable identity.
    """

    left_group: str
    right_group: str
    package: str
    left_req: str
    right_req: str


def _find_group_conflicts(
    per_group: Mapping[str, list[Requirement]],
    environment: dict[str, str],
) -> list[_GroupConflict]:
    """Return the direct group-vs-group conflicts under ``environment``.

    Only direct conflicts are caught; one that emerges through a shared
    transitive dependency falls through to the resolver. The result is
    sorted by ``(left_group, right_group, package)``.
    """
    # Invert to: package -> the groups that name it directly, each with
    # its folded range and the requirement strings behind it. Visiting
    # groups in sorted order makes every pair below read low-to-high.
    requirers: defaultdict[str, list[tuple[str, VersionRange, list[str]]]] = (
        defaultdict(list)
    )
    for group in sorted(per_group):
        ranges, sources = _group_package_ranges(per_group[group], environment)
        for package, package_range in ranges.items():
            requirers[package].append((group, package_range, sources[package]))

    # Two groups conflict on a package when their ranges cannot both
    # hold. Only groups that share a package are paired, so the work
    # scales with real overlap rather than with the number of groups.
    conflicts: list[_GroupConflict] = []
    for package, group_ranges in requirers.items():
        for left, right in itertools.combinations(group_ranges, 2):
            left_group, left_range, left_sources = left
            right_group, right_range, right_sources = right
            if (left_range & right_range).is_empty:
                conflicts.append(
                    _GroupConflict(
                        left_group=left_group,
                        right_group=right_group,
                        package=package,
                        left_req=", ".join(left_sources),
                        right_req=", ".join(right_sources),
                    )
                )

    # Sort so the first conflict, which specific mode reports, is stable.
    conflicts.sort(key=lambda c: (c.left_group, c.right_group, c.package))
    return conflicts


def _check_group_disjointness(
    per_group: Mapping[str, list[Requirement]],
    *,
    environment: dict[str, str],
) -> None:
    """Raise on the first direct conflict between two groups, naming them.

    Single-environment wrapper over :func:`_find_group_conflicts`.
    """
    for conflict in _find_group_conflicts(per_group, environment):
        msg = (
            f"Dependency groups {conflict.left_group!r} and"
            f" {conflict.right_group!r} conflict on {conflict.package!r}: group"
            f" {conflict.left_group!r} requires {conflict.left_req} but group"
            f" {conflict.right_group!r} requires {conflict.right_req}."
        )
        raise ResolutionError(msg)


def _check_group_disjointness_across_tuples(
    per_group: Mapping[str, list[Requirement]],
    tuples: Sequence[MatrixTuple],
) -> None:
    """Raise if a direct group conflict holds on any targeted tuple.

    Checks each tuple's marker environment, aggregates conflicts by
    identity, and names the affected tuples. A no-op below two groups.
    """
    affected: dict[_GroupConflict, set[str]] = defaultdict(set)
    for t in tuples:
        for conflict in _find_group_conflicts(per_group, t.environment):
            affected[conflict].add(t.label)
    if not affected:
        return
    clauses: list[str] = []
    for conflict in sorted(
        affected,
        key=lambda c: (c.left_group, c.right_group, c.package),
    ):
        labels = ", ".join(sorted(affected[conflict]))
        clauses.append(
            f"Dependency groups {conflict.left_group!r} and"
            f" {conflict.right_group!r} conflict on {conflict.package!r} for"
            f" tuple(s) {labels}: group {conflict.left_group!r} requires"
            f" {conflict.left_req} but group {conflict.right_group!r} requires"
            f" {conflict.right_req}."
        )
    raise ResolutionError("; ".join(clauses))


def _load_extra_requirements(
    path: Path,
    selected: Sequence[str],
    environment: dict[str, str] | None = None,
) -> list[Requirement]:
    """Read [project.optional-dependencies] from ``path`` and expand ``selected``.

    Self-references (``{project_name}[a, b]`` inside an extra's
    contents) are expanded transitively so that the third-party deps
    they ultimately reach reach the resolver as root requirements.
    A marker-gated self-reference is walked only when its marker holds
    under ``environment``; see :func:`expand_self_extras`.
    """
    if not selected:
        return []
    return _extra_requirements_from_table(
        read_pyproject_optional_dependencies(path),
        read_pyproject_name(path),
        selected,
        path=path,
        environment=environment,
    )


def _extra_requirements_from_table(
    optional: Mapping[str, Sequence[str]],
    project_name: str | None,
    selected: Sequence[str],
    *,
    path: Path,
    environment: dict[str, str] | None = None,
) -> list[Requirement]:
    """Expand ``selected`` extras from an already-read optional-deps table.

    See :func:`_load_extra_requirements` for the self-reference rules;
    ``path`` is used only for the missing-table error message.
    """
    if not optional:
        msg = (
            "extras requested but [project.optional-dependencies] is"
            f" missing from {path}: {sorted(selected)!r}"
        )
        raise LookupError(msg)
    expanded = expand_self_extras(optional, project_name, selected, environment)
    return select_optional_dependencies(optional, expanded, project_name)


def _validate_conflict_members_exist(
    conflicts: Sequence[ConflictSet],
    optional: Mapping[str, Sequence[str]],
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
) -> None:
    """Raise when a declared conflict names an extra/group the project lacks.

    A member naming an undeclared extra or group can never match, so
    the conflict would be silently inert.  Names compare under
    canonicalisation, matching the loaders.  The caller passes the
    already-read ``optional`` and ``groups`` tables so this does not
    re-parse pyproject.toml.
    """
    known_extras = {canonicalize_name(name) for name in optional}
    known_groups = {canonicalize_name(name) for name in groups}
    unknown: list[str] = []
    for conflict_set in conflicts:
        for member in conflict_set.members:
            known = known_extras if member.kind is ConflictKind.EXTRA else known_groups
            if member.name not in known:
                unknown.append(str(member))
    if unknown:
        joined = ", ".join(unknown)
        msg = (
            f"[tool.nab].conflicts names {joined}, which the project does not"
            " declare in [project.optional-dependencies] or [dependency-groups]"
        )
        raise ConfigError(msg)


@dataclass(frozen=True, slots=True)
class _ForkTables:
    """Pyproject tables read once and reused across every conflict fork."""

    groups: Mapping[str, Sequence[str | Mapping[str, str]]]
    optional: Mapping[str, Sequence[str]]
    project_name: str | None


def _fork_requirement_strings(
    path: Path,
    base_dependencies: Sequence[Requirement],
    fork: ConflictFork,
    tables: _ForkTables,
) -> list[str]:
    """Fold one conflict fork's active groups and extras onto the base deps.

    Each fork resolves a different slice of the selection (one member
    per engaged conflict set), so its requirement list is built
    separately rather than shared across forks.  ``tables`` carries the
    group and optional-dependency tables read once for the whole
    resolve, so the per-fork loop does not re-read the file.
    """
    requirements = list(base_dependencies)
    requirements.extend(
        _group_requirements_from_table(tables.groups, fork.active_groups, path=path)
    )
    requirements.extend(
        expand_extra_requirements(
            tables.optional, tables.project_name, fork.active_extras
        )
    )
    return [str(r) for r in requirements]


def _base_fork(reference: ConflictFork) -> ConflictFork:
    """Return the no-member fork: a reference fork minus its chosen members.

    Every fork shares the same non-conflicting base selection, so any
    fork's active sets with its own chosen members removed recover that
    base.  Resolving it names the deps that install regardless of which
    member is selected.
    """
    chosen = set(reference.selection)
    rest_extras = tuple(
        e
        for e in reference.active_extras
        if (ConflictKind.EXTRA.value, e) not in chosen
    )
    rest_groups = tuple(
        g
        for g in reference.active_groups
        if (ConflictKind.GROUP.value, g) not in chosen
    )
    return ConflictFork(
        selection=(), active_extras=rest_extras, active_groups=rest_groups
    )


def _augment_resolution_error(exc: ResolutionError, provider: Provider) -> None:
    """Append per-package NO_VERSIONS diagnostics to ``exc`` in-place.

    Walks the derivation tree carried on the exception, collects
    every package that appears in a NO_VERSIONS clause, and looks up
    the provider-side reason for each.  When at least one reason is
    available, rewrites the exception's args so that ``str(exc)``
    surfaces the diagnostics alongside the original derivation tree.
    """
    if exc.incompatibility is None:
        return
    packages: list[str] = []
    seen: set[str] = set()
    for package in _walk_no_versions_packages(exc.incompatibility):
        if package in seen:
            continue
        seen.add(package)
        packages.append(package)
    hints: list[str] = []
    for package in packages:
        reason = provider.get_no_versions_reason(package)
        if reason is not None:
            hints.append(f"{package}: {reason}")
    if not hints:
        return
    base = str(exc)
    augmented = base + "\n\nDiagnostics:\n  - " + "\n  - ".join(hints)
    exc.args = (augmented,)


def _walk_no_versions_packages(
    incompatibility: Incompatibility[Any, Any],
) -> list[str]:
    """Return package names from every NO_VERSIONS clause in the tree."""
    out: list[str] = []
    seen_ids: set[int] = set()

    def visit(node: Incompatibility[Any, Any]) -> None:
        if id(node) in seen_ids:
            return
        seen_ids.add(id(node))
        if node.cause is IncompatibilityCause.NO_VERSIONS:
            for term in node.terms:
                pkg = term.package
                if isinstance(pkg, str):
                    out.append(pkg)
        if node.cause_left is not None:
            visit(node.cause_left)
        if node.cause_right is not None:
            visit(node.cause_right)

    visit(incompatibility)
    return out


def _warn_dropped_root_marker(req: Requirement) -> None:
    """Warn when a dropped root requirement tests an extra/group membership.

    A root marker testing ``extra``, ``extras``, or ``dependency_groups``
    evaluates False at resolve time (root activates no extra or group), so the
    dep would otherwise be dropped silently.
    """
    marker_text = str(req.marker)
    if "extra ==" in marker_text or membership_set_in_marker(marker_text):
        _logger.warning(
            "Root requirement %r tests an extra or dependency-group membership "
            "marker; the dep is dropped because root activates no extra or group "
            "at resolve time. For an extra, use pkg[extra] (extras-of-package).",
            str(req),
        )


def _build_resolver_inputs(
    requirements: list[Requirement],
    config: NabProjectConfig,
    *,
    environment: dict[str, str],
) -> tuple[dict[str, VersionRange], set[tuple[str, str]]]:
    """Convert PEP 508 ``Requirement`` objects to the resolver's input shape.

    Requirements whose PEP 508 marker evaluates to ``False`` under
    ``environment`` are skipped, matching pip/uv's root-requirement
    handling.  Repeated package names are intersected into one range;
    an empty intersection raises :class:`ResolutionError`.
    """
    resolver_requirements: dict[str, VersionRange] = {}
    sources: defaultdict[str, list[str]] = defaultdict(list)
    root_extras: set[tuple[str, str]] = set()
    for req in requirements:
        if req.marker is not None and not dependency_marker_holds(
            req.marker, environment
        ):
            _warn_dropped_root_marker(req)
            continue
        if req.url is not None:
            admit_vcs_url(req.url, config.vcs)
            msg = (
                f"VCS requirement admitted by policy but resolver path is not"
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = str(canonicalize_name(req.name))
        previous = resolver_requirements.get(name, VersionRange.full())
        resolver_requirements[name] = previous & req.specifier.to_range()
        sources[name].append(str(req))
        for extra in req.extras:
            extra_key = join_extra(name, extra)
            resolver_requirements[extra_key] = VersionRange.full()
            _, normalized_extra = split_extra(extra_key)
            assert normalized_extra is not None  # join_extra always sets one
            root_extras.add((name, normalized_extra))
    raise_for_unsatisfiable(resolver_requirements, sources, kind="requirement")
    return resolver_requirements, root_extras


def _build_marker_environment(
    *,
    python_version: str,
    overrides: Mapping[str, str],
) -> dict[str, str]:
    """Return the merged PEP 508 environment used for root-marker evaluation.

    Mirrors :class:`Provider`: defaults from
    :func:`default_environment`, then ``python_version`` /
    ``python_full_version`` rewritten from the effective Python, then
    user overrides.
    """
    env: dict[str, str] = {
        key: value
        for key, value in default_environment().items()
        if isinstance(value, str)
    }
    env.update(python_axis_environment(python_version))
    env.update(overrides)
    return env


def _raise_for_local_vcs_python(
    provider: Provider,
    pins: Mapping[str, Version],
    target: Version,
) -> None:
    """Reject a local or VCS pin whose Requires-Python excludes ``target``.

    Index candidates are filtered by Requires-Python while listing; local
    and VCS sources skip that filter, so a source that rejects the resolve
    target could otherwise reach the lock. Mirrors the per-tuple check in
    :mod:`nab_python.universal.resolve`.
    """
    managed = provider.local_sources.keys() | provider.vcs_sources.keys()
    for name, version in pins.items():
        normalized = canonicalize_name(name)
        if normalized not in managed:
            continue
        spec = provider.metadata_cache[(normalized, version)].requires_python
        if spec is not None and target not in spec:
            msg = (
                f"{normalized} {version} requires Python {spec} but the resolve"
                f" targets Python {target}"
            )
            raise ResolutionError(msg)


def _validate_universal_conflict_minimums(
    conflicts: Sequence[ConflictSet],
    tables: _ForkTables,
    selected_extras: Sequence[str],
    active_groups: Sequence[str],
    tuples: Sequence[MatrixTuple],
) -> None:
    """Run the require-one minimums check per tuple, marker-aware.

    A member reached only through a marker-gated self reference is active
    only on the tuples whose environment satisfies that marker, so the
    check expands the self-extra closure against each tuple's environment.
    A tuple on which no member is active fails the policy even when another
    tuple satisfies it.
    """
    for t in tuples:
        active_extras = expand_self_extras(
            tables.optional, tables.project_name, selected_extras, t.environment
        )
        try:
            validate_conflict_minimums(conflicts, active_extras, active_groups)
        except ConflictSelectionError as exc:
            msg = f"{exc} (tuple {t.label})"
            raise ConflictSelectionError(msg) from exc


def _validate_universal_conflict_exclusions(
    conflicts: Sequence[ConflictSet],
    tables: _ForkTables,
    active_extras: Sequence[str],
    active_groups: Sequence[str],
    tuples: Sequence[MatrixTuple],
) -> None:
    """Run the at-most-one exclusion check per tuple, marker-aware.

    A self reference reaches its target only on tuples whose environment
    satisfies its marker, so the self-extra closure is expanded against each
    tuple's environment. Members reached under disjoint markers never share a
    tuple and pass; two that co-activate on one tuple fail.
    """
    expanded_groups = expand_group_includes(tables.groups, active_groups)
    for t in tuples:
        expanded_extras = expand_self_extras(
            tables.optional, tables.project_name, active_extras, t.environment
        )
        try:
            validate_conflict_exclusions(conflicts, expanded_extras, expanded_groups)
        except ConflictSelectionError as exc:
            msg = f"{exc} (tuple {t.label})"
            raise ConflictSelectionError(msg) from exc


def resolve_universal_pyproject(
    path: Path,
    *,
    config: NabProjectConfig | None = None,
    cache_dir: Path | None = None,
    transport: AsyncHttpTransport | None = None,
    offline: bool = False,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    resolution_strategy: ResolutionStrategy | None = None,
) -> UniversalResult:
    """Run a universal resolve for the project at ``path``.

    Reads ``[project].dependencies`` as the requirement list and the
    matrix declaration from ``[tool.nab.matrix]``.  Requires
    ``config.mode == ResolveMode.UNIVERSAL``.

    ``groups`` names PEP 735 dependency groups; ``extras`` names
    entries from ``[project.optional-dependencies]``.  Both are
    folded into every per-tuple resolve.  The CLI passes the
    selections through to ``merge_universal_lock_inputs`` so the
    lockfile records what produced the pin set.
    """
    if config is None:
        config = read_pyproject_config(path)
    if config.mode is not ResolveMode.UNIVERSAL:
        msg = (
            f"resolve_universal_pyproject requires mode = 'universal'; got"
            f" {config.mode.value}.  Set [tool.nab].mode = 'universal'."
        )
        raise UnsupportedModeError(msg)
    if config.matrix is None:  # pragma: no cover - guarded at config parse
        msg = "mode = 'universal' requires a [tool.nab.matrix] table"
        raise UnsupportedModeError(msg)

    base_dependencies = read_pyproject_dependencies(path)
    matrix = Matrix(
        python=config.matrix.python,
        platforms=config.matrix.platforms,
        python_order=config.matrix.python_order,
        python_patches=(
            dict(config.matrix.python_patches)
            if config.matrix.python_patches is not None
            else None
        ),
        implementations=config.matrix.implementations,
    )
    expanded_tuples = matrix.expand()
    group_table = read_pyproject_groups(path)
    tables = _ForkTables(
        groups=group_table,
        optional=read_pyproject_optional_dependencies(path),
        project_name=read_pyproject_name(path),
    )

    # ``default-groups`` is project policy: every default install
    # activates them, so the conflict checks, the fork plan, and the
    # per-fork resolves all fold them into the active group set
    # alongside the CLI selection.
    effective_groups = tuple(dict.fromkeys((*groups, *config.default_groups)))

    if config.conflicts:
        # Reuse the already-read tables for every conflict check, and
        # expand the selection so an umbrella extra or include-group
        # counts toward both the existence and the require-one checks.
        _validate_conflict_members_exist(
            config.conflicts, tables.optional, tables.groups
        )
        _validate_universal_conflict_minimums(
            config.conflicts,
            tables,
            extras,
            expand_group_includes(tables.groups, effective_groups),
            expanded_tuples,
        )

    conflict_fork_list = conflict_forks(extras, effective_groups, config.conflicts)
    forks: list[ResolveFork] = []
    # Forks of an extra-based conflict share the same group selection,
    # so dedupe to skip the (group, group)->tuple scan once it has run
    # for an active_groups tuple.
    seen_group_selections: set[tuple[str, ...]] = set()
    for fork in conflict_fork_list:
        if config.conflicts:
            # Refuse a fork that reaches two members of one exclusive set on a
            # single tuple (for example through an umbrella extra).
            _validate_universal_conflict_exclusions(
                config.conflicts,
                tables,
                fork.active_extras,
                fork.active_groups,
                expanded_tuples,
            )
        if (
            len(fork.active_groups) > 1
            and fork.active_groups not in seen_group_selections
        ):
            seen_group_selections.add(fork.active_groups)
            _check_group_disjointness_across_tuples(
                _group_requirements_by_group_from_table(
                    group_table, fork.active_groups, path=path
                ),
                expanded_tuples,
            )
        forks.append(
            ResolveFork(
                selection=fork.selection,
                requirements=_fork_requirement_strings(
                    path, base_dependencies, fork, tables
                ),
            )
        )

    # With more than one fork the lock writer needs to tell a base
    # dependency from one required by every member, so resolve the
    # no-member requirements too.
    base_requirements = None
    if len(conflict_fork_list) > 1:
        base_requirements = _fork_requirement_strings(
            path, base_dependencies, _base_fork(conflict_fork_list[0]), tables
        )

    effective_strategy = (
        resolution_strategy if resolution_strategy is not None else config.resolution
    )
    return resolve_universal(
        matrix=matrix,
        forks=forks,
        base_requirements=base_requirements,
        transport=transport,
        offline=offline,
        constraints=list(config.constraints) or None,
        cache_dir=cache_dir,
        uploaded_prior_to=config.uploaded_prior_to,
        dist_policy=config.dist_policy,
        build_policy=config.build_policy,
        package_overrides=config.package_overrides,
        index_overrides=config.index_overrides,
        vcs_config=config.vcs,
        local_sources=list(config.local_sources) or None,
        vcs_sources=list(config.vcs_sources) or None,
        vcs_cache_dir=cache_dir / "vcs" if cache_dir is not None else None,
        build_config=config,
        indexes=list(config.indexes),
        index_routes=index_routes_from_config(config) or None,
        resolution_strategy=effective_strategy.value,
    )


_PYTHON_CANDIDATE_MAJORS = (3, 4)
_PYTHON_CANDIDATE_MINORS = range(30)
_PYTHON_CANDIDATE_PATCHES = range(30)


def _resolve_target_python(specifier: str) -> str:
    """Pick a concrete Python version that satisfies ``specifier``.

    ``specifier`` is a PEP 440 specifier set (already validated at
    config-parse time, see :func:`_parse_requires_python`).  The
    resolve target is the lowest enumerated ``M.N.P`` release that the
    specifier admits: deterministic regardless of host, and matches
    the user's written intent ("lock for >=3.13" -> "use 3.13.0
    markers"; "lock for ==3.10.5" -> "use 3.10.5 markers").

    Falls back to the host Python when no enumerated candidate
    satisfies (e.g. an open-ended ``<X.Y`` specifier with no lower
    bound, or a range that admits only versions outside the candidate
    grid).  The host fallback warns via the logger so the operator can
    notice when their lockfile's ``requires-python`` field implies a
    target the resolve could not actually impersonate.
    """
    spec_set = SpecifierSet(specifier)

    # M.N.0 first so >=3.13 resolves to 3.13.0 (not 3.13.<something>).
    # filter() preserves input order, so the first match is the lowest.
    candidates: list[Version] = []
    for major in _PYTHON_CANDIDATE_MAJORS:
        for minor in _PYTHON_CANDIDATE_MINORS:
            candidates.append(Version(f"{major}.{minor}.0"))
            for patch in _PYTHON_CANDIDATE_PATCHES:
                if patch == 0:
                    continue
                candidates.append(Version(f"{major}.{minor}.{patch}"))

    matches = [str(v) for v in spec_set.filter(candidates)]
    if matches:
        return matches[0]
    vi = sys.version_info
    host = f"{vi.major}.{vi.minor}.{vi.micro}"
    _logger.warning(
        "requires-python = %r matches no enumerated CPython release;"
        " falling back to host Python %s for the resolve target",
        specifier,
        host,
    )
    return host


def _build_constraints(
    config: NabProjectConfig, *, environment: dict[str, str]
) -> dict[str, VersionRange]:
    """Parse constraint strings from config into resolver-input ranges.

    Marker-gated constraints whose marker is False in ``environment`` are
    dropped, matching the universal path and pip. Repeated package names are
    intersected into one range; an empty intersection raises
    :class:`ResolutionError`.
    """
    out: dict[str, VersionRange] = {}
    sources: defaultdict[str, list[str]] = defaultdict(list)
    for cstr in config.constraints:
        req = Requirement(cstr)
        if req.extras:
            msg = f"Constraints cannot have extras: {cstr}"
            raise ConfigError(msg)
        if req.marker is not None and not dependency_marker_holds(
            req.marker, environment
        ):
            continue
        if req.url is not None:
            admit_vcs_url(req.url, config.vcs)
            msg = (
                f"VCS constraint admitted by policy but resolver path is not"
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = str(canonicalize_name(req.name))
        out[name] = out.get(name, VersionRange.full()) & req.specifier.to_range()
        sources[name].append(cstr)
    raise_for_unsatisfiable(out, sources, kind="constraint")
    return out
