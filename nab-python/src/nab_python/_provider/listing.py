"""Listing fetch, filter, and prefetch coordination for the provider.

Owns ``fetch_versions`` and the speculative-metadata prefetch
chain that feeds the resolver's ``choose_version`` look-ahead with
already-cached metadata where possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from nab_index.client import SdistFile, WheelFile

from .._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from .._vendor.packaging.version import InvalidVersion, Version
from ..metadata import intern_version as _intern_version

if TYPE_CHECKING:
    import threading
    from collections.abc import Mapping, Sequence

    from nab_resolver.types import RangeProtocol

    from .._vendor.packaging.ranges import VersionRange
    from ..provider import DistFile, Provider


def fetch_versions(provider: Provider, package: str) -> list[tuple[Version, DistFile]]:
    """Fetch and cache available versions for a package.

    Checks the in-memory index first; if missing, requests from
    the coordinator and blocks until the listing arrives.  Local
    sources short-circuit: a registered :class:`LocalSource`
    becomes the only candidate for the package.
    """
    _, _, normalized = provider.split_and_normalize(package)
    if normalized in provider.versions_cache:
        return provider.versions_cache[normalized]

    local = provider.local_sources.get(normalized)
    if local is not None:
        result = provider.materialize_local_source(normalized, local)
        provider.versions_cache[normalized] = result
        return result

    vcs = provider.vcs_sources.get(normalized)
    if vcs is not None:
        result = provider.materialize_vcs_source(normalized, vcs)
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

    # Routed through the method (not the module function) so subclass
    # overrides like UniversalProvider's wheel-tag filter still run.
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


def wheel_by_version(
    provider: Provider,
    normalized: str,
    version_list: list[tuple[Version, DistFile]],
) -> dict[Version, DistFile]:
    """Return the cached ``Version -> DistFile`` mapping for ``normalized``.

    Keeps the cheapest metadata source per version (wheel with PEP 658
    metadata, then any wheel, then sdist), so a same-version sdist does
    not displace the wheel.
    """
    cached = provider.wheel_by_version_cache.get(normalized)
    if cached is None:
        cached = {}
        for version, dist in version_list:
            cached[version] = _prefer_metadata_source(cached.get(version), dist)
        provider.wheel_by_version_cache[normalized] = cached
    return cached


def _prefer_metadata_source(current: DistFile | None, candidate: DistFile) -> DistFile:
    """Return the cheaper metadata source of ``current`` and ``candidate``.

    Same ranking as
    :func:`nab_python._provider.metadata_resolver.pick_dist_for_metadata`:
    a wheel with a PEP 658 ``metadata_url``, then any wheel, then an sdist.
    """
    if current is None:
        return candidate
    return (
        current if _metadata_rank(current) <= _metadata_rank(candidate) else candidate
    )


def _metadata_rank(dist: DistFile) -> int:
    """Rank a dist by metadata-fetch cost (lower is cheaper)."""
    if isinstance(dist, WheelFile):
        return 0 if dist.metadata_url is not None else 1
    return 2


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


def prefetch_root_batch(
    provider: Provider,
    normalized: str,
    versions: list[tuple[Version, DistFile]],
    root_range: RangeProtocol[Version],
) -> None:
    """Batch metadata fetch for the top candidates inside ``root_range``."""
    # Imported lazily because provider.py imports this module; pulling
    # Provider in at the top would create an import cycle.
    from ..provider import Provider as _Provider

    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    for version, dist in versions:
        if len(items) >= _Provider.PREFETCH_BATCH:
            break
        if version not in root_range:
            continue
        if (normalized, version) in provider.deps_cache:
            continue
        if isinstance(dist, WheelFile) and dist.metadata_url is not None:
            items.append(
                (normalized, dist.version, dist.metadata_url, dist.metadata_hash)
            )
    if items:
        provider.coordinator.request_metadata_batch(items)


def prefetch_transitive_best(
    provider: Provider,
    normalized: str,
    versions: list[tuple[Version, DistFile]],
) -> None:
    """Fire metadata prefetch for the single best transitive candidate."""
    # Routed through ``provider.pick_best_candidate`` so existing
    # ``patch.object(provider, "pick_best_candidate", ...)`` mocks
    # in the test suite still drive this prefetch path.
    best = provider.pick_best_candidate(normalized, versions)
    if best is None:
        return
    version, dist = best
    cache_key = (normalized, version)
    if (
        cache_key not in provider.deps_cache
        and isinstance(dist, WheelFile)
        and dist.metadata_url is not None
    ):
        provider.coordinator.request_metadata(
            normalized, dist.version, dist.metadata_url, dist.metadata_hash
        )


def pick_best_candidate(
    provider: Provider,
    normalized: str,
    versions: list[tuple[Version, DistFile]],
) -> tuple[Version, DistFile] | None:
    """Pick the version the resolver will most likely try first."""
    if not versions:
        return None
    if normalized in provider.root_requirements:
        version_range = provider.root_requirements[normalized]
        for version, dist in versions:
            if version in version_range:
                return (version, dist)
        return None
    return versions[0]


def filter_distributions(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
) -> list[tuple[Version, DistFile]]:
    """Filter by requires-python, upload time, and sort.

    Sorting: newest version first. When the effective ``dist-policy``
    is PREFER_WHEEL or SDIST_INSTALL, wheels sort before sdists at
    the same version so the metadata picker hits the cheapest source
    first.  ``normalized`` is the canonical package name used to look
    up the per-package / per-index ``uploaded-prior-to`` and
    ``dist-policy`` overrides; the serving index is read from the
    coordinator.

    The dist-policy and upload-time cutoff are version-scoped: a
    per-package override applies only to candidate versions inside its
    requirement's range, so each version's policy is evaluated against
    its own :class:`Version`.

    :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` is *not*
    filtered here: both wheels and sdists stay in ``versions_cache``
    so the resolver can read whichever source is cheapest at the
    chosen version.  The wheels are dropped later, at lock
    construction time, so only the sdist ends up pinned.
    """
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import DistPolicy

    index_name = provider.serving_index(normalized)

    # Fast path: skip the time-filter dispatch entirely when no cutoff applies.
    time_filter_active = (
        provider.uploaded_prior_to is not None or provider.overrides_set_time
    )

    result: list[tuple[Version, DistFile]] = []
    sort_with_wheel_first = False
    for dist in files:
        provider.stats.distributions_seen += 1
        if isinstance(dist, WheelFile):
            provider.stats.wheels_seen += 1
        else:
            provider.stats.sdists_seen += 1

        # Parse the version first: the policy and the cutoff are
        # version-scoped, so an unparseable version is dropped before
        # either is consulted.
        try:
            version = _intern_version(dist.version)
        except InvalidVersion:
            continue

        effective_dist_policy = provider.effective_dist_policy(
            normalized, version, index_name
        )
        if _excluded_by_dist_policy(dist, effective_dist_policy):
            provider.stats.excluded_by_dist_policy += 1
            continue
        if effective_dist_policy in (DistPolicy.PREFER_WHEEL, DistPolicy.SDIST_INSTALL):
            sort_with_wheel_first = True

        if excluded_by_python(provider, dist):
            continue
        if time_filter_active:
            cutoff = provider.effective_uploaded_prior_to(
                normalized, version, index_name
            )
            if excluded_by_time(provider, normalized, dist, cutoff):
                continue

        result.append((version, dist))

    if sort_with_wheel_first:
        result.sort(
            key=lambda pair: (pair[0], isinstance(pair[1], WheelFile)),
            reverse=True,
        )
    else:
        result.sort(key=lambda pair: pair[0], reverse=True)
    return _canonicalize_equal_versions(result)


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
    for version, _ in result:
        chosen = representative.get(version)
        if chosen is None or (len(version.release), str(version)) < (
            len(chosen.release),
            str(chosen),
        ):
            representative[version] = version
    return [(representative[version], dist) for version, dist in result]


def _excluded_by_dist_policy(dist: DistFile, policy: object) -> bool:
    """Return True when ``policy`` rejects ``dist``'s artifact kind.

    ``WHEEL_ONLY`` drops sdists and ``SDIST_ONLY`` drops wheels; the
    other policies admit both kinds here (``SDIST_INSTALL`` keeps wheels
    as a metadata source and prunes them at lock-construction time).
    """
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import DistPolicy

    if policy == DistPolicy.WHEEL_ONLY:
        return not isinstance(dist, WheelFile)
    if policy == DistPolicy.SDIST_ONLY:
        return isinstance(dist, WheelFile)
    return False


def excluded_by_python(provider: Provider, dist: DistFile) -> bool:
    """Return True when ``dist``'s Requires-Python excludes the target Python."""
    requires_python = dist.requires_python
    if not requires_python or not provider.python_version:
        return False
    cached = provider.requires_python_cache.get(requires_python)
    if cached is None:
        try:
            spec = SpecifierSet(requires_python)
            cached = Version(provider.python_version) not in spec
        except InvalidSpecifier:
            # Malformed Requires-Python on the dist: treat as
            # not-excluded, let downstream logic decide.  Our own
            # python_version is validated at Provider construction.
            cached = False
        provider.requires_python_cache[requires_python] = cached
    if cached:
        provider.stats.excluded_by_python += 1
    return cached


def excluded_by_time(
    provider: Provider, normalized: str, dist: DistFile, cutoff: datetime | None
) -> bool:
    """Return True when ``dist`` was uploaded after ``cutoff``.

    ``cutoff`` is the effective upload-time cutoff for ``normalized``,
    already resolved through the overrides and the global
    ``uploaded-prior-to`` (``None`` means no cutoff applies to this package).
    """
    if cutoff is None:
        return False
    if dist.upload_time is None:
        provider.stats.excluded_by_time += 1
        return True
    try:
        upload_dt = datetime.fromisoformat(dist.upload_time.replace("Z", "+00:00"))
    except ValueError:
        provider.stats.excluded_by_time += 1
        return True

    # PEP 700 mandates timezone-aware UTC upload times; refuse to guess.
    if upload_dt.tzinfo is None:
        # Imported lazily: provider.py imports this module.
        from ..provider import InvalidUploadTimeError

        msg = (
            f"{normalized} {dist.version} has a timezone-naive upload time "
            f"{dist.upload_time!r}; the Simple API requires "
            f"timezone-aware (UTC) upload times"
        )
        raise InvalidUploadTimeError(msg)

    excluded = upload_dt >= cutoff
    if excluded:
        provider.stats.excluded_by_time += 1
    return excluded


def _first_wheel_per_version(
    versions_list: list[tuple[Version, DistFile]],
) -> dict[Version, WheelFile]:
    """First wheel-with-PEP-658-metadata per unique version, in list order."""
    out: dict[Version, WheelFile] = {}
    for version, dist in versions_list:
        if isinstance(dist, WheelFile) and dist.metadata_url is not None:
            out.setdefault(version, dist)
    return out


def prefetch_walk_ahead(
    provider: Provider,
    normalized: str,
    deep_count: int,
) -> None:
    """Submit metadata for the next ``deep_count`` wheels of ``normalized``.

    Called when the scan is about to walk past its ``PREFETCH_BATCH``
    window.  Front-loading the rest of the walk lets ``_try_abort_skip``
    and any restart hit cache instead of one RTT per visit.

    Walks ``versions_cache[normalized]`` directly so same-version
    (wheel, sdist) pairs do not lose the wheel the way they would
    via ``wheel_by_version_cache``.  Skips already-cached versions,
    sdist-only versions, and versions whose metadata the coordinator
    already holds.  Fire-and-forget.
    """
    versions_list = provider.versions_cache.get(normalized)
    if not versions_list:
        return
    wheel_for_v = _first_wheel_per_version(versions_list)
    coordinator_index = provider.coordinator.index
    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    seen_versions: set[Version] = set()
    for version, _ in versions_list:
        if version in seen_versions:
            continue
        seen_versions.add(version)
        if len(seen_versions) > deep_count:
            break
        wheel = wheel_for_v.get(version)
        if wheel is None or (normalized, version) in provider.deps_cache:
            continue
        if coordinator_index.has_metadata(normalized, wheel.version):
            continue
        # ``_first_wheel_per_version`` filters out wheels without metadata_url.
        metadata_url = wheel.metadata_url
        assert metadata_url is not None
        items.append((normalized, wheel.version, metadata_url, wheel.metadata_hash))
    if items:
        provider.coordinator.request_metadata_batch(items)


def prefetch_batch(
    provider: Provider,
    package: str,
    versions: list[Version],
    wheel_by_version_map: dict[Version, DistFile],
) -> list[tuple[Version, str, threading.Event]]:
    """Submit metadata fetches for a batch of candidates.

    Uses request_metadata_batch so all requests reach the fetcher
    as a single queue item and are processed concurrently.
    Returns list of (version, ver_str, event) for submitted requests.
    """
    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    version_map: list[tuple[Version, str]] = []
    for v in versions:
        if (package, v) in provider.deps_cache or v not in wheel_by_version_map:
            continue
        wheel = wheel_by_version_map[v]
        if isinstance(wheel, WheelFile) and wheel.metadata_url is not None:
            items.append(
                (package, wheel.version, wheel.metadata_url, wheel.metadata_hash)
            )
            version_map.append((v, wheel.version))

    if not items:
        return []

    raw = provider.coordinator.request_metadata_batch(items)
    submitted = []
    for (_pkg, _ver, ev), (version, ver_str) in zip(raw, version_map, strict=True):
        submitted.append((version, ver_str, ev))
    return submitted


def await_metadata_batch(
    provider: Provider,
    package: str,
    submitted: list[tuple[Version, str, threading.Event]],
) -> None:
    """Wait for all submitted metadata to arrive, then parse into cache."""
    for version, ver_str, event in submitted:
        cache_key = (package, version)
        if cache_key in provider.deps_cache:
            continue
        event.wait()
        text = provider.coordinator.index.get_metadata(package, ver_str)
        if text is None:
            # No PEP 658 text arrived: leave the version un-cached so
            # look-ahead's get_dependencies runs the sdist fallback (or
            # refuses it) rather than pinning it as dependency-free.
            continue
        if provider.coordinator.index.metadata_from_sdist(package, ver_str):
            # The shared slot holds sdist PKG-INFO from an earlier
            # fallback; caching it here would skip the PEP 643 gate
            # that get_dependencies applies on the from_sdist path.
            continue
        try:
            provider.parse_and_cache_metadata(cache_key, text)
        except (ValueError, InvalidVersion, InvalidSpecifier):
            # Malformed metadata: same reason, refuse via get_dependencies
            # (_invalid_metadata) instead of caching empty deps.
            continue


def prefetch_new_deps(provider: Provider, deps: Mapping[str, VersionRange]) -> None:
    """Submit listing and metadata fetches for newly discovered deps.

    For deps whose listings have already arrived (e.g., from a
    prior prefetch), also fire metadata prefetch for their best
    candidate. This deepens the prefetch cascade so metadata is
    ready before the resolver asks for it.

    Local and VCS sources are skipped; they have no PyPI listing
    and the materialise path in ``fetch_versions`` will surface
    them when the resolver asks.
    """
    for dep in deps:
        _, _, normalized = provider.split_and_normalize(dep)
        if normalized in provider.local_sources or normalized in provider.vcs_sources:
            continue
        if normalized not in provider.versions_cache:
            # Listing not cached: request it. When it arrives,
            # prioritize() will notice and fire metadata prefetch.
            provider.coordinator.request_listing(normalized)
        else:
            # Listing cached: fire speculative metadata prefetch.
            speculative_prefetch(
                provider, normalized, provider.versions_cache[normalized]
            )
