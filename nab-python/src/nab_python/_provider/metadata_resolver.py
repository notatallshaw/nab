"""Metadata fetching, parsing, and dep classification for the provider.

Owns the bulk of ``get_dependencies``'s implementation: fetching
wheel METADATA / sdist PKG-INFO via the coordinator, parsing it
into a :class:`~nab_python.metadata.WheelMetadata`, and classifying
each ``Requires-Dist`` entry into base deps vs per-extra deps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_index.client import SdistFile, WheelFile
from nab_index.local_index import UnsupportedWheelError, read_wheel_metadata

from .._conflict_kind import EMPTY_MEMBERSHIP_SETS
from .._vcs_admission import admit_vcs_url
from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name
from ..metadata import (
    DEPENDENCY_FIELDS,
    WheelMetadata,
    metadata_deps_are_static,
    parse_metadata,
)
from ..requirements_file import (
    InvalidProjectRequirementError,
    _parse_project_requirement,
    _parse_requirements,
    _require_string_list,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .._vendor.packaging.markers import Marker
    from .._vendor.packaging.requirements import Requirement
    from .._vendor.packaging.version import Version
    from ..provider import DistFile, Provider


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
    dist = pick_dist_for_metadata(versions, version)
    metadata_url = dist.metadata_url if isinstance(dist, WheelFile) else None

    text, from_sdist = index.get_metadata_with_origin(normalized, ver_str, metadata_url)
    if text is not None:
        return (text, from_sdist)

    if dist is None:
        msg = f"Version {version} of {package} not found in listing"
        raise MetadataError(msg)

    from_sdist = False
    if isinstance(dist, WheelFile) and dist.metadata_url is not None:
        event = provider.coordinator.request_metadata(
            normalized, ver_str, dist.metadata_url, dist.metadata_hash
        )
        event.wait()
        integrity_error = index.get_metadata_error(
            normalized, ver_str, dist.metadata_url
        )
        if integrity_error is not None:
            raise integrity_error
        metadata_text, from_sdist = index.get_metadata_with_origin(
            normalized, ver_str, dist.metadata_url
        )
    elif isinstance(dist, WheelFile) and dist.local_path is not None:
        try:
            metadata_text = read_wheel_metadata(dist.local_path)
        except UnsupportedWheelError:
            # A contradictory .dist-info is unusable, like an unreadable wheel.
            metadata_text = None
    else:
        metadata_text = None

    if metadata_text is not None:
        return (metadata_text, from_sdist)

    sdist = find_sdist(versions, version)
    if sdist is not None:
        metadata_text, from_sdist = fetch_sdist_metadata(
            provider, normalized, ver_str, sdist
        )
    if metadata_text is not None:
        return (metadata_text, from_sdist)

    msg = (
        f"No metadata for {package}=={version}: "
        f"no PEP 658 metadata and no sdist available"
    )
    raise MetadataError(msg)


def pick_dist_for_metadata(
    versions: Sequence[tuple[Version, DistFile]], version: Version
) -> DistFile | None:
    """Pick the cheapest dist source for ``version``'s metadata.

    Preference order at the same version:

    1. A wheel with a PEP 658 ``metadata_url`` (smallest fetch).
    2. Any wheel (range-fetch / stream still beats an sdist build).
    3. The sdist (PKG-INFO; may require a build if Dynamic).

    The picker is policy-agnostic and applies whatever ``versions``
    holds.  :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` works
    by keeping both kinds of dists in the listing so this preference
    order naturally chooses the wheel when one exists, falling back
    to the sdist when only the sdist is published.
    """
    wheel_with_meta: DistFile | None = None
    wheel_without_meta: DistFile | None = None
    sdist: DistFile | None = None
    for v, d in versions:
        if v != version:
            continue
        if isinstance(d, WheelFile):
            if d.metadata_url is not None:
                if wheel_with_meta is None:
                    wheel_with_meta = d
            elif wheel_without_meta is None:
                wheel_without_meta = d
        elif sdist is None:
            sdist = d
    return wheel_with_meta or wheel_without_meta or sdist


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
        f" pyproject.toml fallback; building requires BuildPolicy.BUILD_REMOTE"
        f" but the effective policy is {effective.value}"
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

    deps = _require_string_list(
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
            _parse_project_requirement(dep_str, source, extra=extra_name)
            for dep_str in _require_string_list(extra_deps, source)
        )
    return provides_extra


def parse_pyproject_deps(deps: list) -> list[Requirement]:
    """Parse a ``project.dependencies`` list, raising on a malformed entry.

    Entries are already validated as strings by :func:`_require_string_list`;
    a string that is not valid PEP 508 raises
    :class:`InvalidProjectRequirementError`, so the whole version is rejected
    rather than resolved with the dependency dropped.
    """
    return _parse_requirements(deps, "[project].dependencies")


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

    if from_sdist and _sdist_deps_need_dynamic(
        metadata,
        trust_unverified=provider.effective_trust_unverified(
            package, version, provider.serving_index(package)
        ),
    ):
        metadata = resolve_dynamic_sdist(provider, cache_key, metadata)

    cache_deps_from_metadata(provider, cache_key, metadata)


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
    # Late import: ``provider`` imports this module at module load.
    from ..provider import _normalize_extra

    package, version = cache_key
    override_deps, override_rp, override_pe = provider.effective_metadata_override(
        package, version
    )
    if override_deps is not None or override_rp is not None or override_pe is not None:
        # Build a fresh record rather than mutate the input: the raw parse is
        # shared across tuples via ``store_parsed_metadata``, so mutating it
        # would leak one tuple's override into another.  Each field falls back
        # to the parsed value when the override leaves it unset.
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

        # An explicit provides-extra wins; else keep the parsed extras, unless
        # the dep list was replaced, which strips their extra-gated lines and
        # leaves them incoherent, so declare none.
        if override_pe is not None:
            provides_extra = list(override_pe)
        elif override_deps is not None:
            provides_extra = []
        else:
            provides_extra = list(metadata.provides_extra)

        metadata = WheelMetadata(
            name=metadata.name,
            version=metadata.version,
            requires_python=requires_python,
            requires_dist=requires_dist,
            provides_extra=provides_extra,
            metadata_version=metadata.metadata_version,
            dynamic=metadata.dynamic,
        )

    # Split the (possibly overridden) requirements into base deps and
    # per-extra deps, deferring any direct-URL deps that aren't yet active.
    provider.metadata_cache[cache_key] = metadata
    provided_extras = {_normalize_extra(e) for e in metadata.provides_extra}
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
        for re in dep_extras:
            base_deps[join_extra(name, re)] = VersionRange.full(admit_arbitrary=False)
    else:
        for extra_name in req_extras:
            edeps = extra_deps_map[extra_name]
            edeps[name] = edeps.get(name, VersionRange.full()) & vi
            for re in dep_extras:
                edeps[join_extra(name, re)] = VersionRange.full(admit_arbitrary=False)
