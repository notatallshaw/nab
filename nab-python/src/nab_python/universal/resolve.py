"""Universal resolution loop.

Runs one specific resolve per matrix tuple, sharing a single
FetchCoordinator so metadata is fetched once across tuples.  Returns a
merged result keyed by package name with the markers under which each
pin applies.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nab_index.multi_index import IndexConfig
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import Resolver

from .._vendor.packaging.markers import Marker
from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.requirements import InvalidRequirement, Requirement
from .._vendor.packaging.utils import canonicalize_name
from ..fetch import (
    DEFAULT_INDEX_NAME,
    DEFAULT_INDEX_URL,
    FetchCoordinator,
    IndexOverride,
)
from ..lockfile import (
    LockInput,
    MissingHashError,
    PinShape,
    build_lock_input_from_provider,
)
from ..provider import (
    BuildPolicy,
    DistPolicy,
    LocalSource,
    VcsConfig,
    VcsSource,
    split_extra,
)
from ..requirements_file import raise_for_unsatisfiable
from .provider import UniversalProvider

__all__ = [
    "TupleResult",
    "UniversalResult",
    "merge_universal_lock_inputs",
    "resolve_universal",
]


logger = logging.getLogger(__name__)

# Cap on the per-tuple ResolutionError message stashed in
# :class:`TupleResult.error`, so a runaway report message never
# bloats the failure summary.
_ERROR_MESSAGE_LIMIT = 200


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from nab_index.transport import AsyncHttpTransport

    from .._vendor.packaging.version import Version
    from ..config import NabProjectConfig
    from .matrix import Matrix, MatrixTuple


@dataclass
class TupleResult:
    """Result of one specific resolve."""

    tuple_: MatrixTuple
    success: bool
    pins: dict[str, Version] = field(default_factory=dict)
    error: str | None = None
    decisions: int = 0
    rounds: int = 0
    wall_time: float = 0.0
    lock_input: LockInput | None = None


@dataclass
class UniversalResult:
    """Merged result across all tuples."""

    matrix: Matrix
    tuple_results: list[TupleResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return True iff every tuple resolved successfully."""
        return all(tr.success for tr in self.tuple_results)

    def merged_lock(self) -> dict[str, list[tuple[str, str]]]:
        """Collapse per-tuple pins into ``{package: [(version, label), ...]}``.

        Adjacent labels picking the same version stay together; the
        labels are tuple ids, not PEP 508 markers.
        """
        out: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
        for tr in self.tuple_results:
            if not tr.success:
                continue
            for pkg, version in tr.pins.items():
                out[pkg].append((str(version), tr.tuple_.label))
        return out


def merge_universal_lock_inputs(
    result: UniversalResult,
    *,
    requires_python: str | None = None,
    created_by: str = "nab",
    extras: Sequence[str] = (),
    dependency_groups: Sequence[str] = (),
    default_groups: Sequence[str] = (),
) -> LockInput:
    """Merge per-tuple :class:`LockInput` objects into one universal lock.

    Each successful tuple's pins are stored under ``per_tuple_pins``
    keyed by the tuple's ``label``; the matching PEP 508 marker
    expression is recorded in ``tuple_markers``.  The downstream pylock
    writer collapses these into one or more ``Package`` entries per
    name with markers attached.

    Tuples with ``lock_input is None`` (resolution succeeded but the
    artefact set is missing a ``sha256`` somewhere) are skipped, which
    means the resulting lock omits those tuples.  Callers that want
    every tuple represented should check ``UniversalResult.success``
    before calling.

    ``dependency_groups`` and ``default_groups`` are recorded at the
    lockfile top level for PEP 735 multi-use locks.  Per-package
    group attribution (markers gated by
    ``'group-x' in dependency_groups``) is not emitted.
    """
    per_tuple_pins: dict[str, dict[str, PinShape]] = {}
    tuple_markers: dict[str, Marker] = {}
    tuple_environments: dict[str, dict[str, str]] = {}
    environments: list[Marker] = []
    for tr in result.tuple_results:
        if not tr.success or tr.lock_input is None:
            continue
        label = tr.tuple_.label
        per_tuple_pins[label] = dict(tr.lock_input.pins)
        marker = Marker(tr.tuple_.marker_string)
        tuple_markers[label] = marker
        tuple_environments[label] = dict(tr.tuple_.environment)
        environments.append(marker)
    return LockInput(
        per_tuple_pins=per_tuple_pins,
        tuple_markers=tuple_markers,
        tuple_environments=tuple_environments,
        environments=environments,
        requires_python=requires_python,
        created_by=created_by,
        extras=tuple(extras),
        dependency_groups=tuple(dependency_groups),
        default_groups=tuple(default_groups),
    )


