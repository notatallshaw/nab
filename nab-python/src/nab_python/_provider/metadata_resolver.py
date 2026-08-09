"""Metadata fetching, parsing, and dep classification for the provider.

Owns the bulk of ``get_dependencies``'s implementation: fetching
wheel METADATA / sdist PKG-INFO via the coordinator, parsing it
into a :class:`~nab_python.metadata.WheelMetadata`, and classifying
each ``Requires-Dist`` entry into base deps vs per-extra deps.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeGuard
from urllib.parse import urlsplit

from nab_index.client import SdistFile, WheelFile
from nab_index.lazy_wheel import RangeOutcome
from nab_index.local_index import (
    UnreadableLocalIndexError,
    UnsupportedWheelError,
    read_wheel_metadata,
)

from .._conflict_kind import EMPTY_MEMBERSHIP_SETS
from .._vcs_admission import admit_vcs_url
from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name
from .._vendor.packaging.version import InvalidVersion
from ..metadata import (
    DEPENDENCY_FIELDS,
    WheelMetadata,
    intern_version,
    metadata_deps_are_static,
    parse_metadata,
)
from ..requirements_file import (
    InvalidProjectRequirementError,
    parse_project_requirement,
    parse_requirements,
    require_string_list,
)
from ..tags import python_axis_accepts

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .._vendor.packaging.markers import Marker
    from .._vendor.packaging.requirements import Requirement
    from .._vendor.packaging.version import Version
    from ..fetch import InMemoryIndex
    from ..provider import DistFile, Provider
    from ..tags import TagSet
    from ..target import ResolveTarget

TargetDepSignature = tuple[
    dict[str, VersionRange],
    dict[str, dict[str, VersionRange]],
    dict[str | None, set[tuple[str, frozenset[str], str]]],
]

logger = logging.getLogger(__name__)

_OFFLINE_METADATA_MISS = "offline mode skipped a metadata fetch with no cached metadata"


def resolve_metadata(
    provider: Provider,
    versions: list[tuple[Version, DistFile]],
    package: str,
    version: Version,
) -> tuple[str, bool]:
    """Get metadata text and whether it came from an sdist.

    Returns ``(metadata_text, from_sdist)``. ``from_sdist`` is
    ``True`` when the text was extracted from a source-distribution
    ``PKG-INFO`` rather than a wheel ``METADATA``: needed because
    only sdist values are subject to the :pep:`643` Dynamic
    guarantees and may need a ``pyproject.toml`` fallback.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import MetadataError

    _, _, normalized = provider.split_and_normalize(package)
    ver_str = str(version)
    index = provider.coordinator.index

    # Sibling wheels of one version can declare different dependencies, so the
    # read is keyed by the artifact this target would install.  ``versions`` is
    # the target's own tag-filtered listing, so the pick is per-target.
    dist = pick_dist_for_metadata(
        versions, version, provider.wheel_tags, provider.target
    )

    # A wheel keys on its sidecar URL, or on its own URL when it publishes none.
    if isinstance(dist, WheelFile):
        metadata_url = dist.metadata_url or dist.url
    else:
        metadata_url = None

    text, from_sdist = index.get_metadata_with_origin(normalized, ver_str, metadata_url)
    if text is not None:
        return (text, from_sdist)

    if dist is None:
        msg = f"Version {version} of {package} not found in listing"
        raise MetadataError(msg)

    metadata_text, from_sdist, unreadable_wheel = _read_direct_wheel_metadata(
        provider, dist, normalized, ver_str
    )

    # Rung 4: a sidecar-less remote wheel recovers its METADATA over ranged
    # HTTP reads.  Fires only for a bare http/https wheel (no PEP 658 URL, no
    # local path); a malformed-UTF-8 blob or an advertised wheel the index
    # cannot serve is re-raised and fails the resolve, like an unserveable
    # sidecar, while a plain miss (including a host without usable ranges)
    # leaves ``metadata_text`` None and the ladder steps to the sdist rung.
    if metadata_text is None and _is_bare_remote_wheel(dist):
        metadata_text, from_sdist = _read_range_metadata(
            provider, normalized, ver_str, dist.url, dist.hashes
        )

    if metadata_text is not None:
        return (metadata_text, from_sdist)

    sdist = find_sdist(versions, version)
    if sdist is not None:
        metadata_text, from_sdist = fetch_sdist_metadata(
            provider, normalized, ver_str, sdist
        )
    if metadata_text is not None:
        return (metadata_text, from_sdist)

    raise _ladder_failure(
        provider,
        package,
        normalized,
        version,
        sdist=sdist,
        metadata_url=metadata_url,
        unreadable_wheel=unreadable_wheel,
    )


