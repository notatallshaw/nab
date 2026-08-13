"""Resolve a project's dependencies for the environments it targets.

One engine serves every project.  ``[tool.nab.matrix]`` declares many
environments and a bare project declares none (the host is the target),
but either way :func:`~nab_python.config.plan_targets` hands back a list
of :class:`~nab_python.target.ResolveTarget` and each one gets a
single-environment resolve against a shared
:class:`~nab_python.fetch.FetchCoordinator`, so metadata is fetched once
across them.

A declared conflict is the one place a resolve can produce more than one
result for an environment, and it turns on what the selection reaches,
not on how many environments the project targets.  Directly co-selecting
two members of an exclusive set *forks*: each member gets its own resolve
and its pins carry a membership clause, so one lock serves both
selections.  A selection that reaches two members only transitively (an
umbrella extra or group) has no fork to carry the second, so it is
*refused*.  Both hold whether or not a matrix is declared.

This module is the host half of that: reading the project, planning the
forks, checking the declared conflicts, binding nab's own marker
predicate, and assembling the lock input.  The search itself lives in
:mod:`nab_python._resolve.engine`, which references nothing here.
"""

from __future__ import annotations

import itertools
import logging
import tempfile
from collections import defaultdict
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from nab_resolver.errors import ResolutionError

