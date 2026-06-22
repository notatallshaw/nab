"""Index-backed provider for nab-resolver.

Fetches package metadata from package indexes on demand using
nab-index, converting PEP 440/508 types into nab-resolver Range
types.  Uses a thread pool with a shared HTTP session to overlap I/O.
"""

from __future__ import annotations

import enum
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from nab_index.client import SdistFile, WheelFile

from ._provider import extras as _extras
from ._provider import listing as _listing
from ._provider import lookahead as _lookahead
from ._provider import metadata_resolver as _metadata_resolver
from ._provider import priority as _priority
from ._provider import sources as _sources
from ._vcs_admission import (
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
)
from ._vendor.packaging.markers import default_environment
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.utils import canonicalize_name
from ._vendor.packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from nab_resolver.types import Incompatibility, RangeProtocol

    from .config import IndexOverride, NabProjectConfig, PackageOverride
    from .fetch import FetchCoordinator
    from .metadata import WheelMetadata

__all__ = [
    "BuildPolicy",
    "DistFile",
    "DistPolicy",
    "ExtrasMode",
    "InvalidUploadTimeError",
    "LocalSource",
    "MetadataError",
    "MissingExtraError",
    "Provider",
    "ProviderStats",
    "ResolutionStrategy",
    "UnsupportedSdistError",
    "UnsupportedVcsError",
    "VcsConfig",
    "VcsPolicy",
    "VcsSource",
    "join_extra",
    "python_axis_environment",
    "split_extra",
]


logger = logging.getLogger(__name__)

_EXTRA_RE = re.compile(r"^(?P<base>[^\[]+)\[(?P<extra>[^\]]+)\]$")


# PEP 508 ``python_version`` is the ``major.minor`` pair;
# ``python_full_version`` is the full ``major.minor.micro`` release.
_PYTHON_VERSION_PARTS = 2
_PYTHON_FULL_VERSION_PARTS = 3


def python_axis_environment(python_version: str) -> dict[str, str]:
    """Map an explicit Python version to its PEP 508 marker keys.

    ``python_version`` is padded to two components and
    ``python_full_version`` to three so patch-precision markers evaluate
    the same here as in the universal matrix. Raises ``InvalidVersion``
    if the input is not a version.
    """
    try:
        release = Version(python_version).release
    except InvalidVersion:
        msg = f"python_version {python_version!r} is not a valid version"
        raise InvalidVersion(msg) from None
    minor = ".".join(str(part) for part in (*release, 0)[:_PYTHON_VERSION_PARTS])
    full = (
        python_version
        if len(release) >= _PYTHON_FULL_VERSION_PARTS
        else ".".join(
            str(part) for part in (*release, 0, 0)[:_PYTHON_FULL_VERSION_PARTS]
        )
    )
    return {"python_version": minor, "python_full_version": full}


def _normalize_extra(extra: str) -> str:
    """Normalize an extra name per PEP 685 (same rules as package names)."""
    return canonicalize_name(extra)


def split_extra(package: str) -> tuple[str, str | None]:
    """Split 'name[extra]' into ('name', 'extra'), or ('name', None).

    The extra name is normalized per PEP 685.
    """
    m = _EXTRA_RE.match(package)
    if m is None:
        return (package, None)
    return (m.group("base"), _normalize_extra(m.group("extra")))


def join_extra(base: str, extra: str) -> str:
    """Join a base name and extra into 'name[extra]'.

    The extra name is normalized per PEP 685.
    """
    return f"{base}[{_normalize_extra(extra)}]"


class MissingExtraError(Exception):
    """Raised when a user-requested extra is not provided by the package."""


class ExtrasMode(enum.Enum):
    """How to handle missing extras (not in Provides-Extra)."""

    WARN = "warn"
    """Log warning, drop the extra, resolution continues (pip's behavior)."""

    ERROR_USER = "error_user"
    """Error for user-provided extras, warn for transitive."""

    BACKTRACK = "backtrack"
    """Error for user-provided, backtrack for transitive."""


class DistPolicy(enum.Enum):
    """How to admit wheels and sdists during resolution."""

    WHEEL_ONLY = "wheel-only"
    """Ignore sdists entirely. Only use wheels with PEP 658 metadata."""

    PREFER_WHEEL = "prefer-wheel"
    """Try wheels first, fall back to sdists for versions without wheels."""

    WHEEL_OR_SDIST = "wheel-or-sdist"
    """Admit both. Newest version wins regardless of artifact kind."""

    SDIST_ONLY = "sdist-only"
    """Reject wheels; sdists only.  Mirrors pip's ``--no-binary <pkg>``."""

    SDIST_INSTALL = "sdist-install"
    """Lock the sdist; resolve from whichever artifact is cheapest.

    Same lockfile shape as :attr:`SDIST_ONLY` (only the sdist is pinned, so
    installers download and build that archive), but the resolver is free to
    consult either the wheel's METADATA (via PEP 658 or a range fetch) or
    the sdist's PKG-INFO when extracting dependency facts.  In practice it
    reads the wheel when one exists at the chosen version because that is
    the cheapest source; when only the sdist is published it falls back to
    PKG-INFO with the usual :pep:`643` and pyproject.toml fallbacks.
    Mirrors a pip install with ``--no-binary <pkg>`` while keeping the
    resolver-time fast paths intact.
    """