def _ladder_failure(
    provider: Provider,
    package: str,
    normalized: str,
    version: Version,
    *,
    sdist: SdistFile | None,
    metadata_url: str | None,
    unreadable_wheel: UnreadableLocalIndexError | None,
) -> Exception:
    """Return the error for a version no rung of the ladder could answer.

    An unreadable local wheel takes precedence, and is deliberately not a
    :class:`~nab_python.provider.MetadataError`: look-ahead treats those as a
    rejection, so the version would be dropped and an older release pinned
    instead of the resolve failing.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import MetadataError

    if unreadable_wheel is not None:
        return unreadable_wheel

    index = provider.coordinator.index
    ver_str = str(version)

    # Only the rung the ladder gave up on can name the reason: an earlier rung
    # skipped offline says nothing about what this one read.
    last_url = sdist.url if sdist is not None else metadata_url

    if index.is_offline_metadata_miss(normalized, ver_str, last_url):
        reason = _OFFLINE_METADATA_MISS
        _report_offline_skip(index, normalized, package, version)
    elif sdist is not None:
        # A fetched sdist with no PKG-INFO is distinct from no sdist at all.
        reason = "no PEP 658 metadata and the sdist has no readable PKG-INFO"
    elif _index_published_sdist(index, normalized, version):
        reason = (
            "no PEP 658 metadata and the sdist was filtered by"
            " requires-python, dist-policy, or upload-time"
        )
    else:
        reason = "no PEP 658 metadata and no sdist available"

    msg = f"No metadata for {package}=={version}: {reason}"
    return MetadataError(msg)


def _index_published_sdist(
    index: InMemoryIndex, normalized: str, version: Version
) -> bool:
    """Whether the index served an sdist for ``version``.

    The ladder only sees the post-filter listing, where an sdist the filter
    dropped and one that was never published both read as absent.
    """
    for dist in index.get_listing(normalized) or ():
        if not isinstance(dist, SdistFile):
            continue
        try:
            if intern_version(dist.version) == version:
                return True
        except InvalidVersion:
            continue
    return False


def _report_offline_skip(
    index: InMemoryIndex, normalized: str, package: str, version: Version
) -> None:
    """Report a release skipped for want of cached metadata.

    The resolver drops the version and can still succeed on an older one, so
    the drop has to be visible.  A cold cache drops as many releases as the
    listing holds, hence one warning per package and an info line per release.
    """
    if index.claim_offline_metadata_warning(normalized):
        logger.warning("Skipping releases of %s: %s", package, _OFFLINE_METADATA_MISS)
    logger.info("Skipping %s==%s: %s", package, version, _OFFLINE_METADATA_MISS)


def _read_direct_wheel_metadata(
    provider: Provider, dist: DistFile | None, package: str, version: str
) -> tuple[str | None, bool, UnreadableLocalIndexError | None]:
    """Rungs 2 and 3: a PEP 658 sidecar read, then a local wheel read.

    Returns ``(metadata_text, from_sdist, unreadable)``; ``from_sdist`` is
    always ``False`` since both sources are wheel METADATA.  A recorded sidecar
    integrity error is re-raised and fails the resolve; a contradictory local
    ``.dist-info`` reads back as ``None`` and the ladder steps on.  A wheel
    that cannot be opened comes back as ``unreadable`` rather than raising, so
    the version's own sdist still gets a turn; the caller raises it when no
    later rung answers.
    """
    index = provider.coordinator.index
    if isinstance(dist, WheelFile) and (url := dist.metadata_url) is not None:
        event = provider.coordinator.request_metadata(
            package, version, url, dist.metadata_hash
        )
        event.wait()
        integrity_error = index.get_metadata_error(package, version, url)
        if integrity_error is not None:
            raise integrity_error
        text, from_sdist = index.get_metadata_with_origin(package, version, url)
        return text, from_sdist, None
    if isinstance(dist, WheelFile) and dist.local_path is not None:
        try:
            return read_wheel_metadata(dist.local_path), False, None
        except UnsupportedWheelError:
            # A contradictory .dist-info is unusable, like a corrupt archive.
            return None, False, None
        except UnreadableLocalIndexError as exc:
            return None, False, exc
    return None, False, None


def _is_bare_remote_wheel(dist: DistFile | None) -> TypeGuard[WheelFile]:
    """Whether ``dist`` is a sidecar-less remote wheel eligible for rung 4.

    A bare wheel has no PEP 658 sidecar (``metadata_url`` is ``None``) and no
    local path, and is served over ``http``/``https`` so its bytes can be read
    with ranged requests.
    """
    return (
        isinstance(dist, WheelFile)
        and dist.metadata_url is None
        and dist.local_path is None
        and urlsplit(dist.url).scheme in ("http", "https")
    )


def _read_range_metadata(
    provider: Provider,
    package: str,
    version: str,
    wheel_url: str,
    wheel_hashes: tuple[tuple[str, str], ...],
) -> tuple[str | None, bool]:
    """Run rung 4 for one bare wheel and return ``(metadata_text, from_sdist)``.

    Blocks on the coordinator's range read, re-raises a recorded metadata
    error (a malformed-UTF-8 blob, an unserveable wheel URL, a full-body wheel
    failing its published hash) so the resolve fails, and otherwise records the
    outcome counter and reads the wheel's slot.  ``wheel_hashes`` are the
    wheel's published digests, verified against a full-body read.  ``from_sdist``
    is ``False``: recovered wheel METADATA is authoritative.
    """
    event = provider.coordinator.request_range_metadata(
        package, version, wheel_url, wheel_hashes
    )
    event.wait()
    index = provider.coordinator.index
    integrity_error = index.get_metadata_error(package, version, wheel_url)
    if integrity_error is not None:
        raise integrity_error
    _record_range_outcome(provider, package, version, wheel_url)
    return index.get_metadata_with_origin(package, version, wheel_url)


def _record_range_outcome(
    provider: Provider, package: str, version: str, wheel_url: str
) -> None:
    """Bump the :class:`ProviderStats` counter for a range read's outcome.

    The mechanical outcome is discovered in nab-index and recorded on the
    index; the provider owns tier accounting.  A read that recorded no outcome
    (a refused or offline-missed request) leaves every counter untouched.
    """
    outcome = provider.coordinator.index.get_range_outcome(package, version, wheel_url)
    if outcome is None:
        return
    if outcome is RangeOutcome.PARTIAL:
        provider.stats.wheel_metadata_range_fetched += 1
    elif outcome is RangeOutcome.FULL_BODY:
        provider.stats.wheel_metadata_range_full_body += 1
    elif outcome is RangeOutcome.UNSUPPORTED:
        provider.stats.wheel_metadata_range_unsupported += 1
    else:
        provider.stats.wheel_metadata_range_missing += 1


def pick_dist_for_metadata(
    versions: Sequence[tuple[Version, DistFile]],
    version: Version,
    tags: TagSet | None,
    target: ResolveTarget | None = None,
) -> DistFile | None:
    """Pick the dist whose metadata answers for ``version``. See :func:`pick_dist`."""
    dists = [d for v, d in versions if v == version]
    return pick_dist(dists, tags, target) if dists else None


def pick_dist(
    dists: Sequence[DistFile],
    tags: TagSet | None,
    target: ResolveTarget | None = None,
) -> DistFile:
    """Pick the dist of one version whose metadata answers for the target.

    ``dists`` are the artifacts of a single version, and must be non-empty.

    Sibling wheels of one version can declare different dependencies, so
    the wheel the ``tags`` rank most specific wins: :pep:`425` is what an
    installer picks by, so that wheel's METADATA is the one the pin has to
    satisfy.  ``tags`` is ``None`` when there is no tag axis to rank by,
    either because nothing said which machine the resolve is for or because
    a marker overlay moved the target off its tags.  ``target`` still names
    the Python, so wheels built for another interpreter drop out
    (:func:`_python_axis_narrowed`): reading a release's dependencies out of a
    wheel the target could never install is the unsoundness
    :func:`check_sibling_metadata_divergence` guards against, one layer
    earlier.

    Among the wheels that remain the cheapest metadata source wins: a wheel
    with a :pep:`658` ``metadata_url``, then any wheel.  Only a version
    publishing no wheel is read from its sdist, which lets
    :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` keep wheels in the
    listing purely as a metadata source.
    """
    if len(dists) == 1:
        return dists[0]

    wheels = [d for d in dists if isinstance(d, WheelFile)]
    if not wheels:
        return dists[0]

    if tags is None:
        wheels = _python_axis_narrowed(target, wheels)
        installed = None
    else:
        # Sidecars first: ``pick`` keeps input order among wheels it ranks equally.
        installed = tags.pick(sorted(wheels, key=lambda w: not w.has_metadata))
    return installed or next((w for w in wheels if w.has_metadata), wheels[0])


def _sdist_deps_need_dynamic(
    metadata: WheelMetadata, *, trust_unverified: bool
) -> bool:
    """Whether an sdist's PKG-INFO deps must route through the dynamic path.

    By default deps are trusted only when :pep:`643` static
    (Metadata-Version 2.2+, no Dynamic dependency field), so a pre-2.2
    PKG-INFO routes through the dynamic path. With ``trust_unverified``
    set (the opt-out) a pre-2.2 PKG-INFO is trusted, so only an explicit
    Dynamic dependency field forces the dynamic path.
    """
    if trust_unverified:
        return bool(DEPENDENCY_FIELDS & metadata.dynamic)
    return not metadata_deps_are_static(metadata)


def resolve_dynamic_sdist(
    provider: Provider,
    cache_key: tuple[str, Version],
    metadata: WheelMetadata,
) -> WheelMetadata:
    """Reconcile a dynamic-deps sdist.

    First the bundled ``pyproject.toml`` is consulted; when its
    ``[project]`` table statically declares ``dependencies`` and
    ``optional-dependencies``, those replace the dynamic PKG-INFO
    values.  When that fallback yields nothing and the effective
    :class:`~nab_python.provider.BuildPolicy` is
    :attr:`~nab_python.provider.BuildPolicy.BUILD_REMOTE`, the sdist is
    fetched, extracted, and handed to a PEP 517 backend by
    :func:`nab_python._provider.build_remote.build_remote_sdist`.  Any
    other effective policy raises
    :class:`~nab_python.provider.UnsupportedSdistError`; the resolver
    skips the version via
    :func:`nab_python._provider.lookahead.look_ahead_ok` and surfaces the
    accumulated reasons if no candidate ultimately works.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import BuildPolicy, UnsupportedSdistError
    from .build_remote import build_remote_sdist

    package, version = cache_key
    canonical = canonicalize_name(package)
    version_str = str(version)
    index = provider.coordinator.index

    cached = index.get_resolved_sdist_metadata(canonical, version_str)
    if cached is not None:
        return cached

    augmented = augment_from_pyproject(provider, package, version, metadata)
    if augmented is not None:
        index.store_resolved_sdist_metadata(canonical, version_str, augmented)
        return augmented
    effective = provider.effective_build_policy(
        canonical, version, provider.serving_index(canonical)
    )
    if effective is BuildPolicy.BUILD_REMOTE:
        built = build_remote_sdist(provider, package, version)
        index.store_resolved_sdist_metadata(canonical, version_str, built)
        return built
    provider.stats.excluded_by_build_policy += 1
    msg = (
        f"{package}=={version} sdist has dynamic dependencies and no static"
        f" pyproject.toml fallback; building requires build-policy"
        f" '{BuildPolicy.BUILD_REMOTE.value}' but the effective policy is"
        f" '{effective.value}'"
    )
    raise UnsupportedSdistError(msg)


def augment_from_pyproject(
    provider: Provider,
    package: str,
    version: Version,
    metadata: WheelMetadata,
) -> WheelMetadata | None:
    """Replace dynamic deps with statically-declared pyproject deps.

    Returns the augmented metadata, or ``None`` if pyproject.toml
    is missing, unparseable, or itself marks deps dynamic via
    ``[project].dynamic``.

    Raises :class:`InvalidProjectRequirementError` when ``dependencies``
    or ``optional-dependencies`` is present but structurally wrong (not
    an array of strings / not a table), rather than silently dropping the
    declared dependencies.  ``get_dependencies`` catches it and rejects the
    candidate version.  A well-typed entry that is not valid PEP 508 is
    dropped with a warning.
    """
    # Late import keeps the resolver-time path off ``WheelMetadata``
    # construction unless the dynamic-deps pyproject fallback fires.
    from ..metadata import WheelMetadata as _WheelMetadata
    from ..metadata import load_static_project

    text = provider.coordinator.index.get_sdist_pyproject(package, str(version))
    project = load_static_project(text) if text is not None else None
    if project is None:
        return None

    deps = require_string_list(
        project.get("dependencies", []), "[project].dependencies"
    )
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        msg = "[project].optional-dependencies must be a table"
        raise InvalidProjectRequirementError(msg)

    requires_dist = list(parse_pyproject_deps(deps))
    provides_extra = extend_with_extras(requires_dist, optional)

    provider.stats.sdist_pyproject_fallbacks += 1
    return _WheelMetadata(
        name=metadata.name,
        version=metadata.version,
        requires_python=metadata.requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
        metadata_version=metadata.metadata_version,
        dynamic=metadata.dynamic,
    )


def extend_with_extras(requires_dist: list[Requirement], optional: dict) -> list[str]:
    """Append extras-gated requirements and return Provides-Extra names.

    A per-extra value that is not an array of strings, or a per-extra entry
    that is not valid PEP 508, raises :class:`InvalidProjectRequirementError`,
    so the version is rejected rather than resolved with the entry dropped.
    """
    provides_extra: list[str] = []
    for extra_name, extra_deps in optional.items():
        source = f"[project].optional-dependencies extra {extra_name!r}"
        provides_extra.append(extra_name)
        requires_dist.extend(
            parse_project_requirement(dep_str, source, extra=extra_name)
            for dep_str in require_string_list(extra_deps, source)
        )
    return provides_extra


def parse_pyproject_deps(deps: list) -> list[Requirement]:
    """Parse a ``project.dependencies`` list, raising on a malformed entry.

    Entries are already validated as strings by :func:`require_string_list`;
    a string that is not valid PEP 508 raises
    :class:`InvalidProjectRequirementError`, so the whole version is rejected
    rather than resolved with the dependency dropped.
    """
    return parse_requirements(deps, "[project].dependencies")


def find_sdist(
    versions: list[tuple[Version, DistFile]],
    version: Version,
) -> SdistFile | None:
    """Find an sdist for a specific version, or None."""
    for v, d in versions:
        if v == version and isinstance(d, SdistFile):
            return d
    return None


def fetch_sdist_metadata(
    provider: Provider, package: str, version: str, sdist: SdistFile
) -> tuple[str | None, bool]:
    """Block until the coordinator returns sdist PKG-INFO text.

    Returns ``(metadata_text, from_sdist)``: the origin comes back with the
    text, so text that landed in the version-level slot from somewhere other
    than the sdist is not put through the :pep:`643` gate as if it were the
    sdist's own PKG-INFO.

    The archive is verified against ``sdist.hashes`` before its PKG-INFO is
    read. A hash mismatch is recorded as an integrity error and re-raised here.
    """
    event = provider.coordinator.request_sdist(
        package, version, sdist.url, sdist.hashes
    )
    event.wait()
    provider.stats.sdist_pkg_info_fetched += 1
    integrity_error = provider.coordinator.index.get_metadata_error(package, version)
    if integrity_error is not None:
        raise integrity_error
    return provider.coordinator.index.get_metadata_with_origin(package, version)


def classify_requirement(
    provider: Provider,
    req: Requirement,
    provided_extras: set[str],
) -> set[str] | None:
    """Classify a requirement by which extras it belongs to.

    Returns None if the marker doesn't match the environment.
    Returns an empty set if the requirement is a base dep (no extra gating).
    Returns a set of normalized extra names if extra-gated.
    """
    marker = req.marker
    if marker is None:
        return set()
    marker_id = id(marker)
    if marker_matches_base(provider, marker, marker_id):
        return set()
    if "extra" not in marker_text(provider, marker, marker_id):
        return None
    matched_extras = marker_matched_extras(provider, marker, marker_id, provided_extras)
    return matched_extras or None


def marker_matches_base(provider: Provider, marker: Marker, marker_id: int) -> bool:
    """Evaluate ``marker`` against the env without ``extra`` set, cached.

    Every dependency marker the resolve reads passes through here, so this
    is where it is recorded for the lock's ``environments`` declaration.
    Recording on the cache miss keeps each distinct marker once.
    """
    result = provider.marker_base_cache.get(marker_id)
    if result is None:
        provider.consulted_markers.add(marker)
        result = marker.evaluate({**provider.environment, **EMPTY_MEMBERSHIP_SETS})
        provider.marker_base_cache[marker_id] = result
    return result


def marker_text(provider: Provider, marker: Marker, marker_id: int) -> str:
    """Return ``str(marker)``, cached. Walks the AST on big graphs."""
    text = provider.marker_text_cache.get(marker_id)
    if text is None:
        text = str(marker)
        provider.marker_text_cache[marker_id] = text
    return text


def marker_matched_extras(
    provider: Provider,
    marker: Marker,
    marker_id: int,
    provided_extras: set[str],
) -> set[str]:
    """Return the extras for which the marker evaluates to True."""
    per_marker = provider.marker_extra_cache.get(marker_id)
    if per_marker is None:
        per_marker = provider.marker_extra_cache[marker_id] = {}
    env = provider.env_with_extra
    matched: set[str] = set()
    for extra_name in provided_extras:
        result = per_marker.get(extra_name)
        if result is None:
            env["extra"] = extra_name
            result = marker.evaluate(env)
            per_marker[extra_name] = result
        if result:
            matched.add(extra_name)
    return matched


def parse_and_cache_metadata(
    provider: Provider,
    cache_key: tuple[str, Version],
    metadata_text: str,
    *,
    from_sdist: bool = False,
) -> None:
    """Parse metadata text and pre-compute per-extra deps.

    Evaluates markers once for all extras, then caches the base
    deps and a per-extra mapping so that get_extra_dependencies
    can do a dict lookup instead of re-iterating requires_dist.

    When ``from_sdist`` is set and the PKG-INFO deps are not trusted as
    final (not :pep:`643` static, or a Dynamic dependency field under
    the dist-policy ``trust-unverified-deps`` opt-out), attempts the
    ``pyproject.toml`` fallback before raising
    :class:`UnsupportedSdistError` under :class:`BuildPolicy.NEVER`.

    The parsed :class:`WheelMetadata` is shared via the
    :class:`~nab_python.fetch.InMemoryIndex` so that universal-mode
    resolves only run :func:`parse_metadata` once per
    ``(package, version)`` regardless of how many tuples ask for it.  The
    cache is keyed on ``metadata_text`` as well, so a tuple holding another
    artifact's text for that version parses it itself.
    Per-tuple classification (marker evaluation, extras admission)
    still runs locally in :func:`cache_deps_from_metadata`.  The
    sdist-dynamic-deps reconciliation in
    :func:`resolve_dynamic_sdist` returns a new dataclass cached in a
    separate coordinator slot, so it too is reused across tuples while
    the shared raw parse stays unreconciled.
    """
    package, version = cache_key
    version_str = str(version)

    metadata = provider.coordinator.index.get_parsed_metadata(
        package, version_str, metadata_text
    )
    if metadata is None:
        metadata = parse_metadata(metadata_text)
        provider.coordinator.index.store_parsed_metadata(
            package, version_str, metadata, metadata_text
        )

    _reject_foreign_metadata(cache_key, metadata)

    if from_sdist and _sdist_deps_need_dynamic(
        metadata,
        trust_unverified=provider.effective_trust_unverified(
            package, version, provider.serving_index(package)
        ),
    ):
        metadata = resolve_dynamic_sdist(provider, cache_key, metadata)

    _reject_incompatible_python(provider, cache_key, metadata)
    cache_deps_from_metadata(provider, cache_key, metadata)


def _declares_served_release(
    cache_key: tuple[str, Version], metadata: WheelMetadata
) -> bool:
    """Whether ``metadata`` claims the release the artifact was served as."""
    package, version = cache_key
    return canonicalize_name(metadata.name) == package and metadata.version == version


def _reject_foreign_metadata(
    cache_key: tuple[str, Version], metadata: WheelMetadata
) -> None:
    """Reject an index candidate whose METADATA declares a different release.

    Nothing binds a :pep:`658` sidecar or an artifact's own METADATA fields
    to the project and version it was served under.  Checked ahead of the
    sdist reconciliation so a contradicting sdist is never built.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import ForeignMetadataError

    if _declares_served_release(cache_key, metadata):
        return

    package, version = cache_key
    msg = (
        f"{package} {version} metadata declares"
        f" {metadata.name}=={metadata.version}, not {package}=={version}"
    )
    raise ForeignMetadataError(msg)


def _reject_incompatible_python(
    provider: Provider, cache_key: tuple[str, Version], metadata: WheelMetadata
) -> None:
    """Reject an index candidate whose METADATA Requires-Python excludes the target.

    The listing gate (:func:`nab_python._provider.listing.excluded_by_python`)
    reads the optional Simple-API ``requires-python`` hint, so a version whose
    listing omits it reaches here unfiltered.  The wheel's own METADATA (or the
    sdist's PKG-INFO) carries the authoritative field; a per-package override
    still replaces it, matching the listing gate.  Raised before the deps are
    cached so no partial state survives the rejection.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import IncompatiblePythonError

    target = provider.target
    if target is None:
        return

    package, version = cache_key
    override_rp = provider.effective_requires_python(package, version)
    spec = (
        SpecifierSet(override_rp)
        if override_rp is not None
        else metadata.requires_python
    )
    if spec is None or target.admits_requires_python(spec):
        return

    msg = (
        f"{package} {version} requires Python {spec} but the"
        f" {target.label} resolve targets Python {target.python_full_version}"
    )
    raise IncompatiblePythonError(msg)


def effective_metadata(
    provider: Provider,
    cache_key: tuple[str, Version],
    metadata: WheelMetadata,
) -> WheelMetadata:
    """Apply the per-package metadata override, or return ``metadata`` as is.

    A fresh record rather than a mutation: the raw parse is shared across
    tuples via ``store_parsed_metadata``, so mutating would leak one tuple's
    override into another.  A replaced dep list strips extra-gated lines, so an
    unset provides-extra declares none rather than keep now-incoherent extras.
    """
    package, version = cache_key
    override_deps, override_rp, override_pe = provider.effective_metadata_override(
        package, version
    )
    if override_deps is None and override_rp is None and override_pe is None:
        return metadata

    requires_python = (
        SpecifierSet(override_rp)
        if override_rp is not None
        else metadata.requires_python
    )
    requires_dist = (
        list(override_deps)
        if override_deps is not None
        else list(metadata.requires_dist)
    )
    if override_pe is not None:
        provides_extra = list(override_pe)
    elif override_deps is not None:
        provides_extra = []
    else:
        provides_extra = list(metadata.provides_extra)

    return WheelMetadata(
        name=metadata.name,
        version=metadata.version,
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
        metadata_version=metadata.metadata_version,
        dynamic=metadata.dynamic,
    )


def cache_deps_from_metadata(
    provider: Provider,
    cache_key: tuple[str, Version],
    metadata: WheelMetadata,
) -> None:
    """Populate ``deps_cache`` + ``extra_deps_map`` from a parsed metadata.

    Shared by the wheel/sdist path (which calls
    :func:`parse_and_cache_metadata` after parsing METADATA text), the
    local-source path (which already has a :class:`WheelMetadata` from
    :func:`nab_python.build_backend.extract_static_metadata`), and the
    skip-fetch branch of
    :meth:`nab_python.provider.Provider.get_dependencies` (which hands in a
    bare :class:`WheelMetadata` for a complete ``dependencies`` override).
    """
    metadata = effective_metadata(provider, cache_key, metadata)

    # Split the (possibly overridden) requirements into base deps and
    # per-extra deps, deferring any direct-URL deps that aren't yet active.
    package = cache_key[0]
    provider.metadata_cache[cache_key] = metadata
    provided_extras: set[str] = {canonicalize_name(e) for e in metadata.provides_extra}
    base_deps: dict[str, VersionRange] = {}
    extra_deps_map: dict[str, dict[str, VersionRange]] = {
        e: {} for e in provided_extras
    }
    deferred_url_extras: dict[str, list[tuple[Requirement, str]]] = {}
    for req in metadata.requires_dist:
        req_extras = classify_requirement(provider, req, provided_extras)
        if req_extras is None:
            continue
        if req.url is not None:
            if _url_dep_is_active(provider, package, req_extras):
                refuse_url_dep(provider, req, req.url)
            else:
                for extra_name in req_extras:
                    deferred_url_extras.setdefault(extra_name, []).append(
                        (req, req.url)
                    )
            continue
        add_classified_dep(req, req_extras, base_deps, extra_deps_map)
    provider.deps_cache[cache_key] = base_deps
    provider.extra_deps_map[cache_key] = extra_deps_map
    provider.deferred_url_extras[cache_key] = deferred_url_extras


def _url_dep_is_active(provider: Provider, package: str, req_extras: set[str]) -> bool:
    """Whether a direct-URL dep must be refused at base-metadata time.

    A base dep (no extra gating) is always active. An extra-gated dep is active
    only when the user requested one of its extras at the root; otherwise its
    refusal defers to the per-extra path.
    """
    if not req_extras:
        return True
    return any((package, extra) in provider.root_extras for extra in req_extras)


def refuse_url_dep(provider: Provider, req: Requirement, url: str) -> None:
    """Refuse a direct-URL requirement, or raise ``NotImplementedError``."""
    admit_vcs_url(url, provider.vcs_config)
    msg = (
        f"VCS dependency admitted by policy but resolver path is not"
        f" implemented: {req.name} @ {url}"
    )
    raise NotImplementedError(msg)


def _classify_requirement_uncached(
    provider: Provider,
    req: Requirement,
    provided_extras: set[str],
) -> set[str] | None:
    """Classify like :func:`classify_requirement`, recording nothing.

    These markers come from wheels :func:`target_dep_signature` parses but
    never keeps in ``metadata_cache``.  The id-keyed marker caches would be
    unsound here: a collected marker's ``id`` can alias a later live marker.
    ``consulted_markers`` would leak a never-installed sibling's clauses into
    the lock's ``environments``.  So each marker is evaluated directly.
    """
    marker = req.marker
    if marker is None:
        return set()
    if marker.evaluate({**provider.environment, **EMPTY_MEMBERSHIP_SETS}):
        return set()
    if "extra" not in str(marker):
        return None
    env = dict(provider.env_with_extra)
    matched: set[str] = set()
    for extra_name in provided_extras:
        env["extra"] = extra_name
        if marker.evaluate(env):
            matched.add(extra_name)
    return matched or None


def target_dep_signature(
    provider: Provider,
    cache_key: tuple[str, Version],
    metadata: WheelMetadata,
) -> TargetDepSignature:
    """Project ``metadata`` to a target-effective dependency signature.

    Returns ``(base_deps, extra_deps_map, url_buckets)``, compared with ``!=``
    to tell two wheels of one version apart by the dependencies they impose on
    this target rather than by raw text.  Each requirement is classified the way
    the resolver classifies deps, so a marker both wheels evaluate the same
    folds away, and ranges go through :func:`add_classified_dep`, so ordering,
    whitespace, and specifier spelling normalize equal.  It records nothing into
    the marker caches or ``consulted_markers``: see
    :func:`_classify_requirement_uncached`.

    ``cache_key`` is the release the wheel was served as, not whatever the
    wheel declares for itself.  The per-package override is applied first, so a
    ``provides-extra`` override is compared in the same view the resolver pins
    from.  A complete ``dependencies`` override takes the skip-fetch path in
    :meth:`nab_python.provider.Provider.get_dependencies` and never reaches
    here.  ``Requires-Python`` is left out: it gates admission, not the
    dependency edges a lock records.

    Unlike :func:`cache_deps_from_metadata` this never raises: a direct-URL dep
    is bucketed instead of routed through :func:`refuse_url_dep`, since the pick
    already refused its own URL deps, so a sibling's URL dep is a divergence to
    report.
    """
    metadata = effective_metadata(provider, cache_key, metadata)
    provided_extras: set[str] = {canonicalize_name(e) for e in metadata.provides_extra}
    base_deps: dict[str, VersionRange] = {}
    extra_deps_map: dict[str, dict[str, VersionRange]] = {
        e: {} for e in provided_extras
    }
    url_buckets: dict[str | None, set[tuple[str, frozenset[str], str]]] = {}
    for req in metadata.requires_dist:
        req_extras = _classify_requirement_uncached(provider, req, provided_extras)
        if req_extras is None:
            continue
        if req.url is not None:
            entry = (canonicalize_name(req.name), frozenset(req.extras), req.url)
            for key in req_extras or {None}:
                url_buckets.setdefault(key, set()).add(entry)
            continue
        add_classified_dep(req, req_extras, base_deps, extra_deps_map)
    return (base_deps, extra_deps_map, url_buckets)


def check_sibling_metadata_divergence(
    provider: Provider,
    versions: Sequence[tuple[Version, DistFile]],
    package: str,
    version: Version,
) -> None:
    """Crash when a resident tie sibling's target deps diverge from the pick's.

    nab reads one version's dependencies from the wheel the target's tags rank
    most preferred and treats it as authoritative.  Two wheels the target's own
    rules cannot rank against each other (a tie) can still declare different
    target-effective dependencies, so pinning from one silently disagrees with
    an install of the other.

    Only siblings already resident in the shared index are compared; this never
    fetches.  On the first tie sibling whose projection differs from the pick's,
    a :class:`~nab_python.provider.SiblingMetadataDivergenceError` is raised.  It
    is deliberately not a :class:`~nab_python.provider.MetadataError`, which
    :meth:`~nab_python.provider.Provider._look_ahead_ok` would turn into a
    dropped candidate: dropping would silently remove a version an installer can
    legitimately install.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import SiblingMetadataDivergenceError

    tags = provider.wheel_tags
    pick = pick_dist_for_metadata(versions, version, tags, provider.target)
    if not isinstance(pick, WheelFile):
        return

    _, _, normalized = provider.split_and_normalize(package)
    ver_str = str(version)
    index = provider.coordinator.index

    pick_text = _resident_wheel_text(index, normalized, ver_str, pick)
    if pick_text is None:
        return

    wheels = [d for v, d in versions if v == version and isinstance(d, WheelFile)]
    if tags is None:
        # The pick came off the same narrowing, so it is one of these.
        wheels = _python_axis_narrowed(provider.target, wheels)
    if len(wheels) <= 1:
        return

    pick_key = None if tags is None else tags.wheel_rank(pick.filename)
    sig_key = (normalized, version)
    pick_sig: TargetDepSignature | None = None
    for sibling in wheels:
        if sibling is pick or not _wheels_tie(tags, pick_key, sibling.filename):
            continue
        sibling_metadata = _tie_sibling_metadata(
            provider, index, normalized, version, sibling
        )
        if sibling_metadata is None:
            continue
        if pick_sig is None:
            pick_sig = target_dep_signature(
                provider, sig_key, parse_metadata(pick_text)
            )
        sibling_sig = target_dep_signature(provider, sig_key, sibling_metadata)
        if sibling_sig == pick_sig:
            continue
        labels = _divergent_dep_labels(pick_sig, sibling_sig)
        msg = (
            f"{package} {version} has tie-ranked wheels {pick.filename} and"
            f" {sibling.filename} that declare different dependencies for this"
            f" target ({', '.join(labels)}). Pin the dependencies with a"
            f" per-package dependencies override to resolve this version."
        )
        raise SiblingMetadataDivergenceError(msg)


def _tie_sibling_metadata(
    provider: Provider,
    index: InMemoryIndex,
    package: str,
    version: Version,
    sibling: WheelFile,
) -> WheelMetadata | None:
    """Parse a resident tie sibling's own METADATA, or None to skip it.

    Skipped when the text is not resident, does not parse, declares a
    different release, or its effective Requires-Python excludes the target.
    Tags rank by :pep:`425` and say nothing about the Python floor, so a tie
    sibling can still declare a Requires-Python an installer would reject for
    this target, and a sibling declaring another release is no candidate for
    this one at all; comparing either would false-crash a version an installer
    resolves fine.  A per-package override wins over the parsed floor.
    """
    text = _resident_wheel_text(index, package, str(version), sibling)
    if text is None:
        return None
    try:
        metadata = parse_metadata(text)
    except ValueError:
        return None
    if not _declares_served_release((package, version), metadata):
        return None
    target = provider.target
    override_rp = provider.effective_requires_python(package, version)
    spec = (
        SpecifierSet(override_rp)
        if override_rp is not None
        else metadata.requires_python
    )
    if (
        target is not None
        and spec is not None
        and not target.admits_requires_python(spec)
    ):
        return None
    return metadata


def _resident_wheel_text(
    index: InMemoryIndex, package: str, version: str, wheel: WheelFile
) -> str | None:
    """Return a wheel's own resident METADATA text, or None if not in hand.

    Reads only the slot the wheel's text would occupy: a bare remote wheel's
    own URL (rung 4), else its sidecar URL.  A local wheel keeps no text in the
    index and reads back None.  Sdist-origin text never stands in for a wheel.
    """
    if _is_bare_remote_wheel(wheel):
        url = wheel.url
    elif wheel.metadata_url is not None:
        url = wheel.metadata_url
    else:
        return None
    text, from_sdist = index.get_metadata_with_origin(package, version, url)
    return None if from_sdist else text


def _wheels_tie(
    tags: TagSet | None,
    pick_key: tuple[int, tuple[int, str]] | None,
    sibling_filename: str,
) -> bool:
    """Whether a sibling wheel ties the pick under the target's install rules.

    With no tag axis nothing ranks the wheels that :func:`_python_axis_narrowed`
    left standing, so each of them is a real ambiguity.  Otherwise a sibling
    ties only when its :meth:`~nab_python.tags.TagSet.wheel_rank` key equals
    the pick's and is not None; a sibling the pick ranks strictly below is
    never installed and is exempt.
    """
    if tags is None:
        return True
    sibling_key = tags.wheel_rank(sibling_filename)
    return sibling_key is not None and sibling_key == pick_key


def _python_axis_narrowed(
    target: ResolveTarget | None, wheels: list[WheelFile]
) -> list[WheelFile]:
    """Keep the wheels the target's Python axis admits, all of them if none.

    A marker overlay cannot rebuild the platform tags, but the target still
    names a Python: whichever ``python_version`` and ``implementation_name``
    the overlay leaves in force.  A wheel built for another interpreter is a
    choice no installer on that Python makes, so it neither answers for the
    version's dependencies nor ties the wheel that does.  The
    ``Requires-Python`` guard in :func:`_tie_sibling_metadata` cannot do this
    work: a release publishing one wheel per interpreter declares one
    ``Requires-Python`` spanning them all, so it admits every sibling.

    Admitting nothing decides nothing, so every wheel stands: the pick keeps
    its listing-order fallback, and the tie set keeps the pick's siblings
    rather than disarming the divergence check over a pick this target cannot
    install either.  Without a target nothing has said which Python the
    resolve is for, so again every wheel stands.
    """
    if target is None:
        return wheels
    admitted = [
        wheel
        for wheel in wheels
        if python_axis_accepts(
            target.python_version, target.implementation, wheel.filename
        )
    ]
    return admitted or wheels


def _divergent_dep_labels(
    pick_sig: TargetDepSignature, sibling_sig: TargetDepSignature
) -> list[str]:
    """Return sorted labels for the signature entries that differ."""
    pick_flat = _flatten_signature(pick_sig)
    sibling_flat = _flatten_signature(sibling_sig)
    return sorted(
        label
        for label in pick_flat.keys() | sibling_flat.keys()
        if pick_flat.get(label) != sibling_flat.get(label)
    )


def _flatten_signature(signature: TargetDepSignature) -> dict[str, object]:
    """Flatten a target-dep signature to labelled values for a diff.

    Base deps, per-extra dep maps, and URL buckets each become one entry.
    """
    base_deps, extra_deps_map, url_buckets = signature
    flat: dict[str, object] = {}
    for name, rng in base_deps.items():
        flat[f"base:{name}"] = rng
    for extra, deps in extra_deps_map.items():
        flat[f"extra:{extra}"] = deps
    for key, entries in url_buckets.items():
        flat[f"url:{key}"] = entries
    return flat


def add_classified_dep(
    req: Requirement,
    req_extras: set[str],
    base_deps: dict[str, VersionRange],
    extra_deps_map: dict[str, dict[str, VersionRange]],
) -> None:
    """Add a classified requirement to the appropriate dep set.

    A name appearing on several ``Requires-Dist`` lines is intersected
    into one range.
    """
    # Late import: ``provider`` imports this module at module load.
    from ..provider import join_extra

    name = canonicalize_name(req.name)
    # A bare dependency enters the solver without arbitrary-string admission;
    # the accumulator identities stay arbitrary-admitting for === literals.
    vi = (
        req.specifier.to_range()
        if req.specifier
        else VersionRange.full(admit_arbitrary=False)
    )
    dep_extras: set[str] = req.extras

    if not req_extras:
        base_deps[name] = base_deps.get(name, VersionRange.full()) & vi
        for re in sorted(dep_extras):
            base_deps[join_extra(name, re)] = VersionRange.full(admit_arbitrary=False)
    else:
        for extra_name in req_extras:
            edeps = extra_deps_map[extra_name]
            edeps[name] = edeps.get(name, VersionRange.full()) & vi
            for re in sorted(dep_extras):
                edeps[join_extra(name, re)] = VersionRange.full(admit_arbitrary=False)