from ._resolve.engine import (
    EnvSignature,
    InstallContexts,
    ProgressSink,
    ResolveFork,
    ResolveResult,
    TargetResult,
    _EngineSettings,
    _resolve_with_micro_narrowing,
    env_signature,
)
from ._vendor.packaging.markers import Marker
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.utils import canonicalize_name
from .config import (
    ConfigError,
    ConflictFork,
    ConflictKind,
    ConflictSelectionError,
    ConflictSet,
    NabProjectConfig,
    configured_group_names,
    conflict_forks,
    index_cache_floors_from_config,
    index_routes_from_config,
    plan_targets,
    read_pyproject_config,
    validate_conflict_exclusions,
    validate_conflict_minimums,
    with_python_override,
)
from .fetch import FetchCoordinator
from .lockfile import LockInput, TargetLock
from .marker_holds import dependency_marker_holds
from .pyproject_files import (
    read_pyproject_build_requires,
    read_pyproject_dependencies,
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
)
from .requirements_file import (
    expand_extra_requirements,
    expand_group_includes,
    expand_self_extras,
    resolve_groups_to_requirements,
    self_extra_markers,
)
from .resolver_inputs import (
    build_resolver_inputs as build_resolver_inputs,  # noqa: PLC0414  (re-export)
)
from .target import (
    UNBOUNDABLE_MARKER_VARIABLES,
    NonIntervalMarkerError,
    ResolveTarget,
    environment_declaration,
    marker_variables,
    micro_boundary_points,
    slices_from_points,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from nab_index.transport import AsyncHttpTransport

    from ._vendor.packaging.version import Version
    from .provider import ResolutionStrategy
    from .resolver_inputs import MarkerHolds


__all__ = [
    "InstallContexts",
    "ProgressSink",
    "ResolveFork",
    "ResolveResult",
    "TargetResult",
    "active_group_names",
    "build_lock_input",
    "config_for_build_requirements",
    "resolve_for_targets",
    "resolve_with_coordinator",
]


_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ConfiguredContext:
    """An install context ``[tool.nab]`` configures rather than a run selects.

    ``name`` is ``None`` for the project's own dependencies when no
    ``base-group`` names them.  Unnamed they cannot be a conflict member,
    so every fork carries them.
    """

    name: str | None
    requirements: tuple[Requirement, ...]


def resolve_for_targets(  # noqa: PLR0913 - the surface mirrors the CLI; bundling into a config object would hide it
    path: Path,
    transport: AsyncHttpTransport,
    *,
    config: NabProjectConfig | None = None,
    cache_dir: Path | None = None,
    offline: bool = False,
    python_version: str | None = None,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    build_requirements: bool = False,
    resolution_strategy: ResolutionStrategy | None = None,
    progress: ProgressSink | None = None,
) -> ResolveResult:
    """Resolve the project at ``path`` for every environment it targets.

    ``config`` defaults to :func:`read_pyproject_config(path)`.  The
    caller supplies ``transport`` so the HTTP library choice stays
    outside nab-python.  ``cache_dir`` and ``offline`` are runtime
    overrides from the CLI; ``python_version`` applies
    :func:`~nab_python.config.with_python_override`, moving the resolve
    target onto that Python and leaving the rest of the environment
    alone.

    ``groups`` and ``extras`` name PEP 735 groups and
    ``[project.optional-dependencies]`` keys to fold in;
    ``resolution_strategy`` overrides ``config.resolution`` when set.

    ``build_requirements`` resolves ``[build-system].requires`` instead of
    the project's dependencies, for a lock of the environment the project
    is built in rather than the one it runs in.  Neither ``groups`` nor
    ``extras`` mean anything there, so passing either raises.

    A target that cannot be resolved is a failed :class:`TargetResult`,
    not an exception, so a matrix reports every target that failed rather
    than only the first.  Everything else (an unreadable pyproject, a
    conflicting selection, an unsupported source) raises.
    """
    if config is None:
        config = read_pyproject_config(path)
    if build_requirements:
        if groups or extras:
            msg = "a build-requirements resolve has no groups or extras to select"
            raise ValueError(msg)
        config = config_for_build_requirements(config)
    config = with_python_override(config, python_version)
    targets = plan_targets(config)

    tables = (
        _tables_for_build_requires(path)
        if build_requirements
        else _ProjectTables(
            dependencies=read_pyproject_dependencies(path),
            groups=read_pyproject_groups(path),
            optional=read_pyproject_optional_dependencies(path),
            project_name=read_pyproject_name(path),
            build_requires=(
                read_pyproject_build_requires(path)
                if config.build_group is not None
                else []
            ),
        )
    )

    # ``default-groups`` is project policy: every default install
    # activates them, so the conflict checks, the fork plan, and the
    # resolves all fold them into the active group set alongside the CLI
    # selection.
    effective_groups = active_group_names(
        groups, config.default_groups, config.base_group
    )

    forks, base_requirements = _plan_forks(
        path,
        tables,
        config,
        targets,
        extras=tuple(extras),
        groups=effective_groups,
    )

    with FetchCoordinator(
        transport,
        indexes=list(config.indexes),
        cache_dir=cache_dir,
        offline=offline,
        index_routes=index_routes_from_config(config),
        index_cache_floors=index_cache_floors_from_config(config),
        on_fetch=progress.on_fetch if progress is not None else None,
    ) as coordinator:
        return resolve_with_coordinator(
            coordinator,
            targets,
            config=config,
            cache_dir=cache_dir,
            forks=forks,
            base_requirements=base_requirements,
            resolution_strategy=resolution_strategy,
            progress=progress,
        )


def resolve_with_coordinator(  # noqa: PLR0913 - the knobs a caller drives a bare resolve with
    coordinator: FetchCoordinator,
    targets: Sequence[ResolveTarget],
    requirements: Sequence[Requirement] = (),
    *,
    config: NabProjectConfig | None = None,
    cache_dir: Path | None = None,
    forks: Sequence[ResolveFork] | None = None,
    base_requirements: Sequence[Requirement] | None = None,
    resolution_strategy: ResolutionStrategy | None = None,
    align_across_targets: bool = True,
    preferences: Mapping[str, Version] | None = None,
    progress: ProgressSink | None = None,
    marker_holds: MarkerHolds | None = None,
) -> ResolveResult:
    """Resolve ``targets`` against an already-open coordinator.

    Splitting this from :func:`resolve_for_targets` lets a caller (and
    every test) drive the engine from requirements it holds, reusing one
    coordinator across resolves and skipping the pyproject read.

    With ``forks`` every target is resolved once per fork, each fork's
    ``selection`` stamped onto the target; without them the resolve runs
    once per target against ``requirements``.

    ``align_across_targets`` threads each target's pins forward as
    preferences for the next, so a package the matrix does not force
    apart keeps one version across targets.  ``preferences`` seeds that,
    e.g. from a previous lock.

    ``base_requirements`` are the no-member requirements (the project
    deps plus any non-conflicting selection).  When given, a final base
    pass resolves them per target so the lock writer can tell a true base
    dependency from one required by every member; pass it only when
    conflict forks ran.

    ``marker_holds`` decides whether a root requirement's marker holds for
    a target's environment; it defaults to nab's own
    :func:`~nab_python.marker_holds.dependency_marker_holds`.  A host
    driving the engine with its own marker machinery passes that instead,
    and then nothing below this call needs ``packaging.markersets``.
    """
    effective = config if config is not None else NabProjectConfig()
    with _source_root(cache_dir, effective) as source_root:
        settings = _EngineSettings(
            coordinator=coordinator,
            config=effective,
            source_root=source_root,
            align=align_across_targets,
            resolution=(
                resolution_strategy
                if resolution_strategy is not None
                else effective.resolution
            ),
            marker_holds=(
                dependency_marker_holds if marker_holds is None else marker_holds
            ),
            progress=progress,
        )

        fork_list = (
            list(forks) if forks is not None else [ResolveFork((), tuple(requirements))]
        )
        constraints = [Requirement(text) for text in effective.constraints]

        return _resolve_with_micro_narrowing(
            list(targets),
            fork_list,
            constraints,
            settings,
            preferences,
            base_requirements,
        )


@contextmanager
def _source_root(
    cache_dir: Path | None, config: NabProjectConfig
) -> Iterator[Path | None]:
    """Yield the directory a declared VCS or archive source materialises under.

    With caching off there is no cache root, but the source still has to
    be materialised to read its version and dependencies, so the run gets
    a temporary directory instead.
    """
    if cache_dir is not None or not (config.vcs_sources or config.archive_sources):
        yield cache_dir
        return

    with tempfile.TemporaryDirectory(
        prefix="nab-sources-", ignore_cleanup_errors=True
    ) as scratch:
        yield Path(scratch)


def active_group_names(
    groups: Sequence[str],
    default_groups: Sequence[str],
    base_group: str | None,
) -> tuple[str, ...]:
    """Return the ``[dependency-groups]`` names this run activates, in order.

    ``base-group`` may be named in ``default-groups`` to keep the
    project's own dependencies in the default selection.  Its
    requirements are ``[project].dependencies``, which are roots already,
    so it is dropped here rather than looked up as a declared group.
    ``groups`` is this run's ``--groups`` selection and keeps the name, so
    selecting a group the project does not declare still raises.
    """
    policy = tuple(
        name
        for name in default_groups
        if base_group is None or canonicalize_name(name) != base_group
    )
    return tuple(dict.fromkeys((*groups, *policy)))


def build_lock_input(
    result: ResolveResult,
    *,
    config: NabProjectConfig | None = None,
    extras: Sequence[str] = (),
    dependency_groups: Sequence[str] = (),
    created_by: str = "nab",
) -> LockInput:
    """Assemble the lock input from a finished resolve.

    Every target that resolved contributes its pins, its forward
    dependency edges, and the environment it declares (see
    :func:`_declared_environments`).  A target that failed contributes
    nothing, so callers that want the whole matrix represented check
    ``result.success`` first.

    ``extras`` and ``dependency_groups`` are this run's selection, which
    the lock records at the top level;  ``default-groups``, the declared
    conflicts, and ``base-group`` are project policy and come from
    ``config``.
    """
    effective = config if config is not None else NabProjectConfig()
    targets: dict[str, TargetLock] = {}
    consulted: dict[EnvSignature, set[Marker]] = {}
    declaring: list[ResolveTarget] = []
    for tr in result.target_results:
        # A target with no lock is a target that did not resolve.
        if tr.lock is None:
            continue

        targets[tr.target.label] = tr.lock

        # Conflict forks repeat an environment under different
        # selections, so the environment is declared once, from
        # everything every fork of it read.
        signature = env_signature(tr.target)
        if signature not in consulted:
            consulted[signature] = set()
            declaring.append(tr.target)
        consulted[signature] |= tr.consulted

    return LockInput(
        targets=targets,
        env_base_names=dict(result.env_base_names),
        environments=_declared_environments(declaring, consulted),
        requires_python=effective.requires_python,
        created_by=created_by,
        extras=tuple(extras),
        dependency_groups=tuple(dependency_groups),
        default_groups=effective.default_groups,
        conflicts=effective.conflicts,
        base_group=effective.base_group,
        build_group=effective.build_group,
    )


def _declared_environments(
    declaring: Sequence[ResolveTarget],
    consulted: Mapping[EnvSignature, set[Marker]],
) -> list[Marker]:
    """Build the lock's PEP 751 ``environments``, one per environment.

    The pins hold for the environments the resolve targeted, so the lock
    says so: every dependency whose marker was False on a target was
    dropped there, and an installer that answers one of those markers
    differently needs a different package set.  Each declaration is built
    from the markers that target's resolve actually read (see
    :func:`~nab_python.target.environment_declaration`).

    A marker on an axis the lock cannot bound (see
    :data:`~nab_python.target.UNBOUNDABLE_MARKER_VARIABLES`) is reported:
    the lock stays open on it, so an installer whose kernel differs will
    still accept the lock, with the dep that marker gated missing.  A marker
    on ``implementation_version`` under a non-CPython target is reported the
    same way (see :func:`~nab_python.target.unboundable_variables`): the
    value there is synthetic, so the lock leaves the axis open.
    """
    variables: set[str] = set()
    for markers in consulted.values():
        for marker in markers:
            variables |= marker_variables(str(marker))

    unboundable = sorted(variables & UNBOUNDABLE_MARKER_VARIABLES)
    if unboundable:
        _logger.warning(
            "A marker in this resolve consults %s, which names the resolving"
            " machine's kernel build; the lockfile cannot declare it, so an"
            " installer whose value differs will still accept this lock and"
            " miss the dependencies that marker gates.",
            ", ".join(unboundable),
        )
    for target in declaring:
        if target.implementation == "cpython":
            continue
        consulted_names: set[str] = set()
        for marker in consulted[env_signature(target)]:
            consulted_names |= marker_variables(str(marker))
        if "implementation_version" in consulted_names:
            _logger.warning(
                "A marker in this resolve consults implementation_version on a"
                " non-CPython target; the value nab uses there is the Python"
                " level, not the interpreter's release, so the lockfile leaves"
                " that axis open and an installer whose value differs will"
                " still accept this lock and miss the dependencies that marker"
                " gates."
            )
            break
    return [
        Marker(environment_declaration(target, consulted[env_signature(target)]))
        for target in declaring
    ]


@dataclass(frozen=True, slots=True)
class _ProjectTables:
    """The pyproject tables a resolve reads, read once."""

    dependencies: list[Requirement]
    groups: Mapping[str, Sequence[str | Mapping[str, str]]]
    optional: Mapping[str, Sequence[str]]
    project_name: str | None
    build_requires: list[Requirement] = field(default_factory=list)
    """``[build-system].requires``, read only when ``[tool.nab].build-group``
    names a group for them.  A ``--build-requirements`` resolve carries them
    in ``dependencies`` instead and leaves this empty."""


def _configured_contexts(
    config: NabProjectConfig, tables: _ProjectTables
) -> tuple[_ConfiguredContext, tuple[_ConfiguredContext, ...]]:
    """Split the configured install contexts into the project's and the rest.

    The project's own dependencies are always a context, named or not.  The
    build requirements are one only when ``build-group`` names them, which
    is also the only time they were read.
    """
    project = _ConfiguredContext(config.base_group, tuple(tables.dependencies))
    selectors = (
        (_ConfiguredContext(config.build_group, tuple(tables.build_requires)),)
        if config.build_group is not None
        else ()
    )
    return project, selectors


def _carried_by(
    fork: ConflictFork,
    project: _ConfiguredContext,
    selectors: Sequence[_ConfiguredContext],
) -> tuple[tuple[Requirement, ...], tuple[_ConfiguredContext, ...]]:
    """Return the configured contexts ``fork`` resolves, project first.

    An unnamed context cannot be a conflict member, so every fork carries
    it; a named one is carried only by the fork that chose it.
    """
    carried = tuple(c for c in selectors if c.name in fork.active_configured)
    if project.name is None or project.name in fork.active_configured:
        return project.requirements, carried
    return (), carried


def _tables_for_build_requires(path: Path) -> _ProjectTables:
    """Read ``path`` as a project whose dependencies are its build requirements.

    ``[build-system].requires`` is one flat list, so the group and extra
    tables are empty, and the project name with them: it is read only to
    expand self-referencing extras.
    """
    return _ProjectTables(
        dependencies=read_pyproject_build_requires(path),
        groups={},
        optional={},
        project_name=None,
    )


def config_for_build_requirements(config: NabProjectConfig) -> NabProjectConfig:
    """Return ``config`` with the settings a build-requirements lock cannot use.

    ``default-groups`` and the conflicts declared over groups and extras
    describe a selection ``[build-system].requires`` does not have, and
    ``base-group`` names the project's own dependencies, which a build
    lock holds none of.  Left in they fail the run rather than narrow it:
    :func:`_tables_for_build_requires` supplies no group or extra table
    for them to resolve against.  ``build-group`` goes too: a lock whose
    roots already are the build requirements has no second context to
    gate them behind.
    """
    return replace(
        config,
        conflicts=(),
        default_groups=(),
        base_group=None,
        build_group=None,
    )


def _plan_forks(
    path: Path,
    tables: _ProjectTables,
    config: NabProjectConfig,
    targets: Sequence[ResolveTarget],
    *,
    extras: tuple[str, ...],
    groups: tuple[str, ...],
) -> tuple[list[ResolveFork], list[Requirement] | None]:
    """Plan the resolves a selection needs, and the base pass they need.

    An engaged conflict forks whether or not a matrix is declared: one
    resolve per choice of member, each carrying its own requirements,
    with the member stamped onto the selection so its pins land under a
    membership-gated marker.  The exclusion check below still refuses a
    selection that reaches two members of a set without directly
    selecting either (an umbrella extra or group), since a fork can only
    carry a directly-selected member.

    The second element is the no-member requirement list, needed only
    when the plan actually forked; see :func:`resolve_with_coordinator`.
    """
    configured = configured_group_names(config)
    project_context, selector_contexts = _configured_contexts(config, tables)
    if config.conflicts:
        _validate_conflict_members_exist(
            config.conflicts, tables.optional, tables.groups, configured
        )
        _check_conflict_minimums(
            config.conflicts,
            tables,
            extras,
            [*expand_group_includes(tables.groups, groups), *configured],
            targets,
        )

    plan = conflict_forks(extras, groups, config.conflicts, configured)
    forks: list[ResolveFork] = []
    # Forks of an extra-based conflict share a group selection, so the
    # (group, group) -> target scan runs once per distinct one.
    scanned_group_selections: set[tuple[str, ...]] = set()
    for fork in plan:
        if config.conflicts:
            _check_conflict_exclusions(
                config.conflicts,
                tables,
                fork.active_extras,
                (*fork.active_groups, *fork.active_configured),
                targets,
            )

        if (
            len(fork.active_groups) > 1
            and fork.active_groups not in scanned_group_selections
        ):
            scanned_group_selections.add(fork.active_groups)
            _check_group_disjointness(
                _group_requirements_by_group(tables.groups, fork.active_groups, path),
                targets,
            )

        project, carried = _carried_by(fork, project_context, selector_contexts)
        forks.append(
            ResolveFork(
                selection=fork.selection,
                requirements=tuple(
                    _fork_requirements(
                        path, tables, fork, project=project, selectors=carried
                    )
                ),
                contexts=InstallContexts(
                    project=project,
                    selectors=_selector_requirements(
                        path, tables, fork, contexts=carried
                    ),
                    name_project=config.base_group is not None,
                ),
            )
        )

    # With more than one fork the lock writer needs to tell a base
    # dependency from one required by every member, so the no-member
    # requirements are resolved too.
    base_requirements = None
    if len(plan) > 1:
        base_fork = _base_fork(plan[0])
        base_project, base_carried = _carried_by(
            base_fork, project_context, selector_contexts
        )
        base_requirements = _fork_requirements(
            path, tables, base_fork, project=base_project, selectors=base_carried
        )
    return forks, base_requirements


def _selector_requirements(
    path: Path,
    tables: _ProjectTables,
    fork: ConflictFork,
    *,
    contexts: Sequence[_ConfiguredContext],
) -> dict[tuple[str, str], tuple[Requirement, ...]]:
    """Split a fork's active extras and groups into one requirement list each.

    The lock writer walks the resolved graph from each of them to gate
    the packages only that extra or group reaches.  A member of the
    fork's own ``selection`` is a selector like any other: a package it
    shares with another active selection has to name both, or the lock
    carries only the other one's clause and an install that selects the
    member alone misses the package.

    A group named in ``default-groups`` is here like any other: PEP 751
    seeds ``dependency_groups`` from ``default-groups`` when the
    installer selects none, so the gate still holds for a default
    install.

    A configured context is a selector no run selects, and only a fork
    that carries it gets one: a fork that did not resolve the build
    requirements must not claim an install context it never walked.
    """
    selectors: dict[tuple[str, str], tuple[Requirement, ...]] = {}
    for context in contexts:
        selectors[(ConflictKind.GROUP.value, context.name)] = context.requirements
    for extra in fork.active_extras:
        member = (ConflictKind.EXTRA.value, str(canonicalize_name(extra)))
        selectors[member] = tuple(_extra_requirements(tables, [extra], path))
    for group in fork.active_groups:
        member = (ConflictKind.GROUP.value, str(canonicalize_name(group)))
        selectors[member] = tuple(_group_requirements(tables.groups, [group], path))
    return selectors


def _fork_requirements(
    path: Path,
    tables: _ProjectTables,
    fork: ConflictFork,
    *,
    project: Sequence[Requirement],
    selectors: Sequence[_ConfiguredContext],
) -> list[Requirement]:
    """Fold one fork's contexts, groups and extras into one requirement list.

    Each fork resolves a different slice of the selection (one member per
    engaged conflict set), so its requirement list is built separately
    rather than shared.  ``project`` and ``selectors`` are the configured
    contexts this fork carries, empty where a declared conflict put one in
    another fork.
    """
    requirements = list(project)
    for context in selectors:
        requirements.extend(context.requirements)
    requirements.extend(_group_requirements(tables.groups, fork.active_groups, path))
    requirements.extend(_extra_requirements(tables, fork.active_extras, path))
    return requirements


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
    rest_configured = tuple(
        g
        for g in reference.active_configured
        if (ConflictKind.GROUP.value, g) not in chosen
    )
    return ConflictFork(
        selection=(),
        active_extras=rest_extras,
        active_groups=rest_groups,
        active_configured=rest_configured,
    )


def _group_requirements(
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    selected: Sequence[str],
    path: Path,
) -> list[Requirement]:
    """Expand ``selected`` PEP 735 groups from an already-read table.

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


def _group_requirements_by_group(
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    selected: Sequence[str],
    path: Path,
) -> dict[str, list[Requirement]]:
    """Like :func:`_group_requirements`, but keyed by the source group."""
    return {group: _group_requirements(groups, [group], path) for group in selected}


def _extra_requirements(
    tables: _ProjectTables, selected: Sequence[str], path: Path
) -> list[Requirement]:
    """Flatten ``selected`` extras to requirements.

    A self-reference (``{project_name}[a, b]`` inside an extra's contents)
    is walked transitively, and its PEP 508 marker is carried onto the
    requirements it reaches, so a marker-gated self-reference activates
    its extra only on the targets whose environment satisfies it (see
    :func:`~nab_python.requirements_file.expand_extra_requirements`).
    """
    if not selected:
        return []
    if not tables.optional:
        msg = (
            "extras requested but [project.optional-dependencies] is"
            f" missing from {path}: {sorted(selected)!r}"
        )
        raise LookupError(msg)
    return expand_extra_requirements(tables.optional, tables.project_name, selected)


def _validate_conflict_members_exist(
    conflicts: Sequence[ConflictSet],
    optional: Mapping[str, Sequence[str]],
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    configured_groups: Sequence[str] = (),
) -> None:
    """Raise when a declared conflict names an extra/group the project lacks.

    A member naming an undeclared extra or group can never match, so
    the conflict would be silently inert.  Names compare under
    canonicalisation, matching the loaders.
    """
    known_extras = {canonicalize_name(name) for name in optional}
    known_groups = {canonicalize_name(name) for name in groups} | {
        canonicalize_name(name) for name in configured_groups
    }
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
            " declare in [project.optional-dependencies] or [dependency-groups],"
            " and which is not [tool.nab].base-group or [tool.nab].build-group"
        )
        raise ConfigError(msg)


def _conflict_check_targets(
    tables: _ProjectTables,
    selected_extras: Sequence[str],
    targets: Sequence[ResolveTarget],
) -> list[ResolveTarget]:
    """Return ``targets`` split at the micros a self-ref marker cuts them at.

    A bare-minor target's ``python_full_version`` is the synthesized
    ``{minor}.0`` floor, so a self reference gated on ``python_full_version
    >= "3.10.4"`` answers for the whole minor by how that floor reads it, and
    a member reached only above the boundary is never seen.  One slice per
    boundary reads the closure where each answer holds.
    """
    markers = self_extra_markers(tables.optional, tables.project_name, selected_extras)
    return [
        sliced
        for target in targets
        for sliced in slices_from_points(target, _tileable_points(target, markers))
    ]


def _tileable_points(target: ResolveTarget, markers: Sequence[Marker]) -> list[Version]:
    """Return the boundaries the tileable ``markers`` cut ``target``'s minor at.

    The self-extra closure is walked without an environment, so it carries
    markers from branches this target never reaches; one of those that cannot
    tile the minor is skipped rather than raised on.  A marker a resolve does
    consult still raises, in :func:`_resolve_with_micro_narrowing`.
    """
    points: set[Version] = set()
    for marker in markers:
        with suppress(NonIntervalMarkerError):
            points.update(micro_boundary_points(target, [marker]))
    return sorted(points)


def _check_conflict_minimums(
    conflicts: Sequence[ConflictSet],
    tables: _ProjectTables,
    selected_extras: Sequence[str],
    active_groups: Sequence[str],
    planned: Sequence[ResolveTarget],
) -> None:
    """Run the require-one minimums check per target, marker-aware.

    A member reached only through a marker-gated self reference is active
    only on the targets whose environment satisfies that marker, so the
    check expands the self-extra closure against each environment the
    ``planned`` targets cover (see :func:`_conflict_check_targets`).  An
    environment on which no member is active fails the policy even when
    another one satisfies it.
    """
    targets = _conflict_check_targets(tables, selected_extras, planned)
    for target in targets:
        active_extras = expand_self_extras(
            tables.optional, tables.project_name, selected_extras, target.marker_env
        )
        try:
            validate_conflict_minimums(conflicts, active_extras, active_groups)
        except ConflictSelectionError as exc:
            raise _named_for_target(exc, target, targets) from exc


def _check_conflict_exclusions(
    conflicts: Sequence[ConflictSet],
    tables: _ProjectTables,
    active_extras: Sequence[str],
    active_groups: Sequence[str],
    planned: Sequence[ResolveTarget],
) -> None:
    """Run the at-most-one exclusion check per target, marker-aware.

    A self reference reaches its extra only where its marker holds, so the
    self-extra closure is expanded against each environment the ``planned``
    targets cover (see :func:`_conflict_check_targets`).  Members reached
    under disjoint markers never share an environment and pass; two that
    co-activate on one fail.

    This runs once per fork, where each fork holds at most one member of
    an engaged set, so it only catches co-selection an umbrella extra or
    group reaches transitively: a member not directly selected cannot be
    assigned to a fork, so ``conflict_forks`` leaves it in the shared
    base where two of them meet.  Directly co-selecting two members forks
    instead of raising here.
    """
    expanded_groups = expand_group_includes(tables.groups, active_groups)
    targets = _conflict_check_targets(tables, active_extras, planned)
    for target in targets:
        expanded_extras = expand_self_extras(
            tables.optional, tables.project_name, active_extras, target.marker_env
        )
        try:
            validate_conflict_exclusions(conflicts, expanded_extras, expanded_groups)
        except ConflictSelectionError as exc:
            raise _named_for_target(exc, target, targets) from exc


def _named_for_target(
    exc: ConflictSelectionError,
    target: ResolveTarget,
    targets: Sequence[ResolveTarget],
) -> ConflictSelectionError:
    """Name the offending tuple, when there is more than one to name."""
    if len(targets) == 1:
        return exc
    return ConflictSelectionError(f"{exc} (tuple {target.label})")


def _group_package_ranges(
    requirements: list[Requirement], environment: Mapping[str, str]
) -> tuple[dict[str, VersionRange], dict[str, list[str]]]:
    """Fold one group's direct requirements into per-package ranges.

    Mirrors :func:`build_resolver_inputs` (marker filtering,
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
        # A bare requirement enters the solver without arbitrary-string
        # admission, keeping subset and equality checks consistent with
        # algebra-derived full-bounded terms. The accumulator identity stays
        # arbitrary-admitting so === literals survive their first intersection.
        previous = ranges.get(name, VersionRange.full())
        term = (
            req.specifier.to_range()
            if req.specifier
            else VersionRange.full(admit_arbitrary=False)
        )
        ranges[name] = previous & term
        sources[name].append(str(req))
    return ranges, sources


@dataclass(frozen=True, slots=True)
class _SelfEmptyGroup:
    """One group whose own requirements on a package leave no version."""

    group: str
    package: str
    reqs: str


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


def _find_self_empty_groups(
    per_group: Mapping[str, list[Requirement]],
    environment: Mapping[str, str],
) -> list[_SelfEmptyGroup]:
    """Return the groups whose own requirements on a package leave no version.

    The result is sorted by ``(group, package)``.
    """
    self_empty: list[_SelfEmptyGroup] = []
    for group in sorted(per_group):
        ranges, sources = _group_package_ranges(per_group[group], environment)
        self_empty.extend(
            _SelfEmptyGroup(
                group=group,
                package=package,
                reqs=", ".join(sources[package]),
            )
            for package in sorted(ranges)
            if ranges[package].is_empty
        )
    return self_empty


def _find_group_conflicts(
    per_group: Mapping[str, list[Requirement]],
    environment: Mapping[str, str],
) -> list[_GroupConflict]:
    """Return the direct group-vs-group conflicts under ``environment``.

    Only direct conflicts are caught; one that emerges through a shared
    transitive dependency falls through to the resolver.  A group that
    already leaves no version on a package is left out of the pairing,
    since no other group can be the cause; it is reported by name from
    :func:`_find_self_empty_groups` instead.  The result is sorted by
    ``(left_group, right_group, package)``.
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
            if package_range.is_empty:
                continue
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

    # Sort so the reported conflict order is stable.
    conflicts.sort(key=lambda c: (c.left_group, c.right_group, c.package))
    return conflicts


def _tuple_scope(labels: set[str], targets: Sequence[ResolveTarget]) -> str:
    """Name the tuples a finding holds on, when there is more than one."""
    if len(targets) == 1:
        return ""
    return f" for tuple(s) {', '.join(sorted(labels))}"


def _check_group_disjointness(
    per_group: Mapping[str, list[Requirement]],
    targets: Sequence[ResolveTarget],
) -> None:
    """Raise on a group that cannot hold on its own, or on a conflicting pair.

    A finding is reported when it holds on any target; the offending
    tuples are named when there is more than one to name.  A group that
    leaves no version on its own is named alone, since no other group
    can be the cause.
    """
    self_empty: dict[_SelfEmptyGroup, set[str]] = defaultdict(set)
    affected: dict[_GroupConflict, set[str]] = defaultdict(set)
    for target in targets:
        for empty in _find_self_empty_groups(per_group, target.marker_env):
            self_empty[empty].add(target.label)
        for conflict in _find_group_conflicts(per_group, target.marker_env):
            affected[conflict].add(target.label)

    if not self_empty and not affected:
        return

    clauses: list[str] = []
    for empty in sorted(self_empty, key=lambda e: (e.group, e.package)):
        where = _tuple_scope(self_empty[empty], targets)
        clauses.append(
            f"Dependency group {empty.group!r} has conflicting requirements on"
            f" {empty.package!r}{where}: {empty.reqs}."
        )

    for conflict in sorted(
        affected,
        key=lambda c: (c.left_group, c.right_group, c.package),
    ):
        where = _tuple_scope(affected[conflict], targets)
        clauses.append(
            f"Dependency groups {conflict.left_group!r} and"
            f" {conflict.right_group!r} conflict on {conflict.package!r}{where}:"
            f" group {conflict.left_group!r} requires {conflict.left_req} but group"
            f" {conflict.right_group!r} requires {conflict.right_req}."
        )

    raise ResolutionError("; ".join(clauses))