class BuildPolicy(enum.Enum):
    """How permissive the resolver is about invoking PEP 517 backends.

    Three levels, strictest to most permissive.  Each level reads static
    metadata from every source it admits; the difference is what is
    permitted to fall through to a backend invocation when the static
    read returns nothing usable.
    """

    NEVER = "never"
    """Static metadata only, from any source.

    Wheels, PEP 643 sdists, sdists with a static ``pyproject.toml`` fallback,
    local checkouts via ``[[tool.nab.local-sources]]``, and VCS clones via
    ``[[tool.nab.vcs-sources]]`` are all read statically.  Sources whose
    metadata is dynamic without a static fallback are skipped.
    """

    BUILD_LOCAL = "build-local"
    """Static metadata everywhere, plus PEP 517 builds on local checkouts.

    Adds backend invocation for ``[[tool.nab.local-sources]]`` and
    workspace members when their ``pyproject.toml`` cannot be read
    statically.  VCS clones and remote PyPI sdists remain static-only.
    """

    BUILD_REMOTE = "build-remote"
    """Builds extend to VCS clones and remote PyPI sdists.

    On top of :attr:`BUILD_LOCAL`, invokes the backend on VCS-cloned
    trees and on fetched sdists when their metadata is dynamic and has
    no static fallback.
    """


class ResolutionStrategy(enum.Enum):
    """Which version the resolver picks within an allowed range.

    Mirrors uv's ``--resolution`` flag.  ``LOWEST_DIRECT`` catches missing
    ``>=`` bounds without dragging the whole transitive graph to its floor.
    """

    HIGHEST = "highest"
    """Newest compatible version (default)."""

    LOWEST = "lowest"
    """Oldest compatible version, transitively."""

    LOWEST_DIRECT = "lowest-direct"
    """Oldest for direct deps; newest for transitive deps."""


@dataclass(frozen=True, slots=True)
class LocalSource:
    """A source tree on disk used as the only candidate for a package.

    ``name`` is the package name; the resolver pins the package to a
    single synthetic version derived from the directory's
    ``[project].version`` field (or ``"0.0.0+local"`` if absent).
    ``path`` is the absolute filesystem path to the source tree.

    ``editable`` requests a PEP 660 editable install in the lockfile;
    ``subdirectory`` is a path under ``path`` for monorepo layouts.
    """

    name: str
    path: str
    editable: bool = False
    subdirectory: str | None = None


@dataclass(frozen=True, slots=True)
class VcsSource:
    """A VCS reference used as the only candidate for a package.

    ``name`` is the package name; ``url`` is the pip-style VCS URL
    (e.g. ``git+https://github.com/x/y.git@<sha>#subdirectory=pkg``).
    The provider clones the repo to its cache and treats the
    checked-out source as a :class:`LocalSource` for metadata
    extraction.
    """

    name: str
    url: str


class MetadataError(Exception):
    """Raised when dependency metadata cannot be extracted."""


class UnsupportedSdistError(MetadataError):
    """Sdist or source tree needs a backend invocation the policy disallows.

    Raised when extraction would require a build the current
    :class:`BuildPolicy` (or its per-package override) does not permit:
    dynamic metadata under :attr:`BuildPolicy.NEVER`, a VCS clone under
    :attr:`BuildPolicy.BUILD_LOCAL`, or a remote sdist build failure
    under :attr:`BuildPolicy.BUILD_REMOTE`.  Caught by
    :meth:`Provider._look_ahead_ok` so the resolver skips the
    version instead of failing.
    """


# Deliberately not a MetadataError: _look_ahead_ok catches MetadataError
# and would silently reject the version; a naive upload-time is a hard error.
class InvalidUploadTimeError(Exception):
    """Raised when an index upload-time is not the timezone-aware UTC PEP 700 needs."""


def _add_extra_marker(dep_str: str, extra_name: str) -> str:
    """Append ``extra == "name"`` to a :pep:`508` dep string.

    Parses with :class:`Requirement` rather than splitting on the first
    ``;`` so a semicolon inside a direct-reference URL is not mistaken
    for the marker separator; an existing marker is combined with ``and``.
    """
    req = Requirement(dep_str)
    extra_marker = f'extra == "{extra_name}"'
    if req.marker is not None:
        marker = f"({req.marker}) and {extra_marker}"
    else:
        marker = extra_marker
    req.marker = None
    return f"{req} ; {marker}"


@dataclass
class ProviderStats:
    """Counters describing what the provider did during a resolve.

    Complements :class:`nab_resolver.ResolverStats` by tracking the PyPI/wheel
    layer (listing fetches, metadata reads, filter rejections).  Used by
    benchmarks to measure prefetch and look-ahead wins.
    """

    listings_fetched: int = 0
    metadata_fetched: int = 0
    sdist_pkg_info_fetched: int = 0
    distributions_seen: int = 0
    wheels_seen: int = 0
    sdists_seen: int = 0
    excluded_by_python: int = 0
    excluded_by_time: int = 0
    excluded_by_dist_policy: int = 0
    excluded_by_build_policy: int = 0
    sdist_pyproject_fallbacks: int = 0
    get_dependencies_calls: int = 0
    choose_version_calls: int = 0
    prioritize_calls: int = 0
    look_ahead_rejections: int = 0


DistFile = WheelFile | SdistFile


# Sentinel for "this override does not set the field".  Distinct from
# ``None``, which is a real value (a disabled upload-time cutoff).
_UNSET = object()


def _unset_if_none(value: object) -> object:
    """Map ``None`` to ``_UNSET``, passing every other value through.

    Most policy fields store ``None`` to mean "unset" on the override
    dataclasses, so wrapping their attribute access in this helper yields
    the ``_UNSET``-or-value shape :meth:`Provider._effective_field`
    expects.  The upload-time helpers, where ``None`` is a real value
    (a disabled cutoff), build that shape themselves and skip this.
    """
    if value is None:
        return _UNSET
    return value


