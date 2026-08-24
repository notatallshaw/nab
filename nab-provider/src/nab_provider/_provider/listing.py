"""Listing fetch, filter, and prefetch coordination for the provider.

Owns ``fetch_versions`` and the speculative-metadata prefetch
chain that feeds the resolver's ``choose_version`` look-ahead with
already-cached metadata where possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from nab_provider._vendor.packaging.version import InvalidVersion, Version
from nab_provider.records import SdistFile, WheelFile

from ..errors import (
    ForeignMetadataError,
    IncompatiblePythonError,
    InvalidUploadTimeError,
)
from ..iso8601 import fast_iso_parser, parse_iso_datetime
from ..metadata import intern_version as _intern_version
from ..policy import DistPolicy
from ..vcs_admission import UnsupportedVcsError
from .metadata_resolver import pick_dist_for_metadata, version_dists

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from datetime import datetime
    from typing import Literal, TypeAlias

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_resolver.types import RangeProtocol

    from ..fetch_port import Waitable
    from ..policy import ArchiveSource, LocalSource, VcsSource
    from ..provider import DistFile, Provider, ProviderStats
    from ..tags import TagSet

    _PreparedListing = tuple[
        list[tuple[Version, DistFile]],
        set[Version],
        bool,
    ]

    # The values :class:`DropCause` names, so a checker holds a cause to
    # that set rather than to ``str``.
    Cause: TypeAlias = Literal[
        "upload-time-missing",
        "upload-time-unparseable",
        "upload-time-naive",
        "upload-time-after-cutoff",
        "dist-policy",
        "sdist-install-no-sdist",
        "requires-python",
        "wheel-tags",
        "invalid-version",
    ]


# Matched to the provider's look-ahead abort threshold: prefetching 8 versions
# covers the worst-case abort scan without overshooting.  Used by the
# speculative root-batch prefetch and by the pipelined scan's batch.
PREFETCH_BATCH = 8


class DropCause:
    """Why the listing filter refused one file, or one whole version.

    A namespace of constants rather than an :class:`enum.Enum`, which is
    the most expensive way to declare a handful of names and is paid on
    every nab invocation, since every one imports this module.

    :data:`REPORT_ORDER` is the order the clauses print in, which is not
    the order the filter applies.  A file is refused at the first rung that
    objects, and the rungs run in this order: ``INVALID_VERSION``,
    ``DIST_POLICY``, ``REQUIRES_PYTHON``, the four ``UPLOAD_TIME_*``,
    ``SDIST_INSTALL_NO_SDIST``, ``WHEEL_TAGS``.
    """

    UPLOAD_TIME_MISSING: Final = "upload-time-missing"
    UPLOAD_TIME_UNPARSEABLE: Final = "upload-time-unparseable"
    UPLOAD_TIME_NAIVE: Final = "upload-time-naive"
    UPLOAD_TIME_AFTER_CUTOFF: Final = "upload-time-after-cutoff"
    DIST_POLICY: Final = "dist-policy"
    SDIST_INSTALL_NO_SDIST: Final = "sdist-install-no-sdist"
    REQUIRES_PYTHON: Final = "requires-python"
    WHEEL_TAGS: Final = "wheel-tags"
    INVALID_VERSION: Final = "invalid-version"

    REPORT_ORDER: Final = (
        UPLOAD_TIME_MISSING,
        UPLOAD_TIME_UNPARSEABLE,
        UPLOAD_TIME_NAIVE,
        UPLOAD_TIME_AFTER_CUTOFF,
        DIST_POLICY,
        SDIST_INSTALL_NO_SDIST,
        REQUIRES_PYTHON,
        WHEEL_TAGS,
        INVALID_VERSION,
    )


def fetch_versions(provider: Provider, package: str) -> list[tuple[Version, DistFile]]:
    """Fetch and cache available versions for a package.

    Checks the in-memory index first; if missing, requests from the coordinator
    and blocks until the listing arrives.  A declared local, VCS or archive
    source short-circuits: it becomes the package's only candidate.
    """
    _, _, normalized = provider.split_and_normalize(package)
    if normalized in provider.versions_cache:
        return provider.versions_cache[normalized]

    declared: LocalSource | VcsSource | ArchiveSource | None = (
        provider.local_sources.get(normalized)
    )
    if declared is None:
        declared = provider.vcs_sources.get(normalized)
    if declared is None:
        declared = provider.archive_sources.get(normalized)

    if declared is not None:
        result = provider.materialize_source(normalized, declared)
        provider.versions_cache[normalized] = result
        return result

    files = provider.coordinator.index.get_listing(normalized)
    if files is None:
        event = provider.coordinator.request_listing(normalized)
        event.wait()
        error = provider.coordinator.index.get_listing_error(normalized)
        if error is not None:
            raise error
        files = provider.coordinator.index.get_listing(normalized)
    # A successful fetch stores at least an empty list; a failed fetch
    # stores an error, re-raised above, so ``files`` is non-None here.
    assert files is not None

    # Routed through the method (not the module function) so a subclass
    # override still runs.
    result = provider.filter_distributions(normalized, files)
    provider.versions_cache[normalized] = result
    provider.stats.listings_fetched += 1

    if result:
        speculative_prefetch(provider, normalized, result)

    return result


def versions_only(
    provider: Provider,
    normalized: str,
    version_list: list[tuple[Version, DistFile]],
) -> list[Version]:
    """Return the cached version-only view for ``normalized``.

    One entry per version, in listing order, so a release with both a
    wheel and an sdist is not listed twice.
    """
    cached = provider.versions_only_cache.get(normalized)
    if cached is None:
        seen: set[Version] = set()
        cached = []
        for version, _ in version_list:
            if version not in seen:
                seen.add(version)
                cached.append(version)
        provider.versions_only_cache[normalized] = cached
    return cached


def speculative_prefetch(
    provider: Provider,
    normalized: str,
    versions: list[tuple[Version, DistFile]],
) -> None:
    """Fire metadata prefetch for likely candidates.

    Called from fetch_versions and prioritize when a listing
    first becomes available. For constrained root requirements,
    batch-prefetch the first N candidates within the root range
    so choose_version's look-ahead finds them cached. For
    transitive deps, just prefetch the single best candidate.
    """
    root_range = provider.root_requirements.get(normalized)
    if root_range is not None and not (~root_range).is_empty:
        prefetch_root_batch(provider, normalized, versions, root_range)
    else:
        prefetch_transitive_best(provider, normalized, versions)


def _has_complete_override(
    provider: Provider, normalized: str, version: Version
) -> bool:
    """Whether a complete ``dependencies`` override replaces this version's metadata.

    Callers skip prefetching such a candidate: ``get_dependencies`` synthesizes
    its deps without a METADATA fetch, so any prefetch would be wasted.
    """
    return provider.effective_dependencies(normalized, version) is not None


def prefetch_root_batch(
    provider: Provider,
    normalized: str,
    versions: list[tuple[Version, DistFile]],
    root_range: RangeProtocol[Version],
) -> None:
    """Batch metadata fetch for the candidates inside ``root_range``.

    Versions go out in the order ``choose_version`` walks them, so the look-ahead
    finds the ones it tries first already cached.
    """
    # Reverse out of place: ``versions`` is the shared cached listing.
    ordered = (
        list(reversed(versions)) if provider.wants_lowest(normalized) else versions
    )

    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    for version, dist in ordered:
        if len(items) >= PREFETCH_BATCH:
            break
        if version not in root_range:
            continue
        if (normalized, version) in provider.deps_cache:
            continue
        if _has_complete_override(provider, normalized, version):
            continue
        if isinstance(dist, WheelFile) and (url := dist.metadata_url) is not None:
            items.append((normalized, dist.version, url, dist.metadata_hash))
    if items:
        provider.coordinator.request_metadata_batch(items)


def prefetch_transitive_best(
    provider: Provider,
    normalized: str,
    versions: list[tuple[Version, DistFile]],
) -> None:
    """Fire metadata prefetch for the single best transitive candidate.

    Requests a candidate at most once. A dep with no root constraint reaches
    here once per parent that names it, and within one resolve the artifact a
    candidate resolves to is fixed. The mark goes on above the override and
    artifact checks, so it records a candidate as seen even when no request
    follows.
    """
    # Routed through ``provider.pick_best_candidate`` so existing
    # ``patch.object(provider, "pick_best_candidate", ...)`` mocks
    # in the test suite still drive this prefetch path.
    best = provider.pick_best_candidate(normalized, versions)
    if best is None:
        return
    version, _ = best
    if (normalized, version) in provider.deps_cache:
        return

    if (normalized, version) in provider.speculative_candidates:
        return
    provider.speculative_candidates.add((normalized, version))

    if _has_complete_override(provider, normalized, version):
        return

    # Prefetch the artifact the read picks, not the listing's first at that version.
    dist = pick_dist_for_metadata(
        versions, version, provider.wheel_tags, provider.target
    )
    if isinstance(dist, WheelFile) and (url := dist.metadata_url) is not None:
        provider.coordinator.request_metadata(
            normalized, dist.version, url, dist.metadata_hash
        )


def pick_best_candidate(
    provider: Provider,
    normalized: str,
    versions: list[tuple[Version, DistFile]],
) -> tuple[Version, DistFile] | None:
    """Pick the version the resolver will most likely try first.

    ``versions`` is newest-first, the order a highest strategy scans, so a
    package under a lowest strategy is read from the other end.
    """
    # Reverse out of place: ``versions`` is the shared cached listing.
    ordered: Iterable[tuple[Version, DistFile]] = (
        reversed(versions) if provider.wants_lowest(normalized) else versions
    )

    version_range = provider.root_requirements.get(normalized)
    for version, dist in ordered:
        if version_range is None or version in version_range:
            return (version, dist)
    return None


def filter_distributions(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
) -> list[tuple[Version, DistFile]]:
    """Filter by wheel tag, requires-python, upload time, and sort.

    Sorting: newest version first. When the effective ``dist-policy``
    is PREFER_WHEEL or SDIST_INSTALL, wheels sort before sdists at
    the same version so the metadata picker hits the cheapest source
    first.  ``normalized`` is the canonical package name used to look
    up the per-package / per-index ``uploaded-prior-to`` and
    ``dist-policy`` overrides; the serving index is read from the
    coordinator.

    This is the single funnel into ``versions_cache``, so what it drops
    is gone from candidate selection, metadata sourcing, every prefetch
    path, look-ahead, the emitted wheel list, and ``nab download``.

    A wheel whose PEP 425 tags the target does not accept is dropped
    (:func:`excluded_by_wheel_tags`), and a version left with no
    compatible wheel and no sdist is dropped with it: the target cannot
    install it, so the resolver must not pin it.  An sdist keeps a
    version alive at every :class:`~nab_provider.provider.BuildPolicy`,
    which is what stops the filter over-refusing a pure-source package;
    the tag check is a wheel's check, as it is in pip.  Look-ahead
    rejects the version later if the sdist's metadata cannot be read
    under the policy in force.

    The dist-policy and upload-time cutoff are version-scoped: a
    per-package override applies only to candidate versions inside its
    requirement's range, so each version's policy is evaluated against
    its own :class:`Version`.

    Under :attr:`~nab_provider.provider.DistPolicy.SDIST_INSTALL` a
    version keeps its wheels in ``versions_cache`` as a cheap metadata
    source only when it also publishes an sdist; a version whose only
    surviving artifact is a wheel has no source to install, so it is
    dropped and never becomes a candidate.  The kept wheels are dropped
    later, at lock construction time, so only the sdist is pinned.

    The filter runs in two passes.  :func:`base_distributions` applies
    everything that has no platform axis (dist policy, Requires-Python,
    upload cutoff, sort order, equal-version canonicalization), and is
    memoised per (package, Python) across the targets of one resolve
    when the provider carries a
    :class:`~nab_provider.provider.ListingFilterCache`.  The wheel-tag
    pass then runs per target on top of that shared list, so a
    linux-only wheel still stays off the Windows target.
    """
    base = base_distributions(provider, normalized, files)
    return _apply_wheel_tags(provider, normalized, base)


def base_distributions(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
) -> list[tuple[Version, DistFile]]:
    """Return the pre-tag filter result, through the shared memo when there is one."""
    cache = provider.listing_filter_cache
    if cache is None:
        return _filter_base(provider, normalized, files)

    return cache.filtered(
        normalized,
        provider.python_version,
        provider.stats,
        partial(_filter_base, provider, normalized, files),
    )


@dataclass(frozen=True, slots=True)
class ListingPolicy:
    """The policy config one listing's files are judged under.

    ``overridden`` is true when a per-package or per-index override can
    vary an answer by version, so the per-candidate lookups have to run.
    Without one, the defaults here answer for every file in the listing:
    ``default_drops_wheels`` and ``default_drops_sdists`` are
    ``default_dist_policy``'s artifact-kind verdict, taken once.
    """

    index_name: str | None
    overridden: bool
    default_dist_policy: DistPolicy
    default_cutoff: datetime | None
    time_filter_active: bool
    default_drops_wheels: bool
    default_drops_sdists: bool


def listing_policy(provider: Provider, normalized: str) -> ListingPolicy:
    """Return the policy answers for one listing, for its per-file loop to consult."""
    default_dist_policy = provider.dist_policy
    return ListingPolicy(
        index_name=provider.serving_index(normalized),
        overridden=provider.has_overrides,
        default_dist_policy=default_dist_policy,
        default_cutoff=provider.uploaded_prior_to,
        time_filter_active=(
            provider.uploaded_prior_to is not None or provider.overrides_set_time
        ),
        default_drops_wheels=default_dist_policy == DistPolicy.SDIST_ONLY,
        default_drops_sdists=default_dist_policy == DistPolicy.WHEEL_ONLY,
    )


def _filter_base(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
) -> list[tuple[Version, DistFile]]:
    """Filter and sort the listing by everything but the target's wheel tags.

    Reads only the listing, the resolve-wide policy config, and the
    target Python, so two targets that differ only by platform get the
    same list back.  Canonicalized here, ahead of the tag pass, so the
    representative version of an equal group is picked from the whole
    listing and does not vary with what a target's tags keep.

    The :attr:`~nab_provider.provider.DistPolicy.SDIST_INSTALL` drop of a
    wheel-only version belongs here rather than in the tag pass: it asks
    whether the version publishes an installable source, and an sdist
    carries no tags, so no target can lose the sdist that keeps the
    version alive.  The answer is the same for every target that shares
    the listing and the policy config, which is what the memo assumes.
    """
    policy = listing_policy(provider, normalized)

    cache = provider.listing_filter_cache
    if cache is None or not cache.shares_pythons:
        result, sdist_install_versions, sort_with_wheel_first = _prepare_listing(
            provider,
            normalized,
            files,
            policy,
            target_drops=True,
        )
    else:
        parsed, sdist_install_versions, sort_with_wheel_first = cache.prepared(
            normalized,
            provider.stats,
            partial(
                _prepare_listing,
                provider,
                normalized,
                files,
                policy,
                target_drops=False,
            ),
        )
        result = [
            pair
            for pair in parsed
            if not python_or_time_cause(provider, normalized, pair[0], pair[1], policy)
        ]

    result = _drop_sdist_install_wheel_only(result, sdist_install_versions)

    if sort_with_wheel_first:
        result.sort(
            key=lambda pair: (pair[0], isinstance(pair[1], WheelFile)),
            reverse=True,
        )
    else:
        result.sort(key=lambda pair: pair[0], reverse=True)
    return _canonicalize_equal_versions(result)


def _count_files_seen(stats: ProviderStats, wheels: int, sdists: int) -> None:
    """Raise the listing counters by the wheels and sdists one pass classified.

    Both preparation passes call this from a ``finally``, so a pass that
    raises part way through still reports the files it reached.
    """
    stats.distributions_seen += wheels + sdists
    stats.wheels_seen += wheels
    stats.sdists_seen += sdists


def _prepare_listing(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
    policy: ListingPolicy,
    *,
    target_drops: bool,
) -> _PreparedListing:
    """Parse and dist-policy-filter the listing.

    With ``target_drops`` the Requires-Python and upload-cutoff drops run
    here as well, which is what a one-Python resolve asks for.  A matrix
    leaves them off so its Pythons can share the pass, and runs them over
    the result instead.

    Returns the surviving (version, file) pairs in listing order, the
    versions the dist-policy pass judged
    :attr:`~nab_provider.provider.DistPolicy.SDIST_INSTALL`, and whether
    any version's policy wants wheels sorted ahead of sdists.
    """
    if target_drops and not policy.overridden:
        return _prepare_listing_defaults(provider, normalized, files, policy)

    result: list[tuple[Version, DistFile]] = []
    sort_with_wheel_first = False
    overridden = policy.overridden
    sdist_install_versions: set[Version] = set()
    wheels_walked = 0
    sdists_walked = 0
    try:
        for dist in files:
            if isinstance(dist, WheelFile):
                wheels_walked += 1
            else:
                sdists_walked += 1

            # Parse the version first: the policy and the cutoff are
            # version-scoped, so an unparseable version is dropped before
            # either is consulted.
            try:
                version = _intern_version(dist.version)
            except InvalidVersion:
                continue

            if overridden:
                effective_dist_policy = provider.effective_dist_policy(
                    normalized, version, policy.index_name
                )
            else:
                effective_dist_policy = policy.default_dist_policy

            if excluded_by_dist_policy(dist, effective_dist_policy):
                provider.stats.excluded_by_dist_policy += 1
                continue

            if effective_dist_policy is DistPolicy.SDIST_INSTALL:
                sdist_install_versions.add(version)
                sort_with_wheel_first = True
            elif effective_dist_policy is DistPolicy.PREFER_WHEEL:
                sort_with_wheel_first = True

            if target_drops and python_or_time_cause(
                provider, normalized, version, dist, policy
            ):
                continue

            result.append((version, dist))
    finally:
        _count_files_seen(provider.stats, wheels_walked, sdists_walked)

    return result, sdist_install_versions, sort_with_wheel_first


def _prepare_listing_defaults(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
    policy: ListingPolicy,
) -> _PreparedListing:
    """Run :func:`_prepare_listing`'s pass with the policy answers already taken.

    Reached when no override can vary an answer by version and the target
    drops belong to this pass, so one dist policy and one cutoff hold for
    the whole listing and each Requires-Python string has one verdict.
    Per file the general pass calls a dist-policy check and a combined
    Requires-Python and cutoff check; here the first is a pair of
    booleans, the second a dict lookup, and only the cutoff still calls
    out.

    ``sort_with_wheel_first`` follows the policy rather than the files
    that survive it, so it can be true where the general pass returns
    false. That happens only when nothing survives, and the caller sorts
    an empty list either way.
    """
    stats = provider.stats
    requires_python_cache = provider.requires_python_cache
    drops_wheels = policy.default_drops_wheels
    drops_sdists = policy.default_drops_sdists
    time_filter_active = policy.time_filter_active
    cutoff = policy.default_cutoff

    dist_policy = policy.default_dist_policy
    sdist_install = dist_policy is DistPolicy.SDIST_INSTALL
    sort_with_wheel_first = sdist_install or dist_policy is DistPolicy.PREFER_WHEEL

    result: list[tuple[Version, DistFile]] = []
    sdist_install_versions: set[Version] = set()
    wheels_walked = 0
    sdists_walked = 0
    try:
        for dist in files:
            is_wheel = isinstance(dist, WheelFile)
            if is_wheel:
                wheels_walked += 1
            else:
                sdists_walked += 1

            # Parse before the kind check so an unparseable file is not
            # counted as a dist-policy exclusion.
            try:
                version = _intern_version(dist.version)
            except InvalidVersion:
                continue

            wrong_kind = drops_wheels if is_wheel else drops_sdists
            if wrong_kind:
                stats.excluded_by_dist_policy += 1
                continue

            if sdist_install:
                sdist_install_versions.add(version)

            requires_python = dist.requires_python
            excluded = (
                requires_python_cache.get(requires_python) if requires_python else False
            )
            if excluded is None:
                excluded = excluded_by_python(provider, dist, None)
            elif excluded:
                # excluded_by_python counts its own drops; a cache hit skips it.
                stats.excluded_by_python += 1

            if excluded:
                continue

            if time_filter_active and _excluded_by_upload_time(
                provider, normalized, dist, cutoff
            ):
                continue

            result.append((version, dist))
    finally:
        _count_files_seen(stats, wheels_walked, sdists_walked)

    return result, sdist_install_versions, sort_with_wheel_first


def _apply_wheel_tags(
    provider: Provider,
    normalized: str,
    base: list[tuple[Version, DistFile]],
) -> list[tuple[Version, DistFile]]:
    """Drop the wheels this target cannot install, and the versions they leave empty.

    Runs per target: the tags are the one axis of the filter the targets
    of a matrix do not share.
    """
    tags = provider.wheel_tags
    if tags is None:
        return base

    result: list[tuple[Version, DistFile]] = []
    tag_rejected_versions: set[Version] = set()
    run_version: Version | None = None
    run_length = 0

    for version, dist in base:
        if not excluded_by_wheel_tags(dist, tags):
            result.append((version, dist))
            continue

        # ``base`` is sorted by version, so a version's rejected wheels arrive
        # together and fold into one tally update.  Identity is enough for the
        # boundary: a split run tallies twice and the counts still add up.
        if version is not run_version:
            if run_version is not None:
                _tally_tag_exclusions(provider, normalized, run_version, run_length)
            tag_rejected_versions.add(version)
            run_version = version
            run_length = 0
        run_length += 1

    if run_version is not None:
        _tally_tag_exclusions(provider, normalized, run_version, run_length)

    if tag_rejected_versions:
        # A version whose every wheel the target refused, and which ships no
        # sdist, has nothing left to install: it is gone, not merely wheel-less.
        kept = {version for version, _ in result}
        provider.stats.excluded_versions_no_compatible_wheel += len(
            tag_rejected_versions - kept
        )

    return result


def python_or_time_cause(
    provider: Provider,
    normalized: str,
    version: Version,
    dist: DistFile,
    policy: ListingPolicy,
) -> Cause | None:
    """Return why Requires-Python or the upload cutoff refuses ``dist``, or None.

    Counts the drop and, on a timezone-naive upload time, refuses the run.
    The diagnosis walk calls this same body rather than a copy of it, and
    brackets the counters it raises; see
    :func:`nab_provider._provider.listing_diagnosis.python_or_time_verdict`,
    which is the total sibling that answers instead of raising.
    """
    if policy.overridden:
        override_rp = provider.effective_requires_python(normalized, version)
    else:
        override_rp = None

    if excluded_by_python(provider, dist, override_rp):
        return DropCause.REQUIRES_PYTHON

    if not policy.time_filter_active:
        return None

    if policy.overridden:
        cutoff = provider.effective_uploaded_prior_to(
            normalized, version, policy.index_name
        )
    else:
        cutoff = policy.default_cutoff

    cause = upload_time_cause(dist, cutoff)
    if cause is None:
        return None
    if cause == DropCause.UPLOAD_TIME_NAIVE:
        raise InvalidUploadTimeError(naive_upload_time_message(normalized, dist))
    provider.stats.excluded_by_time += 1
    return cause


def excluded_by_wheel_tags(dist: DistFile, tags: TagSet) -> bool:
    """Return True when ``dist`` is a wheel the target cannot install.

    An sdist is never excluded here: it carries no tags, and building it
    produces a wheel for whatever machine runs the build.
    """
    return isinstance(dist, WheelFile) and not tags.accepts(dist.filename)


def _tally_tag_exclusions(
    provider: Provider,
    normalized: str,
    version: Version,
    count: int,
) -> None:
    """Add one version's ``count`` tag-rejected wheels to the diagnostic tallies.

    The per-``(package, version)`` count feeds the lock's omitted-wheel count.
    """
    provider.stats.excluded_by_wheel_tags += count
    key = (normalized, version)
    provider.tag_excluded_wheels_by_version[key] = (
        provider.tag_excluded_wheels_by_version.get(key, 0) + count
    )


def parsed_version(raw: str) -> Version | None:
    """Return the interned version, or None when it is not a PEP 440 version."""
    try:
        return _intern_version(raw)
    except InvalidVersion:
        return None


def dropped_release_in_range(
    provider: Provider, normalized: str, version_range: VersionRange
) -> bool:
    """Whether a file the filter dropped carries a version inside ``version_range``.

    Callers ask only when no surviving version falls in the range, so a
    dropped one that does is the release the requirement asked for.  A
    dropped version equal to a surviving one survived under another
    spelling instead: :func:`filter_distributions` collapses equal
    versions onto one representative, and ``===`` compares its string
    form.  Filtering through ``version_range`` keeps the pre-release
    semantics candidate selection uses.
    """
    files = provider.coordinator.index.get_listing(normalized)
    if not files:
        return False

    surviving = {
        version for version, _dist in provider.versions_cache.get(normalized) or []
    }
    dropped = (
        version
        for dist in files
        if (version := parsed_version(dist.version)) is not None
        and version not in surviving
    )
    return any(version_range.filter(dropped))


def sdist_install_wheel_only(
    result: list[tuple[Version, DistFile]],
    sdist_install_versions: set[Version],
) -> set[Version]:
    """Return the SDIST_INSTALL versions of ``result`` whose artifacts are all wheels.

    Shared with the diagnosis walk, so both read this rung from one body.
    """
    if not sdist_install_versions:
        return set()

    versions_with_sdist = {v for v, d in result if isinstance(d, SdistFile)}
    return sdist_install_versions - versions_with_sdist


def _drop_sdist_install_wheel_only(
    result: list[tuple[Version, DistFile]],
    sdist_install_versions: set[Version],
) -> list[tuple[Version, DistFile]]:
    """Drop SDIST_INSTALL versions whose surviving artifacts are all wheels.

    Such a version has no source to install, so it must not reach the
    resolver even though its wheels stay as a cheap metadata source.
    """
    drop = sdist_install_wheel_only(result, sdist_install_versions)
    if not drop:
        return result
    return [pair for pair in result if pair[0] not in drop]


def _canonicalize_equal_versions(
    result: list[tuple[Version, DistFile]],
) -> list[tuple[Version, DistFile]]:
    """Share one ``Version`` object across artifacts of one logical release.

    ``Version("1.0") == Version("1.0.0")`` yet their ``str()`` differ, so a
    release shipping a wheel filename ``1.0`` and an sdist filename ``1.0.0``
    would carry two equal but differently stringed versions, and the pin
    string (``str`` of the decided version) would then vary with resolution
    strategy and listing order.  Collapse each equal group to one
    representative, chosen by fewest release segments then string, so the
    pin is deterministic.
    """
    representative: dict[Version, Version] = {}
    needs_rebuild = False
    for version, _ in result:
        chosen = representative.get(version)
        if chosen is None:
            representative[version] = version
        elif chosen is not version:
            # The listing interns its versions, so two distinct objects that
            # compare equal are two spellings of one release.
            needs_rebuild = True
            if (len(version.release), str(version)) < (
                len(chosen.release),
                str(chosen),
            ):
                representative[version] = version

    if not needs_rebuild:
        return result
    return [(representative[version], dist) for version, dist in result]


def excluded_by_dist_policy(dist: DistFile, policy: object) -> bool:
    """Return True when ``policy`` rejects ``dist``'s artifact kind.

    ``WHEEL_ONLY`` drops sdists and ``SDIST_ONLY`` drops wheels; the
    other policies admit both kinds here (``SDIST_INSTALL`` keeps wheels
    as a metadata source and prunes them at lock-construction time).
    """
    if policy == DistPolicy.WHEEL_ONLY:
        return not isinstance(dist, WheelFile)
    if policy == DistPolicy.SDIST_ONLY:
        return isinstance(dist, WheelFile)
    return False


def excluded_by_python(
    provider: Provider, dist: DistFile, override_rp: str | None
) -> bool:
    """Return True when the target Python is excluded for this candidate.

    ``override_rp`` is the candidate's per-package ``requires-python``
    override and substitutes for ``dist.requires_python``.  Either goes
    through the same cached comparison, keyed by the specifier string,
    since the verdict depends only on that string and the fixed
    ``provider.target``.  The specifier is read at the language minor, so
    a micro segment never excludes a target (see
    :meth:`~nab_provider.target.ResolveTarget.admits_requires_python`).
    """
    effective = override_rp if override_rp is not None else dist.requires_python
    if not effective or provider.target is None:
        return False
    cached = provider.requires_python_cache.get(effective)
    if cached is None:
        try:
            spec = SpecifierSet(effective)
            cached = not provider.target.admits_requires_python(spec)
        except ValueError:
            # Malformed Requires-Python on the dist, or a digit run int()
            # refuses: treat as not-excluded, let downstream logic decide.
            # Our own python_version is validated at Provider construction.
            cached = False
        provider.requires_python_cache[effective] = cached
    if cached:
        provider.stats.excluded_by_python += 1
    return cached


def upload_time_cause(dist: DistFile, cutoff: datetime | None) -> Cause | None:
    """Return which upload-time rule refuses ``dist``, or None when none does.

    ``cutoff`` is the effective upload-time cutoff for the package, already
    resolved through the overrides and the global ``uploaded-prior-to``
    (``None`` means no cutoff applies to it).

    Total: a timezone-naive stamp is answered rather than raised, so a
    diagnosis can meet one without turning a report into an error.  The
    filter's caller raises on that answer.
    """
    # A local file:// artifact has no upload time, so the cutoff cannot apply.
    if cutoff is None or dist.local_path is not None:
        return None

    raw = dist.upload_time
    if raw is None:
        return DropCause.UPLOAD_TIME_MISSING

    try:
        upload_dt = fast_iso_parser(raw)
    except ValueError:
        # The fast parser can be the stricter of the two, so what it rejects
        # still gets the rewriting parse.
        try:
            upload_dt = parse_iso_datetime(raw)
        except ValueError:
            return DropCause.UPLOAD_TIME_UNPARSEABLE

    # PEP 700 mandates timezone-aware UTC upload times; refuse to guess.
    if upload_dt.tzinfo is None:
        return DropCause.UPLOAD_TIME_NAIVE

    if upload_dt >= cutoff:
        return DropCause.UPLOAD_TIME_AFTER_CUTOFF
    return None


def naive_upload_time_message(normalized: str, dist: DistFile) -> str:
    """Return the error text for a candidate whose upload time carries no zone."""
    return (
        f"{normalized} {dist.version} has a timezone-naive upload time "
        f"{dist.upload_time!r}; the Simple API requires "
        f"timezone-aware (UTC) upload times"
    )


def _excluded_by_upload_time(
    provider: Provider, normalized: str, dist: DistFile, cutoff: datetime | None
) -> bool:
    """Return True when ``cutoff`` refuses ``dist``, counting the drop.

    The partial sibling of :func:`upload_time_cause`: a timezone-naive
    stamp refuses the run here rather than being answered.
    """
    cause = upload_time_cause(dist, cutoff)
    if cause is None:
        return False
    if cause == DropCause.UPLOAD_TIME_NAIVE:
        raise InvalidUploadTimeError(naive_upload_time_message(normalized, dist))
    provider.stats.excluded_by_time += 1
    return True


def prefetch_walk_ahead(
    provider: Provider,
    normalized: str,
    version_range: RangeProtocol[Version],
    deep_count: int,
) -> None:
    """Submit metadata for the next ``deep_count`` wheels of ``normalized``.

    Called at the top of the pipelined scan, so a walk that runs past the
    first ``PREFETCH_BATCH`` window hits cache instead of paying one RTT
    per visit.

    ``version_range`` is the range the scan walks.  A version outside it
    is not requested, since the scan never reaches it, but it still fills
    its window slot.

    Takes each version's artifact from
    :func:`~nab_provider._provider.metadata_resolver.version_dists`, so the
    sidecar it warms is the one the read asks for.  Skips already-cached
    versions, versions whose artifact publishes no sidecar, and versions
    whose metadata the coordinator already holds.  Fire-and-forget.
    """
    versions_list = provider.versions_cache.get(normalized)
    if not versions_list:
        return
    picked = version_dists(provider, normalized, versions_list).picked
    coordinator_index = provider.coordinator.index

    # Reverse out of place: ``versions_list`` is the shared cached listing.
    ordered = (
        list(reversed(versions_list))
        if provider.wants_lowest(normalized)
        else versions_list
    )

    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    for version in _walk_ahead_window(ordered, version_range, deep_count):
        if (normalized, version) in provider.deps_cache:
            continue
        dist = picked[version]
        if not isinstance(dist, WheelFile) or (url := dist.metadata_url) is None:
            continue
        if _has_complete_override(provider, normalized, version):
            continue
        if coordinator_index.has_metadata(normalized, dist.version, url):
            continue
        items.append((normalized, dist.version, url, dist.metadata_hash))
    if items:
        provider.coordinator.request_metadata_batch(items)


def _walk_ahead_window(
    ordered: Sequence[tuple[Version, DistFile]],
    version_range: RangeProtocol[Version],
    deep_count: int,
) -> Iterator[Version]:
    """Yield the in-range versions among the first ``deep_count`` distinct ones.

    An excluded version still consumes its slot, so the window covers the
    same stretch of the listing whatever the range is.
    """
    seen: set[Version] = set()
    for version, _ in ordered:
        if version in seen:
            continue
        seen.add(version)
        if len(seen) > deep_count:
            return
        if version in version_range:
            yield version


def prefetch_batch(
    provider: Provider,
    package: str,
    versions: list[Version],
    wheel_by_version_map: dict[Version, DistFile],
) -> list[tuple[Version, str, str, Waitable]]:
    """Submit metadata fetches for a batch of candidates.

    Uses request_metadata_batch so all requests reach the fetcher
    as a single queue item and are processed concurrently.
    Returns list of (version, ver_str, metadata_url, event) for submitted
    requests.  Sibling wheels of one version hold their own texts, so the
    await reads the metadata back by the sidecar URL submitted here.
    """
    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    version_map: list[tuple[Version, str, str]] = []
    for v in versions:
        if (package, v) in provider.deps_cache or v not in wheel_by_version_map:
            continue
        # A complete override supplies the deps, so fetching this version's
        # metadata is wasted work.
        if _has_complete_override(provider, package, v):
            continue
        wheel = wheel_by_version_map[v]
        if isinstance(wheel, WheelFile) and (url := wheel.metadata_url) is not None:
            items.append((package, wheel.version, url, wheel.metadata_hash))
            version_map.append((v, wheel.version, url))

    if not items:
        return []

    raw = provider.coordinator.request_metadata_batch(items)
    submitted = []
    for (_pkg, _ver, ev), (version, ver_str, metadata_url) in zip(
        raw, version_map, strict=True
    ):
        submitted.append((version, ver_str, metadata_url, ev))
    return submitted


def await_metadata_batch(
    provider: Provider,
    package: str,
    submitted: list[tuple[Version, str, str, Waitable]],
) -> None:
    """Wait for all submitted metadata to arrive and queue it for decoding.

    A batch is scanned only until look-ahead accepts a candidate, so the rest
    of it is often never read.  :func:`parse_prefetched_metadata` decodes an
    entry when a caller asks for that candidate's dependencies.

    The integrity check stays at await time: it also reads the version-level
    error slot, which a later sdist failure can write, so a deferred check
    would see a failure this sidecar fetch never had.
    """
    index = provider.coordinator.index
    pending = provider.pending_metadata_parses
    for version, ver_str, metadata_url, event in submitted:
        cache_key = (package, version)
        if cache_key in provider.deps_cache:
            continue
        event.wait()
        if index.get_metadata_error(package, ver_str, metadata_url) is not None:
            continue
        pending[cache_key] = (ver_str, metadata_url)


def parse_prefetched_metadata(
    provider: Provider, cache_key: tuple[str, Version]
) -> None:
    """Decode this version's queued prefetch into ``deps_cache``, if it has one.

    Every rejection below returns without caching, leaving ``get_dependencies``
    to read the metadata itself and decide what the failure means.
    """
    queued = provider.pending_metadata_parses.pop(cache_key, None)
    if queued is None:
        return

    package, _version = cache_key
    ver_str, metadata_url = queued
    text, from_sdist = provider.coordinator.index.get_metadata_with_origin(
        package, ver_str, metadata_url
    )
    if text is None:
        # No PEP 658 text arrived, and caching nothing would pin the version
        # as dependency-free.
        return
    if from_sdist:
        # sdist PKG-INFO: caching it here would skip the PEP 643 gate.
        return

    try:
        provider.parse_and_cache_metadata(cache_key, text)
    except (
        ValueError,
        InvalidVersion,
        InvalidSpecifier,
        ForeignMetadataError,
        IncompatiblePythonError,
        UnsupportedVcsError,
        NotImplementedError,
    ):
        # Malformed metadata, metadata declaring another release, a
        # Python-incompatible Requires-Python, or a refused base
        # direct-URL/VCS dep.
        return


def prefetch_new_deps(provider: Provider, deps: Mapping[str, VersionRange]) -> None:
    """Submit listing and metadata fetches for newly discovered deps.

    For deps whose listings have already arrived (e.g., from a
    prior prefetch), also fire metadata prefetch for their best
    candidate. This deepens the prefetch cascade so metadata is
    ready before the resolver asks for it.

    Local, VCS, and archive sources are skipped; they have no PyPI
    listing and the materialise path in ``fetch_versions`` will
    surface them when the resolver asks.

    A dep named by several parents arrives once per parent, so only
    the first of them requests its listing.
    """
    for dep in deps:
        _, _, normalized = provider.split_and_normalize(dep)
        if (
            normalized in provider.local_sources
            or normalized in provider.vcs_sources
            or normalized in provider.archive_sources
        ):
            continue
        if normalized not in provider.versions_cache:
            # Listing not cached: request it speculatively so its read work
            # overlaps resolver CPU on the fetcher thread. When it arrives,
            # prioritize() notices and fires metadata prefetch.
            if normalized not in provider.speculative_listings:
                provider.speculative_listings.add(normalized)
                provider.coordinator.request_listing(normalized, speculative=True)
        else:
            # Listing cached: fire speculative metadata prefetch.
            speculative_prefetch(
                provider, normalized, provider.versions_cache[normalized]
            )
