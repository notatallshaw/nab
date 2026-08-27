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

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.utils import canonicalize_name

from ..errors import MetadataError, MissingExtraError
from ..extra_keys import join_extra
from ..policy import ExtrasMode
from .listing_diagnosis import MetadataBlock, ReasonKind
from .lookahead import flush_pending_blocks
from .metadata_resolver import refuse_url_dep

if TYPE_CHECKING:
    from nab_provider._vendor.packaging.version import Version
    from nab_resolver.types import RangeProtocol

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

    # Filter by the base's positive range so we don't pick a proxy version
    # that would force base==V into a known-conflicting state.  Intersect
    # rather than test membership: the base's range carries the pre-release
    # admission granted by the requirement that named the extra, while the
    # proxy's own range is built full.
    base_range = provider.solution_ranges.get(normalized)
    if base_range is None:
        logger.debug(
            "no base range for %s; base admission cannot be applied to %s",
            normalized,
            package,
        )
        admit_range = version_range
    else:
        admit_range = version_range & base_range
    candidates = list(admit_range.filter(all_versions, assume_sorted="descending"))

    if provider.wants_lowest(normalized):
        candidates.reverse()

    chosen, unreadable = _pick_in_mode(provider, base, extra, candidates)
    declared_outside: Version | None = None
    if chosen is not None and (normalized, extra) in provider.root_extras:
        chosen, declared_outside = _pick_for_user_extra(
            provider, base, extra, chosen, candidates, all_versions
        )

    # Enumerate pre-releases too: default filtering buffers a pre-release
    # behind any matching final and would drop one that the base's bounds
    # exclude, so it would never be recorded and the proxy would keep a
    # permanent NO_VERSIONS clause past the backjump lifting the base
    # decision.  Membership below is bounds-only, so the blocks stay sound.
    if (
        chosen is None
        and base_range is not None
        and (
            excluded_by_base := [
                v
                for v in version_range.filter(
                    all_versions, prereleases=True, assume_sorted="descending"
                )
                if v not in base_range
            ]
        )
    ):
        _record_base_range_blocks(
            provider, package, normalized, base_range, excluded_by_base
        )
    if chosen is None:
        _record_extra_reason(
            provider,
            package,
            admit_range,
            all_versions,
            candidates,
            unreadable,
            declared_outside=declared_outside,
        )
    return chosen


def _record_extra_reason(
    provider: Provider,
    package: str,
    admit_range: VersionRange,
    all_versions: list[Version],
    candidates: list[Version],
    unreadable: list[MetadataBlock],
    *,
    declared_outside: Version | None,
) -> None:
    """Record why the proxy has no version, so the report can name the extra.

    Four situations are worth telling apart, and none of them is visible
    once the proxy's empty candidate list reaches the resolver: the search
    narrowed the base off every version declaring the extra, no version
    that could be read declares it, none could be read at all, or the base
    has no version to offer at all.  The resolver never asks after the base
    by name, so nothing else records that last one.

    ``declared_outside`` is the version the narrowing put out of reach,
    which the report names in place of a range: the range the search was
    left with is the solver's own and does not always spell as one.

    A base that published versions with none of them in range records
    nothing: no filter refused anything, so there is nothing to name.
    """
    if declared_outside is not None:
        provider.record_extra_no_versions(
            package, ReasonKind.EXTRA_NARROWED, declaring_version=declared_outside
        )
    elif candidates and len(unreadable) == len(candidates):
        provider.record_extra_no_versions(
            package, ReasonKind.EXTRA_METADATA, metadata=tuple(unreadable)
        )
    elif candidates:
        provider.record_extra_no_versions(
            package, ReasonKind.EXTRA_UNDECLARED, version_range=admit_range
        )
    elif not all_versions:
        provider.record_extra_base_empty(package)


