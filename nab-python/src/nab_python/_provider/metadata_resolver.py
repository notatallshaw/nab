"""Metadata fetching, parsing, and dep classification for the provider.

Owns the bulk of ``get_dependencies``'s implementation: fetching
wheel METADATA / sdist PKG-INFO via the coordinator, parsing it
into a :class:`~nab_python.metadata.WheelMetadata`, and classifying
each ``Requires-Dist`` entry into base deps vs per-extra deps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_index.client import SdistFile, WheelFile

from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.requirements import InvalidRequirement, Requirement
from .._vendor.packaging.utils import canonicalize_name
from ..metadata import DEPENDENCY_FIELDS, parse_metadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .._vendor.packaging.markers import Marker
    from .._vendor.packaging.version import Version
    from ..metadata import WheelMetadata
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
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import MetadataError

    _, _, normalized = provider.split_and_normalize(package)
    ver_str = str(version)

    text = provider.coordinator.index.get_metadata(normalized, ver_str)
    if text is not None:
        # Decide source from listing: a wheel-with-metadata-url at this
        # version means the text was wheel METADATA; otherwise sdist
        # PKG-INFO.  ``_metadata`` and ``_sdist`` write to the same
        # slot, so we can't tell from the index alone.
        from_sdist = not has_wheel_metadata_at(versions, version)
        return (text, from_sdist)

    dist = pick_dist_for_metadata(versions, version)
    if dist is None:
        msg = f"Version {version} of {package} not found in listing"
        raise MetadataError(msg)

    if isinstance(dist, WheelFile) and dist.metadata_url is not None:
        event = provider.coordinator.request_metadata(
            normalized, ver_str, dist.metadata_url
        )
        event.wait()
        metadata_text = provider.coordinator.index.get_metadata(normalized, ver_str)
    else:
        metadata_text = None

    if metadata_text is not None:
        return (metadata_text, False)

    sdist = find_sdist(versions, version)
    if sdist is not None:
        metadata_text = fetch_sdist_metadata(provider, normalized, ver_str, sdist)
    if metadata_text is not None:
        return (metadata_text, True)

    msg = (
        f"No metadata for {package}=={version}: "
        f"no PEP 658 metadata and no sdist available"
    )
    raise MetadataError(msg)


def has_wheel_metadata_at(
    versions: Sequence[tuple[Version, DistFile]], version: Version
) -> bool:
    """Report whether the listing has a wheel with PEP 658 metadata."""
    for v, d in versions:
        if v != version:
            continue
        if isinstance(d, WheelFile) and d.metadata_url is not None:
            return True
    return False


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


def is_dynamic_deps(metadata: WheelMetadata) -> bool:
    """Return True when dependency fields are :pep:`643` Dynamic."""
    return bool(DEPENDENCY_FIELDS & metadata.dynamic)


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
    # Late import: ``pypi`` imports this module at module load.
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
    effective = provider.effective_build_policy(canonical)
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
    is missing, malformed, or itself marks deps dynamic via
    ``[project].dynamic``.
    """
    # Late import keeps the resolver-time path off ``WheelMetadata``
    # construction unless the dynamic-deps pyproject fallback fires.
    from ..metadata import WheelMetadata as _WheelMetadata
    from ..metadata import load_static_project

    text = provider.coordinator.index.get_sdist_pyproject(package, str(version))
    project = load_static_project(text) if text is not None else None
    if project is None:
        return None

    deps_field = project.get("dependencies")
    if deps_field is not None and not isinstance(deps_field, list):
        return None
    optional = project.get("optional-dependencies")
    if optional is not None and not isinstance(optional, dict):
        return None

    requires_dist = list(parse_pyproject_deps(deps_field or []))
    provides_extra = extend_with_extras(requires_dist, optional or {})

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
    """Append extras-gated requirements and return Provides-Extra names."""
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import _add_extra_marker

    provides_extra: list[str] = []
    for extra_name, extra_deps in optional.items():
        if not isinstance(extra_deps, list):
            continue
        provides_extra.append(extra_name)
        for dep_str in extra_deps:
            if not isinstance(dep_str, str):
                continue
            with_marker = _add_extra_marker(dep_str, extra_name)
            try:
                requires_dist.append(Requirement(with_marker))
            except InvalidRequirement:
                continue
    return provides_extra


