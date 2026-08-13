"""The resolve engine: targets and forked requirements in, per-target pins out.

Everything here runs below :func:`nab_python.resolve.resolve_with_coordinator`,
which is nab's own host layer: it reads the project, turns a
:class:`~nab_python.config.NabProjectConfig` into the :class:`_EngineSettings`
one run shares, and binds nab's default marker predicate.  Nothing in this
module reads a pyproject, plans a conflict fork or assembles a lock input.

The seam is one-way by construction: no definition here references one in
:mod:`nab_python.resolve`, so a host embedding the engine takes this module and
leaves that one.  ``tasks/check_engine_markersets.py`` walks it from
:func:`_resolve_with_micro_narrowing` and keeps packaging's marker sets off it.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from nab_index.cache import ARCHIVE_BUCKET, VCS_BUCKET
from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import Resolver, ResolverObserver
from nab_resolver.types import IncompatibilityCause

from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.utils import canonicalize_name
from ..lockfile import build_target_lock
from ..provider import ListingFilterCache, Provider, join_extra, split_extra
from ..resolver_inputs import ProxyConstraints, build_resolver_inputs
from ..target import micro_boundary_points, slices_from_points

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from nab_resolver.types import Incompatibility

    from .._vendor.packaging.markers import Marker
    from .._vendor.packaging.requirements import Requirement
    from .._vendor.packaging.version import Version
    from ..config import NabProjectConfig
    from ..fetch import FetchCoordinator
    from ..lockfile import TargetLock
    from ..provider import ResolutionStrategy
    from ..resolver_inputs import MarkerHolds
    from ..target import ResolveTarget


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
    from each of them, so each package is gated on the contexts that
    reach it (see :attr:`~nab_python.lockfile.TargetLock.package_gates`)
    and a default install leaves out what only a selection brings.

    The fork's own ``selection`` is one of those selectors, so a package
    it shares with another active selection names both members and
    installs for either.

    ``project`` is the project's own dependencies *as this fork resolved
    them*, empty in a fork a declared conflict excluded them from.

    ``name_project`` is set when ``[tool.nab].base-group`` names the
    project's own dependencies.  It is a lock-wide fact rather than a
    per-fork one: the name has to mean the same thing in every lock it
    appears in, so the roots are walked even with nothing selected.
    """

    project: tuple[Requirement, ...] = ()
    selectors: Mapping[tuple[str, str], tuple[Requirement, ...]] = field(
        default_factory=dict
    )
    name_project: bool = False


