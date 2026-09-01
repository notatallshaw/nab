"""Index-backed provider for nab-resolver.

Fetches package metadata on demand through a
:class:`~nab_provider.fetch_port.FetchPort`, converting PEP 440/508 types
into nab-resolver Range types.
"""

from __future__ import annotations

import bisect
import logging
import operator
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from itertools import chain
from typing import TYPE_CHECKING, TypeVar, cast

from nab_provider._vendor.packaging.markers import prepare_environment
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from nab_provider._vendor.packaging.utils import canonicalize_name

from ._provider import extras as _extras
from ._provider import listing as _listing
from ._provider import listing_diagnosis as _diagnosis
from ._provider import lookahead as _lookahead
from ._provider import metadata_resolver as _metadata_resolver
from ._provider import priority as _priority
from ._provider import sources as _sources
from .conflict_kind import EMPTY_MEMBERSHIP_SETS
from .errors import (
    ForeignMetadataError,
    IncompatiblePythonError,
    IndexAccessError,
    InvalidUploadTimeError,
    MalformedSimpleResponseError,
    MetadataError,
    MetadataHashMismatchError,
    MissingExtraError,
    OverrideConflictError,
    SdistHashMismatchError,
    SiblingMetadataDivergenceError,
    SourceBuildPolicyError,
    SourceNameMismatchError,
    UnserveableUrlError,
    UnsupportedSdistError,
    WheelHashMismatchError,
)
from .extra_keys import join_extra, split_extra
from .metadata import WheelMetadata
from .policy import (
    ArchiveSource,
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    ExtrasMode,
    LocalSource,
    ResolutionStrategy,
    SourceRequest,
    VcsSource,
)
from .policy import (
    ResolveMode as ResolveMode,  # noqa: PLC0414  (re-export, importable from here)
)
from .records import DistFile, SdistFile, WheelFile
from .target import host_environment
from .vcs_admission import (
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from nab_provider._vendor.packaging.markers import Marker
    from nab_provider._vendor.packaging.requirements import Requirement
    from nab_provider._vendor.packaging.version import Version
    from nab_resolver.types import Incompatibility, RangeProtocol

    from .diagnostics import Diagnostic
    from .fetch_port import FetchPort, Waitable
    from .overrides import IndexOverride, PackageOverride
    from .tags import TagSet
    from .target import ResolveTarget

__all__ = [
    "ArchiveSource",
    "BuildPolicy",
    "DecisionOrder",
    "DistFile",
    "DistPolicy",
    "ExtrasMode",
    "ForeignMetadataError",
    "IncompatiblePythonError",
    "InvalidUploadTimeError",
    "ListingFilterCache",
    "LocalSource",
    "MetadataError",
    "MissingExtraError",
    "Provider",
    "ProviderStats",
    "ResolutionStrategy",
    "SiblingMetadataDivergenceError",
    "SourceNameMismatchError",
    "UnsupportedSdistError",
    "UnsupportedVcsError",
    "VcsConfig",
    "VcsPolicy",
    "VcsSource",
    "join_extra",
    "split_extra",
]


logger = logging.getLogger(__name__)

_PreparedT = TypeVar("_PreparedT")


@dataclass
class ProviderStats:
    """Counters describing what the provider did during a resolve.

    Complements :class:`nab_resolver.resolver.ResolverStats` by tracking the
    PyPI/wheel layer (listing fetches, metadata reads, filter rejections).
    Used by benchmarks to measure prefetch and look-ahead wins.
    """

    listings_fetched: int = 0
    metadata_fetched: int = 0
    sdist_pkg_info_fetched: int = 0
    wheel_metadata_range_fetched: int = 0
    wheel_metadata_range_full_body: int = 0
    wheel_metadata_range_unsupported: int = 0
    wheel_metadata_range_missing: int = 0
    distributions_seen: int = 0
    wheels_seen: int = 0
    sdists_seen: int = 0
    excluded_by_python: int = 0
    excluded_by_time: int = 0
    excluded_by_dist_policy: int = 0
    excluded_by_build_policy: int = 0
    excluded_by_wheel_tags: int = 0
    excluded_versions_no_compatible_wheel: int = 0
    sdist_pyproject_fallbacks: int = 0
    get_dependencies_calls: int = 0
    choose_version_calls: int = 0
    prioritize_calls: int = 0
    look_ahead_rejections: int = 0


_STAT_FIELDS = tuple(stat.name for stat in fields(ProviderStats))

# Every counter of a ProviderStats, in _STAT_FIELDS order.
_counters = operator.attrgetter(*_STAT_FIELDS)


def _replay(stats: ProviderStats, delta: tuple[int, ...]) -> None:
    """Add the counters a memoised pass raised to ``stats``."""
    for name, count in zip(_STAT_FIELDS, delta, strict=True):
        if count:
            setattr(stats, name, getattr(stats, name) + count)


def _since(before: tuple[int, ...], stats: ProviderStats) -> tuple[int, ...]:
    """Return how far each counter of ``stats`` rose since ``before``."""
    return tuple(map(operator.sub, _counters(stats), before))


def _restore(stats: ProviderStats, before: tuple[int, ...]) -> None:
    """Take back every counter a bracketed pass bumped on ``stats``."""
    _replay(stats, tuple(map(operator.sub, before, _counters(stats))))


class ListingFilterCache:
    """Base listing-filter results shared across the targets of one resolve.

    The pre-tag half of the listing filter (see
    :func:`nab_provider._provider.listing.base_distributions`) reads the
    listing's files, the policy config, and the target Python, and has no
    platform axis, so targets that differ only by platform recompute an
    identical list.  Memoising it per (package, Python) leaves only the
    wheel-tag pass to run per target.

    Most of that half reads no Python either: the version parse and the
    dist-policy exclusion do the same work for every Python of a matrix,
    and only the Requires-Python and upload-cutoff drops differ.  When the
    resolve spans more than one Python, :attr:`shares_pythons` is set and
    :meth:`prepared` memoises that inner pass per package, so a
    three-Python matrix walks each listing's files once rather than three
    times.  A one-Python resolve has nothing to share and skips the split,
    since materialising the intermediate list would cost it a pass it does
    not get back.

    One instance is only valid across providers that share a coordinator
    and a policy config, as the targets of one resolve do.
    """

    def __init__(self, pythons: int = 1) -> None:
        """Create an empty cache for a resolve over ``pythons`` Python releases."""
        self.shares_pythons = pythons > 1
        self._entries: dict[
            tuple[str, str | None],
            tuple[list[tuple[Version, DistFile]], tuple[int, ...]],
        ] = {}
        self._prepared: dict[str, tuple[object, tuple[int, ...]]] = {}
        self._diagnosed: dict[tuple[str, str | None], object] = {}

    def filtered(
        self,
        package: str,
        python_version: str | None,
        stats: ProviderStats,
        compute: Callable[[], list[tuple[Version, DistFile]]],
    ) -> list[tuple[Version, DistFile]]:
        """Return the filter result for ``package``, running ``compute`` once.

        A memo hit replays the counters the filter raised onto ``stats``, so
        every target still reports the files it would have walked.
        """
        entry = self._entries.get((package, python_version))
        if entry is not None:
            result, delta = entry
            _replay(stats, delta)
            return list(result)

        before = _counters(stats)
        result = compute()

        self._entries[(package, python_version)] = (list(result), _since(before, stats))
        return result

    def prepared(
        self,
        package: str,
        stats: ProviderStats,
        compute: Callable[[], _PreparedT],
    ) -> _PreparedT:
        """Return the Python-invariant filter half, running ``compute`` once.

        Keyed by package alone, so every Python of a matrix shares one
        result.  A hit replays that pass's counters onto ``stats`` the way
        :meth:`filtered` does, and the caller must treat the result as
        read-only, since the other Pythons hold it too.
        """
        entry = self._prepared.get(package)
        if entry is not None:
            result, delta = entry
            _replay(stats, delta)
            return cast("_PreparedT", result)

        before = _counters(stats)
        result = compute()

        self._prepared[package] = (result, _since(before, stats))
        return result

    def diagnosed(
        self,
        package: str,
        python_version: str | None,
        compute: Callable[[], _PreparedT],
    ) -> _PreparedT:
        """Return the diagnosis walk's base pass, running ``compute`` once.

        Keyed the way :meth:`filtered` is, because the rungs the walk shares
        with the filter's base pass read the same three inputs: the listing,
        the policy config and the target Python.  A matrix whose tuples
        differ only by platform therefore attributes each listing once per
        Python rather than once per failing tuple.

        No counters are replayed: the walk's caller brackets and restores
        every counter its predicates bump, so a hit and a miss leave the
        same totals behind.  The result is held by every target that shares
        the memo, so callers must treat it as read-only.
        """
        key = (package, python_version)
        entry = self._diagnosed.get(key)
        if entry is None:
            entry = compute()
            self._diagnosed[key] = entry
        return cast("_PreparedT", entry)


# Sentinel for "this override does not set the field".  Distinct from
# ``None``, which is a real value (a disabled upload-time cutoff).
_UNSET = object()


def _unset_if_none(value: object) -> object:
    """Map ``None`` to ``_UNSET``, passing every other value through.

    Most policy fields store ``None`` to mean "unset" on the override
    dataclasses, so wrapping their attribute access in this helper yields
    the ``_UNSET``-or-value shape :meth:`Provider._effective_field`
    expects.  :func:`_uploaded_prior_to_value` builds that shape itself,
    because there ``None`` is a real value (a disabled cutoff).
    """
    if value is None:
        return _UNSET
    return value


def _uploaded_prior_to_value(override: PackageOverride | IndexOverride) -> object:
    """Upload-time value: a datetime, ``None`` (disabled), or ``_UNSET``."""
    if override.uploaded_prior_to is not None:
        return override.uploaded_prior_to
    if override.uploaded_prior_to_disabled:
        return None
    return _UNSET


def _dist_policy_value(override: PackageOverride | IndexOverride) -> object:
    """Dist-policy value: a :class:`DistPolicy`, or ``_UNSET`` when unset."""
    return _unset_if_none(override.dist_policy)


# What each field a remedy can name is read with, so the layer lookup and
# the effective value read the same surfaces the same way.
_SOURCE_VALUES: dict[str, Callable[[PackageOverride | IndexOverride], object]] = {
    "uploaded-prior-to": _uploaded_prior_to_value,
    "dist-policy": _dist_policy_value,
}


# The markers an extras proxy records, which read the proxy's own name
# rather than a listing the base package's filter emptied.
_EXTRA_KINDS = frozenset(
    {
        _diagnosis.ReasonKind.EXTRA_UNDECLARED,
        _diagnosis.ReasonKind.EXTRA_METADATA,
        _diagnosis.ReasonKind.EXTRA_NARROWED,
    }
)


# Past this many exclusions the requirement reads worse than the range it states.
_MAX_EXCLUSIONS = 3


def _declared_ranges(
    recorded: _lookahead.DepRangeUnion,
) -> tuple[RangeProtocol[Version], ...]:
    """Return the ranges a blocker's rejected candidates declared on it.

    Stating them one by one keeps the line spellable where their union is
    not; a group that recorded no range falls back to its union.
    """
    return recorded.declared or (recorded.union,)


def _requirement_over_listing(
    constraint: VersionRange,
    universe: Sequence[Version],
    selected: Sequence[Version],
) -> VersionRange | None:
    """Return the requirement admitting exactly ``selected`` out of ``universe``.

    Bounds ``selected`` on each side ``constraint`` is bounded, then excludes
    by name every other listed version those bounds admit.  Built out of
    specifiers, so it has a spelling to render.

    ``None`` when a bound carries a local segment, which an ordering specifier
    does not accept, when the span holds more than ``_MAX_EXCLUSIONS`` versions
    to exclude, or when excluding one by name would take a selected version
    with it.
    """
    clauses: list[str] = []
    if not VersionRange.from_bounds(None, selected[0]).is_subset(constraint):
        clauses.append(f">={selected[0]}")
    if not VersionRange.from_bounds(selected[-1], None).is_subset(constraint):
        clauses.append(f"<={selected[-1]}")

    try:
        bounded = SpecifierSet(",".join(clauses)).to_range()
    except InvalidSpecifier:
        return None

    chosen = set(selected)
    holes: list[Version] = []
    for version in universe:
        if version in bounded and version not in chosen:
            holes.append(version)
            if len(holes) > _MAX_EXCLUSIONS:
                return None

    if not holes:
        return bounded

    clauses.extend(f"!={hole}" for hole in holes)
    stated = SpecifierSet(",".join(clauses)).to_range()

    # ``!=1.0`` also excludes ``1.0+cu118``: PEP 440 ignores a candidate's
    # local label when the specifier carries none.
    if any(version not in stated for version in selected):
        return None
    return stated


class Provider:
    """Lazy index-backed provider for nab-resolver.

    Fetches version lists and metadata through ``coordinator``, the
    port the host supplies; nab's own implementation submits them to a
    background asyncio loop, so transitive deps land during resolution.

    ``target`` is the environment the resolve is for: its markers gate
    every dependency, its Python filters candidates by Requires-Python,
    and its wheel tags filter candidates by PEP 425 compatibility, so a
    version whose only wheels the target cannot install is a version the
    resolver never sees.  Left unset, markers evaluate against the host
    and neither filter runs, since nothing has said which machine the
    resolve targets.

    ``constraints`` are the user's version bounds, keyed by package name;
    a lookup under an extras proxy's ``name[extra]`` key answers with the
    base's bound.  The provider reads them when deciding whether a missing
    root extra is worth reporting.

    ``preferences`` are versions another resolve already decided, tried
    first when they are usable here.  A multi-target resolve passes the
    pins of an already-resolved target, which aligns the matrix on one
    version where every target can take it.  A package the strategy wants
    lowest for ignores the preference and takes its own floor.

    ``listing_filter_cache`` shares the platform-independent half of the
    listing filter with the other targets of the same resolve; see
    :class:`ListingFilterCache`.

    ``decision_order`` chooses whether the decision scan may rank a
    package on whether its listing has landed yet.  See
    :meth:`settled_listing`.
    """

    # Declared in ``_provider.listing``; the scan reads it off the instance.
    PREFETCH_BATCH: int = _listing.PREFETCH_BATCH

    # Batches kept fetching during the previous batch's await in
    # ``choose_version``.  Depth>=2 reordered listing arrivals and
    # destabilised hard scenarios.
    PREFETCH_DEPTH: int = 1

    # Versions front-loaded at the top of the pipelined scan, so a walk
    # that runs past the first ``PREFETCH_BATCH`` hits cache instead of one
    # RTT per visit.  The scan only starts once the first candidate is
    # rejected, so accepting that candidate costs nothing.
    DEEP_PREFETCH_COUNT: int = 64

    # Per-call cap on decision-aware look-ahead rejections so tight version
    # clusters do not emit a flood of redundant grouped binary clauses.
    _BROAD_LA_REJECT_CAP: int = 64

    # When the scan accumulates this many rejections, all sharing the same
    # ``(blocker_pkg, blocker_version)`` (no range/root/metadata blocks),
    # abandon look-ahead for this scan: drop its pending clauses, return
    # the first candidate unchecked, and let the resolver decide it.
    # ``get_dependencies`` will then add the real dep-range clause, which
    # pubgrub's conflict resolution can use to back-jump the offending
    # blocker decision.  Look-ahead's grouped binary clauses are too narrow
    # to drive that back-jump on their own
    # (``docs/analysis/nab_lookahead_monolithic_backjump.md``).
    #
    # Set low because the trigger is conservative: a unique
    # ``(blocker_pkg, blocker_version)`` repeating across every rejection
    # is already a strong signal.
    _LOOKAHEAD_ABORT_THRESHOLD = 4

    # Max force-backtracks one blocker can drive per resolution.
    # One-shot misses sustained culprits; unlimited oscillates on
    # blockers that are also the right pin.
    _MAX_FORCE_BACKTRACKS_PER_PKG = 3

    TIER_AFFECTED = _priority.TIER_AFFECTED
    TIER_NORMAL = _priority.TIER_NORMAL
    TIER_CULPRIT = _priority.TIER_CULPRIT
    CONFLICT_THRESHOLD = _priority.CONFLICT_THRESHOLD
    CULPRIT_DEMOTE_THRESHOLD = _priority.CULPRIT_DEMOTE_THRESHOLD

    def __init__(  # noqa: PLR0913, PLR0915, PLR0917 - resolver config is wide; bundling all flags into one bag is worse for callers
        self,
        coordinator: FetchPort,
        target: ResolveTarget | None = None,
        root_requirements: dict[str, VersionRange] | None = None,
        uploaded_prior_to: datetime | None = None,
        extras_mode: ExtrasMode = ExtrasMode.ERROR_USER,
        root_extras: set[tuple[str, str]] | None = None,
        dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
        build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
        package_overrides: Sequence[PackageOverride] = (),
        index_overrides: Mapping[str, IndexOverride] | None = None,
        vcs_config: VcsConfig | None = None,
        local_sources: list[LocalSource] | None = None,
        vcs_sources: list[VcsSource] | None = None,
        vcs_cache_dir: Path | None = None,
        archive_sources: list[ArchiveSource] | None = None,
        archive_cache_dir: Path | None = None,
        resolution_strategy: ResolutionStrategy | str = ResolutionStrategy.HIGHEST,
        direct_packages: frozenset[str] | None = None,
        preferences: Mapping[str, Version] | None = None,
        listing_filter_cache: ListingFilterCache | None = None,
        *,
        constraints: Mapping[str, VersionRange] | None = None,
        trust_unverified_sdist_deps: bool = False,
        decision_order: DecisionOrder = DecisionOrder.ARRIVAL,
    ) -> None:
        """Construct the provider; see the class docstring for parameters."""
        if isinstance(resolution_strategy, str):
            try:
                resolution_strategy = ResolutionStrategy(resolution_strategy)
            except ValueError as exc:
                valid = sorted(s.value for s in ResolutionStrategy)
                msg = (
                    f"resolution_strategy must be one of {valid!r};"
                    f" got {resolution_strategy!r}"
                )
                raise ValueError(msg) from exc

        self.coordinator = coordinator
        self.target = target
        self.uploaded_prior_to = uploaded_prior_to
        # The pre-tag half of the listing filter, shared with the other
        # targets of this resolve.  ``None`` computes it here instead.
        self.listing_filter_cache = listing_filter_cache

        self.extras_mode = extras_mode
        self.root_extras = root_extras or set()
        self.dist_policy = dist_policy
        self.build_policy = build_policy
        # Opt-out: trust a pre-2.2 sdist's PKG-INFO deps as final instead of
        # routing through the dynamic path. Off by default (strict PEP 643).
        self.trust_unverified_sdist_deps = trust_unverified_sdist_deps
        self._resolution_strategy = resolution_strategy
        # The scan asks this once per package, so keep the answer.
        self.settle_listings = decision_order is DecisionOrder.STABLE
        self._direct_packages: frozenset[str] = direct_packages or frozenset()
        # Versions another target already decided, tried first when they are
        # usable here: uv-style cross-target alignment ("target A picked numpy
        # 2.2.6, ask target B to try 2.2.6 first").  Keyed canonically, to
        # match the provider's own naming scheme.
        self._preferences: dict[str, Version] = {
            canonicalize_name(name): version
            for name, version in (preferences or {}).items()
        }
        self._package_overrides = tuple(package_overrides)
        self._index_overrides: Mapping[str, IndexOverride] = index_overrides or {}

        # With no override on either surface, every lookup is the global default.
        self.has_overrides = bool(self._package_overrides or self._index_overrides)

        # True when any override sets a time cutoff or disables one, so the
        # listing filter can skip the per-candidate dispatch otherwise.
        self.overrides_set_time = any(
            o.uploaded_prior_to is not None or o.uploaded_prior_to_disabled
            for o in self._package_overrides
        ) or any(
            o.uploaded_prior_to is not None or o.uploaded_prior_to_disabled
            for o in self._index_overrides.values()
        )

        self.vcs_config = vcs_config or VcsConfig()
        self.local_sources = _sources.index_local_sources(self, local_sources or [])
        self.vcs_cache_dir = vcs_cache_dir
        self.vcs_pins: dict[str, str] = {}
        self.vcs_sources = _sources.index_vcs_sources(self, vcs_sources or [])
        self.archive_cache_dir = archive_cache_dir
        self.archive_sources = _sources.index_archive_sources(
            self, archive_sources or []
        )

        # ``env_with_extra`` is the marker environment plus the empty
        # lockfile-only set variables (see EMPTY_MEMBERSHIP_SETS).  Without a
        # target it comes from the host and ``python_version`` stays None,
        # which turns the Requires-Python filter off: nothing has declared the
        # Python a candidate could be rejecting (see _provider.listing).
        if target is None:
            self.python_version: str | None = None
            self.environment: dict[str, str] = host_environment()
            self.env_with_extra: dict[str, str | frozenset[str]] = {
                **self.environment,
                **EMPTY_MEMBERSHIP_SETS,
            }
        else:
            self.python_version = target.python_full_version
            self.environment = dict(target.marker_env)
            self.env_with_extra = target.env_with_membership()

        # The wheel tags the target installs.  ``None`` turns the tag filter
        # off, which happens when no target is set (nothing has said which
        # machine the resolve is for) or when a marker overlay moved the target
        # off its tag axis, leaving the tags describing another machine (see
        # ``ResolveTarget.with_marker_overrides``).
        self.wheel_tags: TagSet | None = (
            target.tags if target is not None and target.tags_faithful else None
        )

        # Wheels the tag filter dropped, keyed by (canonical name, version),
        # for the lock's per-package omitted count.
        self.tag_excluded_wheels_by_version: dict[tuple[str, Version], int] = {}

        # Per-package attribution of the listing filter's drops, built on the
        # failure path and never during a resolve.  ``None`` records that the
        # index served nothing to attribute.
        self.listing_diagnoses: dict[str, _diagnosis.ListingDiagnosis | None] = {}

        self.root_requirements = root_requirements or {}
        self.constraints: Mapping[str, VersionRange] = constraints or {}
        self.versions_cache: dict[str, list[tuple[Version, DistFile]]] = {}
        self.deps_cache: dict[tuple[str, Version], dict[str, VersionRange]] = {}
        # Metadata the pipelined scan prefetched, as ``(version string, sidecar
        # URL)``, decoded on the first read of that candidate.  A candidate the
        # solver never reads is never decoded.
        self.pending_metadata_parses: dict[tuple[str, Version], tuple[str, str]] = {}
        # One range per distinct dependency specifier text, shared by every
        # parent that names it.
        self.specifier_ranges: dict[str, VersionRange] = {}
        # Deliberately unbounded and never evicted mid-resolve: it keeps every
        # parsed Requirement (hence every Marker) alive for the whole resolve,
        # which is what makes the id(marker)-keyed marker caches below safe
        # against id reuse. Do not bound it without re-keying those caches.
        self.metadata_cache: dict[tuple[str, Version], WheelMetadata] = {}
        # Per-extra deps, split out of the cached metadata on first read: a
        # release whose extras nothing selects never pays for the split.  Read
        # it through ``extra_deps_map``.
        self._extra_deps: dict[
            tuple[str, Version], dict[str, dict[str, VersionRange]] | None
        ] = {}
        # Direct-URL deps gated behind a provided-but-unrequested extra. The
        # refusal is deferred until the extra is selected, so a plain resolve of
        # a package that merely offers such an extra is not aborted.
        self.deferred_url_extras: dict[
            tuple[str, Version], dict[str, list[tuple[Requirement, str]]]
        ] = {}

        # Memoised sdist-rejections so re-tries do not re-parse PKG-INFO or
        # rebuild.  Value is the cached error message so a re-ask raises the
        # same rejection.
        self._unsupported_sdists: dict[tuple[str, Version], str] = {}

        # Memoised metadata-parse failures (malformed Requires-Dist, etc.)
        # keyed by (canonical_name, Version).  Value is the cached error
        # string so the look-ahead diagnostic stays consistent across
        # repeated lookups without re-parsing the broken text.
        self._invalid_metadata: dict[tuple[str, Version], str] = {}

        # Nested matching cache: prioritize is called many times per resolve
        # so the per-call (normalized, range) tuple alloc is worth avoiding.
        self.matching_cache: dict[str, dict[RangeProtocol[Version], int]] = {}

        # Scan counter, plus the scan in which each name was last seen with its
        # listing still in flight.  See ``arrived_listing``.
        self._scan_generation = 0
        self._absent_listing_scan: dict[str, int] = {}

        # Requires-Python compatibility, keyed by the raw specifier string.
        self.requires_python_cache: dict[str, bool] = {}

        # Every dependency marker evaluated here.  The lock's ``environments``
        # declaration is built from them: it declares the variables they named
        # and how their clauses read, so an installer whose environment answers
        # differently is refused rather than handed a package set chosen for
        # another machine.
        self.consulted_markers: set[Marker] = set()

        # Marker evaluation caches keyed by id(marker); requirement parsing is
        # cached upstream so each distinct marker text shares one Marker. The
        # id keying is safe because metadata_cache keeps every evaluated marker
        # alive (see its note above).
        self.marker_base_cache: dict[int, bool] = {}
        self.marker_extra_cache: dict[int, dict[str, bool]] = {}

        # ``Marker.evaluate`` rebuilds its environment on every call, and this
        # resolve's is fixed, so prepare it up front.  The base environment is
        # never mutated; the extras one has ``extra`` written into it before
        # each evaluation.
        self.prepared_environment = prepare_environment(self.env_with_extra)
        self.prepared_extra_environment = prepare_environment(self.env_with_extra)

        # (base, extra, normalized_name) per input package string.
        self._package_parts: dict[str, tuple[str, str | None, str]] = {}

        # Fast-path priority cache: (Range, normalized name, affected count,
        # priority) per package string.  Range identity is sound because
        # solution.get returns the same object until it changes.
        self.priority_cache: dict[
            str, tuple[RangeProtocol[Version], str, int, tuple[int, int, bool]]
        ] = {}

        # What the speculative prefetch has already seen: the names it
        # requested listings for, and the candidates the transitive path
        # walked. A dep named by several parents reaches both once per parent.
        self.speculative_listings: set[str] = set()
        self.speculative_candidates: set[tuple[str, Version]] = set()

        # Derived views of versions_cache, built lazily alongside the listing.
        self.versions_only_cache: dict[str, list[Version]] = {}
        self.version_dists_cache: dict[str, _metadata_resolver.VersionDists] = {}

        # Widening state: the ascending versions_only view per normalized
        # name, the span-widened parent range per decided (name, version),
        # and the pure neighbor-gap range per (name, version).
        self._ascending_versions_cache: dict[str, list[Version]] = {}
        self._widened_ranges: dict[tuple[str, Version], VersionRange] = {}
        self._gap_widened_ranges: dict[tuple[str, Version], VersionRange] = {}

        self.solution_ranges: Mapping[str, RangeProtocol[Version]] = {}
        self.solution_decisions: Mapping[str, Version] = {}
        self.pending_clauses: list[Incompatibility[str, Version]] = []
        self.pending_blocks: defaultdict[tuple[str, str, Version], list[Version]] = (
            defaultdict(list)
        )
        self.pending_range_blocks: defaultdict[
            tuple[str, str, RangeProtocol[Version]], list[Version]
        ] = defaultdict(list)

        # Self-dependency rejections, keyed by (candidate, the range it
        # declared on itself, the positive range the solution holds).  The
        # merged clause needs only the declared range; the positive range is
        # carried for the no-versions reason line.
        self.pending_self_blocks: defaultdict[
            tuple[str, VersionRange, RangeProtocol[Version]], list[Version]
        ] = defaultdict(list)

        # The dep range each rejected candidate declared for the blocker,
        # unioned per group.  Feeds the membership widening of the flushed
        # blocker term, and the no-versions message, which names the range the
        # candidate declared rather than negating the blocker it hit.
        self.pending_decision_dep_ranges: defaultdict[
            tuple[str, str, Version], _lookahead.DepRangeUnion
        ] = defaultdict(_lookahead.DepRangeUnion.zero)
        self.pending_range_dep_ranges: defaultdict[
            tuple[str, str, RangeProtocol[Version]], _lookahead.DepRangeUnion
        ] = defaultdict(_lookahead.DepRangeUnion.zero)

        # Root-requirement rejections, keyed by (candidate, blocker, the
        # candidate's dependency range, the blocker's root range).
        self.pending_root_blocks: defaultdict[
            tuple[str, str, RangeProtocol[Version], RangeProtocol[Version]],
            list[Version],
        ] = defaultdict(list)

        # Metadata-error rejections, carrying the message so the failure can
        # name the real cause (sdist build needed, malformed PKG-INFO, etc).
        # Keyed by version so a re-checked candidate is counted once.
        self.pending_metadata_blocks: defaultdict[
            str, dict[Version, _diagnosis.MetadataBlock]
        ] = defaultdict(dict)

        # Last NO_VERSIONS marker per package.  Rendered into a sentence only
        # if the resolve goes on to fail; see ``get_no_versions_reason``.
        self._no_versions_reasons: dict[str, _diagnosis.NoVersionsReason] = {}

        # Metadata errors behind the permanent bans, keyed by canonical name
        # and unioned across scans.
        self._metadata_ban_blocks: dict[
            str, dict[Version, _diagnosis.MetadataBlock]
        ] = {}

        # Blocker packages queued for force back-track by the resolver after
        # the next ``choose_version`` returns.  Populated by the look-ahead
        # abort path; drained by ``consume_force_backtrack_targets``.  uv-style
        # signal: when the scan rejects many candidates all blamed on the same
        # blocker decision, we have strong evidence the blocker is the culprit
        # and ask the resolver to back-jump it now rather than burn a full
        # natural-path conflict cycle.
        self._force_backtrack_targets: list[str] = []

        # Per-blocker fire count for force-backtrack. Each abort that
        # names the blocker bumps the count. The abort path stops
        # queueing once ``_MAX_FORCE_BACKTRACKS_PER_PKG`` is reached.
        self._force_backtrack_counts: dict[str, int] = {}

        # Set while ``has_satisfying_version`` probes.  Both look-ahead
        # shortcuts can return a version the decided blocker rejects: the abort
        # returns its first pick, and past ``_BROAD_LA_REJECT_CAP`` rejections
        # the scan drops decision-checking.  Suppress both so the probe scans to
        # a real answer.
        self._probing_satisfiable = False

        self.stats = ProviderStats()

        if self.root_requirements:
            for pkg in self.root_requirements:
                _, _, normalized = self.split_and_normalize(pkg)
                if (
                    normalized in self.local_sources
                    or normalized in self.vcs_sources
                    or normalized in self.archive_sources
                ):
                    continue
                self.coordinator.request_listing(normalized)

    @property
    def extra_deps_map(self) -> _metadata_resolver.ExtraDepsMap:
        """Per-extra deps per parsed release, split out when first read.

        A new view over ``_extra_deps`` each time it is asked for.  A stored
        one would hold this provider in a reference cycle, and a matrix resolve
        would then keep every finished target's caches until the collector ran.
        """
        return _metadata_resolver.ExtraDepsMap(self, self._extra_deps)

    def defer_extra_deps(self, cache_key: tuple[str, Version]) -> None:
        """Record a release as parsed, with its per-extra split not built yet."""
        self._extra_deps[cache_key] = None

    def fetch_versions(self, package: str) -> list[tuple[Version, DistFile]]:
        """See :func:`nab_provider._provider.listing.fetch_versions`."""
        return _listing.fetch_versions(self, package)

    def serving_index(self, canonical_name: str) -> str | None:
        """Return the index that served ``canonical_name``'s listing, or None.

        Drawn from the coordinator's record of which configured index a
        package's listing came from; ``None`` before any listing resolves
        or for synthetic (local / VCS / archive) sources.
        """
        return self.coordinator.index.get_listing_index(canonical_name)

    def effective_build_policy(
        self,
        canonical_name: str,
        version: Version,
        index_name: str | None = None,
    ) -> BuildPolicy:
        """Return the build policy for ``canonical_name==version`` from ``index_name``.

        Caller must canonicalise the name first.  A per-package override
        whose version range contains ``version`` and a per-index override
        for ``index_name`` that both set ``build-policy`` are a conflict
        (raises :class:`~nab_provider.errors.OverrideConflictError`).
        """
        result = self._effective_field(
            canonical_name,
            version,
            index_name,
            field="build-policy",
            value=lambda o: _unset_if_none(o.build_policy),
        )
        if result is _UNSET:
            return self.build_policy
        assert isinstance(result, BuildPolicy)
        return result

    def effective_build_policy_for_source(self, canonical_name: str) -> BuildPolicy:
        """Return the build policy for a synthetic local/VCS/archive source.

        These sources have no serving index and their version is not
        known until the backend runs, so the version-scoped lookup does
        not apply.  A per-package override is honoured only when it uses a
        bare-name requirement (full range); a version-scoped override does
        not govern such a source's build decision.
        """
        for override in self._package_overrides:
            if (
                override.name == canonical_name
                and override.build_policy is not None
                and not str(override.requirement.specifier)
            ):
                return override.build_policy
        return self.build_policy

    def effective_dist_policy(
        self,
        canonical_name: str,
        version: Version,
        index_name: str | None = None,
    ) -> DistPolicy:
        """Return the dist policy for ``name==version`` served from ``index_name``."""
        if not self.has_overrides:
            return self.dist_policy

        result = self._effective_field(
            canonical_name,
            version,
            index_name,
            field="dist-policy",
            value=_dist_policy_value,
        )
        if result is _UNSET:
            return self.dist_policy
        assert isinstance(result, DistPolicy)
        return result

    def effective_uploaded_prior_to(
        self,
        canonical_name: str,
        version: Version,
        index_name: str | None = None,
    ) -> datetime | None:
        """Return the upload-time cutoff for ``canonical_name==version``, or None.

        A matching override may set an absolute cutoff or disable it (the
        ``false`` form); a disabling override returns ``None``.  Falls
        back to the global ``uploaded_prior_to`` when no override sets the
        field.  A per-package (range-matching) and a per-index override
        that both set the field are a conflict.
        """
        result = self._effective_field(
            canonical_name,
            version,
            index_name,
            field="uploaded-prior-to",
            value=_uploaded_prior_to_value,
        )
        if result is _UNSET:
            return self.uploaded_prior_to
        # ``_uploaded_prior_to_value`` yields only ``None`` (a disabled
        # cutoff) or a datetime.
        return cast("datetime | None", result)

    def effective_trust_unverified(
        self,
        canonical_name: str,
        version: Version,
        index_name: str | None = None,
    ) -> bool:
        """Return the sdist-trust flag for ``canonical_name==version``."""
        result = self._effective_field(
            canonical_name,
            version,
            index_name,
            field="dist-policy.trust-unverified-deps",
            value=lambda o: _unset_if_none(o.dist_trust_unverified_deps),
        )
        if result is _UNSET:
            return self.trust_unverified_sdist_deps
        assert isinstance(result, bool)
        return result

    def effective_dependencies(
        self, canonical_name: str, version: Version
    ) -> tuple[Requirement, ...] | None:
        """Return the ``dependencies`` metadata override for ``name==version``.

        Metadata overrides live only on the per-package surface, so this
        is a single-surface lookup with no per-index arbitration and no
        :class:`OverrideConflictError`.  Returns the replacement
        requirement tuple (possibly empty, meaning "no runtime deps")
        when a range-matching override sets ``dependencies``, else
        ``None`` when nothing overrides them.
        """
        override = self._matching_package_override(
            canonical_name,
            version,
            lambda o: _unset_if_none(o.dependencies),
        )
        if override is None:
            return None
        return override.dependencies

    def effective_requires_python(
        self, canonical_name: str, version: Version
    ) -> str | None:
        """Return the ``requires-python`` metadata override for ``name==version``.

        Single-surface (per-package only), like
        :meth:`effective_dependencies`.  Returns the raw specifier string a
        range-matching override sets (which may be ``""``, meaning "no Python
        requirement"), else ``None`` when nothing overrides it.
        """
        if not self._package_overrides:
            return None

        override = self._matching_package_override(
            canonical_name,
            version,
            lambda o: _unset_if_none(o.requires_python),
        )
        if override is None:
            return None
        return override.requires_python

    def effective_provides_extra(
        self, canonical_name: str, version: Version
    ) -> tuple[str, ...] | None:
        """Return the ``provides-extra`` metadata override for ``name==version``.

        Single-surface (per-package only).  Returns the declared extras
        (possibly empty, meaning "no extras") a range-matching override
        sets, else ``None`` when nothing overrides them.
        """
        override = self._matching_package_override(
            canonical_name,
            version,
            lambda o: _unset_if_none(o.provides_extra),
        )
        if override is None:
            return None
        return override.provides_extra

    def _source_metadata_override(
        self, canonical_name: str
    ) -> tuple[tuple[Requirement, ...] | None, str | None, tuple[str, ...] | None]:
        """Resolve a local/VCS/archive source's metadata override bundle.

        These sources have no listing and their version is not known
        until materialised, so a metadata override governs them only through
        a bare-name requirement (full range); a version-scoped override does
        not match a source.  Mirrors
        :meth:`effective_build_policy_for_source`.  Each field is taken from
        the first bare-name entry that sets it (a present ``""`` or ``()`` is
        a set value); the parse-time overlap rules make that entry unique.
        """
        deps: tuple[Requirement, ...] | None = None
        requires_python: str | None = None
        provides_extra: tuple[str, ...] | None = None
        for override in self._package_overrides:
            if override.name != canonical_name:
                continue
            if str(override.requirement.specifier):
                continue
            if deps is None and override.dependencies is not None:
                deps = override.dependencies
            if requires_python is None and override.requires_python is not None:
                requires_python = override.requires_python
            if provides_extra is None and override.provides_extra is not None:
                provides_extra = override.provides_extra
        return (deps, requires_python, provides_extra)

    def effective_metadata_override(
        self, canonical_name: str, version: Version
    ) -> tuple[tuple[Requirement, ...] | None, str | None, tuple[str, ...] | None]:
        """Resolve the ``(dependencies, requires_python, provides_extra)`` override.

        A local/VCS/archive source selects bare-name-only (its materialised
        version is not knowable to the user when writing the selector); every
        other candidate selects version-scoped.  Each field resolves
        independently, so the three may come from different entries.
        """
        if (
            canonical_name in self.local_sources
            or canonical_name in self.vcs_sources
            or canonical_name in self.archive_sources
        ):
            return self._source_metadata_override(canonical_name)
        return (
            self.effective_dependencies(canonical_name, version),
            self.effective_requires_python(canonical_name, version),
            self.effective_provides_extra(canonical_name, version),
        )

    def _matching_package_override(
        self, canonical_name: str, version: Version, sets_field: Callable[..., object]
    ) -> PackageOverride | None:
        """Return the per-package override for ``version`` that sets the field.

        At most one matches because the parse-time non-overlap check
        forbids two same-field entries with overlapping ranges.
        """
        for override in self._package_overrides:
            if override.name != canonical_name:
                continue
            if version not in override.version_range:
                continue
            if sets_field(override) is not _UNSET:
                return override
        return None

    def _effective_field(
        self,
        canonical_name: str,
        version: Version,
        index_name: str | None,
        *,
        field: str,
        value: Callable[[PackageOverride | IndexOverride], object],
    ) -> object:
        """Resolve one policy field for a candidate across both override surfaces.

        Returns the per-package value when a range-matching per-package
        override sets ``field``, else the per-index value when the serving
        index's override sets it, else ``_UNSET`` (the caller substitutes
        the global default).  When BOTH surfaces set the field for this
        candidate, raises :class:`~nab_provider.errors.OverrideConflictError`:
        the two surfaces are deliberately not ranked.

        Both override types spell the policy fields the same way, so one
        ``value`` callable reads either.  It returns ``_UNSET`` when the
        override does not set the field, and the value (which may be
        ``None`` for a disabled upload-time cutoff) when it does.
        """
        pkg = self._matching_package_override(canonical_name, version, value)
        idx = self._index_overrides.get(index_name) if index_name is not None else None
        idx_value = value(idx) if idx is not None else _UNSET

        if pkg is not None and idx_value is not _UNSET:
            msg = (
                f"override conflict for {canonical_name}=={version} served from"
                f" index {index_name!r}: both a per-package override"
                f" ({str(pkg.requirement)!r}) and the per-index override set"
                f" {field!r}.  The per-package and per-index surfaces are not"
                " ranked; remove one of the two settings for this field."
            )
            raise OverrideConflictError(msg)

        if pkg is not None:
            return value(pkg)
        return idx_value

    def force_backtrack_count(self, canonical_name: str) -> int:
        """How many times this package has triggered force-backtrack."""
        return self._force_backtrack_counts.get(canonical_name, 0)

    def has_invalid_metadata(self, canonical_name: str, version: Version) -> bool:
        """Return True if metadata parsing previously failed for this pin."""
        return (canonical_name, version) in self._invalid_metadata

    def invalid_metadata_reason(
        self, canonical_name: str, version: Version
    ) -> str | None:
        """Return the recorded parse failure for this pin, or ``None``."""
        return self._invalid_metadata.get((canonical_name, version))

    def materialize_source(
        self,
        normalized: str,
        source: LocalSource | VcsSource | ArchiveSource,
    ) -> list[tuple[Version, DistFile]]:
        """Have the host materialise ``source`` and seed its one candidate.

        The build policy is resolved here, out of the provider's overrides; the
        directory read, the clone and the download are the host's.
        """
        request = SourceRequest(
            package=normalized,
            source=source,
            build_policy=self.effective_build_policy_for_source(normalized),
            vcs_cache_dir=self.vcs_cache_dir,
            archive_cache_dir=self.archive_cache_dir,
            require_pin=self.vcs_config.require_pin,
        )

        try:
            event = self.coordinator.request_source_listing(request)
        except SourceBuildPolicyError:
            self.stats.excluded_by_build_policy += 1
            raise
        event.wait()

        # The port raises on failure, so a request that returned left a result.
        materialized = self.coordinator.index.get_source(normalized)
        assert materialized is not None

        if materialized.commit_sha is not None:
            self.vcs_pins[normalized] = materialized.commit_sha

        result: list[tuple[Version, DistFile]] = []
        for version, sdist in _sources.seed_synthetic_listing(
            self,
            normalized,
            materialized.path,
            materialized.metadata,
            source.descriptor,
        ):
            result.append((version, sdist))
        return result

    def versions_only(
        self,
        normalized: str,
        version_list: list[tuple[Version, DistFile]],
    ) -> list[Version]:
        """See :func:`nab_provider._provider.listing.versions_only`."""
        return _listing.versions_only(self, normalized, version_list)

    def _wheel_by_version(
        self,
        normalized: str,
        version_list: list[tuple[Version, DistFile]],
    ) -> dict[Version, DistFile]:
        """Return the picked-dist view of ``normalized``'s listing.

        See :func:`nab_provider._provider.metadata_resolver.version_dists`.
        """
        return _metadata_resolver.version_dists(self, normalized, version_list).picked

    def speculative_prefetch(
        self,
        normalized: str,
        versions: list[tuple[Version, DistFile]],
    ) -> None:
        """See :func:`nab_provider._provider.listing.speculative_prefetch`."""
        _listing.speculative_prefetch(self, normalized, versions)

    def _prefetch_walk_ahead(
        self, normalized: str, version_range: RangeProtocol[Version]
    ) -> None:
        """See :func:`nab_provider._provider.listing.prefetch_walk_ahead`."""
        _listing.prefetch_walk_ahead(
            self, normalized, version_range, self.DEEP_PREFETCH_COUNT
        )

    def filter_distributions(
        self, normalized: str, files: Sequence[WheelFile | SdistFile]
    ) -> list[tuple[Version, DistFile]]:
        """See :func:`nab_provider._provider.listing.filter_distributions`."""
        return _listing.filter_distributions(self, normalized, files)

    def pick_best_candidate(
        self,
        normalized: str,
        versions: list[tuple[Version, DistFile]],
    ) -> tuple[Version, DistFile] | None:
        """See :func:`nab_provider._provider.listing.pick_best_candidate`."""
        return _listing.pick_best_candidate(self, normalized, versions)

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        """Pick a version within the allowed range, respecting the strategy.

        A preference another resolve decided wins when it is in range and
        usable here; otherwise the strategy picks.
        """
        assert isinstance(version_range, VersionRange)
        self.stats.choose_version_calls += 1

        base, extra, normalized = self.split_and_normalize(package)

        preferred = self._preferred_version(
            package, base, extra, normalized, version_range
        )
        if preferred is not None:
            self._flush_pending_blocks()
            return preferred

        if extra is not None:
            return _extras.choose_extra_version(
                self, package, base, extra, version_range
            )

        version_list = self.fetch_versions(package)
        all_versions = self.versions_only(normalized, version_list)
        candidates = self._ordered_candidates(
            normalized, version_range, all_versions, version_list
        )
        first = next(candidates, None)
        if first is None:
            self._record_no_versions_reason(
                package, all_versions, version_range=version_range
            )
            return None

        no_lookahead = not self.root_requirements and not self.solution_decisions
        if no_lookahead:
            return first

        wheel_by_version = self._wheel_by_version(normalized, version_list)
        return self._run_full_scan(
            normalized,
            first,
            candidates,
            wheel_by_version,
            package,
            all_versions,
            version_range,
        )

    def _ordered_candidates(
        self,
        normalized: str,
        version_range: VersionRange,
        all_versions: list[Version],
        version_list: list[tuple[Version, DistFile]],
    ) -> Iterator[Version]:
        """Return the in-range versions in the order the strategy walks them.

        ``VersionRange.filter`` bisects a sorted listing lazily, so HIGHEST
        walks the newest-first view and LOWEST the cached ascending one, and
        the two reverse each other while a final release is in range. When
        none is, each walk flushes its buffered pre-releases after the ones
        it admitted in place, which is not a mirror image, so LOWEST falls
        back to reversing the newest-first walk whenever its first match is
        a pre-release.
        """
        if not self.wants_lowest(normalized):
            return version_range.filter(all_versions, assume_sorted="descending")

        ascending = self._ascending_versions(normalized, version_list)
        oldest_first = version_range.filter(ascending, assume_sorted="ascending")
        first = next(oldest_first, None)
        if first is None:
            return iter(())

        if first.is_prerelease:
            newest_first = version_range.filter(
                all_versions, assume_sorted="descending"
            )
            return reversed(list(newest_first))

        return chain((first,), oldest_first)

    def _preferred_version(
        self,
        package: str,
        base: str,
        extra: str | None,
        normalized: str,
        version_range: VersionRange,
    ) -> Version | None:
        """Return the preferred version for ``package``, or None to pick fresh.

        A preference is honored only when it is in range and usable here: a
        base version needs extractable metadata, an extras proxy
        additionally needs to declare the extra.  A package the strategy
        wants lowest for keeps its own floor, so alignment cannot make the
        result depend on the order the targets resolve in.
        """
        preferred = self._preferences.get(normalized)
        if preferred is None or self.wants_lowest(normalized):
            return None

        version_list = self.fetch_versions(package)

        # The proxy's range is built full(), so intersect it with the base's
        # positive range, which carries the pre-release admission granted by
        # the requirement that named the extra.  This mirrors
        # choose_extra_version.
        admit_range = version_range
        if extra is not None:
            base_range = self.solution_ranges.get(normalized)
            if base_range is not None:
                admit_range = version_range & base_range

        if not self._admits_preference(
            normalized, version_list, admit_range, preferred
        ):
            return None

        usable = (
            _extras.version_provides_extra(self, base, extra, preferred)
            if extra is not None
            else self._look_ahead_ok(normalized, preferred, check_decisions=True)
        )
        return preferred if usable else None

    def _admits_preference(
        self,
        normalized: str,
        version_list: list[tuple[Version, DistFile]],
        admit_range: VersionRange,
        preferred: Version,
    ) -> bool:
        """Report whether ``preferred`` is one of the listing's in-range versions.

        A final release is answered by ``contains`` on the range plus a lookup
        in the listing's picked-dist view.  A pre-release needs the ``filter``
        walk instead: ``contains`` reads only the configured pre-release policy,
        while ``filter`` also applies the PEP 440 buffering and the range's
        opt-in region.
        """
        if preferred.is_prerelease:
            all_versions = self.versions_only(normalized, version_list)
            in_range = admit_range.filter(all_versions, assume_sorted="descending")
            return preferred in in_range
        return preferred in admit_range and preferred in self._wheel_by_version(
            normalized, version_list
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        """Report whether a usable version exists, side-effect-free.

        Runs the real ``choose_version`` over ``version_range`` so look-ahead
        rejections are honored, then rolls back the state it records: the queued
        clauses, the force-backtrack signal, and the pending look-ahead blocks
        are dropped, and the force-backtrack budget and no-versions reasons are
        restored to their pre-probe values.  A failed-resolve attribution probe
        therefore cannot alter a later decision.

        The one exception is ``package``'s own no-versions reason.  When the
        un-narrowed range yields no version because a transitive conflict
        rejected every candidate, this probe is the only pass that names the
        blocker, so its reason is kept rather than rolled back to the generic
        no-match the constraint-narrowed pass recorded.  The reason map only
        labels a ``NO_VERSIONS`` clause, so keeping it cannot alter a decision.

        The probe also suppresses the two look-ahead shortcuts that could
        otherwise report a version the decided blocker rejects:
        ``_probing_satisfiable`` skips the abort and keeps checking decisions
        past ``_BROAD_LA_REJECT_CAP``.

        The un-narrowed range spans versions the constraint clipped away, so
        look-ahead can reach one whose metadata raises a hard error the narrowed
        resolve never touched (a failed integrity check, a tie-ranked-wheel
        divergence, or an advertised sidecar the index answered it will not
        serve).  Each names a fault of that one version, so the probe catches
        them and returns ``False`` rather than aborting; the crash still fires
        when the version is pinned for real.

        A transient transport failure is deliberately not in that tuple.  A 5xx
        that outlived the retry budget, or a dropped connection, says nothing
        about the version, and swallowing it would report "no satisfying
        candidate" for a version that has one and hand back a different
        resolution instead of failing.  The ``finally`` restores the snapshot
        either way.
        """
        saved_counts = dict(self._force_backtrack_counts)
        saved_reasons = dict(self._no_versions_reasons)

        self._probing_satisfiable = True
        try:
            return self.choose_version(package, version_range) is not None
        except (
            MetadataHashMismatchError,
            SdistHashMismatchError,
            WheelHashMismatchError,
            UnsupportedVcsError,
            InvalidUploadTimeError,
            OverrideConflictError,
            SiblingMetadataDivergenceError,
            NotImplementedError,
            MalformedSimpleResponseError,
            UnserveableUrlError,
        ):
            return False
        finally:
            self._probing_satisfiable = False
            self.consume_pending_clauses()
            self.consume_force_backtrack_targets()
            _lookahead.reset_pending_blocks(self)
            self._force_backtrack_counts = saved_counts

            # Restore the snapshot but keep the probe's own blocker reason.
            probed_reason = self._no_versions_reasons.get(package)
            self._no_versions_reasons = saved_reasons
            if probed_reason is not None:
                self._no_versions_reasons[package] = probed_reason

    def _run_full_scan(
        self,
        normalized: str,
        first: Version,
        rest: Iterator[Version],
        wheel_by_version: dict[Version, DistFile],
        package: str,
        all_versions: list[Version],
        version_range: RangeProtocol[Version],
    ) -> Version | None:
        """Run the decision-aware look-ahead scan over candidates.

        ``rest`` is only drawn from once ``first`` has been rejected.
        """
        broad_rejections = 0
        if self._look_ahead_ok(normalized, first, check_decisions=True):
            self._flush_pending_blocks()
            return first
        self.stats.look_ahead_rejections += 1
        broad_rejections += 1

        found = self._scan_candidates_pipelined(
            normalized,
            list(rest),
            wheel_by_version,
            broad_rejections,
            version_range,
            first_candidate=first,
        )
        if found is not None:
            self._flush_pending_blocks()
            return found

        # Every candidate rejected. Flush so the resolver replaces the default
        # NO_VERSIONS clause with the grouped binary incompatibilities.
        blockers, metadata = self._capture_lookahead_blockers(normalized)
        self._flush_pending_blocks()
        self._record_no_versions_reason(
            package, all_versions, blockers=blockers, metadata=metadata
        )
        return None

    def _scan_candidates_pipelined(
        self,
        normalized: str,
        remaining: list[Version],
        wheel_by_version: dict[Version, DistFile],
        broad_rejections: int,
        version_range: RangeProtocol[Version],
        *,
        first_candidate: Version | None = None,
    ) -> Version | None:
        """Scan ``remaining`` with ``PREFETCH_DEPTH`` batches in flight.

        Returns the first ``_look_ahead_ok`` version, or ``first_candidate``
        if the scan trips the monolithic-rejection abort, or ``None`` when
        every candidate was rejected.  Caller flushes pending blocks on
        every return path.

        Abort semantics: when ``broad_rejections`` crosses
        ``_LOOKAHEAD_ABORT_THRESHOLD`` and every queued rejection for
        ``normalized`` shares one ``(blocker_pkg, blocker_version)``,
        discard the misleading pending decision blocks for
        ``normalized`` and return ``first_candidate``.  The resolver then
        decides that candidate tentatively, ``get_dependencies`` emits the
        actual dep-range clause, and pubgrub back-jumps the offending
        blocker on its own.  Sound because no clause is emitted by the
        abort path.
        """
        # Front-load deep metadata so a walk past the first batch hits cache.
        self._prefetch_walk_ahead(normalized, version_range)

        starts_iter = iter(range(0, len(remaining), self.PREFETCH_BATCH))
        in_flight: deque[
            tuple[list[Version], list[tuple[Version, str, str, Waitable]]]
        ] = deque()
        for _ in range(self.PREFETCH_DEPTH):
            start = next(starts_iter, None)
            if start is None:
                break
            batch = remaining[start : start + self.PREFETCH_BATCH]
            submitted = self._prefetch_batch(normalized, batch, wheel_by_version)
            in_flight.append((batch, submitted))

        while in_flight:
            batch, submitted = in_flight.popleft()
            # Refill before awaiting so the next fetch overlaps the wait.
            next_start = next(starts_iter, None)
            if next_start is not None:
                next_batch = remaining[next_start : next_start + self.PREFETCH_BATCH]
                next_submitted = self._prefetch_batch(
                    normalized, next_batch, wheel_by_version
                )
                in_flight.append((next_batch, next_submitted))
            self._await_metadata_batch(normalized, submitted)
            outcome, broad_rejections = self._scan_batch(
                normalized, batch, broad_rejections, first_candidate
            )
            if outcome is not None:
                return outcome
        return None

    def _scan_batch(
        self,
        normalized: str,
        batch: list[Version],
        broad_rejections: int,
        first_candidate: Version | None,
    ) -> tuple[Version | None, int]:
        """Look-ahead-check each version. Return (winner, new_rejection_count).

        Winner is the first compatible candidate, or ``first_candidate``
        when the abort fires, or None to keep scanning further batches.
        """
        for version in batch:
            check_decisions = (
                self._probing_satisfiable
                or broad_rejections < self._BROAD_LA_REJECT_CAP
            )
            if self._look_ahead_ok(
                normalized, version, check_decisions=check_decisions
            ):
                return version, broad_rejections
            self.stats.look_ahead_rejections += 1
            if not check_decisions:
                continue
            broad_rejections += 1
            if first_candidate is None:
                continue
            if broad_rejections < self._LOOKAHEAD_ABORT_THRESHOLD:
                continue
            if self._probing_satisfiable:
                continue
            if self._try_abort_lookahead(normalized):
                return first_candidate, broad_rejections
        return None, broad_rejections

    def _try_abort_lookahead(self, normalized: str) -> bool:
        """Run the monolithic-rejection abort. Return True when it fires.

        Firing queues the blocker for force-backtrack, up to the per-blocker
        cap; the caller then falls back to its first candidate.
        """
        blocker = self._should_abort_lookahead(normalized)
        if blocker is None:
            return False
        self._discard_pending_decision_blocks(normalized)
        blocker_pkg, _ = blocker
        prior_fires = self._force_backtrack_counts.get(blocker_pkg, 0)
        if (
            blocker_pkg not in self._force_backtrack_targets
            and prior_fires < self._MAX_FORCE_BACKTRACKS_PER_PKG
        ):
            self._force_backtrack_targets.append(blocker_pkg)
            self._force_backtrack_counts[blocker_pkg] = prior_fires + 1
        return True

    def _should_abort_lookahead(self, normalized: str) -> tuple[str, Version] | None:
        """Return the single shared blocker if every rejection blames it.

        The trigger is intentionally narrow: only when *every* rejection for
        ``normalized`` is a decision block with the same ``(blocker_pkg,
        blocker_version)`` key and no other kind of block was queued.
        Returns ``(blocker_pkg, blocker_version)`` when the condition holds,
        else ``None``.  Mixed-cause scans keep the per-version clauses
        because at least one rejection cause is a real constraint the
        resolver still needs to learn.
        """
        seen: set[tuple[str, Version]] = set()
        for cand, blocker_pkg, blocker_version in self.pending_blocks:
            if cand == normalized:
                seen.add((blocker_pkg, blocker_version))
                if len(seen) > 1:
                    return None
        if len(seen) != 1 or self._has_non_decision_block(normalized):
            return None
        return next(iter(seen))

    def _has_non_decision_block(self, normalized: str) -> bool:
        """Whether ``normalized`` was rejected for a reason beyond a decision.

        Range, self-dependency, root and metadata blocks each state a
        constraint the resolver still has to learn, so the abort path must
        leave their clauses alone.
        """
        return (
            normalized in self.pending_metadata_blocks
            or any(cand == normalized for cand, *_ in self.pending_range_blocks)
            or any(cand == normalized for cand, *_ in self.pending_self_blocks)
            or any(cand == normalized for cand, *_ in self.pending_root_blocks)
        )

    def _discard_pending_decision_blocks(self, normalized: str) -> None:
        """Drop decision-block entries for ``normalized`` without emitting clauses.

        Used by the look-ahead abort path: the blocker clauses the queue
        would otherwise produce are exactly the ones that mislead the
        resolver into picking a deep candidate.  The other block kinds are
        left in place because the abort path only fires when none exist for
        this candidate; this helper still scopes its delete to the matching
        candidate name for safety.
        """
        self.pending_blocks = defaultdict(
            list,
            {k: v for k, v in self.pending_blocks.items() if k[0] != normalized},
        )
        # Drop the matching accumulators too: a stale one would over-count a
        # later group under the same key and disable its widening.
        self.pending_decision_dep_ranges = defaultdict(
            _lookahead.DepRangeUnion.zero,
            {
                k: v
                for k, v in self.pending_decision_dep_ranges.items()
                if k[0] != normalized
            },
        )

    def wants_lowest(self, normalized: str) -> bool:
        """Whether the resolver should pick the minimum version for ``normalized``.

        Lookup keys are canonical names; extras-proxy callers must
        pass the *base* name (the strategy decision is keyed off the
        underlying package, not the proxy).
        """
        if self._resolution_strategy is ResolutionStrategy.LOWEST:
            return True
        if self._resolution_strategy is ResolutionStrategy.LOWEST_DIRECT:
            return normalized in self._direct_packages
        return False

    def _record_no_versions_reason(
        self,
        package: str,
        all_versions: list[Version],
        *,
        blockers: Sequence[_diagnosis.Blocker] = (),
        metadata: tuple[_diagnosis.MetadataBlock, ...] = (),
        version_range: VersionRange | None = None,
    ) -> None:
        """Record why ``choose_version`` returned ``None`` for ``package``.

        Runs during the resolve, on every ask that returns no version, which
        is ordinary backtracking and not failure.  So it stores a marker and
        renders nothing: no listing is walked, no version parsed and no
        sentence built until :meth:`get_no_versions_reason` is asked for one,
        which happens once, after the resolve has already failed.

        ``blockers`` and ``metadata`` carry the look-ahead rejection causes
        when every candidate that fell in ``version_range`` was rejected:
        either because of an already-decided package, a positive-range
        constraint, a root-requirement disagreement, or because the
        candidate's metadata could not be read under the current
        build policy.  When supplied, the recorded reason names those
        causes so the user does not see a bare "no version matches
        the requirement", which would suggest the package is
        missing from the index when in fact it is the resolver's
        transitive constraints (or a too-strict build policy) that
        excluded every candidate.

        ``version_range`` is passed only when no surviving version fell
        inside it.  A version the listing filter dropped that does fall
        inside it is the release the requirement asked for, so the reason
        names the filters that dropped it rather than reporting no match.
        The marker carries the range; which filters fired is decided later.

        ``all_versions`` is post-filter, so an empty one means either the
        index served no files or every file it served was dropped by one of
        the listing filter's rungs.  The stored listing tells absence from
        incompatibility apart, except that it is also empty for an index
        skipped offline and for a page that named files nab could not use.
        Both are marked when stored, so the reason names what happened
        instead of absence.

        A look-ahead rejection emits a clause that removes the rejected
        versions from the range, so the resolver asks again over a range
        nothing falls in.  That second ask has no blockers of its own, so
        its no-match reason must not overwrite the one naming the blocker.
        """
        if not all_versions:
            _, _, normalized = self.split_and_normalize(package)
            reason = self._empty_listing_marker(normalized)
        elif blockers or metadata:
            # Look-ahead rejection: candidates DID match the range but
            # were rejected.  Naming the blocker is more useful than
            # a generic "no version matches" line, which would
            # otherwise fire because ``all_versions`` contains
            # versions inside ``version_range``.
            reason = _diagnosis.NoVersionsReason(
                _diagnosis.ReasonKind.BLOCKERS,
                blockers=tuple(blockers),
                metadata=metadata,
            )
        elif package in self._no_versions_reasons:
            # The weakest reason: keep whatever is already recorded.
            return
        else:
            reason = _diagnosis.NoVersionsReason(
                _diagnosis.ReasonKind.NO_MATCH, version_range=version_range
            )
        self._no_versions_reasons[package] = reason

    def record_extra_no_versions(
        self,
        package: str,
        kind: _diagnosis.Kind,
        *,
        metadata: tuple[_diagnosis.MetadataBlock, ...] = (),
        version_range: VersionRange | None = None,
        declaring_version: Version | None = None,
    ) -> None:
        """Record why an extras proxy found no version of its base to offer.

        ``choose_version`` hands a proxy to the extras chooser before either
        listing-level record is made, so without this the proxy reaches the
        report with nothing to say and the tree names a package the
        ``Diagnostics:`` section cannot.
        """
        self._no_versions_reasons[package] = _diagnosis.NoVersionsReason(
            kind,
            metadata=metadata,
            version_range=version_range,
            declaring_version=declaring_version,
        )

    def record_extra_base_empty(self, package: str) -> None:
        """Record that ``package``'s base has no version to offer at all.

        Which listing-level situation the base is in is left to the render.
        This runs during the resolve, where an ask that finds nothing is
        ordinary backtracking rather than a failure.
        """
        self._no_versions_reasons[package] = _diagnosis.EXTRA_BASE_EMPTY

    def _empty_listing_marker(self, normalized: str) -> _diagnosis.NoVersionsReason:
        """Classify a package the filter left with nothing, without walking it.

        Reads only what the index client already holds, so an ask that ends
        here during ordinary backtracking builds no sentence.
        """
        index = self.coordinator.index
        if index.get_listing(normalized):
            return _diagnosis.FILTERED_EMPTY
        if index.is_offline_listing_miss(normalized):
            return _diagnosis.OFFLINE_MISS

        served = self._served_page_marker(normalized)
        if served is not None:
            return served
        return self._absent_listing_marker(normalized)

    def _served_page_marker(
        self, normalized: str
    ) -> _diagnosis.NoVersionsReason | None:
        """Classify an empty listing by the marks its page was stored with.

        One page can carry several, so the order is the one the report
        prefers: the most specific reason nothing came off the page first,
        the bare fact that it named files last.  ``None`` when no index
        marked a page for ``normalized``.
        """
        index = self.coordinator.index
        if index.is_unreadable_only_listing(normalized):
            return _diagnosis.UNREADABLE_ONLY
        if index.is_unreachable_only_listing(normalized):
            return _diagnosis.UNREACHABLE_ONLY
        if index.is_all_yanked_listing(normalized):
            return _diagnosis.YANKED_ONLY
        if index.is_no_usable_file_listing(normalized):
            return _diagnosis.NONE_USABLE
        return None

    def _absent_listing_marker(self, normalized: str) -> _diagnosis.NoVersionsReason:
        """Classify a package no index served a page for.

        A pin routes the ask to one index, so missing there is not missing
        from the configured set.
        """
        if self.coordinator.index.is_pinned_listing(normalized):
            return _diagnosis.PINNED_ABSENT
        return _diagnosis.ABSENT

    def _capture_lookahead_blockers(
        self, normalized: str
    ) -> tuple[list[_diagnosis.Blocker], tuple[_diagnosis.MetadataBlock, ...]]:
        """Snapshot the pending look-ahead rejections for ``normalized``.

        Returns one record per dependency the scan found holding every
        candidate out, plus the metadata failures recorded against them.
        The queues reset at the next flush, so the ranges are taken now and
        spelled only if the resolve fails.
        """
        out: list[_diagnosis.Blocker] = []

        for cand, blocker_pkg, blocker_version in self.pending_blocks:
            if cand != normalized:
                continue
            recorded = self.pending_decision_dep_ranges[
                (cand, blocker_pkg, blocker_version)
            ]

            # The blocker is decided, so the record keeps that version rather
            # than a singleton range, which has no specifier spelling.
            out.append(
                _diagnosis.Blocker(
                    _diagnosis.BlockerKind.DECIDED,
                    blocker_pkg,
                    _declared_ranges(recorded),
                    blocker_version,
                )
            )

        for cand, blocker_pkg, pos_range in self.pending_range_blocks:
            if cand != normalized:
                continue
            recorded = self.pending_range_dep_ranges[(cand, blocker_pkg, pos_range)]
            out.append(
                _diagnosis.Blocker(
                    _diagnosis.BlockerKind.HELD,
                    blocker_pkg,
                    _declared_ranges(recorded),
                    pos_range,
                )
            )

        for cand, dep_range, pos_range in self.pending_self_blocks:
            if cand != normalized:
                continue
            out.append(
                _diagnosis.Blocker(
                    _diagnosis.BlockerKind.HELD,
                    cand,
                    (dep_range,),
                    pos_range,
                )
            )

        for (
            cand,
            blocker_pkg,
            dep_range,
            root_range,
        ) in self.pending_root_blocks:
            if cand != normalized:
                continue
            out.append(
                _diagnosis.Blocker(
                    _diagnosis.BlockerKind.ROOT,
                    blocker_pkg,
                    (dep_range,),
                    root_range,
                )
            )

        meta = self.pending_metadata_blocks.get(normalized) or {}
        return out, tuple(meta.values())

    def record_metadata_ban(
        self, normalized: str, blocks: Mapping[Version, _diagnosis.MetadataBlock]
    ) -> None:
        """Accumulate the metadata errors behind ``normalized``'s permanent ban.

        The ban lasts the whole resolve, so its reason has to outlive the scan
        that raised it, and bans from several scans union into one line.
        """
        recorded = self._metadata_ban_blocks.setdefault(normalized, {})
        for version, message in blocks.items():
            recorded.setdefault(version, message)

    def get_no_versions_reason(self, package: str) -> Diagnostic | None:
        """Return the recorded reason for ``package``'s NO_VERSIONS clause.

        Ranked by specificity, not by which pass wrote first: a recorded
        reason that names a cause wins, and a metadata ban beats the two
        that say only that nothing matched.

        This is where the sentence is built, and the only place it is built.
        Reaching it means the resolve has already failed, so the marker is
        rendered here rather than on the resolve path.

        Returns ``None`` if no diagnostic was captured (e.g. the
        package was decided successfully or failed for a non-listing
        reason such as a metadata parse error).
        """
        recorded = self._no_versions_reasons.get(package)
        if recorded is not None and not recorded.is_generic:
            return self._render_no_versions_reason(package, recorded)

        normalized = canonicalize_name(package)
        blocks = self._metadata_ban_blocks.get(normalized)
        if blocks:
            return _diagnosis.metadata_diagnostic(
                self, normalized, list(blocks.values())
            )
        if recorded is None:
            return None
        return self._render_no_versions_reason(package, recorded)

    def _render_no_versions_reason(
        self, package: str, recorded: _diagnosis.NoVersionsReason
    ) -> Diagnostic:
        """Turn one recorded marker into the entry the user reads."""
        fixed = _diagnosis.FIXED_DIAGNOSTICS.get(recorded.kind)
        if fixed is not None:
            return fixed
        if recorded.kind == _diagnosis.ReasonKind.BLOCKERS:
            _, _, normalized = self.split_and_normalize(package)
            return _diagnosis.blockers_diagnostic(
                self, normalized, recorded.blockers, recorded.metadata
            )
        if recorded.kind == _diagnosis.ReasonKind.PINNED_ABSENT:
            return self._render_pinned_absent_reason(package)
        if recorded.kind == _diagnosis.ReasonKind.EXTRA_BASE_EMPTY:
            return self._render_extra_base_reason(package)
        if recorded.kind in _EXTRA_KINDS:
            return self._render_extra_reason(package, recorded)
        return self._render_listing_reason(package, recorded)

    def _render_pinned_absent_reason(self, package: str) -> Diagnostic:
        """Render a package routed to an index that does not carry it.

        The name is read back from the store, which recorded it in the call
        that marked the pin, so the marker carries nothing and the record
        path stays allocation-free.
        """
        _, _, normalized = self.split_and_normalize(package)
        index_name = self.serving_index(normalized)
        assert index_name is not None
        return _diagnosis.pinned_index_diagnostic(index_name)

    def _render_extra_base_reason(self, package: str) -> Diagnostic:
        """Render an extras proxy whose base package ran out of versions.

        The extra plays no part: nothing read a ``Provides-Extra`` before
        the base came back empty.  So the entry is the base package's own,
        remedy included, since ``packages."foo"`` is the entry a user edits
        to admit files for ``foo[bar]``.
        """
        _, _, normalized = self.split_and_normalize(package)
        return self._render_no_versions_reason(
            package, self._empty_listing_marker(normalized)
        )

    def _render_extra_reason(
        self, package: str, recorded: _diagnosis.NoVersionsReason
    ) -> Diagnostic:
        """Render an extras proxy left with no version of its base package."""
        base, extra, _ = self.split_and_normalize(package)
        assert extra is not None
        searched = recorded.version_range
        return _diagnosis.extra_diagnostic(
            base,
            extra,
            recorded,
            "" if searched is None else self._format_blocker_range(searched),
        )

    def _render_listing_reason(
        self, package: str, recorded: _diagnosis.NoVersionsReason
    ) -> Diagnostic:
        """Render the two markers whose entry comes from the listing walk.

        Falls back to the no-match line wherever the walk has nothing to
        say: a local, VCS or archive source has no index page for a filter
        to have dropped anything from, and an in-range marker whose range
        holds nothing the filter dropped is a requirement for a version the
        index never published.
        """
        _, _, normalized = self.split_and_normalize(package)
        diagnosis = (
            self.diagnose_listing(normalized)
            if self._walk_would_be_read(normalized, recorded)
            else None
        )
        if diagnosis is None:
            return _diagnosis.NO_MATCH
        if recorded.kind == _diagnosis.ReasonKind.FILTERED_EMPTY:
            return _diagnosis.empty_listing_diagnostic(self, normalized, diagnosis)

        # The screen passed, so the marker carries the range it screened.
        assert recorded.version_range is not None
        filtered = _diagnosis.in_range_diagnostic(
            self, normalized, recorded.version_range, diagnosis
        )
        return filtered if filtered is not None else _diagnosis.NO_MATCH

    def _walk_would_be_read(
        self, normalized: str, recorded: _diagnosis.NoVersionsReason
    ) -> bool:
        """Whether the walk's detail would reach ``recorded``'s sentence.

        The walk records one refusal per file the filter dropped, and the
        in-range lead throws every one of them away unless the filter
        dropped a release inside the range that was asked.  A requirement
        naming a version the index never published is the ordinary way to
        reach that, so the cheap question runs first.
        """
        if recorded.kind == _diagnosis.ReasonKind.FILTERED_EMPTY:
            return True
        return recorded.version_range is not None and _listing.dropped_release_in_range(
            self, normalized, recorded.version_range
        )

    def diagnose_listing(self, normalized: str) -> _diagnosis.ListingDiagnosis | None:
        """Attribute ``normalized``'s listing drops, once per package per target.

        The walk calls the filter's own predicates, which bump counters the
        benchmarks read, so it is bracketed: whatever it added to
        :attr:`stats` and to the per-version tag tally is taken back before
        the answer is returned.
        """
        cached = self.listing_diagnoses.get(normalized, _UNSET)
        if cached is not _UNSET:
            return cast("_diagnosis.ListingDiagnosis | None", cached)

        before = _counters(self.stats)
        tag_counts = dict(self.tag_excluded_wheels_by_version)
        try:
            diagnosis = _diagnosis.walk_listing(self, normalized)
        finally:
            _restore(self.stats, before)
            self.tag_excluded_wheels_by_version.clear()
            self.tag_excluded_wheels_by_version.update(tag_counts)

        self.listing_diagnoses[normalized] = diagnosis
        return diagnosis

    def filtered_sdist_diagnostic(
        self, normalized: str, version: Version
    ) -> Diagnostic | None:
        """Name the listing-filter rung that took ``version``'s sdist.

        Asked while rendering a failure, for a version the metadata ladder
        marked: it knew the index published an sdist and that the filter
        removed it, but naming the rung is a walk, so it left the marker
        instead.  Returns the report entry, or ``None`` when the walk cannot
        name a rung.
        """
        diagnosis = self.diagnose_listing(normalized)
        # The ladder marks only after reading an sdist out of the raw
        # listing, so the walk had files to partition.
        assert diagnosis is not None
        return _diagnosis.filtered_sdist_diagnostic(
            self, normalized, version, diagnosis
        )

    def override_source(
        self,
        canonical_name: str,
        version: Version,
        index_name: str | None,
        *,
        field: _diagnosis.Field,
    ) -> _diagnosis.Remedy:
        """Return the config layer that set ``field`` for this candidate.

        Reads the two override surfaces through the same matcher
        :meth:`_effective_field` reads them with, but never raises: it runs
        while a failure is being rendered, where the probe may already have
        swallowed the conflict :meth:`_effective_field` would.
        """
        value = _SOURCE_VALUES[field]
        override = self._matching_package_override(canonical_name, version, value)
        if override is not None:
            return self._entry_remedy(field, _diagnosis.OverrideLayer.PACKAGE, override)

        if index_name is not None:
            index = self._index_overrides.get(index_name)
            if index is not None and value(index) is not _UNSET:
                return _diagnosis.Remedy(
                    field,
                    _diagnosis.OverrideLayer.INDEX,
                    index_name,
                    index_name,
                )

        scoped = self._scoped_entry(canonical_name, field, value)
        if scoped is not None:
            return scoped

        bare = self._bare_name_entry(canonical_name)
        if bare is not None:
            return self._entry_remedy(
                field, _diagnosis.OverrideLayer.GLOBAL_BARE_ENTRY, bare
            )
        return _diagnosis.Remedy(
            field, _diagnosis.OverrideLayer.GLOBAL, "", canonical_name
        )

    def _entry_remedy(
        self,
        field: _diagnosis.Field,
        layer: _diagnosis.Layer,
        override: PackageOverride,
    ) -> _diagnosis.Remedy:
        """Build the remedy that changes ``override``'s own config entry."""
        label = override.source_label
        return _diagnosis.Remedy(
            field,
            layer,
            label or str(override.requirement),
            str(override.requirement),
            self._entry_covers(label),
        )

    def _entry_covers(self, label: str) -> int:
        """Count the packages the entry labelled ``label`` matches.

        A ``[[package-rules]]`` entry becomes one override per requirement
        in its ``match`` list, each carrying the entry's label, so a remedy
        naming the entry can say how much changing it moves.  An override a
        host built itself carries no label and speaks for its own package.
        """
        if not label:
            return 1
        return len(
            {
                override.name
                for override in self._package_overrides
                if override.source_label == label
            }
        )

    def _scoped_entry(
        self,
        canonical_name: str,
        field: _diagnosis.Field,
        value: Callable[[PackageOverride | IndexOverride], object],
    ) -> _diagnosis.Remedy | None:
        """Return the entry setting ``canonical_name``'s ``field`` over another range.

        Asked only where the project-level value answered, so a bare-name
        entry would have matched the candidate and any entry found here is
        version-scoped around it.  A second entry for the package would
        overlap that one, which the config layer refuses, so the remedy for
        this candidate points at widening the entry that exists.
        """
        for override in self._package_overrides:
            if override.name == canonical_name and value(override) is not _UNSET:
                return self._entry_remedy(
                    field, _diagnosis.OverrideLayer.GLOBAL_SCOPED_ENTRY, override
                )
        return None

    def _bare_name_entry(self, canonical_name: str) -> PackageOverride | None:
        """Return the name-keyed entry a bare-name remedy would collide with.

        Asked where no entry sets the failing field, so the remedy is the
        one that writes ``packages."<name>"``.  Where the package already
        has a table under that exact key, TOML refuses a second declaration
        of it and the remedy has to name the table instead.  A
        ``[[package-rules]]`` entry is an array element rather than that
        table, so it does not collide.
        """
        for override in self._package_overrides:
            if (
                override.name_keyed
                and override.name == canonical_name
                and str(override.requirement) == canonical_name
            ):
                return override
        return None

    def _prefetch_batch(
        self,
        package: str,
        versions: list[Version],
        wheel_by_version: dict[Version, DistFile],
    ) -> list[tuple[Version, str, str, Waitable]]:
        """See :func:`nab_provider._provider.listing.prefetch_batch`."""
        return _listing.prefetch_batch(self, package, versions, wheel_by_version)

    def _await_metadata_batch(
        self,
        package: str,
        submitted: list[tuple[Version, str, str, Waitable]],
    ) -> None:
        """See :func:`nab_provider._provider.listing.await_metadata_batch`."""
        _listing.await_metadata_batch(self, package, submitted)

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
    ) -> None:
        """Accept a snapshot of the resolver's positive ranges and decisions.

        Decision-only forward checking is safer than reasoning over
        derivations because backjumping a decision also undoes its derivations.

        Both maps are read-only and pinned to the moment the caller took them,
        so storing the references is enough.
        """
        self.solution_ranges = positive_ranges
        self.solution_decisions = decisions

    def _look_ahead_ok(
        self, package: str, version: Version, *, check_decisions: bool = True
    ) -> bool:
        """See :func:`nab_provider._provider.lookahead.look_ahead_ok`."""
        return _lookahead.look_ahead_ok(
            self, package, version, check_decisions=check_decisions
        )

    def _flush_pending_blocks(self) -> None:
        """See :func:`nab_provider._provider.lookahead.flush_pending_blocks`."""
        _lookahead.flush_pending_blocks(self)

    def consume_pending_clauses(self) -> list[Incompatibility[str, Version]]:
        """Drain queued binary clauses for the resolver to absorb."""
        clauses = self.pending_clauses
        self.pending_clauses = []
        return clauses

    def consume_force_backtrack_targets(self) -> list[str]:
        """Drain blocker packages queued by the look-ahead abort path.

        See ``ResolverProvider.consume_force_backtrack_targets`` for the
        contract.  Returning a non-empty list asks the resolver to skip
        deciding the candidate just returned by ``choose_version`` and
        instead targeted-back-track these packages immediately.
        """
        targets = self._force_backtrack_targets
        self._force_backtrack_targets = []
        return targets

    def _ascending_versions(
        self,
        normalized: str,
        version_list: list[tuple[Version, DistFile]],
    ) -> list[Version]:
        """Return the ascending version universe for ``normalized``, cached.

        The reversed ``versions_only`` view of the post-filter listing, so
        pre-release, dev, post, and local versions all fence widening.
        Yanked files are dropped at nab-index parse time and can never be
        selected; if that ever moves into a provider-level filter, this
        universe must be sourced below it, or widened ranges would span
        selectable yanked versions.
        """
        cached = self._ascending_versions_cache.get(normalized)
        if cached is None:
            cached = list(reversed(self.versions_only(normalized, version_list)))
            self._ascending_versions_cache[normalized] = cached
        return cached

    def widen_decision(self, package: str, version: Version) -> VersionRange | None:
        """Return the widened parent range for a decided ``version``, or None.

        For a base package the range spans adjacent listed versions whose
        cached dependency dicts equal the decided version's, then widens to
        the open gap around that span, so every selectable version inside
        has exactly the dependencies being recorded (see
        ``ResolverProvider.widen_decision`` for the contract).  Under a
        lowest preference the upward half is capped and that side keeps
        the plain neighbor gap (see ``_span_identical_deps``); otherwise
        the span runs both ways.  Extras proxies keep the pure neighbor
        gap over the base package's universe: their dependency sets are
        per-extra-context.  Local, VCS, and archive sources (synthesized
        single-version listings) and packages whose listing is not cached
        are not widened.

        The span is computed once from ``deps_cache`` and memoized.  Later
        fetches cannot invalidate it: metadata is immutable and the
        universe never grows mid-resolve, so recomputing could only widen
        the span.  The cap is not part of the memo key: the strategy and
        the direct-package set are both fixed at construction, so
        ``wants_lowest`` gives one answer per package for the provider's
        whole life.
        """
        _, extra, normalized = self.split_and_normalize(package)
        return self._widen(normalized, version, span=extra is None)

    def widen_decision_gap(self, package: str, version: Version) -> VersionRange | None:
        """Return ``version``'s pure neighbor-gap range, or None.

        Contract: the gap contains ``version`` and no other listed version,
        so a term built from it names exactly ``version``.  Look-ahead
        terms widen through this path: a ``widen_decision`` span does not
        meet that contract.  Gating matches ``widen_decision``.
        """
        _, _, normalized = self.split_and_normalize(package)
        return self._widen(normalized, version, span=False)

    def _widen(
        self, normalized: str, version: Version, *, span: bool
    ) -> VersionRange | None:
        """Widen ``version`` over ``normalized``'s universe; memoized per shape."""
        if (
            normalized in self.local_sources
            or normalized in self.vcs_sources
            or normalized in self.archive_sources
        ):
            return None
        version_list = self.versions_cache.get(normalized)
        if version_list is None:
            return None

        key = (normalized, version)
        memo = self._widened_ranges if span else self._gap_widened_ranges
        widened = memo.get(key)
        if widened is None:
            universe = self._ascending_versions(normalized, version_list)
            below = bisect.bisect_left(universe, version)
            above = bisect.bisect_right(universe, version)
            if span:
                below, above = self._span_identical_deps(
                    normalized, version, universe, below, above
                )
            prev = universe[below - 1] if below else None
            nxt = universe[above] if above < len(universe) else None

            widened = VersionRange.from_bounds(
                prev, nxt, include_lower=False, include_upper=False
            )
            memo[key] = widened
        return widened

    def _span_identical_deps(
        self,
        normalized: str,
        version: Version,
        universe: list[Version],
        below: int,
        above: int,
    ) -> tuple[int, int]:
        """Extend ``[below, above)`` across neighbors with equal cached deps.

        Reads ``deps_cache`` only, never fetches.  A neighbor whose cached
        dependency dict is missing or differs fences the span; so does a
        decided version whose own deps are not cached.

        The upward half is capped when ``wants_lowest`` picks the minimum
        for this package, the same per-package answer ``choose_version``
        orders candidates by.  Under a lowest preference the answer sits
        near the floor, so an upward span carries the search away from it
        a whole run at a time; capping leaves the plain neighbor gap
        there, and the search resumes at the adjacent listed version.
        Every other package spans both ways.
        """
        cached = self.deps_cache
        deps = cached.get((normalized, version))
        if deps is None:
            return below, above
        while below and cached.get((normalized, universe[below - 1])) == deps:
            below -= 1
        if self.wants_lowest(normalized):
            return below, above
        top = len(universe)
        while above < top and cached.get((normalized, universe[above])) == deps:
            above += 1
        return below, above

    def narrow_for_display(
        self, package: object, constraint: RangeProtocol[Version]
    ) -> RangeProtocol[Version]:
        """Map a possibly-widened ``constraint`` back onto listed versions.

        Returns a requirement admitting the same listed versions as
        ``constraint``, built out of specifiers so :meth:`format_range` has a
        spelling to print.  Where no short requirement states those versions,
        ``constraint`` stands if it spells, and is snapped onto the listing if
        it does not.

        A constraint containing every listed version becomes the full range,
        so it reads as "any version".  One containing none is returned
        unchanged, as is any constraint under an empty listing: promoting
        there would widen a constraint no version satisfies.

        Render-time only and cache-only: the ROOT sentinel (a non-str
        package) and packages whose listing is not cached return
        ``constraint`` unchanged, and nothing is ever fetched.
        """
        if not isinstance(package, str):
            return constraint
        _, _, normalized = self.split_and_normalize(package)
        version_list = self.versions_cache.get(normalized)
        if version_list is None:
            return constraint
        assert isinstance(constraint, VersionRange)

        universe = self._ascending_versions(normalized, version_list)
        selected = [version for version in universe if version in constraint]
        if not selected:
            return constraint
        if len(selected) == len(universe):
            return VersionRange.full(admit_arbitrary=False)

        spelled = _requirement_over_listing(constraint, universe, selected)
        if spelled is not None:
            return spelled
        has_spelling = constraint.to_specifier_set() is not None
        return constraint if has_spelling else constraint.snap_bounds(universe)

    def format_range(self, constraint: RangeProtocol[Version]) -> str:
        """Render ``constraint`` for a failure report.

        ``VersionRange`` has no ``__str__``, so interpolating one gives the
        debug repr, including the internal boundary-kind sentinels.  A range a
        specifier set can spell reads as that specifier set, so ``==3.0.0``
        shows the way a user would have written it.

        An unconstrained range renders as nothing, leaving the package name to
        carry the line, and the empty range gets a phrase rather than the
        ``<0`` a specifier set spells it with.  A range with no specifier
        spelling, such as a disjunction, keeps the range's own rendering.
        """
        assert isinstance(constraint, VersionRange)
        if constraint.is_empty:
            return "no version"
        if (~constraint).is_empty:
            return ""
        specifier_set = constraint.to_specifier_set()
        if specifier_set is None:
            return str(constraint)
        return str(specifier_set)

    def _format_blocker_range(self, constraint: RangeProtocol[Version]) -> str:
        """Render one side of a blocker line, naming an unconstrained range.

        :meth:`format_range` spells an unconstrained range as nothing, which
        would end the line on a dangling ``in``.
        """
        return self.format_range(constraint) or "any version"

    def get_dependencies(
        self, package: str, version: Version
    ) -> dict[str, VersionRange]:
        """Fetch .metadata and return dependencies as VersionRange."""
        self.stats.get_dependencies_calls += 1

        base, extra, normalized = self.split_and_normalize(package)
        if extra is not None:
            return _extras.get_extra_dependencies(self, base, extra, version)

        cache_key = (normalized, version)

        # Before the cache check: a queued prefetch lands in deps_cache only
        # when this decodes it.
        _listing.parse_prefetched_metadata(self, cache_key)

        if cache_key in self.deps_cache:
            return self.deps_cache[cache_key]
        cached_unsupported = self._unsupported_sdists.get(cache_key)
        if cached_unsupported is not None:
            raise UnsupportedSdistError(cached_unsupported)
        cached_invalid = self._invalid_metadata.get(cache_key)
        if cached_invalid is not None:
            raise MetadataError(cached_invalid)

        versions = self.fetch_versions(package)

        # Local, VCS, and archive sources pre-populate metadata during
        # fetch_versions.
        if cache_key in self.metadata_cache and (
            normalized in self.local_sources
            or normalized in self.vcs_sources
            or normalized in self.archive_sources
        ):
            _metadata_resolver.cache_deps_from_metadata(
                self, cache_key, self.metadata_cache[cache_key]
            )
            return self.deps_cache[cache_key]

        # Skip-fetch: a complete ``dependencies`` override (even an empty
        # tuple) supplies the deps, so no METADATA fetch or build is needed.
        # After the local/VCS/archive branch so sources still materialise;
        # metadata caching applies the remaining override fields to the bare
        # record.
        if self.effective_dependencies(normalized, version) is not None:
            _metadata_resolver.cache_deps_from_metadata(
                self,
                cache_key,
                WheelMetadata(name=normalized, version=version),
            )
            self.prefetch_new_deps(self.deps_cache[cache_key])
            return self.deps_cache[cache_key]

        metadata_text, from_sdist = _metadata_resolver.resolve_metadata(
            self, versions, package, version
        )
        self._parse_and_cache_metadata_guarded(
            cache_key, metadata_text, from_sdist=from_sdist
        )
        _metadata_resolver.check_sibling_metadata_divergence(
            self, versions, package, version
        )

        self.stats.metadata_fetched += 1
        self.prefetch_new_deps(self.deps_cache[cache_key])

        return self.deps_cache[cache_key]

    def _parse_and_cache_metadata_guarded(
        self, cache_key: tuple[str, Version], metadata_text: str, *, from_sdist: bool
    ) -> None:
        """Parse fetched metadata, routing each failure to its own cache.

        A disallowed build, a candidate ruled out by its own metadata, and an
        unparseable payload each record their own failure kind so a re-query
        is answered from cache. Hard errors propagate unrecorded.
        """
        package, version = cache_key
        try:
            self.parse_and_cache_metadata(
                cache_key, metadata_text, from_sdist=from_sdist
            )
        except UnsupportedSdistError as exc:
            self._unsupported_sdists[cache_key] = str(exc)
            raise
        except (ForeignMetadataError, IncompatiblePythonError) as exc:
            self._invalid_metadata[cache_key] = str(exc)
            raise
        except (
            SdistHashMismatchError,
            MetadataHashMismatchError,
            IndexAccessError,
            UnsupportedVcsError,
            NotImplementedError,
            InvalidUploadTimeError,
            OverrideConflictError,
        ):
            # A hash mismatch, a build-remote archive the index failed to serve,
            # a refused direct-URL dep, a naive upload-time hit, or a
            # contradictory config override while building an sdist is a hard
            # error, not a skip.
            raise
        except Exception as exc:
            logger.warning(
                "Skipping %s==%s: metadata cannot be parsed (%s)."
                " Subsequent lookups for this version reuse the cached"
                " failure and do not re-emit this warning.",
                package,
                version,
                exc,
            )
            msg = f"Invalid metadata for {package}=={version}: {exc}"
            self._invalid_metadata[cache_key] = msg
            raise MetadataError(msg) from exc

    def prefetch_new_deps(self, deps: dict[str, VersionRange]) -> None:
        """See :func:`nab_provider._provider.listing.prefetch_new_deps`."""
        _listing.prefetch_new_deps(self, deps)

    def parse_and_cache_metadata(
        self,
        cache_key: tuple[str, Version],
        metadata_text: str,
        *,
        from_sdist: bool = False,
    ) -> None:
        """See :func:`._provider.metadata_resolver.parse_and_cache_metadata`."""
        _metadata_resolver.parse_and_cache_metadata(
            self, cache_key, metadata_text, from_sdist=from_sdist
        )

    def split_and_normalize(self, package: str) -> tuple[str, str | None, str]:
        """Return ``(base, extra, normalized_base)`` for ``package``, cached."""
        cached = self._package_parts.get(package)
        if cached is not None:
            return cached
        base, extra = split_extra(package)
        normalized = canonicalize_name(base)
        result = (base, extra, normalized)
        self._package_parts[package] = result
        return result

    def begin_decision_scan(self) -> Callable[[str], bool] | None:
        """Open a decision scan, expiring the last one's in-flight answers.

        The coming scan re-reads the index, then holds any name it finds still
        in flight that way until the next call.

        Offers no probe under :attr:`DecisionOrder.STABLE`, where a scan waits
        for a listing instead of ranking its absence and so has nothing to wait
        on between scans.
        """
        self._scan_generation += 1
        return None if self.settle_listings else self._listing_landed

    def _listing_landed(self, package: str) -> bool:
        """Return whether the listing ``package``'s key waits on has arrived.

        Asked of the base package: an extras proxy's own ``is_ready`` reads the
        base's ``versions_cache`` entry, which ``prioritize`` fills only once
        the listing has landed, so it would hold the proxy past the arrival it
        is waiting for.
        """
        base, _, _ = self.split_and_normalize(package)
        return self.is_ready(base)

    def arrived_listing(self, normalized: str) -> list[DistFile] | None:
        """Return ``normalized``'s listing, or None while it is in flight.

        The fetcher thread publishes listings asynchronously, so a bare index
        read can answer differently for two packages compared inside one
        decision scan, and differently for the two halves of one package's
        sort key.  A name first seen in flight stays in flight until the next
        ``begin_decision_scan``, so one scan sorts against one view of what
        has landed.
        """
        if self._absent_listing_scan.get(normalized) == self._scan_generation:
            return None
        listing = self.coordinator.index.get_listing(normalized)
        if listing is None:
            self._absent_listing_scan[normalized] = self._scan_generation
        return listing

    def settled_listing(self, normalized: str) -> list[DistFile] | None:
        """Return ``normalized``'s listing, waiting once for it to land.

        The blocking counterpart of :meth:`arrived_listing`, used by the
        decision scan under :attr:`DecisionOrder.STABLE`: waiting for the
        fetch gives the scan the same version count whatever the HTTP
        cache held, where reading what has arrived so far does not.

        A listing that already failed is not re-requested, and one wait is
        enough because every terminal path in the fetcher sets the event.
        ``None`` still means there is no listing to count, so a caller must
        not spin on it.
        """
        listing = self.coordinator.index.get_listing(normalized)
        if listing is not None:
            return listing
        if self.coordinator.index.get_listing_error(normalized) is not None:
            return None
        self.coordinator.request_listing(normalized).wait()
        return self.coordinator.index.get_listing(normalized)

    def is_ready(self, package: str) -> bool:
        """Check if a package's listing is available without blocking.

        Used by the resolver to prefer packages with cached data,
        letting it make progress while other listings are in flight.

        Under :attr:`DecisionOrder.STABLE` it needs no blocking half of its
        own.  ``prioritize`` runs first in the same sort key and has already
        settled the listing into ``versions_cache``; what is left is a
        failed listing or a package served from a local, VCS, or archive
        source, and no listing is ever requested for those.
        """
        _, extra, normalized = self.split_and_normalize(package)
        if extra is not None:
            return normalized in self.versions_cache
        if normalized in self.versions_cache:
            return True
        return self.arrived_listing(normalized) is not None

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int, bool]:
        """Prioritize packages for resolution order.

        Returns ``(tier, matching_count, is_base)``.  Affected packages
        with high ``conflict_counts`` are promoted to tier 0 so they
        decide first inside a conflict cluster; runaway culprits with
        high ``culprit_counts`` are demoted to tier 2 (uv's
        deprioritise-on-conflict).  Everything else is tier 1.

        See :mod:`nab_provider._provider.priority` for the implementation.
        """
        return _priority.prioritize(
            self, package, version_range, conflict_counts, culprit_counts
        )

    def local_source_for(self, canonical_name: str) -> LocalSource | None:
        """Return the local source registered under ``canonical_name`` or None."""
        return self.local_sources.get(canonicalize_name(canonical_name))

    def vcs_source_for(self, canonical_name: str) -> VcsSource | None:
        """Return the VCS source registered under ``canonical_name`` or None."""
        return self.vcs_sources.get(canonicalize_name(canonical_name))

    def archive_source_for(self, canonical_name: str) -> ArchiveSource | None:
        """Return the archive source registered under ``canonical_name`` or None."""
        return self.archive_sources.get(canonicalize_name(canonical_name))

    def vcs_pin_for(self, canonical_name: str) -> str | None:
        """Return the post-clone commit SHA for ``canonical_name``, or None.

        Written by :meth:`materialize_source` after the host's shallow clone
        resolves the ref to a 40-char SHA.
        """
        return self.vcs_pins.get(canonicalize_name(canonical_name))

    def dist_files_for(self, canonical_name: str, version: Version) -> list[DistFile]:
        """Return every distribution file the resolver saw at ``version``.

        Drawn from the cached listing populated during the resolve, so
        callers do not pay another fetch.  When the package was never
        listed (synthetic / not asked for), returns an empty list.
        """
        normalized = canonicalize_name(canonical_name)
        listing = self.versions_cache.get(normalized, [])
        return [dist for v, dist in listing if v == version]

    def tag_excluded_wheel_count(self, canonical_name: str, version: Version) -> int:
        """Return how many wheels the tag filter dropped at ``version`` (0 if none)."""
        normalized = canonicalize_name(canonical_name)
        return self.tag_excluded_wheels_by_version.get((normalized, version), 0)