def parse_pyproject_deps(deps: list) -> list[Requirement]:
    """Parse a ``project.dependencies`` list, dropping malformed entries."""
    out: list[Requirement] = []
    for dep_str in deps:
        if not isinstance(dep_str, str):
            continue
        try:
            out.append(Requirement(dep_str))
        except InvalidRequirement:
            continue
    return out


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
) -> str | None:
    """Block until the coordinator returns sdist PKG-INFO text."""
    event = provider.coordinator.request_sdist(package, version, sdist.url)
    event.wait()
    provider.stats.sdist_pkg_info_fetched += 1
    return provider.coordinator.index.get_metadata(package, version)


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
    """Evaluate ``marker`` against the env without ``extra`` set, cached."""
    result = provider.marker_base_cache.get(marker_id)
    if result is None:
        result = marker.evaluate(provider.environment)
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

    When ``from_sdist`` is set and PKG-INFO marks dependency-related
    fields as :pep:`643` Dynamic, attempts the ``pyproject.toml``
    fallback before raising :class:`UnsupportedSdistError` under
    :class:`BuildPolicy.NEVER`.

    The parsed :class:`WheelMetadata` is shared via the
    :class:`~nab_python.fetch.InMemoryIndex` so that universal-mode
    resolves only run :func:`parse_metadata` once per
    ``(package, version)`` regardless of how many tuples ask for it.
    Per-tuple classification (marker evaluation, extras admission)
    still runs locally in :func:`cache_deps_from_metadata`.  The
    sdist-dynamic-deps reconciliation in
    :func:`resolve_dynamic_sdist` returns a *new* dataclass and is
    therefore cached only by the (per-provider) ``metadata_cache``;
    the coordinator-level entry stays the raw parse so subsequent
    tuples can re-apply their own dynamic-resolution rules without
    inheriting this tuple's pyproject fallback.
    """
    package, version = cache_key
    version_str = str(version)
    metadata = provider.coordinator.index.get_parsed_metadata(package, version_str)
    if metadata is None:
        metadata = parse_metadata(metadata_text)
        provider.coordinator.index.store_parsed_metadata(package, version_str, metadata)
    if from_sdist and is_dynamic_deps(metadata):
        metadata = resolve_dynamic_sdist(provider, cache_key, metadata)
    cache_deps_from_metadata(provider, cache_key, metadata)


def cache_deps_from_metadata(
    provider: Provider,
    cache_key: tuple[str, Version],
    metadata: WheelMetadata,
) -> None:
    """Populate ``deps_cache`` + ``extra_deps_map`` from a parsed metadata.

    Shared by the wheel/sdist path (which calls
    :func:`parse_and_cache_metadata` after parsing METADATA text)
    and the local-source path (which already has a
    :class:`WheelMetadata` from
    :func:`nab_python.build_backend.extract_static_metadata`).
    """
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import _normalize_extra

    provider.metadata_cache[cache_key] = metadata
    provided_extras = {_normalize_extra(e) for e in metadata.provides_extra}
    base_deps: dict[str, VersionRange] = {}
    extra_deps_map: dict[str, dict[str, VersionRange]] = {
        e: {} for e in provided_extras
    }
    for req in metadata.requires_dist:
        req_extras = classify_requirement(provider, req, provided_extras)
        if req_extras is None:
            continue
        add_classified_dep(req, req_extras, base_deps, extra_deps_map)
    provider.deps_cache[cache_key] = base_deps
    provider.extra_deps_map[cache_key] = extra_deps_map


def add_classified_dep(
    req: Requirement,
    req_extras: set[str],
    base_deps: dict[str, VersionRange],
    extra_deps_map: dict[str, dict[str, VersionRange]],
) -> None:
    """Add a classified requirement to the appropriate dep set."""
    # Late import: ``pypi`` imports this module at module load.
    from ..provider import join_extra

    name = canonicalize_name(req.name)
    vi = req.specifier.to_range()
    dep_extras: set[str] = req.extras

    if not req_extras:
        base_deps[name] = vi
        for re in dep_extras:
            base_deps[join_extra(name, re)] = VersionRange.full()
    else:
        for extra_name in req_extras:
            edeps = extra_deps_map[extra_name]
            edeps[name] = vi
            for re in dep_extras:
                edeps[join_extra(name, re)] = VersionRange.full()
