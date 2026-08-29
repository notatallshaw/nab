"""Resolve a project's dependencies for the environments it targets.

The host hands in one :class:`~nab_provider.target.ResolveTarget` per
environment, whether it read a matrix declaring many or a bare project
declaring none.  Each gets a single-environment resolve against a shared
:class:`~nab_project.fetch.FetchCoordinator`, so a package's listing is
read once across them, a version's wheel metadata once per wheel they
pick, and an sdist's ``PKG-INFO`` once for the version.

A declared conflict, matrix or not, is where a resolve produces more than
one result for an environment.  Directly co-selecting two members of an
exclusive set forks: each member gets its own resolve and its pins carry a
membership clause, so one lock serves both.  A selection that reaches two
members only transitively (an umbrella extra or group) has no fork to
carry the second, so it is refused.

This module reads the project, plans the forks and assembles the lock
input; the search lives in :mod:`nab_project._resolve.engine`.
"""

from __future__ import annotations

import itertools
import logging
import tempfile
from collections import defaultdict
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.errors import ConfigError
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.pep508 import parse_requirement
from nab_provider.provider import ListingFilterCache
from nab_provider.requirements_file import (
    expand_extra_requirements,
    expand_group_includes,
    expand_self_extras,
    resolve_groups_to_requirements,
    self_extra_markers,
)
from nab_provider.resolver_inputs import (
    build_resolver_inputs as build_resolver_inputs,  # noqa: PLC0414  (re-export)
)
from nab_provider.target import (
    UNBOUNDABLE_MARKER_VARIABLES,
    NonIntervalMarkerError,
    ResolveTarget,
    environment_declaration,
    marker_variables,
    micro_boundary_points,
    slices_from_points,
)
from nab_resolver.errors import ResolutionError

from . import pyproject_files, toml_io
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
from .conflicts import (
    ConflictFork,
    ConflictKind,
    ConflictSelectionError,
    ConflictSet,
    conflict_forks,
    validate_conflict_exclusions,
    validate_conflict_minimums,
)
from .fetch import (
    DEFAULT_MAX_CONCURRENCY,
    FetchCoordinator,
    index_cache_floors,
    index_routes,
)
from .inputs import ResolveInputs
from .lockfile import LockInput, TargetLock

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from nab_index.transport import AsyncHttpTransport
    from nab_provider._vendor.packaging.requirements import Requirement
    from nab_provider._vendor.packaging.version import Version
    from nab_provider.provider import ResolutionStrategy
    from nab_provider.resolver_inputs import MarkerHolds


