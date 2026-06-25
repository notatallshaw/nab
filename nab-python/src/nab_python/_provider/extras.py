"""Extras-of-extras expansion for the provider.

The provider models extras as proxy packages: ``foo[bar]`` is a
distinct package whose only candidates are the versions of ``foo``
that declare ``bar`` in their ``Provides-Extra`` field.  This
module owns the per-extra version chooser, the per-extra
dependency lookup, and the missing-extra fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .._vendor.packaging.ranges import VersionRange
from .metadata_resolver import refuse_url_dep

if TYPE_CHECKING:
    from nab_resolver.types import RangeProtocol

    from .._vendor.packaging.version import Version
    from ..provider import Provider


logger = logging.getLogger(__name__)


def choose_extra_version(
    provider: Provider,
    package: str,
    base: str,
    extra: str,
    version_range: VersionRange,
) -> Version | None:
    """Pick a version for an extras proxy package.

    Delegates to the base package's version list. In BACKTRACK mode,
    eagerly checks if the version provides the extra and skips it
    if not.  The strategy decision (highest vs lowest) is keyed off
    the *base* canonical name; an extras proxy never gets a different
    answer than its underlying package.
    """
    _, _, normalized = provider.split_and_normalize(base)
    version_list = provider.fetch_versions(base)
    all_versions = provider.versions_only(normalized, version_list)
    candidates = list(version_range.filter(all_versions))

    # Filter by base's positive range so we don't pick a proxy version
    # that would force base==V into a known-conflicting state.
    base_range = provider.solution_ranges.get(normalized)
    excluded_by_base: list[Version] = []
    if base_range is not None:
        kept: list[Version] = []
        for v in candidates:
            if v in base_range:
                kept.append(v)
            else:
                excluded_by_base.append(v)
        candidates = kept

    if provider.wants_lowest(normalized):
        candidates = list(reversed(candidates))

    chosen = _pick_in_mode(provider, base, extra, candidates)
    if chosen is None and excluded_by_base and base_range is not None:
        _record_base_range_blocks(
            provider, package, normalized, base_range, excluded_by_base
        )
    return chosen


def _pick_in_mode(
    provider: Provider,
    base: str,
    extra: str,
    candidates: list[Version],
) -> Version | None:
    """Pick a candidate honoring ``ExtrasMode``.

    Fetches base metadata so an extraction failure (unparseable
    PKG-INFO, or an sdist build the policy disallows) becomes a
    candidate skip instead of a fatal error during the later
    dependency fetch.  BACKTRACK mode additionally checks
    ``Provides-Extra`` for transitive extras.

    Missing-metadata cases (no PEP 658, no sdist) skip transitive
    extras but fall through for user-requested ones; mock test
    coordinators rely on this.
    """
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import (
        ExtrasMode,
        MetadataError,
        UnsupportedSdistError,
        _normalize_extra,
    )

    _, _, normalized = provider.split_and_normalize(base)
    is_user = (normalized, extra) in provider.root_extras
    backtrack = provider.extras_mode == ExtrasMode.BACKTRACK
    for version in candidates:
        if provider.has_invalid_metadata(normalized, version):
            continue
        try:
            provider.get_dependencies(base, version)
        except UnsupportedSdistError:
            continue
        except MetadataError:
            if not is_user or provider.has_invalid_metadata(normalized, version):
                continue
            return version
        if is_user or not backtrack:
            return version
        metadata = provider.metadata_cache.get((normalized, version))
        provided = (
            {_normalize_extra(e) for e in metadata.provides_extra}
            if metadata
            else set()
        )
        if metadata is None or extra in provided:
            return version
    return None


def _record_base_range_blocks(
    provider: Provider,
    proxy_pkg: str,
    base_normalized: str,
    base_range: RangeProtocol[Version],
    excluded: list[Version],
) -> None:
    """Push binary clauses for proxy candidates filtered by base's range.

    Each excluded version V records ``{proxy_pkg == V, base ==
    base_decision}`` (or the range-block analogue) impossible.
    Without these, the resolver only sees a single-term NO_VERSIONS
    clause for the proxy and cannot connect the proxy's
    unsatisfiability to the base decision that caused it; with them,
    conflict resolution can learn to revisit the base decision.

    The caller guarantees ``excluded`` is non-empty: filtering only
    populates it when ``base_range`` is set, so the range-block path
    always has a target to record against.  When the resolver has
    already decided the base, recording against the decision is
    tighter (a singleton blocker) than recording against the range.
    """
    # Late import: ``lookahead`` shares state with this module
    # through ``pypi`` and importing it at module load creates a cycle.
    from .lookahead import flush_pending_blocks

    base_decision = provider.solution_decisions.get(base_normalized)
    if base_decision is not None:
        for v in excluded:
            provider.pending_blocks[(proxy_pkg, base_normalized, base_decision)].append(
                v
            )
    else:
        for v in excluded:
            provider.pending_range_blocks[
                (proxy_pkg, base_normalized, base_range)
            ].append(v)
    flush_pending_blocks(provider)


def get_extra_dependencies(
    provider: Provider,
    base: str,
    extra: str,
    version: Version,
) -> dict[str, VersionRange]:
    """Get dependencies for an extras proxy package."""
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import MetadataError, join_extra

    _, _, normalized = provider.split_and_normalize(base)
    extra_key = join_extra(normalized, extra)
    cache_key = (extra_key, version)
    if cache_key in provider.deps_cache:
        return provider.deps_cache[cache_key]

    # Ensure base metadata is fetched and cached.
    provider.get_dependencies(base, version)
    base_cache_key = (normalized, version)
    metadata = provider.metadata_cache.get(base_cache_key)
    if metadata is None:  # pragma: no cover
        # get_dependencies(base, version) above always populates
        # metadata_cache on success or raises; this is defensive.
        msg = f"No metadata cached for {base}=={version}"
        raise MetadataError(msg)

    deferred = provider.deferred_url_extras.get(base_cache_key, {}).get(extra)
    if deferred:
        req, url = deferred[0]
        refuse_url_dep(provider, req, url)

    extra_map = provider.extra_deps_map.get(base_cache_key, {})
    if extra not in extra_map:
        return handle_missing_extra(provider, normalized, extra, version, cache_key)

    deps = dict(extra_map[extra])
    # Pin the base, intersected with any bound the extra itself
    # places on it (``foo>=2; extra == "bar"``).
    deps[normalized] = deps.get(
        normalized, VersionRange.full()
    ) & VersionRange.singleton(version)

    provider.deps_cache[cache_key] = deps
    provider.prefetch_new_deps(deps)
    return deps


def handle_missing_extra(
    provider: Provider,
    normalized: str,
    extra: str,
    version: Version,
    cache_key: tuple[str, Version],
) -> dict[str, VersionRange]:
    """Handle a request for an extra not in Provides-Extra.

    In ERROR_USER and BACKTRACK modes, user-provided extras raise
    immediately. Transitive missing extras always warn and return
    only the base dep (BACKTRACK skips these versions in
    choose_version before we get here).
    """
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import ExtrasMode, MissingExtraError

    is_user = (normalized, extra) in provider.root_extras
    if is_user and provider.extras_mode != ExtrasMode.WARN:
        msg = f"{normalized}=={version} does not provide extra '{extra}'"
        raise MissingExtraError(msg)

    logger.warning(
        "%s==%s does not provide extra '%s'",
        normalized,
        version,
        extra,
    )
    # The extra contributes no deps at this version, but the proxy
    # must still pin its base: without the pin the proxy and the base
    # can settle on different versions, and if the base's version does
    # provide the extra its dependencies are silently dropped.
    deps = {normalized: VersionRange.singleton(version)}
    provider.deps_cache[cache_key] = deps
    return deps
