"""Index-backed provider for nab-resolver.

Fetches package metadata from package indexes on demand using
nab-index, converting PEP 440/508 types into nab-resolver Range
types.  A FetchCoordinator overlaps index I/O on a background
asyncio loop.
"""

from __future__ import annotations

import bisect
import enum
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, cast

from nab_index.client import (
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    SdistFile,
    SdistHashMismatchError,
    WheelFile,
    WheelHashMismatchError,
)
from nab_index.errors import IndexAccessError
from nab_index.transport import UnserveableUrlError

from ._conflict_kind import EMPTY_MEMBERSHIP_SETS
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
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.utils import canonicalize_name
from .metadata import WheelMetadata
from .target import host_environment

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from nab_resolver.types import Incompatibility, RangeProtocol

    from ._vendor.packaging.markers import Marker
    from ._vendor.packaging.requirements import Requirement
    from ._vendor.packaging.version import Version
    from .config import IndexOverride, NabProjectConfig, PackageOverride
    from .fetch import FetchCoordinator
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

_EXTRA_RE = re.compile(r"^(?P<base>[^\[]+)\[(?P<extra>[^\]]+)\]$")


def split_extra(package: str) -> tuple[str, str | None]:
    """Split 'name[extra]' into ('name', 'extra'), or ('name', None).

    The extra name is normalized per PEP 685.
    """
    m = _EXTRA_RE.match(package)
    if m is None:
        return (package, None)
    return (m.group("base"), canonicalize_name(m.group("extra")))


def join_extra(base: str, extra: str) -> str:
    """Join a base name and extra into 'name[extra]'.

    The extra name is normalized per PEP 685.
    """
    return f"{base}[{canonicalize_name(extra)}]"


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


class ResolveMode(enum.Enum):
    """How the resolver interprets the project.

    ``SPECIFIC`` resolves one target, the host or an impersonated
    marker environment.  ``UNIVERSAL`` resolves one target per tuple
    declared in ``[tool.nab.matrix]``.  Both run the same engine over a
    list of targets.  ``UNIVERSAL``'s multi-target lockfile format is
    *experimental* and may change; the resolver itself is the same one
    ``SPECIFIC`` runs.  Users opt in by setting
    ``[tool.nab].mode = "universal"`` and declaring ``[tool.nab.matrix]``.
    """

    SPECIFIC = "specific"
    UNIVERSAL = "universal"


class DistPolicy(enum.Enum):
    """How to admit wheels and sdists during resolution."""

    WHEEL_ONLY = "wheel-only"
    """Ignore sdists entirely. Use wheels, reading PEP 658 metadata when
    published or an HTTP range read of the wheel otherwise."""

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
    local checkouts via ``[[tool.nab.local-sources]]``, VCS clones via
    ``[[tool.nab.vcs-sources]]``, and archive sources via
    ``[[tool.nab.archive-sources]]`` are all read statically.  A source whose
    metadata cannot be read statically raises :class:`UnsupportedSdistError`,
    which skips a PyPI sdist version but ends the resolve for a declared
    source.
    """

    BUILD_LOCAL = "build-local"
    """Static metadata everywhere, plus PEP 517 builds on local checkouts.

    Adds backend invocation for ``[[tool.nab.local-sources]]`` and
    workspace members when their ``pyproject.toml`` cannot be read
    statically.  VCS clones, archive sources, and remote PyPI sdists
    remain static-only.
    """

    BUILD_REMOTE = "build-remote"
    """Builds extend to VCS clones, archive sources, and remote PyPI sdists.

    On top of :attr:`BUILD_LOCAL`, invokes the backend on VCS-cloned
    trees, extracted archive trees, and fetched sdists when their
    metadata is dynamic and has no static fallback.
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


class DecisionOrder(enum.Enum):
    """Whether arrived listings may steer which package is decided next."""

    ARRIVAL = "arrival"
    """Rank on what has already landed, so the search keeps moving (default)."""

    STABLE = "stable"
    """Wait for each listing, so the sort key cannot see which had arrived."""


