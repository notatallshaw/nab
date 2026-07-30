"""Listing fetch, filter, and prefetch coordination for the provider.

Owns ``fetch_versions`` and the speculative-metadata prefetch
chain that feeds the resolver's ``choose_version`` look-ahead with
already-cached metadata where possible.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from nab_index.client import SdistFile, WheelFile

from .._iso8601 import parse_iso_datetime
from .._vcs_admission import UnsupportedVcsError
from .._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from .._vendor.packaging.version import InvalidVersion, Version
from ..metadata import intern_version as _intern_version
from .metadata_resolver import pick_dist, pick_dist_for_metadata

if TYPE_CHECKING:
    import threading
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from nab_resolver.types import RangeProtocol

    from .._vendor.packaging.ranges import VersionRange
    from ..provider import DistFile, DistPolicy, Provider
    from ..tags import TagSet


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

    archive = provider.archive_sources.get(normalized)
    if archive is not None:
        # The download-and-verify guards are gated everywhere; only the
        # post-extraction success tail needs the tar data filter (see sources.py).
        result = provider.materialize_archive_source(normalized, archive)
        provider.versions_cache[normalized] = result  # pragma: no cover
        return result  # pragma: no cover

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


def wheel_by_version(
    provider: Provider,
    normalized: str,
    version_list: list[tuple[Version, DistFile]],
) -> dict[Version, DistFile]:
    """Return the cached ``Version -> DistFile`` mapping for ``normalized``.

    Each version maps to the dist
    :func:`~nab_python._provider.metadata_resolver.pick_dist` picks, so a
    prefetch keyed off this mapping warms the metadata the read asks for.
    """
    cached = provider.wheel_by_version_cache.get(normalized)
    if cached is None:
        grouped: dict[Version, list[DistFile]] = {}
        for version, dist in version_list:
            grouped.setdefault(version, []).append(dist)

        cached = {
            version: pick_dist(dists, provider.wheel_tags, provider.target)
            for version, dists in grouped.items()
        }
        provider.wheel_by_version_cache[normalized] = cached
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
    # Imported lazily because provider.py imports this module; pulling
    # Provider in at the top would create an import cycle.
    from ..provider import Provider as _Provider

    # Reverse out of place: ``versions`` is the shared cached listing.
    ordered = (
        list(reversed(versions)) if provider.wants_lowest(normalized) else versions
    )

    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    for version, dist in ordered:
        if len(items) >= _Provider.PREFETCH_BATCH:
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
    """Fire metadata prefetch for the single best transitive candidate."""
    # Routed through ``provider.pick_best_candidate`` so existing
    # ``patch.object(provider, "pick_best_candidate", ...)`` mocks
    # in the test suite still drive this prefetch path.
    best = provider.pick_best_candidate(normalized, versions)
    if best is None:
        return
    version, _ = best
    if _has_complete_override(provider, normalized, version):
        return

    # Prefetch the artifact the read picks, not the listing's first at that version.
    dist = pick_dist_for_metadata(
        versions, version, provider.wheel_tags, provider.target
    )

    cache_key = (normalized, version)
    if (
        cache_key not in provider.deps_cache
        and isinstance(dist, WheelFile)
        and (url := dist.metadata_url) is not None
    ):
        provider.coordinator.request_metadata(
            normalized, dist.version, url, dist.metadata_hash
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
    version alive at every :class:`~nab_python.provider.BuildPolicy`,
    which is what stops the filter over-refusing a pure-source package;
    the tag check is a wheel's check, as it is in pip.  Look-ahead
    rejects the version later if the sdist's metadata cannot be read
    under the policy in force.

    The dist-policy and upload-time cutoff are version-scoped: a
    per-package override applies only to candidate versions inside its
    requirement's range, so each version's policy is evaluated against
    its own :class:`Version`.

    Under :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` a
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
    :class:`~nab_python.provider.ListingFilterCache`.  The wheel-tag
    pass then runs per target on top of that shared list, so a
    linux-only wheel still stays off the Windows target.
    """
    base = base_distributions(provider, normalized, files)
    result = _apply_wheel_tags(provider, normalized, base)

    if not result and len(base) < len(files):
        # The base pass (dist-policy, requires-python, upload cutoff) dropped a
        # file, so an empty result is not the tag pass alone.
        provider.base_filtered_packages.add(normalized)
    return result


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

    The :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` drop of a
    wheel-only version belongs here rather than in the tag pass: it asks
    whether the version publishes an installable source, and an sdist
    carries no tags, so no target can lose the sdist that keeps the
    version alive.  The answer is the same for every target that shares
    the listing and the policy config, which is what the memo assumes.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import DistPolicy

    index_name = provider.serving_index(normalized)

    # Fast path: skip the time-filter dispatch entirely when no cutoff applies.
    time_filter_active = (
        provider.uploaded_prior_to is not None or provider.overrides_set_time
    )

    result: list[tuple[Version, DistFile]] = []
    sort_with_wheel_first = False
    policy_by_version: dict[Version, DistPolicy] = {}
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
        policy_by_version[version] = effective_dist_policy
        if effective_dist_policy in (DistPolicy.PREFER_WHEEL, DistPolicy.SDIST_INSTALL):
            sort_with_wheel_first = True

        if _excluded_by_python_or_time(
            provider,
            normalized,
            version,
            dist,
            index_name=index_name,
            time_filter_active=time_filter_active,
        ):
            continue

        result.append((version, dist))

    result = _drop_sdist_install_wheel_only(result, policy_by_version)

    if sort_with_wheel_first:
        result.sort(
            key=lambda pair: (pair[0], isinstance(pair[1], WheelFile)),
            reverse=True,
        )
    else:
        result.sort(key=lambda pair: pair[0], reverse=True)
    return _canonicalize_equal_versions(result)


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
    for version, dist in base:
        if excluded_by_wheel_tags(provider, normalized, version, dist, tags):
            tag_rejected_versions.add(version)
            continue
        result.append((version, dist))

    if tag_rejected_versions:
        # A version whose every wheel the target refused, and which ships no
        # sdist, has nothing left to install: it is gone, not merely wheel-less.
        kept = {version for version, _ in result}
        provider.stats.excluded_versions_no_compatible_wheel += len(
            tag_rejected_versions - kept
        )

    return result


def _excluded_by_python_or_time(
    provider: Provider,
    normalized: str,
    version: Version,
    dist: DistFile,
    *,
    index_name: str | None,
    time_filter_active: bool,
) -> bool:
    """Return True when Requires-Python or the upload cutoff rejects ``dist``."""
    if excluded_by_python(provider, normalized, version, dist):
        return True
    if not time_filter_active:
        return False
    cutoff = provider.effective_uploaded_prior_to(normalized, version, index_name)
    return excluded_by_time(provider, normalized, dist, cutoff)


def excluded_by_wheel_tags(
    provider: Provider,
    normalized: str,
    version: Version,
    dist: DistFile,
    tags: TagSet,
) -> bool:
    """Return True when ``dist`` is a wheel the target cannot install.

    An sdist is never excluded here: it carries no tags, and building it
    produces a wheel for whatever machine runs the build.  Tallied per
    package (so a no-candidate package can say why) and per
    ``(package, version)``.
    """
    if not isinstance(dist, WheelFile) or tags.accepts(dist.filename):
        return False
    provider.stats.excluded_by_wheel_tags += 1
    provider.tag_excluded_wheels[normalized] = (
        provider.tag_excluded_wheels.get(normalized, 0) + 1
    )
    key = (normalized, version)
    provider.tag_excluded_wheels_by_version[key] = (
        provider.tag_excluded_wheels_by_version.get(key, 0) + 1
    )
    return True


def _drop_sdist_install_wheel_only(
    result: list[tuple[Version, DistFile]],
    policy_by_version: Mapping[Version, DistPolicy],
) -> list[tuple[Version, DistFile]]:
    """Drop SDIST_INSTALL versions whose surviving artifacts are all wheels.

    Such a version has no source to install, so it must not reach the
    resolver even though its wheels stay as a cheap metadata source.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import DistPolicy

    versions_with_sdist = {v for v, d in result if isinstance(d, SdistFile)}
    drop = {
        v
        for v in policy_by_version
        if policy_by_version[v] is DistPolicy.SDIST_INSTALL
        and v not in versions_with_sdist
    }
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


def _excluded_by_dist_policy(dist: DistFile, policy: object) -> bool:
    """Return True when ``policy`` rejects ``dist``'s artifact kind.

    ``WHEEL_ONLY`` drops sdists and ``SDIST_ONLY`` drops wheels; the
    other policies admit both kinds here (``SDIST_INSTALL`` keeps wheels
    as a metadata source and prunes them at lock-construction time).
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import DistPolicy

    if policy == DistPolicy.WHEEL_ONLY:
        return not isinstance(dist, WheelFile)
    if policy == DistPolicy.SDIST_ONLY:
        return isinstance(dist, WheelFile)
    return False


def excluded_by_python(
    provider: Provider, normalized: str, version: Version, dist: DistFile
) -> bool:
    """Return True when the target Python is excluded for this candidate.

    A per-package ``requires-python`` override substitutes for
    ``dist.requires_python`` and goes through the same cached comparison,
    keyed by the specifier string; the verdict depends only on that string
    and the fixed ``provider.target``.  A minor-interval target admits the
    candidate when the specifier overlaps the whole minor; a whole target
    when its single release satisfies it (see
    :meth:`~nab_python.target.ResolveTarget.admits_requires_python`).
    """
    override_rp = provider.effective_requires_python(normalized, version)
    effective = override_rp if override_rp is not None else dist.requires_python
    if not effective or provider.target is None:
        return False
    cached = provider.requires_python_cache.get(effective)
    if cached is None:
        try:
            spec = SpecifierSet(effective)
            cached = not provider.target.admits_requires_python(spec)
        except InvalidSpecifier:
            # Malformed Requires-Python on the dist: treat as
            # not-excluded, let downstream logic decide.  Our own
            # python_version is validated at Provider construction.
            cached = False
        provider.requires_python_cache[effective] = cached
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
    if dist.local_path is not None:
        # A local file:// artifact has no upload time, so the cutoff cannot apply.
        return False
    if dist.upload_time is None:
        provider.stats.excluded_by_time += 1
        return True
    try:
        upload_dt = parse_iso_datetime(dist.upload_time)
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


def prefetch_walk_ahead(
    provider: Provider,
    normalized: str,
    deep_count: int,
) -> None:
    """Submit metadata for the next ``deep_count`` wheels of ``normalized``.

    Called when the scan is about to walk past its ``PREFETCH_BATCH``
    window.  Front-loading the rest of the walk lets ``_try_abort_skip``
    and any restart hit cache instead of one RTT per visit.

    Takes each version's artifact from :func:`wheel_by_version`, so the
    sidecar it warms is the one the read asks for.  Skips already-cached
    versions, versions whose artifact publishes no sidecar, and versions
    whose metadata the coordinator already holds.  Fire-and-forget.
    """
    versions_list = provider.versions_cache.get(normalized)
    if not versions_list:
        return
    picked = wheel_by_version(provider, normalized, versions_list)
    coordinator_index = provider.coordinator.index
    items: list[tuple[str, str, str, tuple[str, str] | None]] = []
    seen_versions: set[Version] = set()
    for version, _ in versions_list:
        if version in seen_versions:
            continue
        seen_versions.add(version)
        if len(seen_versions) > deep_count:
            break
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


def prefetch_batch(
    provider: Provider,
    package: str,
    versions: list[Version],
    wheel_by_version_map: dict[Version, DistFile],
) -> list[tuple[Version, str, str, threading.Event]]:
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
    submitted: list[tuple[Version, str, str, threading.Event]],
) -> None:
    """Wait for all submitted metadata to arrive, then parse into cache."""
    # Imported lazily: provider.py imports this module.
    from ..provider import ForeignMetadataError, IncompatiblePythonError

    for version, ver_str, metadata_url, event in submitted:
        cache_key = (package, version)
        if cache_key in provider.deps_cache:
            continue
        event.wait()
        integrity_error = provider.coordinator.index.get_metadata_error(
            package, ver_str, metadata_url
        )
        if integrity_error is not None:
            # Leave the version un-cached instead of aborting the batch.
            # The error stays recorded, so get_dependencies re-raises it
            # only if the scan actually selects this version.
            continue
        text, from_sdist = provider.coordinator.index.get_metadata_with_origin(
            package, ver_str, metadata_url
        )
        if text is None:
            # No PEP 658 text arrived: leave the version un-cached so
            # look-ahead's get_dependencies runs the sdist fallback (or
            # refuses it) rather than pinning it as dependency-free.
            continue
        if from_sdist:
            # The sidecar served nothing and the read fell back to sdist
            # PKG-INFO; caching it here would skip the PEP 643 gate that
            # get_dependencies applies on the from_sdist path.
            continue
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
            # direct-URL/VCS dep: leave the version un-cached so
            # get_dependencies re-raises at selection time instead of aborting
            # the speculative prefetch of a candidate the scan may never pick.
            continue


def prefetch_new_deps(provider: Provider, deps: Mapping[str, VersionRange]) -> None:
    """Submit listing and metadata fetches for newly discovered deps.

    For deps whose listings have already arrived (e.g., from a
    prior prefetch), also fire metadata prefetch for their best
    candidate. This deepens the prefetch cascade so metadata is
    ready before the resolver asks for it.

    Local, VCS, and archive sources are skipped; they have no PyPI
    listing and the materialise path in ``fetch_versions`` will
    surface them when the resolver asks.
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
            # Listing not cached: request it. When it arrives,
            # prioritize() will notice and fire metadata prefetch.
            provider.coordinator.request_listing(normalized)
        else:
            # Listing cached: fire speculative metadata prefetch.
            speculative_prefetch(
                provider, normalized, provider.versions_cache[normalized]
            )
