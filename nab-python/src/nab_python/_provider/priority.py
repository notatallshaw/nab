"""Priority computation for :class:`nab_python.provider.Provider`.

Owns the tier/matching/culprit logic that backs ``prioritize``.
Affected packages with high conflict counts get tier 0 (decide
first inside a conflict cluster); runaway top culprits get tier 2
(uv's deprioritise-on-conflict); everything else gets tier 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_resolver.types import RangeProtocol

    from .._vendor.packaging.version import Version
    from ..provider import Provider


CONFLICT_THRESHOLD = 5

# Demotion requires a runaway gap to the second-highest culprit, so
# co-dominant culprits keep standard ordering.
CULPRIT_DEMOTE_THRESHOLD = 5

# Lower number = higher priority.
TIER_AFFECTED = 0
TIER_NORMAL = 1
TIER_CULPRIT = 2

# Matching count used while a listing is in flight, so not-yet-fetched
# packages sort behind ready ones.
_NO_LISTING_PRIOR = 1000


def compute_tier(
    normalized: str,
    affected_count: int,
    culprit_count: int,
    culprit_counts: Mapping[str, int] | None,
    *,
    force_backtracked: bool = False,
) -> int:
    """Decide the priority tier from conflict and culprit counts.

    ``force_backtracked`` short-circuits the gap rule: the look-ahead
    abort is a precise enough culprit signal on its own.
    """
    if affected_count >= CONFLICT_THRESHOLD:
        return TIER_AFFECTED
    if force_backtracked:
        return TIER_CULPRIT
    if is_dominant_culprit(normalized, culprit_count, culprit_counts):
        return TIER_CULPRIT
    return TIER_NORMAL


def compute_matching(
    provider: Provider,
    normalized: str,
    version_range: RangeProtocol[Version],
) -> int:
    """Return the count of cached versions of ``normalized`` in ``version_range``.

    Also fires speculative metadata prefetch when this is the first time we
    notice the listing has arrived in the coordinator index.  Returns
    :data:`_NO_LISTING_PRIOR` while the listing is still in flight, reading
    arrival through ``arrived_listing`` so it agrees with ``is_ready`` for the
    whole decision scan.
    """
    per_pkg = provider.matching_cache.get(normalized)
    if per_pkg is not None:
        cached = per_pkg.get(version_range)
        if cached is not None:
            return cached

    # Local/VCS/archive sources short-circuit the listing path; their synthetic
    # listing is materialised lazily by fetch_versions.
    has_local_source = (
        normalized in provider.local_sources
        or normalized in provider.vcs_sources
        or normalized in provider.archive_sources
    )
    if normalized not in provider.versions_cache and not has_local_source:
        files = provider.arrived_listing(normalized)
        if files is not None:
            versions = provider.filter_distributions(normalized, files)
            provider.versions_cache[normalized] = versions
            provider.stats.listings_fetched += 1
            provider.speculative_prefetch(normalized, versions)

    if normalized in provider.versions_cache:
        versions = provider.versions_cache[normalized]
        matching = sum(1 for v, _ in versions if v in version_range)
    elif has_local_source:
        matching = 1
    else:
        # Not cached, so a later scan re-checks the index and the
        # listing-arrival side effect above can still fire.
        return _NO_LISTING_PRIOR

    if per_pkg is None:
        per_pkg = provider.matching_cache[normalized] = {}
    per_pkg[version_range] = matching
    return matching


def is_dominant_culprit(
    package: str,
    package_count: int,
    culprit_counts: Mapping[str, int] | None,
) -> bool:
    """Return True when ``package`` is the runaway top culprit.

    Demote only when the gap to the next culprit is >= CULPRIT_DEMOTE_THRESHOLD;
    co-dominant culprits stay within ~1 of each other so the standard ordering
    wins.
    """
    if culprit_counts is None or package_count < CULPRIT_DEMOTE_THRESHOLD:
        return False
    second_highest = max(
        (count for other, count in culprit_counts.items() if other != package),
        default=0,
    )
    return package_count - second_highest >= CULPRIT_DEMOTE_THRESHOLD


def prioritize(
    provider: Provider,
    package: str,
    version_range: RangeProtocol[Version],
    conflict_counts: Mapping[str, int],
    culprit_counts: Mapping[str, int] | None = None,
) -> tuple[int, int, bool]:
    """Prioritize packages for resolution order.

    Returns ``(tier, matching_count, is_base)``.  Extras proxies sort before
    their base at equal tier so they pin the base version directly (avoids
    the backtrack storm when the base is decided before the extras proxy).

    Never blocks on I/O.
    """
    provider.stats.prioritize_calls += 1
    _, extra, normalized = provider.split_and_normalize(package)
    affected_count = conflict_counts.get(normalized, 0)
    culprit_count = (
        culprit_counts.get(normalized, 0) if culprit_counts is not None else 0
    )
    force_backtracked = provider.force_backtrack_count(normalized) > 0

    # Fast path: when culprit_count is below the demote threshold AND the
    # package was not force-backtracked, the tier depends only on
    # affected_count and is safe to cache by Range identity.
    cacheable = culprit_count < CULPRIT_DEMOTE_THRESHOLD and not force_backtracked
    if cacheable:
        cached = provider.priority_cache.get(package)
        if (
            cached is not None
            and cached[0] is version_range
            and cached[1] == affected_count
        ):
            return cached[2]

    tier = compute_tier(
        normalized,
        affected_count,
        culprit_count,
        culprit_counts,
        force_backtracked=force_backtracked,
    )
    matching = compute_matching(provider, normalized, version_range)
    priority = (tier, matching, extra is None)

    # Don't cache the in-flight placeholder; compute_matching's listing-arrival
    # side effect (speculative prefetch) lives in the cache-miss branch.
    if cacheable and normalized in provider.versions_cache:
        provider.priority_cache[package] = (version_range, affected_count, priority)
    return priority
