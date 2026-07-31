"""Resolve a project's dependencies for the environments it targets.

One engine serves every project.  ``[tool.nab.matrix]`` declares many
environments and a bare project declares none (the host is the target),
but either way :func:`~nab_python.config.plan_targets` hands back a list
of :class:`~nab_python.target.ResolveTarget` and each one gets a
single-environment resolve against a shared
:class:`~nab_python.fetch.FetchCoordinator`, so metadata is fetched once
across them.

A declared conflict is the one place the two differ, and it differs by
what the project declared rather than by how many targets it has: a
matrix *forks* (it resolves each conflicting member separately and marks
the pins with a membership clause, so one lock serves both selections),
while a project resolving for a single environment *refuses* a selection
that activates two members of one exclusive set, because a single
environment's lock has nowhere to put the second one.
"""

from __future__ import annotations

import itertools
import logging
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from nab_index.cache import ARCHIVE_BUCKET, VCS_BUCKET
from nab_resolver.resolver import (
    Incompatibility,
    IncompatibilityCause,
    ResolutionError,
    Resolver,
    ResolverObserver,
)

from ._conflict_kind import dependency_marker_holds, membership_set_in_marker
from ._vcs_admission import admit_vcs_url
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
    conflict_forks,
    index_routes_from_config,
    plan_targets,
    read_pyproject_config,
    validate_conflict_exclusions,
    validate_conflict_minimums,
    with_python_override,
)
from .fetch import FetchCoordinator
from .lockfile import LockInput, TargetLock, build_target_lock
from .provider import (
    ListingFilterCache,
    Provider,
    ResolutionStrategy,
    join_extra,
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
    self_extra_markers,
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


__all__ = [
    "InstallContexts",
    "ProgressSink",
    "ResolveFork",
    "ResolveResult",
    "TargetResult",
    "build_lock_input",
    "resolve_for_targets",
    "resolve_with_coordinator",
]


_logger = logging.getLogger(__name__)

# One environment, as a hashable key: two targets that differ only by
# their conflict-fork selection share it.
EnvSignature = tuple[tuple[str, str], ...]


class ProgressSink(Protocol):
    """What the engine reports resolve progress to; the CLI implements it.

    ``on_fetch`` fires once per package listing fetched (from the fetcher
    thread); ``on_pin`` reports the current count of decided packages (from
    the resolving thread).  Both are best-effort display hooks.
    """

    def on_fetch(self) -> None:
        """Record that one package listing has been fetched."""

    def on_pin(self, decided: int) -> None:
        """Record the current count of decided (pinned) packages."""


class _ResolveObserver(ResolverObserver[str, "Version"]):
    """Log resolver decisions at DEBUG and drive an optional progress sink.

    A decision level is the count of packages currently decided, so it is the
    live pinned gauge; a backjump lowers it, keeping the count honest under
    backtracking.  Logging is unconditional (the log level gates it, so ``-vv``
    surfaces the pin trace); ``sink`` is present only while a progress line is
    being rendered.
    """

    def __init__(self, sink: ProgressSink | None) -> None:
        self._sink = sink

    def on_decision(self, package: str, version: Version, level: int) -> None:
        _logger.debug("pinned %s %s", package, version)
        if self._sink is not None:
            self._sink.on_pin(level)

    def on_backjump(self, from_level: int, to_level: int) -> None:
        _logger.debug("backjumped from level %d to %d", from_level, to_level)
        if self._sink is not None:
            self._sink.on_pin(to_level)


@dataclass(frozen=True, slots=True)
class InstallContexts:
    """A fork's requirements, split back into the contexts PEP 751 installs.

    ``project`` is the project's own dependencies, and ``selectors``
    holds one requirement list per active extra and group, keyed by its
    ``(kind, name)`` member.  The lock writer walks the resolved graph
    from each of them, so a package only a selection reaches is gated on
    it (see :attr:`~nab_python.lockfile.TargetLock.package_gates`) and a
    default install leaves it out.

    The fork's own ``selection`` is one of those selectors, so a package
    it shares with another active selection names both members and
    installs for either.
    """

    project: tuple[Requirement, ...] = ()
    selectors: Mapping[tuple[str, str], tuple[Requirement, ...]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class ResolveFork:
    """A conflict fork's resolver input: a selection and its requirements.

    ``selection`` is the conflicting members active in this fork, empty
    for an unforked resolve; ``requirements`` are the requirements folded
    for it (the project's dependencies plus the groups and extras the
    selection activates).  Each fork runs against every target, with its
    ``selection`` stamped onto each so the pins land under a distinct
    label and a membership-gated marker.

    ``contexts`` is that same requirement list split into the install
    contexts the lock has to distinguish; ``None`` for a caller that
    resolves a bare requirement list and has no project to split it
    into, which leaves every package unconditional.
    """

    selection: tuple[tuple[str, str], ...]
    requirements: tuple[Requirement, ...]
    contexts: InstallContexts | None = None


@dataclass
class TargetResult:
    """One target's resolve: its pins, or why it has none.

    ``lock`` is what this target contributes to the lockfile, and is
    present exactly when the resolve succeeded.  ``consulted`` is every
    marker the resolve read (root, constraint, and dependency), which is
    what the lock declares its environment from.
    """

    target: ResolveTarget
    success: bool
    pins: dict[str, Version] = field(default_factory=dict)
    error: ResolutionError | None = None
    consulted: frozenset[Marker] = frozenset()
    lock: TargetLock | None = None
    decisions: int = 0
    rounds: int = 0
    conflicts: int = 0
    backjumps: int = 0
    metadata_fetched: int = 0
    distributions_seen: int = 0
    wall_time: float = 0.0


@dataclass
class ResolveResult:
    """The finished resolve: one :class:`TargetResult` per target per fork.

    ``base_results`` and ``env_base_names`` are populated only when
    conflict forks ran: they record what a no-member resolve of each
    environment produced, which is how the lock writer tells a base
    dependency from one that only a member requires.  A failed base pass
    leaves ``env_base_names`` incomplete, so it counts against
    :attr:`success`.
    """

    targets: tuple[ResolveTarget, ...]
    target_results: list[TargetResult] = field(default_factory=list)
    base_results: list[TargetResult] = field(default_factory=list)
    env_base_names: dict[EnvSignature, frozenset[str]] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Whether every target, and every base pass, resolved."""
        return all(tr.success for tr in self.every_result)

    @property
    def every_result(self) -> tuple[TargetResult, ...]:
        """Every per-target resolve, the base passes included."""
        return (*self.target_results, *self.base_results)

    def raise_for_failure(self) -> None:
        """Re-raise the first target's :class:`ResolutionError`, if any.

        For a caller with no per-target reporting of its own (a build-env
        resolve, say), a failed target is just a failed resolve.
        """
        for tr in self.every_result:
            if tr.error is not None:
                raise tr.error

    def merged_pins(self) -> dict[str, list[tuple[str, str]]]:
        """Collapse the per-target pins into ``{package: [(version, label)]}``.

        The labels are target ids, not PEP 508 markers; the lockfile
        writer is what turns them into markers.
        """
        out: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
        for tr in self.target_results:
            if not tr.success:
                continue
            for package, version in tr.pins.items():
                out[package].append((str(version), tr.target.label))
        return dict(out)


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

    A target that cannot be resolved is a failed :class:`TargetResult`,
    not an exception, so a matrix reports every target that failed rather
    than only the first.  Everything else (an unreadable pyproject, a
    conflicting selection, an unsupported source) raises.
    """
    if config is None:
        config = read_pyproject_config(path)
    config = with_python_override(config, python_version)
    targets = plan_targets(config)

    tables = _ProjectTables(
        dependencies=read_pyproject_dependencies(path),
        groups=read_pyproject_groups(path),
        optional=read_pyproject_optional_dependencies(path),
        project_name=read_pyproject_name(path),
    )

    # ``default-groups`` is project policy: every default install
    # activates them, so the conflict checks, the fork plan, and the
    # resolves all fold them into the active group set alongside the CLI
    # selection.
    effective_groups = tuple(dict.fromkeys((*groups, *config.default_groups)))

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


# The number of split-and-resolve passes the micro-narrowing fixpoint runs
# before giving up.  Each pass resolves the slices the previous pass revealed,
# which can expose a boundary reachable only above an earlier split; the set of
# split points grows every pass, so a real graph converges in a couple.  The
# cap turns a graph that somehow does not converge into a loud error rather than
# a hang.
_MAX_MICRO_SPLIT_PASSES = 10

# Terms in a look-ahead grouped clause.
_GROUPED_CLAUSE_TERMS = 2


def _resolve_with_micro_narrowing(
    targets: Sequence[ResolveTarget],
    fork_list: Sequence[ResolveFork],
    constraints: Sequence[Requirement],
    settings: _EngineSettings,
    preferences: Mapping[str, Version] | None,
    base_requirements: Sequence[Requirement] | None,
) -> ResolveResult:
    """Resolve ``targets``, then split any minor a marker cut and re-resolve.

    A consulted marker can cut a minor's micro line
    (``python_full_version < "3.10.2"``).  Resolving the minor once at its
    synthesized ``.0`` declares the whole minor by how ``.0`` read the clause,
    excluding the real interpreters on the other side.  Resolving one target
    per micro slice instead lets each slice declare its own environment row and
    pins.

    The split points come from the markers a resolve consulted, so a boundary
    reachable only above an earlier split is not visible until that slice has
    been resolved.  The loop is a fixpoint: it re-splits and re-resolves until a
    pass reveals no new boundary.  Only a minor that split is re-resolved; every
    target no marker cut (host targets among them, since they name a real micro)
    keeps its first-pass result.
    """
    result = _resolve_passes(
        targets, fork_list, constraints, settings, preferences, base_requirements
    )
    seed = _threaded_preferences(
        dict(preferences or {}), result.target_results, align=settings.align
    )
    points: list[list[Version]] = [[] for _ in targets]
    combined = result
    for _ in range(_MAX_MICRO_SPLIT_PASSES):
        grown = _grow_micro_points(targets, points, combined)
        if grown is None:
            return combined
        points = grown
        split_sigs = {
            env_signature(target)
            for target, target_points in zip(targets, points, strict=True)
            if target_points
        }
        slices = [
            sliced
            for target, target_points in zip(targets, points, strict=True)
            if target_points
            for sliced in slices_from_points(target, target_points)
        ]
        slice_result = _resolve_passes(
            slices, fork_list, constraints, settings, seed, base_requirements
        )
        combined = _merge_micro_results(targets, result, slice_result, split_sigs)
    msg = (
        "environment micro-boundary splitting did not converge in"
        f" {_MAX_MICRO_SPLIT_PASSES} passes"
    )
    raise ResolutionError(msg)


def _grow_micro_points(
    targets: Sequence[ResolveTarget],
    points: Sequence[Sequence[Version]],
    result: ResolveResult,
) -> list[list[Version]] | None:
    """Return ``points`` grown by the boundaries ``result`` consulted, or None.

    None means no minor gained a split point: the fixpoint has settled.  Each
    target's boundaries are gathered from every slice it currently has, so a
    boundary a marker consults only above an earlier split is picked up once
    that slice has been resolved.
    """
    consulted_by_sig: dict[EnvSignature, set[Marker]] = defaultdict(set)
    for tr in result.every_result:
        consulted_by_sig[env_signature(tr.target)] |= set(tr.consulted)

    grown: list[list[Version]] = []
    changed = False
    for target, target_points in zip(targets, points, strict=True):
        found = set(target_points)
        for sliced in slices_from_points(target, target_points):
            consulted = consulted_by_sig.get(env_signature(sliced), set())
            found.update(micro_boundary_points(target, consulted))
        ordered = sorted(found)
        if ordered != list(target_points):
            changed = True
        grown.append(ordered)
    return grown if changed else None


def _merge_micro_results(
    targets: Sequence[ResolveTarget],
    result: ResolveResult,
    slice_result: ResolveResult,
    split_sigs: set[EnvSignature],
) -> ResolveResult:
    """Fold ``slice_result`` back over the first-pass ``result``.

    A target that split is dropped from ``result`` (its ``.0`` entry and its
    base pass) and its slices are taken from ``slice_result`` instead; every
    unsplit target keeps its first-pass entry, so it is never resolved again.
    """

    def kept(results: Sequence[TargetResult]) -> list[TargetResult]:
        return [tr for tr in results if env_signature(tr.target) not in split_sigs]

    env_base_names = {
        sig: names
        for sig, names in result.env_base_names.items()
        if sig not in split_sigs
    }
    env_base_names.update(slice_result.env_base_names)
    unsplit = tuple(t for t in targets if env_signature(t) not in split_sigs)
    return ResolveResult(
        targets=(*unsplit, *slice_result.targets),
        target_results=kept(result.target_results) + list(slice_result.target_results),
        base_results=kept(result.base_results) + list(slice_result.base_results),
        env_base_names=env_base_names,
    )


def _resolve_passes(
    targets: Sequence[ResolveTarget],
    fork_list: Sequence[ResolveFork],
    constraints: Sequence[Requirement],
    settings: _EngineSettings,
    preferences: Mapping[str, Version] | None,
    base_requirements: Sequence[Requirement] | None,
) -> ResolveResult:
    """Resolve every fork against every target, plus the base pass.

    The fork loop threads each target's pins forward across the whole run;
    the base pass, when given, records the no-member pins per environment.
    """
    accumulated = dict(preferences or {})
    results: list[TargetResult] = []
    for fork in fork_list:
        fork_targets = [
            t.with_selection(fork.selection) if fork.selection else t for t in targets
        ]
        pass_results = _run_pass(
            fork_targets,
            fork.requirements,
            constraints,
            settings,
            accumulated,
            fork.contexts,
        )
        results.extend(pass_results)
        accumulated = _threaded_preferences(
            accumulated, pass_results, align=settings.align
        )

    # A base (no-member) pass names the deps that install regardless of
    # which member is chosen, so the writer keeps the membership clause
    # on a dep required only by members.
    base_results: list[TargetResult] = []
    env_base_names: dict[EnvSignature, frozenset[str]] = {}
    if base_requirements is not None:
        base_results = _run_pass(
            list(targets), base_requirements, constraints, settings, preferences or {}
        )
        for tr in base_results:
            if tr.success:
                env_base_names[env_signature(tr.target)] = frozenset(
                    canonicalize_name(name) for name in tr.pins
                )
            else:
                _logger.warning(
                    "Base attribution skipped for tuple %s: %s",
                    tr.target.label,
                    tr.error,
                )

    return ResolveResult(
        targets=tuple(targets),
        target_results=results,
        base_results=base_results,
        env_base_names=env_base_names,
    )


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
    the lock records at the top level;  ``default-groups`` and the
    declared conflicts are project policy and come from ``config``.
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
    )


def env_signature(target: ResolveTarget) -> EnvSignature:
    """Return ``target``'s environment as a hashable key."""
    return tuple(sorted(target.marker_env.items()))


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
class _EngineSettings:
    """What every per-target resolve in one run shares."""

    coordinator: FetchCoordinator
    config: NabProjectConfig
    # Where a declared VCS clone or archive extraction lands, the cache root
    # unless caching is off.
    source_root: Path | None
    align: bool
    resolution: ResolutionStrategy
    progress: ProgressSink | None = None
    # Shared by every target of every pass: the coordinator and the policy
    # config the pre-tag half of the listing filter reads are both fixed here.
    listing_filter_cache: ListingFilterCache = field(default_factory=ListingFilterCache)
    # The root requirements already reported by _warn_dropped_root_marker. The
    # same roots are read once per target per fork plus once in the base pass,
    # and one mistaken requirement is worth one warning.
    warned_root_markers: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _ProjectTables:
    """The pyproject tables a resolve reads, read once."""

    dependencies: list[Requirement]
    groups: Mapping[str, Sequence[str | Mapping[str, str]]]
    optional: Mapping[str, Sequence[str]]
    project_name: str | None


def _threaded_preferences(
    accumulated: dict[str, Version],
    results: Sequence[TargetResult],
    *,
    align: bool,
) -> dict[str, Version]:
    """Fold a pass's pins into the preferences the next pass starts from."""
    if not align:
        return accumulated
    for tr in results:
        if tr.success:
            accumulated.update(tr.pins)
    return accumulated


def _run_pass(
    targets: Sequence[ResolveTarget],
    requirements: Sequence[Requirement],
    constraints: Sequence[Requirement],
    settings: _EngineSettings,
    preferences: Mapping[str, Version],
    contexts: InstallContexts | None = None,
) -> list[TargetResult]:
    """Resolve every target in ``targets`` once, in order.

    With alignment on, each target's pins are threaded forward as
    preferences for the next, so the pins stay aligned across targets
    wherever the environments admit it.

    ``contexts`` splits ``requirements`` into the install contexts the
    lock gates its packages on; see :class:`InstallContexts`.
    """
    results: list[TargetResult] = []
    accumulated = dict(preferences)
    for target in targets:
        tr = _resolve_one_target(
            target, requirements, constraints, settings, accumulated, contexts
        )
        results.append(tr)
        accumulated = _threaded_preferences(accumulated, [tr], align=settings.align)
    return results


def _resolve_one_target(
    target: ResolveTarget,
    requirements: Sequence[Requirement],
    constraints: Sequence[Requirement],
    settings: _EngineSettings,
    preferences: Mapping[str, Version],
    contexts: InstallContexts | None = None,
) -> TargetResult:
    """Run one single-environment resolve for ``target``."""
    config = settings.config
    environment = target.marker_env
    try:
        resolver_requirements, root_extras = _build_resolver_inputs(
            requirements,
            config,
            environment=environment,
            warned=settings.warned_root_markers,
        )
        resolver_constraints, _ = _build_resolver_inputs(
            constraints,
            config,
            environment=environment,
            kind="constraint",
            warned=settings.warned_root_markers,
        )
    except ResolutionError as exc:
        return TargetResult(target=target, success=False, error=exc)

    source_root = settings.source_root
    provider = Provider(
        settings.coordinator,
        target=target,
        root_requirements=resolver_requirements,
        root_extras=root_extras,
        uploaded_prior_to=config.uploaded_prior_to,
        dist_policy=config.dist_policy,
        build_policy=config.build_policy,
        package_overrides=config.package_overrides,
        index_overrides=config.index_overrides,
        trust_unverified_sdist_deps=config.trust_unverified_sdist_deps,
        vcs_config=config.vcs,
        local_sources=list(config.local_sources) or None,
        vcs_sources=list(config.vcs_sources) or None,
        vcs_cache_dir=source_root / VCS_BUCKET if source_root is not None else None,
        archive_sources=list(config.archive_sources) or None,
        archive_cache_dir=(
            source_root / ARCHIVE_BUCKET if source_root is not None else None
        ),
        build_config=config,
        resolution_strategy=settings.resolution,
        direct_packages=frozenset(
            name for name in resolver_requirements if split_extra(name)[1] is None
        ),
        preferences=dict(preferences),
        listing_filter_cache=settings.listing_filter_cache,
    )
    observer = _ResolveObserver(settings.progress)
    resolver: Resolver[str, Version] = Resolver(
        provider, observer=observer, range_type=VersionRange, root_version="0"
    )

    _logger.debug("resolving %s", target.label)
    start = time.monotonic()
    try:
        raw = resolver.resolve(resolver_requirements, constraints=resolver_constraints)
        pins = {k: v for k, v in raw.items() if split_extra(k)[1] is None}
        _raise_for_source_python(provider, target, pins)
    except ResolutionError as exc:
        _augment_resolution_error(exc, provider)
        return TargetResult(
            target=target,
            success=False,
            error=exc,
            wall_time=time.monotonic() - start,
            **_target_stats(resolver, provider),
        )
    elapsed = time.monotonic() - start
    _logger.info(
        "resolved %d packages for %s in %.2fs (%d distributions seen, %d fetched)",
        len(pins),
        target.label,
        elapsed,
        provider.stats.distributions_seen,
        provider.stats.metadata_fetched,
    )
    base_roots, selector_roots = _install_context_roots(contexts, environment)
    return TargetResult(
        target=target,
        success=True,
        pins=pins,
        consulted=_consulted_markers(provider, requirements, constraints),
        lock=build_target_lock(
            provider,
            target,
            pins,
            indexes=settings.coordinator.indexes,
            resolved_keys=raw,
            base_roots=base_roots,
            selector_roots=selector_roots,
        ),
        wall_time=elapsed,
        **_target_stats(resolver, provider),
    )


def _install_context_roots(
    contexts: InstallContexts | None, environment: Mapping[str, str]
) -> tuple[frozenset[str] | None, dict[tuple[str, str], frozenset[str]] | None]:
    """Return the lock writer's install-context roots for one target.

    ``(None, None)`` when there is no selection to attribute packages to,
    which leaves every package unconditional.  A requirement whose marker
    this target's environment fails is dropped, exactly as the resolve
    dropped it, so it gates nothing.
    """
    if contexts is None or not contexts.selectors:
        return None, None
    return (
        _root_keys(contexts.project, environment),
        {
            member: _root_keys(requirements, environment)
            for member, requirements in contexts.selectors.items()
        },
    )


def _root_keys(
    requirements: Sequence[Requirement], environment: Mapping[str, str]
) -> frozenset[str]:
    """Return the resolver keys ``requirements`` names directly.

    The same shape :func:`_build_resolver_inputs` feeds the resolver: a
    canonical name per requirement, plus a ``name[extra]`` proxy key per
    requested extra, with marker-excluded requirements dropped.
    """
    keys: set[str] = set()
    for req in requirements:
        if req.marker is not None and not dependency_marker_holds(
            req.marker, environment
        ):
            continue
        name = str(canonicalize_name(req.name))
        keys.add(name)
        keys.update(join_extra(name, extra) for extra in req.extras)
    return frozenset(keys)


def _consulted_markers(
    provider: Provider,
    requirements: Sequence[Requirement],
    constraints: Sequence[Requirement],
) -> frozenset[Marker]:
    """Every PEP 508 marker this resolve read.

    The provider records the markers it read off the dependency graph;
    the root requirements and constraints are collected here, since their
    markers are evaluated before the provider exists.
    """
    consulted = set(provider.consulted_markers)
    for req in itertools.chain(requirements, constraints):
        if req.marker is not None:
            consulted.add(req.marker)
    return frozenset(consulted)


def _target_stats(
    resolver: Resolver[str, Version], provider: Provider
) -> dict[str, int]:
    """Return the resolver and provider counters for a :class:`TargetResult`."""
    return {
        "rounds": resolver.stats.rounds,
        "decisions": resolver.stats.decisions,
        "conflicts": resolver.stats.conflicts,
        "backjumps": resolver.stats.backjumps,
        "metadata_fetched": provider.stats.metadata_fetched,
        "distributions_seen": provider.stats.distributions_seen,
    }


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
    if config.conflicts:
        _validate_conflict_members_exist(
            config.conflicts, tables.optional, tables.groups
        )
        _check_conflict_minimums(
            config.conflicts,
            tables,
            extras,
            expand_group_includes(tables.groups, groups),
            targets,
        )

    plan = conflict_forks(extras, groups, config.conflicts)
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
                fork.active_groups,
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

        forks.append(
            ResolveFork(
                selection=fork.selection,
                requirements=tuple(_fork_requirements(path, tables, fork)),
                contexts=InstallContexts(
                    project=tuple(tables.dependencies),
                    selectors=_selector_requirements(path, tables, fork),
                ),
            )
        )

    # With more than one fork the lock writer needs to tell a base
    # dependency from one required by every member, so the no-member
    # requirements are resolved too.
    base_requirements = None
    if len(plan) > 1:
        base_requirements = _fork_requirements(path, tables, _base_fork(plan[0]))
    return forks, base_requirements


def _selector_requirements(
    path: Path, tables: _ProjectTables, fork: ConflictFork
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
    """
    selectors: dict[tuple[str, str], tuple[Requirement, ...]] = {}
    for extra in fork.active_extras:
        member = (ConflictKind.EXTRA.value, str(canonicalize_name(extra)))
        selectors[member] = tuple(_extra_requirements(tables, [extra], path))
    for group in fork.active_groups:
        member = (ConflictKind.GROUP.value, str(canonicalize_name(group)))
        selectors[member] = tuple(_group_requirements(tables.groups, [group], path))
    return selectors


def _fork_requirements(
    path: Path, tables: _ProjectTables, fork: ConflictFork
) -> list[Requirement]:
    """Fold one fork's active groups and extras onto the project deps.

    Each fork resolves a different slice of the selection (one member per
    engaged conflict set), so its requirement list is built separately
    rather than shared.
    """
    requirements = list(tables.dependencies)
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
    return ConflictFork(
        selection=(), active_extras=rest_extras, active_groups=rest_groups
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
) -> None:
    """Raise when a declared conflict names an extra/group the project lacks.

    A member naming an undeclared extra or group can never match, so
    the conflict would be silently inert.  Names compare under
    canonicalisation, matching the loaders.
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
    environment: Mapping[str, str],
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

    # Sort so the reported conflict order is stable.
    conflicts.sort(key=lambda c: (c.left_group, c.right_group, c.package))
    return conflicts


def _check_group_disjointness(
    per_group: Mapping[str, list[Requirement]],
    targets: Sequence[ResolveTarget],
) -> None:
    """Raise on a direct conflict between two selected groups, naming them.

    A conflict is reported when it holds on any target; the offending
    tuples are named when there is more than one to name.  A no-op below
    two groups.
    """
    affected: dict[_GroupConflict, set[str]] = defaultdict(set)
    for target in targets:
        for conflict in _find_group_conflicts(per_group, target.marker_env):
            affected[conflict].add(target.label)
    if not affected:
        return
    clauses: list[str] = []
    for conflict in sorted(
        affected,
        key=lambda c: (c.left_group, c.right_group, c.package),
    ):
        where = (
            f" for tuple(s) {', '.join(sorted(affected[conflict]))}"
            if len(targets) > 1
            else ""
        )
        clauses.append(
            f"Dependency groups {conflict.left_group!r} and"
            f" {conflict.right_group!r} conflict on {conflict.package!r}{where}:"
            f" group {conflict.left_group!r} requires {conflict.left_req} but group"
            f" {conflict.right_group!r} requires {conflict.right_req}."
        )
    raise ResolutionError("; ".join(clauses))


def _warn_dropped_root_marker(req: Requirement, warned: set[str]) -> None:
    """Warn when a dropped root requirement tests an extra/group membership.

    A root marker testing ``extra``, ``extras``, or ``dependency_groups``
    evaluates False at resolve time (root activates no extra or group), so the
    dep would otherwise be dropped silently.  ``warned`` carries the
    requirements already reported in this run, so one mistaken requirement is
    reported once rather than once per target per fork.
    """
    marker_text = str(req.marker)
    if "extra ==" not in marker_text and not membership_set_in_marker(marker_text):
        return
    text = str(req)
    if text in warned:
        return
    warned.add(text)
    _logger.warning(
        "Root requirement %r tests an extra or dependency-group membership "
        "marker; the dep is dropped because root activates no extra or group "
        "at resolve time. For an extra, use pkg[extra] (extras-of-package).",
        text,
    )


def _build_resolver_inputs(
    requirements: Sequence[Requirement],
    config: NabProjectConfig,
    *,
    environment: Mapping[str, str],
    kind: str = "requirement",
    warned: set[str] | None = None,
) -> tuple[dict[str, VersionRange], set[tuple[str, str]]]:
    """Convert PEP 508 requirements to the resolver's input shape.

    Requirements whose PEP 508 marker evaluates to ``False`` under
    ``environment`` are skipped, matching pip/uv's root-requirement
    handling.  Repeated package names are intersected into one range;
    an empty intersection raises :class:`ResolutionError`.  A direct-URL
    or VCS requirement is refused by :func:`admit_vcs_url`; resolving one
    is not implemented.

    ``kind`` is ``"requirement"`` or ``"constraint"``.  A constraint may
    not carry extras, and shapes the error wording; the returned extras
    set is empty for one.

    ``warned`` is the run's set of already-reported extra/group root
    markers (see :func:`_warn_dropped_root_marker`); a caller that does
    not share one gets a fresh set, so it warns per call.
    """
    resolver_requirements: dict[str, VersionRange] = {}
    sources: defaultdict[str, list[str]] = defaultdict(list)
    root_extras: set[tuple[str, str]] = set()
    already_warned = set() if warned is None else warned
    for req in requirements:
        if kind == "constraint" and req.extras:
            msg = f"Constraints cannot have extras: {req}"
            raise ConfigError(msg)
        if req.marker is not None and not dependency_marker_holds(
            req.marker, environment
        ):
            _warn_dropped_root_marker(req, already_warned)
            continue
        if req.url is not None:
            admit_vcs_url(req.url, config.vcs)
            msg = (
                f"VCS {kind} admitted by policy but resolver path is not"
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = str(canonicalize_name(req.name))
        previous = resolver_requirements.get(name, VersionRange.full())
        term = (
            req.specifier.to_range()
            if req.specifier
            else VersionRange.full(admit_arbitrary=False)
        )
        resolver_requirements[name] = previous & term
        sources[name].append(str(req))
        for extra in sorted(req.extras):
            extra_key = join_extra(name, extra)
            resolver_requirements[extra_key] = VersionRange.full(admit_arbitrary=False)
            _, normalized_extra = split_extra(extra_key)
            assert normalized_extra is not None  # join_extra always sets one
            root_extras.add((name, normalized_extra))
    raise_for_unsatisfiable(resolver_requirements, sources, kind=kind)
    return resolver_requirements, root_extras


def _raise_for_source_python(
    provider: Provider,
    target: ResolveTarget,
    pins: Mapping[str, Version],
) -> None:
    """Reject a local, VCS, or archive pin whose Requires-Python excludes ``target``.

    Index candidates are filtered by Requires-Python while listing and again
    from their fetched metadata; local, VCS, and archive sources skip both, so
    a source that rejects the resolve target could otherwise reach the lock.
    """
    managed = (
        provider.local_sources.keys()
        | provider.vcs_sources.keys()
        | provider.archive_sources.keys()
    )
    if not managed:
        return
    for name, version in pins.items():
        normalized = canonicalize_name(name)
        if normalized not in managed:
            continue
        spec = provider.metadata_cache[(normalized, version)].requires_python
        if spec is not None and not target.admits_requires_python(spec):
            msg = (
                f"{normalized} {version} requires Python {spec} but the"
                f" {target.label} resolve targets Python {target.python_full_version}"
            )
            raise ResolutionError(msg)


def _augment_resolution_error(exc: ResolutionError, provider: Provider) -> None:
    """Append per-package no-versions diagnostics to ``exc`` in-place.

    Walks the derivation tree carried on the exception, collects every
    package a rejection clause names (see
    :func:`_walk_no_versions_packages`), and looks up the provider-side
    reason for each.  When at least one reason is available, rewrites the
    exception's args so that ``str(exc)`` surfaces the diagnostics
    alongside the original derivation tree.

    Best-effort: reasons are keyed by package name and outlive the ask
    that recorded them, so a package whose earlier ask found no version
    keeps its hint even when the tree names it over a later range.
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
    """Return the packages a no-versions diagnostic may name.

    NO_VERSIONS clauses name every package they carry.  Look-ahead grouped
    clauses (DEPENDENCY cause, two positive terms) name their candidate: a
    widened union covering the whole listing conflicts by propagation, with
    no second ``choose_version`` ask to raise a NO_VERSIONS clause.  The
    caller drops packages with no recorded reason.

    The walk is iterative: the tree gains a level per conflict, so a deeply
    backtracked resolve overflows the recursion limit.
    """
    out: list[str] = []
    seen_ids: set[int] = set()
    stack: list[Incompatibility[Any, Any]] = [incompatibility]

    while stack:
        node = stack.pop()
        if id(node) in seen_ids:
            continue
        seen_ids.add(id(node))

        if node.cause is IncompatibilityCause.NO_VERSIONS:
            for term in node.terms:
                pkg = term.package
                if isinstance(pkg, str):
                    out.append(pkg)
        elif (
            node.cause is IncompatibilityCause.DEPENDENCY
            and len(node.terms) == _GROUPED_CLAUSE_TERMS
            and node.terms[0].is_positive()
            and node.terms[1].is_positive()
        ):
            pkg = node.terms[0].package
            if isinstance(pkg, str):
                out.append(pkg)

        # Right before left, so the left cause pops first and names keep their order.
        if node.cause_right is not None:
            stack.append(node.cause_right)
        if node.cause_left is not None:
            stack.append(node.cause_left)

    return out