__all__ = [
    "InstallContexts",
    "ProgressSink",
    "ResolveFork",
    "ResolveResult",
    "TargetResult",
    "active_group_names",
    "build_lock_input",
    "inputs_for_build_requirements",
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


def resolve_for_targets(  # noqa: PLR0913 - the knobs of a project resolve
    path: Path,
    transport: AsyncHttpTransport,
    *,
    targets: Sequence[ResolveTarget],
    inputs: ResolveInputs | None = None,
    cache_dir: Path | None = None,
    offline: bool = False,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    build_requirements: bool = False,
    resolution_strategy: ResolutionStrategy | None = None,
    progress: ProgressSink | None = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> ResolveResult:
    """Resolve the project at ``path`` for each of ``targets``.

    ``targets`` are the environments the host chose to resolve for and
    ``inputs`` the settings it read out of the project.  ``transport`` is
    the caller's, so the HTTP library choice stays outside nab-project;
    ``cache_dir`` and ``offline`` are runtime overrides from the CLI.

    ``groups`` and ``extras`` name PEP 735 groups and
    ``[project.optional-dependencies]`` keys to fold in;
    ``resolution_strategy`` overrides ``inputs.resolution`` when set.

    ``build_requirements`` resolves ``[build-system].requires`` instead of
    the project's dependencies; neither ``groups`` nor ``extras`` mean
    anything there, so passing either raises.

    A target that cannot be resolved is a failed :class:`TargetResult`,
    not an exception, so a matrix reports every failed target, not only
    the first.  Everything else (an unreadable pyproject, a conflicting
    selection, an unsupported source) raises.
    """
    inputs = ResolveInputs() if inputs is None else inputs
    if build_requirements:
        if groups or extras:
            msg = "a build-requirements resolve has no groups or extras to select"
            raise ValueError(msg)
        inputs = inputs_for_build_requirements(inputs)

    tables = _project_tables(
        path, build_group=inputs.build_group, build_requirements=build_requirements
    )

    # ``default-groups`` is project policy: a default install activates
    # them, so they join the CLI selection.
    effective_groups = active_group_names(
        groups, inputs.default_groups, inputs.base_group
    )

    forks, base_requirements = _plan_forks(
        path,
        tables,
        inputs,
        targets,
        extras=tuple(extras),
        groups=effective_groups,
    )

    with FetchCoordinator(
        transport,
        indexes=list(inputs.indexes),
        cache_dir=cache_dir,
        offline=offline,
        index_routes=index_routes(inputs),
        index_cache_floors=index_cache_floors(inputs),
        on_fetch=progress.on_fetch if progress is not None else None,
        build_config=inputs,
        max_concurrency=max_concurrency,
    ) as coordinator:
        return resolve_with_coordinator(
            coordinator,
            targets,
            inputs=inputs,
            cache_dir=cache_dir,
            forks=forks,
            base_requirements=base_requirements,
            resolution_strategy=resolution_strategy,
            progress=progress,
        )


def resolve_with_coordinator(  # noqa: PLR0913 - the knobs of a bare resolve
    coordinator: FetchCoordinator,
    targets: Sequence[ResolveTarget],
    requirements: Sequence[Requirement] = (),
    *,
    inputs: ResolveInputs | None = None,
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

    With ``forks`` every target is resolved once per fork, each fork's
    ``selection`` stamped onto the target; without them the resolve runs
    once per target against ``requirements``.

    ``align_across_targets`` threads each target's pins forward as
    preferences for the next, so a package the matrix does not force
    apart keeps one version across targets.  ``preferences`` seeds that,
    e.g. from a previous lock.

    ``base_requirements`` are the no-member requirements (the project
    deps plus any non-conflicting selection).  When given, a final base
    pass resolves them per target; pass it only when conflict forks ran.

    ``marker_holds`` decides whether a root requirement's marker holds for
    a target's environment, defaulting to
    :func:`~nab_provider.marker_holds.dependency_marker_holds`.  A host
    with its own marker machinery passes that instead and keeps
    ``nab_markersets`` off the engine's path.
    """
    inputs = ResolveInputs() if inputs is None else inputs
    with _source_root(cache_dir, inputs) as source_root:
        settings = _EngineSettings(
            coordinator=coordinator,
            inputs=inputs,
            source_root=source_root,
            align=align_across_targets,
            resolution=(
                resolution_strategy
                if resolution_strategy is not None
                else inputs.resolution
            ),
            marker_holds=(
                dependency_marker_holds if marker_holds is None else marker_holds
            ),
            progress=progress,
            listing_filter_cache=ListingFilterCache(
                len({target.python_full_version for target in targets})
            ),
        )

        fork_list = (
            list(forks) if forks is not None else [ResolveFork((), tuple(requirements))]
        )
        constraints = [parse_requirement(text) for text in inputs.constraints]

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
    cache_dir: Path | None, inputs: ResolveInputs
) -> Iterator[Path | None]:
    """Yield the directory a declared VCS or archive source materialises under.

    With caching off there is no cache root, but the source still has to be
    materialised to read its version and dependencies, so the run gets a
    temporary directory.
    """
    if cache_dir is not None or not (inputs.vcs_sources or inputs.archive_sources):
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

    ``base-group`` may be named in ``default-groups`` to keep the project's
    own dependencies in the default selection.  Its requirements are
    ``[project].dependencies``, roots already, so it is dropped rather than
    looked up as a group.  ``groups`` keeps the name, so selecting a group
    the project does not declare still raises.
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
    inputs: ResolveInputs | None = None,
    extras: Sequence[str] = (),
    dependency_groups: Sequence[str] = (),
    created_by: str = "nab",
) -> LockInput:
    """Assemble the lock input from a finished resolve.

    Every target that resolved contributes its pins, its forward
    dependency edges, and the environment it declares (see
    :func:`_declared_environments`).  A target that failed contributes
    nothing.

    ``extras`` and ``dependency_groups`` are this run's selection;
    ``default-groups``, the conflicts and ``base-group`` are project
    policy and come from ``inputs``.
    """
    inputs = ResolveInputs() if inputs is None else inputs
    targets: dict[str, TargetLock] = {}
    consulted: dict[EnvSignature, set[Marker]] = {}
    declaring: list[ResolveTarget] = []
    for tr in result.target_results:
        if tr.lock is None:
            continue

        targets[tr.target.label] = tr.lock

        # Conflict forks repeat an environment under different selections,
        # so it is declared once, from everything every fork read.
        signature = env_signature(tr.target)
        if signature not in consulted:
            consulted[signature] = set()
            declaring.append(tr.target)
        consulted[signature] |= tr.consulted

    return LockInput(
        targets=targets,
        env_base_names=dict(result.env_base_names),
        environments=_declared_environments(declaring, consulted),
        requires_python=inputs.requires_python,
        created_by=created_by,
        extras=tuple(extras),
        dependency_groups=tuple(dependency_groups),
        default_groups=inputs.default_groups,
        conflicts=inputs.conflicts,
        base_group=inputs.base_group,
        build_group=inputs.build_group,
    )


def _declared_environments(
    declaring: Sequence[ResolveTarget],
    consulted: Mapping[EnvSignature, set[Marker]],
) -> list[Marker]:
    """Build the lock's PEP 751 ``environments``, one per environment.

    Every dependency whose marker was False on a target was dropped there,
    so an installer answering one of those markers differently needs a
    different package set.  Each declaration is built from the markers that
    target's resolve read (see
    :func:`~nab_provider.target.environment_declaration`).
    """
    # A matrix consults the same marker on every target it expands to.
    texts = {str(marker) for markers in consulted.values() for marker in markers}
    variables: set[str] = set()
    for text in texts:
        variables |= marker_variables(text)

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
    inputs: ResolveInputs, tables: _ProjectTables
) -> tuple[_ConfiguredContext, tuple[_ConfiguredContext, ...]]:
    """Split the configured install contexts into the project's and the rest.

    The project's own dependencies are always a context, named or not.  The
    build requirements are one only when ``build-group`` names them.
    """
    project = _ConfiguredContext(inputs.base_group, tuple(tables.dependencies))
    selectors = (
        (_ConfiguredContext(inputs.build_group, tuple(tables.build_requires)),)
        if inputs.build_group is not None
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


def _project_tables(
    path: Path, *, build_group: str | None, build_requirements: bool
) -> _ProjectTables:
    """Read every table the resolve needs off one parse of ``path``.

    The document is not returned, so the resolve holds the tables it reads
    rather than the whole file.
    """
    document = toml_io.load_path(path)
    if build_requirements:
        return _tables_for_build_requires(document, path)
    return _ProjectTables(
        dependencies=pyproject_files.project_dependencies(document),
        groups=pyproject_files.dependency_groups(document),
        optional=pyproject_files.project_optional_dependencies(document),
        project_name=pyproject_files.project_name(document),
        build_requires=(
            pyproject_files.build_system_requires(document, path)
            if build_group is not None
            else []
        ),
    )


def _tables_for_build_requires(
    document: Mapping[str, Any], path: Path
) -> _ProjectTables:
    """Read ``document`` as a project whose dependencies are its build requirements.

    ``[build-system].requires`` is one flat list, so the group and extra
    tables are empty, and the project name with them: it is read only to
    expand self-referencing extras.  ``path`` names the file in the errors.
    """
    return _ProjectTables(
        dependencies=pyproject_files.build_system_requires(document, path),
        groups={},
        optional={},
        project_name=None,
    )


def inputs_for_build_requirements(inputs: ResolveInputs) -> ResolveInputs:
    """Return ``inputs`` with the settings a build-requirements lock cannot use.

    ``default-groups``, ``base-group`` and the conflicts over groups and
    extras describe a selection ``[build-system].requires`` does not have,
    and ``build-group``'s roots already are the build requirements.
    """
    return inputs.replace(
        conflicts=(),
        default_groups=(),
        base_group=None,
        build_group=None,
    )


def _configured_group_names(inputs: ResolveInputs) -> tuple[str, ...]:
    """Return the group names active by configuration rather than by selection."""
    return tuple(
        name for name in (inputs.base_group, inputs.build_group) if name is not None
    )


def _plan_forks(
    path: Path,
    tables: _ProjectTables,
    inputs: ResolveInputs,
    targets: Sequence[ResolveTarget],
    *,
    extras: tuple[str, ...],
    groups: tuple[str, ...],
) -> tuple[list[ResolveFork], list[Requirement] | None]:
    """Plan the resolves a selection needs, plus their base pass.

    An engaged conflict forks: one resolve per choice of member, each
    carrying its own requirements.  A fork can only carry a member the
    selection named directly, so the exclusion check below still refuses a
    selection that reaches two members without naming either.

    The second element is the no-member requirement list, needed only when
    the plan forked; see :func:`resolve_with_coordinator`.
    """
    configured = _configured_group_names(inputs)
    project_context, selector_contexts = _configured_contexts(inputs, tables)
    if inputs.conflicts:
        _validate_conflict_members_exist(
            inputs.conflicts, tables.optional, tables.groups, configured
        )
        _check_conflict_minimums(
            inputs.conflicts,
            tables,
            extras,
            [*expand_group_includes(tables.groups, groups), *configured],
            targets,
        )

    plan = conflict_forks(extras, groups, inputs.conflicts, configured)
    forks: list[ResolveFork] = []
    # Forks of an extra-based conflict share a group selection, so the
    # group-pair scan runs once per distinct one.
    scanned_group_selections: set[tuple[str, ...]] = set()
    for fork in plan:
        if inputs.conflicts:
            _check_conflict_exclusions(
                inputs.conflicts,
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
                    name_project=inputs.base_group is not None,
                ),
            )
        )

    # With more than one fork the lock writer needs the no-member pins too.
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

    The lock writer gates each package on the selectors that reach it, so a
    package two active selections share has to name both or an install of
    one alone misses it.  The fork's own ``selection`` is a selector like
    any other.

    A group named in ``default-groups`` is included, since PEP 751 seeds
    ``dependency_groups`` from it when the installer selects none.

    A configured context is a selector no run selects, and only the fork
    that walked it gets one.
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

    ``project`` and ``selectors`` are the configured contexts this fork
    carries, empty where a declared conflict put one in another fork.
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
    fork's active sets minus its own chosen members recover it.
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

    A self-reference (``{project_name}[a, b]`` inside an extra) is walked
    transitively, and its PEP 508 marker is carried onto the
    requirements it reaches (see
    :func:`~nab_provider.requirements_file.expand_extra_requirements`).
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
    the conflict would be silently inert.  Names compare canonicalised,
    as ``conflicts`` stores them.
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
    """Return ``targets`` split at the micro boundaries a self-ref marker cuts.

    A bare-minor target's ``python_full_version`` is the synthesized
    ``{minor}.0`` floor, so a self reference gated on ``python_full_version
    >= "3.10.4"`` answers for the whole minor from that floor and a member
    reached only above the boundary is never seen.
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
    markers from branches this target never reaches; one that cannot tile the
    minor is skipped rather than raised on.  A marker a resolve does consult
    still raises, from the micro-narrowing loop.
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

    A member behind a marker-gated self reference is active only where that
    marker holds, so the closure is expanded per environment (see
    :func:`_conflict_check_targets`).  An environment on which no member is
    active fails even when another environment satisfies the policy.
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

    The closure is expanded per environment (see
    :func:`_conflict_check_targets`), so members reached under disjoint
    markers pass and two that co-activate on one environment fail.

    Each fork holds at most one member of an engaged set, so this catches
    only co-selection an umbrella extra or group reaches transitively:
    ``conflict_forks`` cannot assign a member the selection did not name to
    a fork, and leaves it in the shared base where two of them meet.
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
        # A bare requirement enters without arbitrary-string admission, so
        # subset and equality checks stay consistent with algebra-derived
        # full-bounded terms. The accumulator identity keeps admission, so
        # === literals survive their first intersection.
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
    already leaves no version on a package is left out of the pairing and
    reported by :func:`_find_self_empty_groups` instead.  The result is
    sorted by ``(left_group, right_group, package)``.
    """
    # Invert to: package -> the groups naming it directly, each with its
    # folded range and the requirement strings behind it.  Sorted order
    # makes every pair below read low-to-high.
    requirers: defaultdict[str, list[tuple[str, VersionRange, list[str]]]] = (
        defaultdict(list)
    )
    for group in sorted(per_group):
        ranges, sources = _group_package_ranges(per_group[group], environment)
        for package, package_range in ranges.items():
            if package_range.is_empty:
                continue
            requirers[package].append((group, package_range, sources[package]))

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

    A finding is reported when it holds on any target.
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