def _pick_in_mode(
    provider: Provider,
    base: str,
    extra: str,
    candidates: list[Version],
) -> tuple[Version | None, list[MetadataBlock]]:
    """Pick a candidate honoring ``ExtrasMode``, and say what was unreadable.

    Fetches base metadata so an extraction failure (unparseable PKG-INFO,
    a disallowed sdist build, or no metadata source at all) skips the
    candidate rather than raising later, when the proxy refetches the base
    to expand the extra. This applies to user-requested extras too, since
    the proxy always needs the base metadata. BACKTRACK mode additionally
    checks ``Provides-Extra`` for transitive extras.

    The second return value carries one record per candidate skipped for
    an unreadable metadata source, which tells a proxy that found nothing
    apart from one whose candidates simply do not declare the extra.
    """
    _, _, normalized = provider.split_and_normalize(base)
    is_user = (normalized, extra) in provider.root_extras
    backtrack = provider.extras_mode == ExtrasMode.BACKTRACK
    unreadable: list[MetadataBlock] = []
    for version in candidates:
        cached = provider.invalid_metadata_reason(normalized, version)
        if cached is not None:
            unreadable.append(MetadataBlock(cached))
            continue
        try:
            provider.get_dependencies(base, version)
        except MetadataError as exc:
            unreadable.append(MetadataBlock(str(exc), exc.filtered_sdist_version))
            continue
        if is_user or not backtrack:
            return version, unreadable
        metadata = provider.metadata_cache.get((normalized, version))
        provided = (
            {canonicalize_name(e) for e in metadata.provides_extra}
            if metadata
            else set()
        )
        if metadata is None or extra in provided:
            return version, unreadable
    return None, unreadable


def _pick_for_user_extra(
    provider: Provider,
    base: str,
    extra: str,
    chosen: Version,
    candidates: list[Version],
    all_versions: list[Version],
) -> tuple[Version | None, Version | None]:
    """Keep or drop ``chosen`` when the root asked for ``extra``.

    A root extra pins the first in-range version even when that version
    lacks the extra, so the miss is reported against it rather than
    against an older version that declares it.  The exception is a range
    the search narrowed off every version declaring the extra: reporting
    no version there leaves a clause the search can backjump on.  The
    check runs against the root requirement's range intersected with the
    user's constraint, so the answer follows the index rather than the
    metadata fetched so far.

    Returns the version to offer and, where the narrowing is what lost the
    extra, the newest version outside the search that declares it.
    """
    if provider.extras_mode == ExtrasMode.WARN:
        return chosen, None

    _, _, normalized = provider.split_and_normalize(base)
    root_range = provider.root_requirements.get(normalized, VersionRange.full())
    constraint = provider.constraints.get(normalized)
    if constraint is not None:
        root_range = root_range & constraint

    admitted = set(candidates)
    outside = [v for v in root_range.filter(all_versions) if v not in admitted]
    if not outside:
        return chosen, None

    if any(version_provides_extra(provider, base, extra, v) for v in candidates):
        return chosen, None

    # Declared outside the narrowed range but not inside it: the
    # narrowing is what lost the extra, so let it be backjumped away.
    declaring = next(
        (v for v in outside if version_provides_extra(provider, base, extra, v)),
        None,
    )
    if declaring is not None:
        return None, declaring
    return chosen, None


def version_provides_extra(
    provider: Provider,
    base: str,
    extra: str,
    version: Version,
) -> bool:
    """Whether ``version`` of ``base`` declares ``extra`` and yields metadata here.

    Honoring a cross-tuple preference for a ``base[extra]`` proxy is only
    safe when the preferred version both provides the extra and has
    extractable metadata in this tuple.
    """
    _, _, normalized = provider.split_and_normalize(base)
    try:
        provider.get_dependencies(base, version)
    except MetadataError:
        return False

    metadata = provider.metadata_cache[(normalized, version)]
    provided = {canonicalize_name(e) for e in metadata.provides_extra}
    return extra in provided


def _record_base_range_blocks(
    provider: Provider,
    proxy_pkg: str,
    base_normalized: str,
    base_range: RangeProtocol[Version],
    excluded: list[Version],
) -> None:
    """Push binary clauses for proxy candidates filtered by base's range.

    Each excluded version V records ``proxy_pkg`` at V with ``base`` at
    ``base_decision`` (or the range-block analogue) impossible.
    Without these, the resolver only sees a single-term NO_VERSIONS
    clause for the proxy and cannot connect the proxy's
    unsatisfiability to the base decision that caused it; with them,
    conflict resolution can learn to revisit the base decision.

    The caller guarantees ``excluded`` is non-empty: filtering only
    populates it when ``base_range`` is set, so the range-block path
    always has a target to record against.  When the resolver has
    already decided the base, recording against the decision is
    tighter (the blocker names one selectable version) than recording
    against the range.
    """
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