@dataclass(frozen=True, slots=True)
class ResolveFork:
    """A conflict fork's resolver input: a selection and its requirements.

    ``selection`` is the conflicting members active in this fork, empty
    for an unforked resolve; ``requirements`` are the requirements folded
    for it: the configured contexts this fork carries plus the groups and
    extras the selection activates.  Each fork runs against every target, with its
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
        slice_result = _resolve_slices(
            targets,
            points,
            fork_list,
            constraints,
            settings,
            preferences,
            base_requirements,
            result,
        )
        combined = _merge_micro_results(targets, result, slice_result, split_sigs)
    msg = (
        "environment micro-boundary splitting did not converge in"
        f" {_MAX_MICRO_SPLIT_PASSES} passes"
    )
    raise ResolutionError(msg)


def _resolve_slices(
    targets: Sequence[ResolveTarget],
    points: Sequence[Sequence[Version]],
    fork_list: Sequence[ResolveFork],
    constraints: Sequence[Requirement],
    settings: _EngineSettings,
    preferences: Mapping[str, Version] | None,
    base_requirements: Sequence[Requirement] | None,
    first_pass: ResolveResult,
) -> ResolveResult:
    """Resolve every split target's slices, one pass per fork.

    A fork's pass walks the whole matrix in order: a split target has its
    slices resolved, and an unsplit target folds its first-pass pins in where
    it sits rather than resolving a second time.  Alignment therefore threads
    through the slices in matrix order, as it does in the first pass.
    """
    sliced = [
        slices_from_points(target, target_points) if target_points else []
        for target, target_points in zip(targets, points, strict=True)
    ]

    accumulated = dict(preferences or {})
    results: list[TargetResult] = []
    for index, fork in enumerate(fork_list):
        # first_pass holds one contiguous run of results per fork, in order.
        fork_first = first_pass.target_results[
            index * len(targets) : (index + 1) * len(targets)
        ]

        for target_slices, first_result in zip(sliced, fork_first, strict=True):
            if not target_slices:
                accumulated = _threaded_preferences(
                    accumulated, [first_result], align=settings.align
                )
                continue

            fork_slices = [
                t.with_selection(fork.selection) if fork.selection else t
                for t in target_slices
            ]
            pass_results = _run_pass(
                fork_slices,
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

    all_slices = [t for group in sliced for t in group]
    base_results, env_base_names = _base_pass(
        all_slices, base_requirements, constraints, settings, preferences or {}
    )
    return ResolveResult(
        targets=tuple(all_slices),
        target_results=results,
        base_results=base_results,
        env_base_names=env_base_names,
    )


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

    base_results, env_base_names = _base_pass(
        targets, base_requirements, constraints, settings, preferences or {}
    )
    return ResolveResult(
        targets=tuple(targets),
        target_results=results,
        base_results=base_results,
        env_base_names=env_base_names,
    )


def _base_pass(
    targets: Sequence[ResolveTarget],
    base_requirements: Sequence[Requirement] | None,
    constraints: Sequence[Requirement],
    settings: _EngineSettings,
    preferences: Mapping[str, Version],
) -> tuple[list[TargetResult], dict[EnvSignature, frozenset[str]]]:
    """Resolve the no-member requirements per target, if there are any.

    The pass names the deps that install regardless of which member is chosen,
    so the writer keeps the membership clause on a dep required only by members.
    """
    if base_requirements is None:
        return [], {}

    results = _run_pass(
        list(targets), base_requirements, constraints, settings, preferences
    )

    env_base_names: dict[EnvSignature, frozenset[str]] = {}
    for tr in results:
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

    return results, env_base_names


def env_signature(target: ResolveTarget) -> EnvSignature:
    """Return ``target``'s environment as a hashable key."""
    return tuple(sorted(target.marker_env.items()))


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
    # Whether a root requirement's marker holds for one target's environment.
    # The engine takes it rather than importing one, because the predicate a
    # dependency marker needs is the resolve path's only marker-set dependency.
    marker_holds: MarkerHolds
    progress: ProgressSink | None = None
    # Shared by every target of every pass: the coordinator and the policy
    # config the pre-tag half of the listing filter reads are both fixed here.
    listing_filter_cache: ListingFilterCache = field(default_factory=ListingFilterCache)
    # The root requirements already reported by _warn_dropped_root_marker. The
    # same roots are read once per target per fork plus once in the base pass,
    # and one mistaken requirement is worth one warning.
    warned_root_markers: set[str] = field(default_factory=set)


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
        root_requirements, resolver_requirements, root_extras = build_resolver_inputs(
            requirements,
            config.vcs,
            environment=environment,
            marker_holds=settings.marker_holds,
            warned=settings.warned_root_markers,
        )
        constraint_ranges = build_resolver_inputs(
            constraints,
            config.vcs,
            environment=environment,
            marker_holds=settings.marker_holds,
            kind="constraint",
            warned=settings.warned_root_markers,
        ).ranges
        resolver_constraints = ProxyConstraints(constraint_ranges)
    except ResolutionError as exc:
        return TargetResult(target=target, success=False, error=exc)

    source_root = settings.source_root
    provider = Provider(
        settings.coordinator,
        target=target,
        root_requirements=resolver_requirements,
        constraints=resolver_constraints,
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
        decision_order=config.decision_order,
        resolution_strategy=settings.resolution,
        direct_packages=frozenset(
            name for name in resolver_requirements if split_extra(name)[1] is None
        ),
        preferences=dict(preferences),
        listing_filter_cache=settings.listing_filter_cache,
    )
    observer = _ResolveObserver(settings.progress)
    resolver: Resolver[str, Version] = Resolver(
        provider,
        observer=observer,
        range_type=VersionRange,
        root_version="0",
        format_range=provider.format_range,
    )

    _logger.debug("resolving %s", target.label)
    start = time.monotonic()
    try:
        raw = resolver.resolve(root_requirements, constraints=resolver_constraints)
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
    base_roots, selector_roots = _install_context_roots(
        contexts, environment, settings.marker_holds
    )
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
    contexts: InstallContexts | None,
    environment: Mapping[str, str],
    marker_holds: MarkerHolds,
) -> tuple[frozenset[str] | None, dict[tuple[str, str], frozenset[str]] | None]:
    """Return the lock writer's install-context roots for one target.

    ``(None, None)`` when nothing needs attributing, which leaves every
    package unconditional: no selection to name, and no name for the
    project's own dependencies either.  A requirement whose marker this
    target's environment fails is dropped, exactly as the resolve dropped
    it, so it gates nothing.
    """
    if contexts is None or not (contexts.selectors or contexts.name_project):
        return None, None
    return (
        _root_keys(contexts.project, environment, marker_holds),
        {
            member: _root_keys(requirements, environment, marker_holds)
            for member, requirements in contexts.selectors.items()
        },
    )


def _root_keys(
    requirements: Sequence[Requirement],
    environment: Mapping[str, str],
    marker_holds: MarkerHolds,
) -> frozenset[str]:
    """Return the resolver keys ``requirements`` names directly.

    The same shape :func:`build_resolver_inputs` feeds the resolver: a
    canonical name per requirement, plus a ``name[extra]`` proxy key per
    requested extra, with marker-excluded requirements dropped.
    """
    keys: set[str] = set()
    for req in requirements:
        if req.marker is not None and not marker_holds(req.marker, environment):
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
                f" {target.label} resolve targets Python {target.python_version}"
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