def resolve_universal(  # noqa: PLR0913 - surface area mirrors uv's resolution knobs; bundling all flags into a config object hides the call-site documentation
    matrix: Matrix,
    requirements: list[str],
    *,
    transport: AsyncHttpTransport | None = None,
    offline: bool = False,
    constraints: list[str] | None = None,
    cache_dir: Path | None = None,
    uploaded_prior_to: datetime | None = None,
    uploaded_prior_to_overrides: Mapping[str, datetime | None] | None = None,
    dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
    dist_policy_overrides: Mapping[str, DistPolicy] | None = None,
    build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    vcs_config: VcsConfig | None = None,
    local_sources: list[LocalSource] | None = None,
    vcs_sources: list[VcsSource] | None = None,
    vcs_cache_dir: Path | None = None,
    build_config: NabProjectConfig | None = None,
    indexes: list[IndexConfig] | None = None,
    index_overrides: list[IndexOverride] | None = None,
    resolution_strategy: str = "highest",
    align_across_tuples: bool = True,
    preferences: dict[str, Version] | None = None,
) -> UniversalResult:
    """Run a universal resolve for ``matrix``.

    Returns a :class:`UniversalResult` with one :class:`TupleResult`
    per (python_version, platform) combination.

    ``resolution_strategy``: ``"highest"`` (default), ``"lowest"``, or
    ``"lowest-direct"``.  Mirrors uv's ``--resolution`` flag.

    ``align_across_tuples``: when True, after each tuple's resolve we
    accumulate its pins as preferences for subsequent tuples.

    ``preferences``: a starting set of preferred ``{name: Version}``,
    e.g. read from a previous lock.
    """
    if indexes is None:
        indexes = [IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL)]
    effective_transport: AsyncHttpTransport = (
        transport if transport is not None else Urllib3AsyncTransport()
    )
    with FetchCoordinator(
        effective_transport,
        indexes=indexes,
        cache_dir=cache_dir,
        offline=offline,
        index_overrides=index_overrides,
    ) as coordinator:
        return resolve_with_coordinator(
            coordinator,
            matrix,
            requirements,
            constraints=constraints,
            uploaded_prior_to=uploaded_prior_to,
            uploaded_prior_to_overrides=uploaded_prior_to_overrides,
            dist_policy=dist_policy,
            dist_policy_overrides=dist_policy_overrides,
            build_policy=build_policy,
            build_policy_overrides=build_policy_overrides,
            vcs_config=vcs_config,
            local_sources=local_sources,
            vcs_sources=vcs_sources,
            vcs_cache_dir=vcs_cache_dir,
            build_config=build_config,
            resolution_strategy=resolution_strategy,
            align_across_tuples=align_across_tuples,
            preferences=preferences,
        )


def resolve_with_coordinator(  # noqa: PLR0913 - mirrors resolve_universal's surface
    coordinator: FetchCoordinator,
    matrix: Matrix,
    requirements: list[str],
    *,
    constraints: list[str] | None = None,
    uploaded_prior_to: datetime | None = None,
    uploaded_prior_to_overrides: (Mapping[str, datetime | None] | None) = None,
    dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
    dist_policy_overrides: Mapping[str, DistPolicy] | None = None,
    build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    vcs_config: VcsConfig | None = None,
    local_sources: list[LocalSource] | None = None,
    vcs_sources: list[VcsSource] | None = None,
    vcs_cache_dir: Path | None = None,
    build_config: NabProjectConfig | None = None,
    resolution_strategy: str = "highest",
    align_across_tuples: bool = True,
    preferences: dict[str, Version] | None = None,
) -> UniversalResult:
    """Run a universal resolve against an already-open coordinator.

    Splitting the entry point lets callers (and tests) reuse a single
    :class:`FetchCoordinator` across multiple resolves and avoids
    transport setup in unit tests.  See :func:`resolve_universal` for
    the full parameter documentation.
    """
    tuples = matrix.expand()
    _warn_extra_marker_at_root(requirements)
    direct_packages = frozenset(_direct_package_names(requirements))
    initial_preferences: dict[str, Version] = dict(preferences or {})

    return UniversalResult(
        matrix=matrix,
        tuple_results=_run_pass(
            tuples,
            requirements,
            constraints,
            coordinator=coordinator,
            uploaded_prior_to=uploaded_prior_to,
            uploaded_prior_to_overrides=uploaded_prior_to_overrides,
            dist_policy=dist_policy,
            dist_policy_overrides=dist_policy_overrides,
            build_policy=build_policy,
            build_policy_overrides=build_policy_overrides,
            vcs_config=vcs_config,
            local_sources=local_sources,
            vcs_sources=vcs_sources,
            vcs_cache_dir=vcs_cache_dir,
            build_config=build_config,
            resolution_strategy=resolution_strategy,
            direct_packages=direct_packages,
            preferences=initial_preferences,
            align_serial=align_across_tuples,
        ),
    )