@dataclass(frozen=True, slots=True)
class LocalSource:
    """A source tree on disk used as the only candidate for a package.

    ``name`` is the package name; the resolver pins the package to a
    single synthetic version, read from the directory's
    ``[project].version`` field or computed by the build backend when
    that field is declared dynamic.  ``path`` is the absolute filesystem
    path to the source tree.

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


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    """A direct-URL archive used as the only candidate for a package.

    ``name`` is the package name; ``url`` is the archive URL carrying its
    hash (and optional subdirectory) in the fragment, e.g.
    ``https://example.com/x-1.0.tar.gz#sha256=<hex>``.  The provider
    downloads and hash-verifies the archive, then extracts and treats it
    as a :class:`LocalSource` for metadata extraction.
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
    under :attr:`BuildPolicy.BUILD_REMOTE`.  For a PyPI sdist it is
    caught by :meth:`Provider._look_ahead_ok`, so the resolver skips
    the version.  A declared source (local, VCS, archive, or workspace
    member) is read while listing its one version, so the error ends
    the resolve instead.
    """


class ForeignMetadataError(MetadataError):
    """An index candidate's METADATA declares a different release.

    Core metadata ``Name`` and ``Version`` say which release an artifact is, so
    a candidate whose METADATA (or :pep:`658` sidecar) names another project or
    version describes some other release's dependencies.  Caught by
    :meth:`Provider._look_ahead_ok` so the resolver skips the version.
    """


class IncompatiblePythonError(MetadataError):
    """An index candidate's METADATA Requires-Python excludes the resolve target.

    The Simple-API ``requires-python`` hint is optional, so the listing gate
    admits a version whose listing omits it.  Once the wheel METADATA (or sdist
    PKG-INFO) is fetched, its authoritative ``Requires-Python`` is checked and
    an incompatible candidate is rejected.  Caught by
    :meth:`Provider._look_ahead_ok` so the resolver skips the version.
    """


# Deliberately not a MetadataError: _look_ahead_ok catches MetadataError
# and would silently reject the version; a naive upload-time is a hard error.
class InvalidUploadTimeError(Exception):
    """Raised when an index upload-time is not the timezone-aware UTC PEP 700 needs."""


# Deliberately not a MetadataError: _look_ahead_ok catches those and skips the
# version, but tie-ranked wheels that disagree on a target's dependencies are an
# ambiguity nab cannot resolve, so it must abort rather than drop the version.
class SiblingMetadataDivergenceError(Exception):
    """Raised when a version's tie-ranked wheels declare different target deps.

    nab reads one wheel's dependencies per version and treats it as
    authoritative, so a tie whose wheels declare different dependencies is an
    ambiguity: pinning from one silently disagrees with an install of the other.
    """


# Deliberately not a MetadataError: _look_ahead_ok catches those and skips the
# version, but a name mismatch is a misconfiguration that must abort.
class SourceNameMismatchError(Exception):
    """Raised when a materialised source's project name differs from its declaration.

    A local, VCS, or archive source maps a declared ``name`` to a directory,
    repo, or archive and becomes the only candidate for that package.  When the
    source's own ``[project].name`` does not canonicalise to the declared name,
    it provides a different distribution, so pinning it would carry the wrong
    version and dependencies.
    """


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


DistFile = WheelFile | SdistFile


_STAT_FIELDS = tuple(stat.name for stat in fields(ProviderStats))


def _counters(stats: ProviderStats) -> tuple[int, ...]:
    """Return every counter of ``stats``, in ``_STAT_FIELDS`` order."""
    return tuple(getattr(stats, name) for name in _STAT_FIELDS)


class ListingFilterCache:
    """Base listing-filter results shared across the targets of one resolve.

    The pre-tag half of the listing filter (see
    :func:`nab_python._provider.listing.base_distributions`) reads the
    listing's files, the policy config, and the target Python, and has no
    platform axis, so targets that differ only by platform recompute an
    identical list.  Memoising it per (package, Python) leaves only the
    wheel-tag pass to run per target.

    One instance is only valid across providers that share a coordinator
    and a policy config, as the targets of one resolve do.
    """

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: dict[
            tuple[str, str | None],
            tuple[list[tuple[Version, DistFile]], tuple[int, ...]],
        ] = {}

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
            for name, count in zip(_STAT_FIELDS, delta, strict=True):
                if count:
                    setattr(stats, name, getattr(stats, name) + count)
            return list(result)

        before = _counters(stats)
        result = compute()

        delta = tuple(
            now - was for now, was in zip(_counters(stats), before, strict=True)
        )
        self._entries[(package, python_version)] = (list(result), delta)
        return result


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
    A FetchCoordinator submits listing fetches to a background
    asyncio loop, so transitive deps are fetched concurrently with
    resolution.

    ``target`` is the environment the resolve is for: its markers gate
    every dependency, its Python filters candidates by Requires-Python,
    and its wheel tags filter candidates by PEP 425 compatibility, so a
    version whose only wheels the target cannot install is a version the
    resolver never sees.  Left unset, markers evaluate against the host
    and neither filter runs, since nothing has said which machine the
    resolve targets.

    ``constraints`` are the user's version bounds, keyed as the resolver
    keys packages, so an extras proxy carries its base's bound under its
    own ``name[extra]`` key.  The provider reads them when deciding
    whether a missing root extra is worth reporting.

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
    _LOOKAHEAD_ABORT_THRESHOLD = 8

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
        coordinator: FetchCoordinator,
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
        build_config: NabProjectConfig | None = None,
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

        # Passed through to the build env when extract_source_metadata
        # falls through to a PEP 517 backend; static-only callers leave None.
        self.build_config = build_config
        self.extras_mode = extras_mode
        self.root_extras = root_extras or set()
        self._dist_policy = dist_policy
        self.build_policy = build_policy
        # Opt-out: trust a pre-2.2 sdist's PKG-INFO deps as final instead of
        # routing through the dynamic path. Off by default (strict PEP 643).
        # ``build_config`` carries the project's own setting; the argument is
        # for a caller that has no config (a benchmark harness).
        self.trust_unverified_sdist_deps = trust_unverified_sdist_deps or (
            build_config is not None and build_config.trust_unverified_sdist_deps
        )
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
        self._has_overrides = bool(self._package_overrides or self._index_overrides)

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

        # ``environment`` backs every marker evaluation; ``env_with_extra``
        # is the reused per-evaluation copy of it, seeded with the empty
        # lockfile-only set variables (see EMPTY_MEMBERSHIP_SETS).  Without a
        # target both come from the host and ``python_version`` stays None,
        # which turns the Requires-Python filter off: nothing has declared
        # the Python a candidate could be rejecting (see _provider.listing).
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

        # Wheels the tag filter dropped, per canonical name, so a package left
        # with no candidate can say the target has no compatible wheel rather
        # than blame requires-python or the cutoff.
        self.tag_excluded_wheels: dict[str, int] = {}

        # The same drops keyed by (canonical name, version), for the lock's
        # per-package omitted count.  Bumped alongside tag_excluded_wheels.
        self.tag_excluded_wheels_by_version: dict[tuple[str, Version], int] = {}

        # Canonical names whose listing lost a file to requires-python,
        # dist-policy, or the upload cutoff before the tag pass ran.  A
        # tag-rejected wheel on some other version must not then claim the
        # whole package failed on wheel tags alone.
        self.base_filtered_packages: set[str] = set()

        self.root_requirements = root_requirements or {}
        self.constraints: Mapping[str, VersionRange] = constraints or {}
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
        # Memoised str(marker) for the cheap "extra" in marker_text gate.
        self.marker_text_cache: dict[int, str] = {}

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
        self.pending_metadata_blocks: defaultdict[str, dict[Version, str]] = (
            defaultdict(dict)
        )

        # Last NO_VERSIONS reason per package; consumed by resolve.py to
        # enrich ResolutionError messages.
        self._no_versions_reasons: dict[str, str] = {}

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

    def fetch_versions(self, package: str) -> list[tuple[Version, DistFile]]:
        """See :func:`nab_python._provider.listing.fetch_versions`."""
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
        if not self._has_overrides:
            return self._dist_policy

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
        if not self._has_overrides:
            return self.uploaded_prior_to

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

    def materialize_archive_source(
        self,
        normalized: str,
        source: ArchiveSource,
    ) -> list[tuple[Version, DistFile]]:  # pragma: no cover (see sources.py)
        """See :func:`nab_python._provider.sources.materialize_archive_source`."""
        result: list[tuple[Version, DistFile]] = []
        for version, sdist in _sources.materialize_archive_source(
            self, normalized, source
        ):
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
            return self._choose_extra_version(package, base, extra, version_range)

        version_list = self.fetch_versions(package)
        all_versions = self.versions_only(normalized, version_list)
        candidates = list(
            version_range.filter(all_versions, assume_sorted="descending")
        )

        # VersionRange.filter yields newest-first; reverse for LOWEST so
        # look-ahead walks oldest -> newest.
        if self.wants_lowest(normalized):
            candidates.reverse()

        no_lookahead = not self.root_requirements and not self.solution_decisions
        if no_lookahead or not candidates:
            if not candidates:
                self._record_no_versions_reason(
                    package, all_versions, version_range=version_range
                )
            return candidates[0] if candidates else None

        wheel_by_version = self._wheel_by_version(normalized, version_list)
        return self._run_full_scan(
            normalized, candidates, wheel_by_version, package, all_versions
        )

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

        all_versions = self.versions_only(normalized, self.fetch_versions(package))

        # The proxy's range is built full(), so intersect it with the base's
        # positive range, which carries the pre-release admission granted by
        # the requirement that named the extra.  This mirrors
        # choose_extra_version.
        admit_range = version_range
        if extra is not None:
            base_range = self.solution_ranges.get(normalized)
            if base_range is not None:
                admit_range = version_range & base_range

        in_range = admit_range.filter(all_versions, assume_sorted="descending")
        if preferred not in in_range:
            return None

        usable = (
            _extras.version_provides_extra(self, base, extra, preferred)
            if extra is not None
            else self._look_ahead_ok(normalized, preferred, check_decisions=True)
        )
        return preferred if usable else None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        """Report whether a usable version exists, side-effect-free.

        Runs the real ``choose_version`` over ``version_range`` so look-ahead
        rejections are honored, then rolls back the state it records: the queued
        clauses and force-backtrack signal are drained, and the force-backtrack
        budget and no-versions reasons are restored to their pre-probe values.
        A failed-resolve attribution probe therefore cannot alter a later
        decision.

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
        # Late import: config imports provider at module load.
        from .config import OverrideConflictError  # noqa: PLC0415

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
            self._force_backtrack_counts = saved_counts

            # Restore the snapshot but keep the probe's own blocker reason.
            probed_reason = self._no_versions_reasons.get(package)
            self._no_versions_reasons = saved_reasons
            if probed_reason is not None:
                self._no_versions_reasons[package] = probed_reason

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
        discard the misleading pending decision blocks for
        ``normalized`` and return ``first_candidate``.  The resolver then
        decides that candidate tentatively, ``get_dependencies`` emits the
        actual dep-range clause, and pubgrub back-jumps the offending
        blocker on its own.  Sound because no clause is emitted by the
        abort path.
        """
        # Front-load deep metadata so a walk past the first batch hits cache.
        self.prefetch_walk_ahead(normalized)

        starts_iter = iter(range(0, len(remaining), self.PREFETCH_BATCH))
        in_flight: deque[
            tuple[list[Version], list[tuple[Version, str, str, threading.Event]]]
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

        Used by the look-ahead abort path: the blocker clauses the queue
        would otherwise produce are exactly the ones that mislead the
        resolver into picking a deep candidate.  Range / root / metadata
        blocks are left in place because the abort path only fires when none
        exist for this candidate; this helper still scopes its delete to the
        matching candidate name for safety.
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
        blockers: list[str] | None = None,
        version_range: VersionRange | None = None,
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

        ``version_range`` is passed only when no surviving version fell
        inside it.  A version the listing filter dropped that does fall
        inside it is the release the requirement asked for, so the reason
        names the filter rather than reporting no match.

        ``all_versions`` is post-filter, so an empty one means either the
        index served no files or every file it served was dropped by the
        wheel-tag filter, requires-python, dist-policy, or the upload-time
        cutoff.  The stored listing tells absence from incompatibility
        apart, except that it is also empty for an index skipped offline
        and for a page of formats nab does not read (``.zip`` sdists,
        ``.exe`` installers).  Both are marked when stored so the reason
        names them instead of absence.
        The wheel-tag case (a Windows-only package on a Linux target) is
        named only when the base pass dropped nothing, or when the file
        it dropped was an sdist: there the reason names both the rejected
        tags and the filtered sdist, because the sdist is what the user
        can bring back.  A base-filtered wheel alongside a tag-rejected
        wheel on another version reports the base-filter reason alone.

        A look-ahead rejection emits a clause that removes the rejected
        versions from the range, so the resolver asks again over a range
        nothing falls in.  That second ask has no blockers of its own, so
        its no-match reason must not overwrite the one naming the blocker.
        """
        _, _, normalized = self.split_and_normalize(package)
        if not all_versions:
            raw_listing = self.coordinator.index.get_listing(normalized)
            tag_excluded = self.tag_excluded_wheels.get(normalized, 0)
            if not raw_listing:
                if self.coordinator.index.is_offline_listing_miss(normalized):
                    reason = "offline mode skipped an index with no cached listing"
                elif self.coordinator.index.is_unreadable_only_listing(normalized):
                    reason = (
                        "found on index but no file is a wheel or a .tar.gz sdist"
                        " (the formats nab reads)"
                    )
                else:
                    reason = "package not found on any configured index"
            elif tag_excluded and normalized not in self.base_filtered_packages:
                reason = (
                    f"found on index but none of the wheel's tags are compatible"
                    f" with the resolve target ({tag_excluded} wheels rejected),"
                    f" and no sdist is available to build from"
                )
            elif tag_excluded and any(isinstance(f, SdistFile) for f in raw_listing):
                # A present sdist beside tag-rejected wheels was dropped by
                # the base pass (it would otherwise keep its version alive),
                # so name both causes rather than the base filter alone.
                reason = (
                    f"found on index but none of the wheel's tags are compatible"
                    f" with the resolve target ({tag_excluded} wheels rejected),"
                    f" and the sdist was filtered by requires-python,"
                    f" dist-policy, or upload-time"
                )
            else:
                reason = (
                    "found on index but no distribution is compatible "
                    "(all filtered by requires-python, dist-policy, or upload-time)"
                )
        elif blockers:
            # Look-ahead rejection: candidates DID match the range but
            # were rejected.  Naming the blocker is more useful than
            # a generic "no version matches" line, which would
            # otherwise fire because ``all_versions`` contains
            # versions inside ``version_range``.
            joined = "; ".join(blockers)
            reason = f"every version in range was rejected: {joined}"
        elif package in self._no_versions_reasons:
            # The weakest reason: keep whatever is already recorded.
            return
        elif version_range is not None and _listing.has_filtered_in_range_release(
            self, normalized, version_range, all_versions
        ):
            reason = (
                "found on index but every version matching the requirement"
                " was filtered (by requires-python, wheel tags, dist-policy,"
                " or upload-time)"
            )
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
            dep_range = self.pending_decision_dep_ranges[
                (cand, blocker_pkg, blocker_version)
            ].union
            # The blocker is decided, so the line names that version rather
            # than a singleton range, which has no specifier spelling.
            out.append(
                f"requires {blocker_pkg} in {self.format_range(dep_range)}"
                f" but solution has it at {blocker_version}"
            )

        for cand, blocker_pkg, pos_range in self.pending_range_blocks:
            if cand != normalized:
                continue
            dep_range = self.pending_range_dep_ranges[
                (cand, blocker_pkg, pos_range)
            ].union
            out.append(
                f"requires {blocker_pkg} in {self.format_range(dep_range)}"
                f" but solution has it in {self.format_range(pos_range)}"
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
                f"requires {blocker_pkg} in {self.format_range(dep_range)}"
                f" but root has it in {self.format_range(root_range)}"
            )

        # Collapse repeated metadata-error blockers (one per version) into
        # a single "N versions failed (first: <msg>)" line.
        meta = self.pending_metadata_blocks.get(normalized)
        if meta:
            count = len(meta)
            first_msg = next(iter(meta.values()))
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
    ) -> list[tuple[Version, str, str, threading.Event]]:
        """See :func:`nab_python._provider.listing.prefetch_batch`."""
        return _listing.prefetch_batch(self, package, versions, wheel_by_version)

    def _await_metadata_batch(
        self,
        package: str,
        submitted: list[tuple[Version, str, str, threading.Event]],
    ) -> None:
        """See :func:`nab_python._provider.listing.await_metadata_batch`."""
        _listing.await_metadata_batch(self, package, submitted)

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
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

    def _ascending_versions(
        self,
        normalized: str,
        version_list: list[tuple[Version, DistFile]],
    ) -> list[Version]:
        """Return the widening universe for ``normalized``: ascending, cached.

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

        A constraint containing every listed version is promoted to the full
        range rather than snapped, so it reads as "any version".  An empty
        universe never promotes: that would widen a constraint no version
        satisfies.

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
        if (
            universe
            and universe[-1] in constraint
            and all(version in constraint for version in universe)
        ):
            return VersionRange.full(admit_arbitrary=False)
        return constraint.snap_bounds(universe)

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
            self._cache_deps_from_metadata(cache_key, self.metadata_cache[cache_key])
            return self.deps_cache[cache_key]

        # Skip-fetch: a complete ``dependencies`` override (even an empty
        # tuple) supplies the deps, so no METADATA fetch or build is needed.
        # After the local/VCS/archive branch so sources still materialise;
        # ``_cache_deps_from_metadata`` stamps the remaining override fields
        # onto the bare record.
        if self.effective_dependencies(normalized, version) is not None:
            self._cache_deps_from_metadata(
                cache_key, WheelMetadata(name=normalized, version=version)
            )
            self.prefetch_new_deps(self.deps_cache[cache_key])
            return self.deps_cache[cache_key]

        metadata_text, from_sdist = self._resolve_metadata(versions, package, version)
        self._parse_and_cache_metadata_guarded(
            cache_key, metadata_text, from_sdist=from_sdist
        )
        self._check_sibling_metadata_divergence(versions, package, version)

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
        from .config import OverrideConflictError  # noqa: PLC0415 (config import cycle)

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

    def _check_sibling_metadata_divergence(
        self,
        versions: list[tuple[Version, DistFile]],
        package: str,
        version: Version,
    ) -> None:
        """Check the version's tie-ranked wheels for divergent target deps.

        See :func:`._provider.metadata_resolver.check_sibling_metadata_divergence`.
        """
        _metadata_resolver.check_sibling_metadata_divergence(
            self, versions, package, version
        )

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

    def begin_decision_scan(self) -> None:
        """Open a decision scan, expiring the last one's in-flight answers.

        The coming scan re-reads the index, then holds any name it finds still
        in flight that way until the next call.
        """
        self._scan_generation += 1

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

    def archive_source_for(self, canonical_name: str) -> ArchiveSource | None:
        """Return the archive source registered under ``canonical_name`` or None."""
        return self.archive_sources.get(canonicalize_name(canonical_name))

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

    def tag_excluded_wheel_count(self, canonical_name: str, version: Version) -> int:
        """Return how many wheels the tag filter dropped at ``version`` (0 if none)."""
        normalized = canonicalize_name(canonical_name)
        return self.tag_excluded_wheels_by_version.get((normalized, version), 0)