class Provider:
    """Lazy index-backed provider for nab-resolver.

    Fetches version lists and .metadata from PyPI via nab-index.
    A thread pool submits listing fetches in the background so
    transitive deps are fetched concurrently with resolution.
    The HTTP connection pool is shared across threads for reuse.
    """

    # Drives two prefetch paths: the speculative root-batch prefetch
    # fired when a listing first arrives, and the scan batch in
    # ``_scan_candidates_pipelined``.  Matched to the abort threshold
    # below: prefetching 8 versions covers the worst-case abort scan
    # without overshooting.  Larger batches waste bandwidth and
    # in-flight HTTP slots on metadata the resolver never decides;
    # smaller batches starve the look-ahead pipeline.
    PREFETCH_BATCH: int = 8

    # Batches kept fetching during the previous batch's await in
    # ``choose_version``.  Depth>=2 reordered listing arrivals and
    # destabilised hard scenarios.
    PREFETCH_DEPTH: int = 1

    # Once the first look-ahead candidate fails, the resolver walks
    # versions one at a time via the abort-skip path.  Front-load
    # metadata for the next K so the walk hits cache instead of one
    # RTT per visit.  Only fires from _scan_candidates_pipelined, so
    # scenarios that accept the first candidate pay nothing.
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
    # is already a strong signal. Combined with the per-package skip
    # below, subsequent calls for the same package skip look-ahead while
    # the blocker decision is unchanged.
    _LOOKAHEAD_ABORT_THRESHOLD = 8

    # Max force-backtracks one blocker can drive per resolution.
    # One-shot misses sustained culprits; unlimited oscillates on
    # blockers that are also the right pin.
    _MAX_FORCE_BACKTRACKS_PER_PKG = 3

    # Re-exported from _provider.priority so test references keep resolving.
    TIER_AFFECTED = _priority.TIER_AFFECTED
    TIER_NORMAL = _priority.TIER_NORMAL
    TIER_CULPRIT = _priority.TIER_CULPRIT
    CONFLICT_THRESHOLD = _priority.CONFLICT_THRESHOLD
    CULPRIT_DEMOTE_THRESHOLD = _priority.CULPRIT_DEMOTE_THRESHOLD

    def __init__(  # noqa: PLR0913, PLR0915 - resolver config is wide; bundling all flags into one bag is worse for callers
        self,
        coordinator: FetchCoordinator,
        python_version: str | None = None,
        root_requirements: dict[str, VersionRange] | None = None,
        uploaded_prior_to: datetime | None = None,
        extras_mode: ExtrasMode = ExtrasMode.ERROR_USER,
        root_extras: set[tuple[str, str]] | None = None,
        dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
        build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
        package_overrides: Sequence[PackageOverride] = (),
        index_overrides: Mapping[str, IndexOverride] | None = None,
        vcs_config: VcsConfig | None = None,
        marker_environment: dict[str, str] | None = None,
        local_sources: list[LocalSource] | None = None,
        vcs_sources: list[VcsSource] | None = None,
        vcs_cache_dir: Path | None = None,
        build_config: NabProjectConfig | None = None,
        resolution_strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
        direct_packages: frozenset[str] | None = None,
        *,
        trust_unverified_sdist_deps: bool = False,
    ) -> None:
        """Construct the provider; see the class docstring for parameters."""
        self.coordinator = coordinator
        self.python_version = python_version
        if marker_environment:
            # The Requires-Python candidate filter reads self.python_version, so
            # an impersonated target Python in the overlay must move it too,
            # keeping the filter aligned with marker evaluation. Mirrors
            # UniversalProvider.
            self.python_version = (
                marker_environment.get("python_full_version")
                or marker_environment.get("python_version")
                or python_version
            )
        self.uploaded_prior_to = uploaded_prior_to

        # Passed through to the build env when extract_source_metadata
        # falls through to a PEP 517 backend; static-only callers leave None.
        self.build_config = build_config
        self.extras_mode = extras_mode
        self.root_extras = root_extras or set()
        self._dist_policy = dist_policy
        self.build_policy = build_policy
        # Opt-out: trust a pre-2.2 sdist's PKG-INFO deps as final instead of
        # routing through the dynamic path. Off by default (strict PEP 643).
        self.trust_unverified_sdist_deps = trust_unverified_sdist_deps
        self._resolution_strategy = resolution_strategy
        self._direct_packages: frozenset[str] = direct_packages or frozenset()
        self._package_overrides = tuple(package_overrides)
        self._index_overrides: Mapping[str, IndexOverride] = index_overrides or {}
        # True when any override sets a time cutoff or disables one, so the
        # listing filter can skip the per-candidate dispatch otherwise.
        self.overrides_set_time = any(
            o.uploaded_prior_to is not None or o.uploaded_prior_to_disabled
            for o in self._package_overrides
        ) or any(
            o.uploaded_prior_to is not None or o.uploaded_prior_to_disabled
            for o in self._index_overrides.values()
        )

        if marker_environment:
            self._check_marker_overlay_build_policy(build_policy)

        self.vcs_config = vcs_config or VcsConfig()
        self.local_sources = _sources.index_local_sources(self, local_sources or [])
        self.vcs_cache_dir = vcs_cache_dir
        self.vcs_pins: dict[str, str] = {}
        self.vcs_sources = _sources.index_vcs_sources(self, vcs_sources or [])

        # default_environment() returns a TypedDict whose ``.items()`` view
        # widens values to ``object``; rebuild as a concrete ``dict[str, str]``
        # so mutations and the env_with_extra copy below stay typed.
        env_init: dict[str, str] = {
            key: value
            for key, value in default_environment().items()
            if isinstance(value, str)
        }
        self.environment: dict[str, str] = env_init
        if python_version is not None:
            self.environment.update(python_axis_environment(python_version))
        if marker_environment:
            for key, value in marker_environment.items():
                self.environment[key] = value

        self.root_requirements = root_requirements or {}
        self.versions_cache: dict[str, list[tuple[Version, DistFile]]] = {}
        self.deps_cache: dict[tuple[str, Version], dict[str, VersionRange]] = {}
        # Unbounded by design and never evicted mid-resolve: it keeps every
        # parsed Requirement (hence every Marker) alive for the whole resolve,
        # which is what makes the id(marker)-keyed marker caches below safe
        # against id reuse. Do not bound it without re-keying those caches.
        self.metadata_cache: dict[tuple[str, Version], WheelMetadata] = {}
        self.extra_deps_map: dict[
            tuple[str, Version], dict[str, dict[str, VersionRange]]
        ] = {}

        # Memoised sdist-rejections so re-tries do not re-parse PKG-INFO.
        self._unsupported_sdists: set[tuple[str, Version]] = set()

        # Memoised metadata-parse failures (malformed Requires-Dist, etc.)
        # keyed by (canonical_name, Version).  Value is the cached error
        # string so the look-ahead diagnostic stays consistent across
        # repeated lookups without re-parsing the broken text.
        self._invalid_metadata: dict[tuple[str, Version], str] = {}

        # Nested matching cache: prioritize is called many times per resolve
        # so the per-call (normalized, range) tuple alloc is worth avoiding.
        self.matching_cache: dict[str, dict[RangeProtocol[Version], int]] = {}

        # Requires-Python compatibility, keyed by the raw specifier string.
        self.requires_python_cache: dict[str, bool] = {}

        # Marker evaluation caches keyed by id(marker); requirement parsing is
        # cached upstream so each distinct marker text shares one Marker. The
        # id keying is safe because metadata_cache keeps every evaluated marker
        # alive (see its note above).
        self.marker_base_cache: dict[int, bool] = {}
        self.marker_extra_cache: dict[int, dict[str, bool]] = {}
        # Memoised str(marker) for the cheap "extra" in marker_text gate.
        self.marker_text_cache: dict[int, str] = {}
        # Reused per-evaluation environment dict (avoids a copy per requirement).
        self.env_with_extra: dict[str, str] = dict(self.environment)

        # (base, extra, normalized_name) per input package string.
        self._package_parts: dict[str, tuple[str, str | None, str]] = {}

        # Fast-path priority cache, keyed by package + Range identity +
        # affected count.  Range identity is sound because solution.get
        # returns the same object until it changes.
        self.priority_cache: dict[
            str, tuple[RangeProtocol[Version], int, tuple[int, int, bool]]
        ] = {}

        # Derived views of versions_cache, built lazily alongside the listing.
        self.versions_only_cache: dict[str, list[Version]] = {}
        self.wheel_by_version_cache: dict[str, dict[Version, DistFile]] = {}

        self.solution_ranges: dict[str, RangeProtocol[Version]] = {}
        self.solution_decisions: dict[str, Version] = {}
        self.pending_clauses: list[Incompatibility[str, Version]] = []
        self.pending_blocks: defaultdict[tuple[str, str, Version], list[Version]] = (
            defaultdict(list)
        )
        self.pending_range_blocks: defaultdict[
            tuple[str, str, RangeProtocol[Version]], list[Version]
        ] = defaultdict(list)

        # Diagnostic-only: root_requirements feed PubGrub directly, so these
        # blockers never need flushing as incompatibilities; they exist purely
        # so the failure message can name the excluding root requirement.
        self.pending_root_blocks: defaultdict[
            tuple[str, str, RangeProtocol[Version], RangeProtocol[Version]],
            list[Version],
        ] = defaultdict(list)

        # Diagnostic-only: metadata-error rejections so the failure message
        # can name the real cause (sdist build needed, malformed PKG-INFO, etc).
        self.pending_metadata_blocks: defaultdict[str, list[tuple[Version, str]]] = (
            defaultdict(list)
        )

        # Last NO_VERSIONS reason per package; consumed by resolve.py to
        # enrich ResolutionError messages.
        self._no_versions_reasons: dict[str, str] = {}

        # Per-package record of "look-ahead aborted at this blocker decision".
        # While the blocker is still decided to the recorded version, the next
        # ``choose_version`` for this package skips look-ahead entirely and
        # returns the first candidate; re-running the scan would just hit the
        # same monolithic-rejection pattern and abort again.  Cleared per
        # package when the blocker's decision changes (back-jump unblocks it).
        self._lookahead_aborted: dict[str, tuple[str, Version]] = {}

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

        self.stats = ProviderStats()

        if self.root_requirements:
            for pkg in self.root_requirements:
                _, _, normalized = self.split_and_normalize(pkg)
                if normalized in self.local_sources or normalized in self.vcs_sources:
                    continue
                self.coordinator.request_listing(normalized)

    def _check_marker_overlay_build_policy(self, build_policy: BuildPolicy) -> None:
        """Reject a non-``NEVER`` build policy under a marker overlay.

        Backends run on the host, so invoking one under a marker overlay
        would produce metadata that does not match the impersonated
        target.  The guard inspects both override surfaces as well as the
        global so a single build override cannot quietly opt out of the
        soundness check.
        """
        offending: list[tuple[str, BuildPolicy]] = []
        if build_policy is not BuildPolicy.NEVER:
            offending.append(("<global>", build_policy))
        offending.extend(
            (o.name, o.build_policy)
            for o in self._package_overrides
            if o.build_policy not in (None, BuildPolicy.NEVER)
        )
        offending.extend(
            (f"index:{name}", o.build_policy)
            for name, o in self._index_overrides.items()
            if o.build_policy not in (None, BuildPolicy.NEVER)
        )
        if not offending:
            return
        rendered = ", ".join(f"{name}={policy.value}" for name, policy in offending)
        msg = (
            "marker_environment overlay requires BuildPolicy.NEVER globally"
            " and in every override that sets build-policy; got"
            f" {rendered}.  Backends run on the host and report metadata for"
            " the host, not the impersonated target."
        )
        raise ValueError(msg)

    def fetch_versions(self, package: str) -> list[tuple[Version, DistFile]]:
        """See :func:`nab_python._provider.listing.fetch_versions`."""
        return _listing.fetch_versions(self, package)

    def serving_index(self, canonical_name: str) -> str | None:
        """Return the index that served ``canonical_name``'s listing, or None.

        Drawn from the coordinator's record of which configured index a
        package's listing came from; ``None`` before any listing resolves
        or for synthetic (local / VCS) sources.
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
        (raises :class:`~nab_python.config.OverrideConflictError`).
        """
        result = self._effective_field(
            canonical_name,
            version,
            index_name,
            field="build-policy",
            package_value=lambda o: _unset_if_none(o.build_policy),
            index_value=lambda o: _unset_if_none(o.build_policy),
        )
        if result is _UNSET:
            return self.build_policy
        assert isinstance(result, BuildPolicy)
        return result

    def effective_build_policy_for_source(self, canonical_name: str) -> BuildPolicy:
        """Return the build policy for a synthetic local/VCS source.

        These sources have no serving index and their version is not
        known until the backend runs, so the version-scoped lookup does
        not apply.  A per-package override is honoured only when it uses a
        bare-name requirement (full range); a version-scoped override does
        not govern a local/VCS source's build decision.
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
        result = self._effective_field(
            canonical_name,
            version,
            index_name,
            field="dist-policy",
            package_value=lambda o: _unset_if_none(o.dist_policy),
            index_value=lambda o: _unset_if_none(o.dist_policy),
        )
        if result is _UNSET:
            return self._dist_policy
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
            package_value=self._package_uploaded_prior_to,
            index_value=self._index_uploaded_prior_to,
        )
        if result is _UNSET:
            return self.uploaded_prior_to
        # ``result`` is either ``None`` (a disabled cutoff) or a datetime;
        # the upload-time value helpers only ever yield those two.
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
            package_value=lambda o: _unset_if_none(o.dist_trust_unverified_deps),
            index_value=lambda o: _unset_if_none(o.dist_trust_unverified_deps),
        )
        if result is _UNSET:
            return self.trust_unverified_sdist_deps
        assert isinstance(result, bool)
        return result

    @staticmethod
    def _package_uploaded_prior_to(override: PackageOverride) -> object:
        """Per-package upload-time value: a datetime, ``None`` (disabled), or unset."""
        if override.uploaded_prior_to is not None:
            return override.uploaded_prior_to
        if override.uploaded_prior_to_disabled:
            return None
        return _UNSET

    @staticmethod
    def _index_uploaded_prior_to(override: IndexOverride) -> object:
        """Per-index upload-time value: a datetime, ``None`` (disabled), or unset."""
        if override.uploaded_prior_to is not None:
            return override.uploaded_prior_to
        if override.uploaded_prior_to_disabled:
            return None
        return _UNSET

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
        package_value: Callable[[PackageOverride], object],
        index_value: Callable[[IndexOverride], object],
    ) -> object:
        """Resolve one policy field for a candidate across both override surfaces.

        Returns the per-package value when a range-matching per-package
        override sets ``field``, else the per-index value when the serving
        index's override sets it, else ``_UNSET`` (the caller substitutes
        the global default).  When BOTH surfaces set the field for this
        candidate, raises :class:`~nab_python.config.OverrideConflictError`:
        the two surfaces are deliberately not ranked.

        Each value callable returns ``_UNSET`` when its override does not
        set the field, and the actual value (which may be ``None`` for a
        disabled upload-time cutoff) when it does.
        """
        pkg = self._matching_package_override(canonical_name, version, package_value)
        idx = self._index_overrides.get(index_name) if index_name is not None else None
        idx_value = index_value(idx) if idx is not None else _UNSET

        if pkg is not None and idx_value is not _UNSET:
            # Late import: config imports provider at module load.
            from .config import OverrideConflictError  # noqa: PLC0415

            msg = (
                f"override conflict for {canonical_name}=={version} served from"
                f" index {index_name!r}: both a per-package override"
                f" ({str(pkg.requirement)!r}) and the per-index override set"
                f" {field!r}.  The per-package and per-index surfaces are not"
                " ranked; remove one of the two settings for this field."
            )
            raise OverrideConflictError(msg)

        if pkg is not None:
            return package_value(pkg)
        return idx_value

    def force_backtrack_count(self, canonical_name: str) -> int:
        """How many times this package has triggered force-backtrack."""
        return self._force_backtrack_counts.get(canonical_name, 0)

    def has_invalid_metadata(self, canonical_name: str, version: Version) -> bool:
        """Return True if metadata parsing previously failed for this pin."""
        return (canonical_name, version) in self._invalid_metadata

    def materialize_local_source(
        self,
        normalized: str,
        source: LocalSource,
    ) -> list[tuple[Version, DistFile]]:
        """See :func:`nab_python._provider.sources.materialize_local_source`."""
        result: list[tuple[Version, DistFile]] = []
        for version, sdist in _sources.materialize_local_source(
            self, normalized, source
        ):
            result.append((version, sdist))
        return result

    def materialize_vcs_source(
        self,
        normalized: str,
        source: VcsSource,
    ) -> list[tuple[Version, DistFile]]:
        """See :func:`nab_python._provider.sources.materialize_vcs_source`."""
        result: list[tuple[Version, DistFile]] = []
        for version, sdist in _sources.materialize_vcs_source(self, normalized, source):
            result.append((version, sdist))
        return result

    def versions_only(
        self,
        normalized: str,
        version_list: list[tuple[Version, DistFile]],
    ) -> list[Version]:
        """See :func:`nab_python._provider.listing.versions_only`."""
        return _listing.versions_only(self, normalized, version_list)

    def _wheel_by_version(
        self,
        normalized: str,
        version_list: list[tuple[Version, DistFile]],
    ) -> dict[Version, DistFile]:
        """See :func:`nab_python._provider.listing.wheel_by_version`."""
        return _listing.wheel_by_version(self, normalized, version_list)

    def speculative_prefetch(
        self,
        normalized: str,
        versions: list[tuple[Version, DistFile]],
    ) -> None:
        """See :func:`nab_python._provider.listing.speculative_prefetch`."""
        _listing.speculative_prefetch(self, normalized, versions)

    def prefetch_walk_ahead(self, normalized: str) -> None:
        """See :func:`nab_python._provider.listing.prefetch_walk_ahead`."""
        _listing.prefetch_walk_ahead(self, normalized, self.DEEP_PREFETCH_COUNT)

    def filter_distributions(
        self, normalized: str, files: Sequence[WheelFile | SdistFile]
    ) -> list[tuple[Version, DistFile]]:
        """See :func:`nab_python._provider.listing.filter_distributions`."""
        return _listing.filter_distributions(self, normalized, files)

    def pick_best_candidate(
        self,
        normalized: str,
        versions: list[tuple[Version, DistFile]],
    ) -> tuple[Version, DistFile] | None:
        """See :func:`nab_python._provider.listing.pick_best_candidate`."""
        return _listing.pick_best_candidate(self, normalized, versions)

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        """Pick a version within the allowed range, respecting the strategy."""
        assert isinstance(version_range, VersionRange)
        self.stats.choose_version_calls += 1

        base, extra, normalized = self.split_and_normalize(package)
        if extra is not None:
            return self._choose_extra_version(package, base, extra, version_range)

        version_list = self.fetch_versions(package)
        all_versions = self.versions_only(normalized, version_list)
        candidates = list(version_range.filter(all_versions))

        # VersionRange.filter yields newest-first; reverse for LOWEST so
        # look-ahead walks oldest -> newest.
        if self.wants_lowest(normalized):
            candidates.reverse()

        no_lookahead = not self.root_requirements and not self.solution_decisions
        if no_lookahead or not candidates:
            if not candidates:
                self._record_no_versions_reason(package, all_versions)
            return candidates[0] if candidates else None

        skip = self._try_abort_skip(normalized, candidates[0])
        if skip is not None:
            return skip

        wheel_by_version = self._wheel_by_version(normalized, version_list)
        return self._run_full_scan(
            normalized, candidates, wheel_by_version, package, all_versions
        )

    def _try_abort_skip(self, normalized: str, first: Version) -> Version | None:
        """Return the first candidate when a prior abort is still valid.

        While the recorded blocker decision is unchanged, a re-run of
        the full scan would just trip the abort again. A warm cache
        hit returns directly; otherwise a non-decision look-ahead
        guards against unreadable wheels. Returns None when no
        recorded abort applies, or when the candidate fails the gate.
        """
        aborted = self._lookahead_aborted.get(normalized)
        if aborted is None:
            return None
        blocker_pkg, blocker_version = aborted
        if self.solution_decisions.get(blocker_pkg) != blocker_version:
            del self._lookahead_aborted[normalized]
            return None
        if (normalized, first) in self.deps_cache:
            return first
        if self._look_ahead_ok(normalized, first, check_decisions=False):
            self._flush_pending_blocks()
            return first
        return None

    def _run_full_scan(
        self,
        normalized: str,
        candidates: list[Version],
        wheel_by_version: dict[Version, DistFile],
        package: str,
        all_versions: list[Version],
    ) -> Version | None:
        """Run the decision-aware look-ahead scan over candidates."""
        broad_rejections = 0
        if self._look_ahead_ok(normalized, candidates[0], check_decisions=True):
            self._flush_pending_blocks()
            return candidates[0]
        self.stats.look_ahead_rejections += 1
        broad_rejections += 1

        found = self._scan_candidates_pipelined(
            normalized,
            candidates[1:],
            wheel_by_version,
            broad_rejections,
            first_candidate=candidates[0],
        )
        if found is not None:
            self._flush_pending_blocks()
            return found

        # Every candidate rejected. Flush so the resolver replaces the default
        # NO_VERSIONS clause with the grouped binary incompatibilities.
        blockers = self._capture_lookahead_blockers(normalized)
        self._flush_pending_blocks()
        self._record_no_versions_reason(package, all_versions, blockers=blockers)
        return None

    def _choose_extra_version(
        self, package: str, base: str, extra: str, version_range: VersionRange
    ) -> Version | None:
        """See :func:`nab_python._provider.extras.choose_extra_version`."""
        return _extras.choose_extra_version(self, package, base, extra, version_range)

    def _scan_candidates_pipelined(
        self,
        normalized: str,
        remaining: list[Version],
        wheel_by_version: dict[Version, DistFile],
        broad_rejections: int,
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
        discard the misleading singleton-blocker pending blocks for
        ``normalized`` and return ``first_candidate``.  The resolver then
        decides that candidate tentatively, ``get_dependencies`` emits the
        actual dep-range clause, and pubgrub back-jumps the offending
        blocker on its own.  Sound because no clause is emitted by the
        abort path.
        """
        # Front-load deep metadata before the scan: by the time the
        # 8-batch trips the abort, the rest of the walk is in flight.
        self.prefetch_walk_ahead(normalized)

        starts_iter = iter(range(0, len(remaining), self.PREFETCH_BATCH))
        in_flight: deque[
            tuple[list[Version], list[tuple[Version, str, threading.Event]]]
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
            check_decisions = broad_rejections < self._BROAD_LA_REJECT_CAP
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
            if self._try_abort_lookahead(normalized):
                return first_candidate, broad_rejections
        return None, broad_rejections

    def _try_abort_lookahead(self, normalized: str) -> bool:
        """Run the monolithic-rejection abort. Return True when fired.

        Records the abort state, queues the blocker for force-backtrack
        (up to the per-blocker cap), and returns True so the caller
        falls back to its first candidate.
        """
        blocker = self._should_abort_lookahead(normalized)
        if blocker is None:
            return False
        self._discard_pending_decision_blocks(normalized)
        self._lookahead_aborted[normalized] = blocker
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

        The trigger is intentionally narrow: only when *every* rejection
        for ``normalized`` is a decision block with the same
        ``(blocker_pkg, blocker_version)`` key and there are no
        range / root / metadata blocks.  Returns ``(blocker_pkg,
        blocker_version)`` when the condition holds, else ``None``.
        Mixed-cause scans keep the per-version clauses because at least
        one rejection cause is a real constraint the resolver still
        needs to learn.
        """
        seen: set[tuple[str, Version]] = set()
        for cand, blocker_pkg, blocker_version in self.pending_blocks:
            if cand == normalized:
                seen.add((blocker_pkg, blocker_version))
                if len(seen) > 1:
                    return None
        if len(seen) != 1:
            return None
        if any(cand == normalized for cand, *_ in self.pending_range_blocks):
            return None
        if any(cand == normalized for cand, *_ in self.pending_root_blocks):
            return None
        if normalized in self.pending_metadata_blocks:
            return None
        return next(iter(seen))

    def _discard_pending_decision_blocks(self, normalized: str) -> None:
        """Drop decision-block entries for ``normalized`` without emitting clauses.

        Used by the look-ahead abort path: the singleton-blocker clauses
        the queue would otherwise produce are exactly the ones that
        mislead the resolver into picking a deep candidate.  Range / root
        / metadata blocks are left in place because the abort path only
        fires when none exist for this candidate; this helper still
        scopes its delete to the matching candidate name for safety.
        """
        self.pending_blocks = defaultdict(
            list,
            {k: v for k, v in self.pending_blocks.items() if k[0] != normalized},
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
        blockers: list[str] | None = None,
    ) -> None:
        """Record why ``choose_version`` returned ``None`` for ``package``.

        ``blockers`` carries the look-ahead rejection causes when
        every candidate that fell in ``version_range`` was rejected:
        either because of an already-decided package, a positive-range
        constraint, a root-requirement disagreement, or because the
        candidate's metadata could not be read under the current
        build policy.  When supplied, the recorded reason names those
        causes so the user does not see a bare "no version matches
        the requirement", which would suggest the package is
        missing from the index when in fact it is the resolver's
        transitive constraints (or a too-strict build policy) that
        excluded every candidate.
        """
        if not all_versions:
            reason = "package not found on any configured index"
        elif blockers:
            # Look-ahead rejection: candidates DID match the range but
            # were rejected.  Naming the blocker is more useful than
            # a generic "no version matches" line, which would
            # otherwise fire because ``all_versions`` contains
            # versions inside ``version_range``.
            joined = "; ".join(blockers)
            reason = f"every version in range was rejected: {joined}"
        else:
            reason = "no version matches the requirement"
        self._no_versions_reasons[package] = reason

    def _capture_lookahead_blockers(self, normalized: str) -> list[str]:
        """Summarise pending look-ahead rejections for ``normalized``.

        Returns one human-readable string per blocker source
        (decisions, positive ranges, root disagreements, metadata errors).
        """
        out: list[str] = []

        for cand, blocker_pkg, blocker_version in self.pending_blocks:
            if cand != normalized:
                continue
            out.append(f"requires {blocker_pkg} != {blocker_version}")

        for cand, blocker_pkg, blocker_range in self.pending_range_blocks:
            if cand != normalized:
                continue
            out.append(
                f"requires {blocker_pkg} in {blocker_range}"
                " (disjoint with current solution range)"
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
                f"requires {blocker_pkg} in {dep_range} but root has it in {root_range}"
            )

        # Collapse repeated metadata-error blockers (one per version) into
        # a single "N versions failed (first: <msg>)" line.
        meta = self.pending_metadata_blocks.get(normalized)
        if meta:
            count = len(meta)
            first_msg = meta[0][1]
            if count == 1:
                out.append(first_msg)
            else:
                out.append(
                    f"{count} versions failed metadata extraction (first: {first_msg})"
                )

        return out

    def get_no_versions_reason(self, package: str) -> str | None:
        """Return the recorded reason for ``package``'s NO_VERSIONS clause.

        Returns ``None`` if no diagnostic was captured (e.g. the
        package was decided successfully or failed for a non-listing
        reason such as a metadata parse error).
        """
        return self._no_versions_reasons.get(package)

    def _prefetch_batch(
        self,
        package: str,
        versions: list[Version],
        wheel_by_version: dict[Version, DistFile],
    ) -> list[tuple[Version, str, threading.Event]]:
        """See :func:`nab_python._provider.listing.prefetch_batch`."""
        return _listing.prefetch_batch(self, package, versions, wheel_by_version)

    def _await_metadata_batch(
        self,
        package: str,
        submitted: list[tuple[Version, str, threading.Event]],
    ) -> None:
        """See :func:`nab_python._provider.listing.await_metadata_batch`."""
        _listing.await_metadata_batch(self, package, submitted)

    def receive_partial_solution_hint(
        self,
        positive_ranges: dict[str, RangeProtocol[Version]],
        decisions: dict[str, Version],
    ) -> None:
        """Accept a snapshot of the resolver's positive-range assignments.

        Decision-only forward checking is safer than reasoning over
        derivations because backjumping a decision also undoes its derivations.

        The caller hands over fresh snapshots it does not retain or mutate, so
        we store them directly. We only ever read these maps, never mutate them
        in place; both are reassigned wholesale on the next hint.
        """
        self.solution_ranges = positive_ranges
        self.solution_decisions = decisions

    def _look_ahead_ok(
        self, package: str, version: Version, *, check_decisions: bool = True
    ) -> bool:
        """See :func:`nab_python._provider.lookahead.look_ahead_ok`."""
        return _lookahead.look_ahead_ok(
            self, package, version, check_decisions=check_decisions
        )

    def _flush_pending_blocks(self) -> None:
        """See :func:`nab_python._provider.lookahead.flush_pending_blocks`."""
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

    def get_dependencies(
        self, package: str, version: Version
    ) -> dict[str, VersionRange]:
        """Fetch .metadata and return dependencies as VersionRange."""
        self.stats.get_dependencies_calls += 1

        base, extra, normalized = self.split_and_normalize(package)
        if extra is not None:
            return self._get_extra_dependencies(base, extra, version)

        cache_key = (normalized, version)
        if cache_key in self.deps_cache:
            return self.deps_cache[cache_key]
        if cache_key in self._unsupported_sdists:
            effective = self.effective_build_policy(
                normalized, version, self.serving_index(normalized)
            )
            msg = (
                f"{normalized}=={version} sdist metadata could not be extracted"
                f" under BuildPolicy.{effective.name} (cached prior failure)"
            )
            raise UnsupportedSdistError(msg)
        cached_invalid = self._invalid_metadata.get(cache_key)
        if cached_invalid is not None:
            raise MetadataError(cached_invalid)

        versions = self.fetch_versions(package)

        # Local + VCS sources pre-populate metadata during fetch_versions.
        if cache_key in self.metadata_cache and (
            normalized in self.local_sources or normalized in self.vcs_sources
        ):
            self._cache_deps_from_metadata(cache_key, self.metadata_cache[cache_key])
            return self.deps_cache[cache_key]

        metadata_text, from_sdist = self._resolve_metadata(versions, package, version)

        try:
            self.parse_and_cache_metadata(
                cache_key, metadata_text, from_sdist=from_sdist
            )
        except UnsupportedSdistError:
            self._unsupported_sdists.add(cache_key)
            raise
        except (UnsupportedVcsError, NotImplementedError):
            # A refused direct-URL dep is a hard error, not a parse skip.
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

        self.stats.metadata_fetched += 1
        self.prefetch_new_deps(self.deps_cache[cache_key])

        return self.deps_cache[cache_key]

    def prefetch_new_deps(self, deps: dict[str, VersionRange]) -> None:
        """See :func:`nab_python._provider.listing.prefetch_new_deps`."""
        _listing.prefetch_new_deps(self, deps)

    def _resolve_metadata(
        self,
        versions: list[tuple[Version, DistFile]],
        package: str,
        version: Version,
    ) -> tuple[str, bool]:
        """See :func:`nab_python._provider.metadata_resolver.resolve_metadata`."""
        return _metadata_resolver.resolve_metadata(self, versions, package, version)

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

    def _cache_deps_from_metadata(
        self,
        cache_key: tuple[str, Version],
        metadata: WheelMetadata,
    ) -> None:
        """See :func:`._provider.metadata_resolver.cache_deps_from_metadata`."""
        _metadata_resolver.cache_deps_from_metadata(self, cache_key, metadata)

    def _get_extra_dependencies(
        self,
        base: str,
        extra: str,
        version: Version,
    ) -> dict[str, VersionRange]:
        """See :func:`nab_python._provider.extras.get_extra_dependencies`."""
        return _extras.get_extra_dependencies(self, base, extra, version)

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

    def is_ready(self, package: str) -> bool:
        """Check if a package's listing is available without blocking.

        Used by the resolver to prefer packages with cached data,
        letting it make progress while other listings are in flight.
        """
        _, extra, normalized = self.split_and_normalize(package)
        if extra is not None:
            return normalized in self.versions_cache
        if normalized in self.versions_cache:
            return True
        return self.coordinator.index.get_listing(normalized) is not None

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

        See :mod:`nab_python._provider.priority` for the implementation.
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

    def vcs_pin_for(self, canonical_name: str) -> str | None:
        """Return the post-clone commit SHA for ``canonical_name``, or None.

        Written by :func:`~nab_python._provider.sources.materialize_vcs_source`
        after the shallow clone resolves the ref to a 40-char SHA.
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