def _run_pass(  # noqa: PLR0913
    tuples: list[MatrixTuple],
    requirements: list[str],
    constraints: list[str] | None,
    *,
    coordinator: FetchCoordinator,
    uploaded_prior_to: datetime | None,
    uploaded_prior_to_overrides: Mapping[str, datetime | None] | None = None,
    dist_policy: DistPolicy,
    dist_policy_overrides: Mapping[str, DistPolicy] | None = None,
    build_policy: BuildPolicy,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    vcs_config: VcsConfig | None = None,
    local_sources: list[LocalSource] | None = None,
    vcs_sources: list[VcsSource] | None = None,
    vcs_cache_dir: Path | None = None,
    build_config: NabProjectConfig | None = None,
    resolution_strategy: str,
    direct_packages: frozenset[str],
    preferences: dict[str, Version],
    align_serial: bool,
) -> list[TupleResult]:
    """Run one serial pass of resolution across ``tuples``.

    When ``align_serial=True``, each tuple's pins are threaded forward
    as preferences for the next so the per-Python pins stay aligned
    where the matrix admits it.
    """

    def resolve(t: MatrixTuple, current_prefs: dict[str, Version]) -> TupleResult:
        try:
            tuple_requirements, tuple_constraints = _parse_tuple_inputs(
                requirements, constraints, t.environment
            )
        except ResolutionError as exc:
            return TupleResult(
                tuple_=t,
                success=False,
                error=f"{type(exc).__name__}: {exc}"[:_ERROR_MESSAGE_LIMIT],
            )
        return _resolve_one_tuple(
            coordinator,
            t,
            tuple_requirements,
            tuple_constraints,
            uploaded_prior_to=uploaded_prior_to,
            uploaded_prior_to_overrides=uploaded_prior_to_overrides,
            dist_policy=dist_policy,
            dist_policy_overrides=dist_policy_overrides,
            build_policy=build_policy,
            build_policy_overrides=build_policy_overrides,
            vcs_config=vcs_config,
            local_sources=local_sources,
            vcs_sources=vcs_sources,
            vcs_cache_dir=vcs_cache_dir,
            build_config=build_config,
            resolution_strategy=resolution_strategy,
            preferences=dict(current_prefs),
            direct_packages=direct_packages,
        )

    out: list[TupleResult] = []
    accumulated = dict(preferences)
    for t in tuples:
        tr = resolve(t, accumulated)
        out.append(tr)
        if align_serial and tr.success:
            accumulated.update(tr.pins)
    return out


def _warn_extra_marker_at_root(requirements: list[str]) -> list[str]:
    """Warn when a root requirement uses an ``extra ==`` marker.

    Hole 1.6 plug.  ``packaging`` defaults the ``extra`` variable to
    ``""`` at root, so a marker like ``pkg ; extra == "test"`` evaluates
    False during root parsing and the dep is silently dropped.  The
    user almost certainly meant the ``parent[test]`` extra-of-parent
    syntax, not a self-referential extra marker.

    Returns the list of requirement strings that triggered the warning
    so callers can write tests against the diagnostic without parsing
    the log output.
    """
    flagged: list[str] = []
    for req_str in requirements:
        try:
            req = Requirement(req_str)
        except InvalidRequirement:  # pragma: no cover - re-raised at resolve time
            continue
        marker_text = str(req.marker or "")
        if "extra ==" in marker_text or "extra==" in marker_text:
            flagged.append(req_str)
            logger.warning(
                "Root requirement %r uses an ``extra`` marker; the dep will be "
                "silently dropped because root has no parent extra.  Did you "
                "mean ``pkg[extra]`` (extras-of-package) instead?",
                req_str,
            )
    return flagged


def _direct_package_names(requirements: list[str]) -> set[str]:
    """Return the canonical names of the user's direct dependencies.

    Malformed requirement strings raise downstream during the actual
    resolve; here we just collect names for the lowest-direct strategy
    and let them surface naturally.
    """
    return {canonicalize_name(Requirement(req_str).name) for req_str in requirements}


def _parse_requirements(
    reqs: list[str],
    environment: dict[str, str],
    *,
    kind: str = "requirement",
) -> dict[str, VersionRange]:
    """Convert PEP 508 strings to resolver requirements for ``environment``.

    Marker-gated requirements whose marker evaluates to False in
    ``environment`` are dropped.  This matches what
    ``Provider._classify_requirement`` does for transitive deps;
    we just apply the same rule at the root.

    Repeated package names are intersected into one range; an empty
    intersection raises :class:`ResolutionError`.  ``kind``
    ("requirement" or "constraint") only shapes that error's wording.
    """
    out: dict[str, VersionRange] = {}
    sources: defaultdict[str, list[str]] = defaultdict(list)
    for req_str in reqs:
        req = Requirement(req_str)
        if req.marker is not None and not req.marker.evaluate(environment):
            continue
        name = canonicalize_name(req.name)
        out[name] = out.get(name, VersionRange.full()) & req.specifier.to_range()
        sources[name].append(req_str)
        for extra in req.extras:
            out[f"{name}[{extra}]"] = VersionRange.full()
    raise_for_unsatisfiable(out, sources, kind=kind)
    return out


def _parse_tuple_inputs(
    requirements: list[str],
    constraints: list[str] | None,
    environment: dict[str, str],
) -> tuple[dict[str, VersionRange], dict[str, VersionRange] | None]:
    """Parse a tuple's root requirements and constraints for its env.

    Raises :class:`ResolutionError` if either folds to an empty range.
    """
    parsed_requirements = _parse_requirements(requirements, environment)
    parsed_constraints = (
        _parse_requirements(constraints, environment, kind="constraint")
        if constraints
        else None
    )
    return parsed_requirements, parsed_constraints


def _resolve_one_tuple(  # noqa: PLR0913
    coordinator: FetchCoordinator,
    t: MatrixTuple,
    requirements: dict[str, VersionRange],
    constraints: dict[str, VersionRange] | None,
    *,
    uploaded_prior_to: datetime | None,
    uploaded_prior_to_overrides: Mapping[str, datetime | None] | None = None,
    dist_policy: DistPolicy,
    dist_policy_overrides: Mapping[str, DistPolicy] | None = None,
    build_policy: BuildPolicy,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    vcs_config: VcsConfig | None = None,
    local_sources: list[LocalSource] | None = None,
    vcs_sources: list[VcsSource] | None = None,
    vcs_cache_dir: Path | None = None,
    build_config: NabProjectConfig | None = None,
    resolution_strategy: str = "highest",
    preferences: dict[str, Version] | None = None,
    direct_packages: frozenset[str] = frozenset(),
) -> TupleResult:
    """Run one single-environment resolve for ``t``."""
    provider = UniversalProvider(
        coordinator,
        marker_environment=t.environment,
        root_requirements=requirements,
        uploaded_prior_to=uploaded_prior_to,
        uploaded_prior_to_overrides=uploaded_prior_to_overrides,
        dist_policy=dist_policy,
        dist_policy_overrides=dist_policy_overrides,
        build_policy_overrides=build_policy_overrides,
        vcs_config=vcs_config,
        local_sources=local_sources,
        vcs_sources=vcs_sources,
        vcs_cache_dir=vcs_cache_dir,
        build_config=build_config,
        build_policy=build_policy,
        preferences=preferences,
        resolution_strategy=resolution_strategy,
        direct_packages=direct_packages,
        platform_spec=t.platform_spec,
    )
    resolver: Resolver[str, Version] = Resolver(
        provider,
        range_type=VersionRange,
        root_version="0",
        max_iterations=50_000,
    )
    start = time.monotonic()
    try:
        raw = resolver.resolve(requirements, constraints=constraints)
    except ResolutionError as exc:
        return TupleResult(
            tuple_=t,
            success=False,
            error=f"{type(exc).__name__}: {exc}"[:_ERROR_MESSAGE_LIMIT],
            wall_time=time.monotonic() - start,
            rounds=resolver.stats.rounds,
            decisions=resolver.stats.decisions,
        )
    elapsed = time.monotonic() - start
    pins = {k: v for k, v in raw.items() if split_extra(k)[1] is None}
    try:
        lock_input = build_lock_input_from_provider(
            provider, pins, indexes=coordinator.indexes
        )
    except MissingHashError as exc:
        return TupleResult(
            tuple_=t,
            success=False,
            pins=pins,
            error=f"MissingHashError: {exc}"[:_ERROR_MESSAGE_LIMIT],
            wall_time=elapsed,
            rounds=resolver.stats.rounds,
            decisions=resolver.stats.decisions,
        )
    return TupleResult(
        tuple_=t,
        success=True,
        pins=pins,
        wall_time=elapsed,
        rounds=resolver.stats.rounds,
        decisions=resolver.stats.decisions,
        lock_input=lock_input,
    )
