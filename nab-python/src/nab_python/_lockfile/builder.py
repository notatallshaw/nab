"""Build a :class:`LockInput` from a finished resolve.

The provider's caches still hold the listings the resolver consumed
when this runs, so artefact hashes and per-file Requires-Python can
be read directly without a second fetch.  This module also owns the
``read_lockfile_anchor`` helper used by ``nab lock`` to keep
``P<n>D`` durations stable across re-locks.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, overload
from urllib.parse import quote, urlsplit, urlunsplit

import tomli

from nab_index.client import SdistFile, WheelFile

from .._toml import tool_nab_section
from .._vendor.packaging.pylock import Pylock, PylockValidationError
from .._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from nab_index.multi_index import IndexConfig

    from .._vendor.packaging.version import Version
    from ..lockfile import (
        IndexPin,
        LockInput,
        PinShape,
        SdistArtifact,
        VcsPin,
        WheelArtifact,
    )
    from ..provider import DistPolicy, LocalSource, VcsSource


__all__ = [
    "MissingHashError",
    "MissingSdistError",
    "MissingVcsCommitError",
    "build_lock_input_from_provider",
    "read_lockfile_anchor",
    "read_lockfile_packages",
    "require_artifact_hashes",
]


class _LockInputIndex(Protocol):
    """Protocol for the InMemoryIndex slice the builder reads."""

    def get_listing_index(self, package: str) -> str | None:
        """Return the configured index name that served ``package``."""
        ...


class _LockInputCoordinator(Protocol):
    """Protocol for the FetchCoordinator slice the builder reads."""

    @property
    def index(self) -> _LockInputIndex:
        """The underlying index used to look up serving-index labels."""
        ...


class LockInputProvider(Protocol):
    """Structural protocol for the provider slice the builder reads.

    Mirrors the public surface :class:`~nab_python.provider.Provider`
    exposes that :func:`build_lock_input_from_provider` consumes; tests
    may supply a stub without inheriting the full Provider class.
    """

    deps_cache: Mapping[tuple[str, Version], Mapping[str, object]]
    """Direct dependencies per ``(canonical name, version)``."""

    extra_deps_map: Mapping[tuple[str, Version], Mapping[str, Mapping[str, object]]]
    """Per-extra dependencies per ``(canonical name, version)``."""

    @property
    def coordinator(self) -> _LockInputCoordinator:
        """Coordinator used to look up the index that served a listing."""
        ...

    def local_source_for(self, canonical_name: str, /) -> LocalSource | None:
        """Return the configured LocalSource for ``canonical_name`` or None."""
        ...

    def vcs_source_for(self, canonical_name: str, /) -> VcsSource | None:
        """Return the configured VcsSource for ``canonical_name`` or None."""
        ...

    def vcs_pin_for(self, canonical_name: str, /) -> str | None:
        """Return the resolved 40-char SHA captured during materialisation."""
        ...

    def dist_files_for(
        self, canonical_name: str, version: Version, /
    ) -> list[WheelFile | SdistFile]:
        """Return the listing slice that matches ``(canonical_name, version)``."""
        ...

    def effective_dist_policy(
        self, canonical_name: str, version: Version, index_name: str | None = None, /
    ) -> DistPolicy:
        """Return the effective :class:`DistPolicy` for ``canonical_name==version``."""
        ...

    def effective_requires_python(
        self, canonical_name: str, version: Version, /
    ) -> str | None:
        """Return the ``requires-python`` override for ``canonical_name==version``."""
        ...


class MissingHashError(ValueError):
    """A distribution chosen by the resolver has no usable hash.

    PEP 751 requires at least one hash per artefact.  When an index
    serves a wheel or sdist without a ``hashes`` map (rare on PyPI,
    common on file:// indexes), the lock writer cannot emit a
    spec-compliant entry.  Surface the failure with the offending
    package and filename so the user can either add a hash to their
    local index or exclude the package.
    """


class MissingSdistError(ValueError):
    """A ``sdist-install`` package's pinned version has no sdist.

    Under :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` the
    resolver may read a wheel's metadata but the lock must pin only the
    sdist.  When the pinned version publishes wheels but no sdist, the
    wheels are dropped and nothing is left to pin.  Surface the package
    and version so the user can pick a version with an sdist or relax
    the policy, rather than emitting an empty package the spec rejects.
    """


class MissingVcsCommitError(ValueError):
    """A VCS source reached the lock writer without a resolved commit SHA.

    PEP 751 requires ``packages.vcs.commit-id`` to be an immutable
    identifier.  nab records the post-clone SHA on the provider during
    materialisation, before any version can be pinned, so a missing SHA
    here means a VCS source was pinned without being cloned.  Surface it
    loudly rather than emit a branch name or empty string as the commit
    id, which would silently produce a non-reproducible lock.
    """


def read_lockfile_anchor(path: Path) -> datetime | None:
    """Return the ``[tool.nab].created-at`` timestamp from ``path`` if any.

    Used by ``nab lock`` to keep ``P<n>D`` durations stable across
    re-locks: the anchor used for the previous resolve is read back
    and reused unless the user passes ``--upgrade``.

    Returns ``None`` when ``path`` does not exist, is not valid TOML,
    is not a PEP 751-shaped pylock, or is missing the ``[tool.nab]``
    block.  Naive timestamps (no timezone offset) are coerced to UTC
    for symmetry with the writer; this is informational provenance, so
    a missing offset is recoverable rather than fatal.
    """
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            data = tomli.load(f)
    except (OSError, tomli.TOMLDecodeError):
        return None
    nab = tool_nab_section(data)
    raw = nab.get("created-at") if isinstance(nab, dict) else None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        # Python 3.10's fromisoformat rejects a trailing 'Z'; 3.11+ accept it.
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def read_lockfile_packages(path: Path) -> dict[str, Version] | None:
    """Return the ``name -> version`` map from a prior pylock at ``path``.

    Used by ``nab lock`` to diff a re-lock against the previous result.
    Packages without a recorded version (direct-reference entries that
    omit it) are skipped.

    Returns ``None`` when ``path`` does not exist, is not valid TOML,
    or is not a spec-compliant PEP 751 lockfile; the caller falls back
    to a no-diff summary line.
    """
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            data = tomli.load(f)
        pylock = Pylock.from_dict(data)
    except (OSError, tomli.TOMLDecodeError, PylockValidationError):
        return None
    return {
        str(pkg.name): pkg.version for pkg in pylock.packages if pkg.version is not None
    }


def _strip_userinfo(url: str) -> str:
    """Return ``url`` with credential userinfo removed.

    Lockfiles are committed to version control, so an index or VCS URL
    carrying an embedded ``user:password`` must not be written verbatim.
    An SSH login such as ``git@`` is the protocol login, not a secret,
    and is required to clone, so it is kept (only an embedded password is
    dropped); host case and port are preserved.  A no-op for URLs without
    userinfo.
    """
    parts = urlsplit(url)
    userinfo, sep, host = parts.netloc.rpartition("@")
    if not sep:
        return url
    if parts.scheme.endswith("ssh"):
        login = userinfo.split(":", 1)[0]
        host = f"{login}@{host}"
    return urlunsplit(parts._replace(netloc=host))


def build_lock_input_from_provider(  # noqa: PLR0913 - each flag maps to a distinct lockfile field
    provider: LockInputProvider,
    pins: Mapping[str, Version],
    *,
    requires_python: str | None = None,
    extras: Sequence[str] = (),
    dependency_groups: Sequence[str] = (),
    default_groups: Sequence[str] = (),
    created_by: str = "nab",
    indexes: Sequence[IndexConfig] = (),
    resolved_keys: Iterable[str] = (),
) -> LockInput:
    """Build a :class:`LockInput` from a finished resolve.

    ``provider`` is the :class:`Provider` that drove the resolve;
    its caches still hold the listings the resolver consumed.  ``pins``
    is the canonical-name -> :class:`Version` mapping returned by the
    resolver after extras keys have been stripped.

    ``dependency_groups`` lists the PEP 735 groups whose requirements
    were folded into this resolve; ``default_groups`` is the subset
    that a default install (no ``--group`` flag) should apply.

    ``resolved_keys`` is the full set of resolver result keys, including
    ``name[extra]`` proxies; it is read to find which extras activated
    so their edges join the forward dependency graph.

    All wheels and the sdist for each pinned version are recorded so
    the lockfile is portable across architectures of the same Python.
    """
    from ..lockfile import LocalPin, LockInput

    lock_pins: dict[str, PinShape] = {}
    for raw_name, version in pins.items():
        canonical = canonicalize_name(raw_name)
        local_source = provider.local_source_for(canonical)
        if local_source is not None:
            lock_pins[canonical] = LocalPin(
                name=canonical,
                version=str(version),
                path=str(Path(local_source.path).resolve()),
                editable=local_source.editable,
                subdirectory=local_source.subdirectory,
            )
            continue
        vcs_source = provider.vcs_source_for(canonical)
        if vcs_source is not None:
            lock_pins[canonical] = _vcs_pin_from_source(
                canonical,
                version,
                vcs_source,
                resolved_sha=provider.vcs_pin_for(canonical),
            )
            continue
        lock_pins[canonical] = _index_pin_from_listing(
            provider, canonical, version, indexes
        )
    return LockInput(
        pins=lock_pins,
        requires_python=requires_python,
        created_by=created_by,
        extras=tuple(extras),
        dependency_groups=tuple(dependency_groups),
        default_groups=tuple(default_groups),
        dependencies=_forward_dependency_graph(provider, pins, resolved_keys),
    )


def _forward_dependency_graph(
    provider: LockInputProvider,
    pins: Mapping[str, Version],
    resolved_keys: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Build the forward dependency graph among the locked packages.

    Each pinned package maps to the canonical names of its direct
    dependencies that are themselves pinned.  Base dependencies come
    from ``deps_cache``; an activated extra (a ``name[extra]`` key in
    ``resolved_keys``) folds that extra's dependencies in too.  Names
    not in ``pins`` are dropped so every edge points at a real
    ``[[packages]]`` entry.
    """
    from ..provider import split_extra

    activated_extras: defaultdict[str, set[str]] = defaultdict(set)
    for key in resolved_keys:
        base, extra = split_extra(key)
        if extra is not None:
            activated_extras[canonicalize_name(base)].add(extra)

    pinned = {canonicalize_name(name) for name in pins}
    graph: dict[str, tuple[str, ...]] = {}
    for raw_name, version in pins.items():
        canonical = canonicalize_name(raw_name)
        cache_key = (canonical, version)
        dep_names = {
            canonicalize_name(split_extra(dep)[0])
            for dep in provider.deps_cache.get(cache_key, {})
        }
        extra_map = provider.extra_deps_map.get(cache_key, {})
        for extra in activated_extras.get(canonical, ()):
            dep_names.update(
                canonicalize_name(split_extra(dep)[0])
                for dep in extra_map.get(extra, {})
            )
        dep_names &= pinned
        # An umbrella extra (pkg[all] pulling pkg[graphviz]) can name its own
        # package; drop it so pkg is never an edge to itself.
        dep_names.discard(canonical)
        if dep_names:
            graph[canonical] = tuple(sorted(dep_names))
    return graph


def _index_pin_from_listing(
    provider: LockInputProvider,
    canonical: str,
    version: Version,
    indexes: Sequence[IndexConfig],
) -> IndexPin:
    """Construct an :class:`IndexPin` for an index-served package.

    The recorded ``index`` is the URL of the configured index that
    served the package's listing during the resolve, looked up from
    the coordinator's :class:`InMemoryIndex` (which records the
    serving index by name) and resolved against ``indexes`` for the
    URL.  A pinned package's serving index is always recorded and is
    one of ``indexes``, so the URL is always known.

    Under :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` the
    package's wheels stayed in ``versions_cache`` as a possible
    metadata source for the resolver; only the sdist is emitted
    into the lock so installers download and build that archive.

    A ``requires-python`` metadata override takes precedence over the
    Simple-API value so the pin records the specifier the resolver
    actually admitted against; a conforming :pep:`751` installer would
    otherwise reject a widened pin whose lock still carried the narrow
    artefact value.
    """
    from ..fetch import DEFAULT_INDEX_URL
    from ..lockfile import IndexPin, SdistArtifact, WheelArtifact
    from ..provider import DistPolicy

    files = list(provider.dist_files_for(canonical, version))
    serving = provider.coordinator.index.get_listing_index(canonical)
    if (
        provider.effective_dist_policy(canonical, version, serving)
        is DistPolicy.SDIST_INSTALL
    ):
        files = [f for f in files if not isinstance(f, WheelFile)]
        if not any(isinstance(f, SdistFile) for f in files):
            msg = (
                f"{canonical}=={version} has no sdist, but its dist-policy is "
                f"'sdist-install'; pick a version that publishes an sdist or "
                f"change dist-policy for {canonical}"
            )
            raise MissingSdistError(msg)

    wheels = tuple(
        _build_artifact(f, WheelArtifact) for f in files if isinstance(f, WheelFile)
    )
    sdist_file = next((f for f in files if isinstance(f, SdistFile)), None)
    sdist = (
        _build_artifact(sdist_file, SdistArtifact) if sdist_file is not None else None
    )

    override_rp = provider.effective_requires_python(canonical, version)
    requires_python = (
        override_rp if override_rp is not None else _common_requires_python(files)
    )

    serving_name = provider.coordinator.index.get_listing_index(canonical)
    by_name = {ix.name: ix.url for ix in indexes}
    index_url = by_name.get(serving_name) if serving_name is not None else None
    if index_url is None:
        if by_name:
            # Can't happen for a real pin (the serving index is always
            # recorded and configured); raise, don't guess.
            msg = (
                f"{canonical}: recorded serving index {serving_name!r} is "
                "not one of the configured indexes"
            )
            raise AssertionError(msg)
        # No indexes configured (unit tests only); use the default root.
        index_url = DEFAULT_INDEX_URL
    return IndexPin(
        name=canonical,
        version=str(version),
        index=_strip_userinfo(index_url),
        sdist=sdist,
        wheels=wheels,
        requires_python=requires_python,
    )


@overload
def _build_artifact(
    source: WheelFile | SdistFile,
    cls: type[WheelArtifact],
) -> WheelArtifact: ...
@overload
def _build_artifact(
    source: WheelFile | SdistFile,
    cls: type[SdistArtifact],
) -> SdistArtifact: ...
def _build_artifact(
    source: WheelFile | SdistFile,
    cls: type[WheelArtifact | SdistArtifact],
) -> WheelArtifact | SdistArtifact:
    hashes = _filter_acceptable_hashes(source.hashes)
    return cls(
        filename=source.filename,
        url=_strip_userinfo(source.url),
        hashes=hashes,
        size=source.size,
        upload_time=_parse_upload_time(source.upload_time),
        local_path=source.local_path,
    )


def _parse_upload_time(raw: str | None) -> datetime | None:
    """Parse an index ``upload-time`` string to a UTC ``datetime``.

    Accepts the RFC 3339 form the Simple/JSON API serves (``Z`` or an
    explicit offset) and normalizes it to UTC (PEP 751 requires UTC for
    the emitted field). Returns ``None`` when the field is absent,
    unparseable, or has no timezone; the timestamp is informational, so
    a bad value is dropped rather than fatal.
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _filter_acceptable_hashes(
    hashes: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Return the subset of ``hashes`` whose algorithm is consumable.

    Pip's hash-checking mode and PEP 751 both accept any of sha256,
    sha384, or sha512; nab forwards every recorded entry so consumers
    can pick.  Unacceptable algorithms (e.g. md5) are dropped, so the
    result may be empty.
    """
    from ..lockfile import ACCEPTED_HASH_ALGORITHMS

    return tuple(
        (algo, digest)
        for algo, digest in sorted(hashes)
        if algo in ACCEPTED_HASH_ALGORITHMS
    )


def require_artifact_hashes(lock_input: LockInput) -> None:
    """Raise :class:`MissingHashError` if a pinned artefact has no hash.

    PEP 751 and pip's hash-checking mode each need at least one of
    sha256/sha384/sha512 per artefact.  The plain ``name==version``
    writer records no hash and does not call this.
    """
    from ..lockfile import ACCEPTED_HASH_ALGORITHMS, IndexPin

    pin_groups: Iterable[Mapping[str, PinShape]] = (
        lock_input.pins,
        *lock_input.per_tuple_pins.values(),
    )
    for pins in pin_groups:
        for pin in pins.values():
            if not isinstance(pin, IndexPin):
                continue
            artefacts = (*pin.wheels, *((pin.sdist,) if pin.sdist is not None else ()))
            for artefact in artefacts:
                if not artefact.hashes:
                    msg = (
                        f"{pin.name}: artefact {artefact.filename!r} has no "
                        f"acceptable hash (need one of "
                        f"{list(ACCEPTED_HASH_ALGORITHMS)!r})"
                    )
                    raise MissingHashError(msg)


def _common_requires_python(files: Iterable[WheelFile | SdistFile]) -> str | None:
    """Return the package-level Requires-Python value, or ``None``.

    An artefact with no Requires-Python is unconstrained, so a single
    such artefact leaves the whole package unconstrained. Otherwise the
    value survives only when every artefact agrees.

    A malformed value counts as unconstrained too, matching
    ``excluded_by_python``, which admits a dist whose Requires-Python is
    an unparseable specifier on any Python. So the lock writer always
    parses a valid specifier or ``None``, and the pin is never
    over-constrained by an artefact whose floor nab could not read.
    """
    seen: set[str] = set()
    for f in files:
        if f.requires_python is None:
            return None
        try:
            SpecifierSet(f.requires_python)
        except InvalidSpecifier:
            return None
        seen.add(f.requires_python)
    if len(seen) == 1:
        return next(iter(seen))
    return None


def _vcs_pin_from_source(
    canonical: str,
    version: Version,
    source: VcsSource,
    *,
    resolved_sha: str | None,
) -> VcsPin:
    """Build a :class:`VcsPin` from a :class:`VcsSource`.

    ``resolved_sha`` is the post-clone SHA recorded on the provider by
    :func:`~nab_python._provider.sources.materialize_vcs_source`.  A VCS
    source cannot be pinned without first being materialised, so a
    ``None`` here is an internal invariant violation: raise
    :class:`MissingVcsCommitError` rather than emit a branch name or
    empty string as ``commit_id``.

    ``requested_revision`` is the URL's ``@<ref>``, kept only when it
    is a named ref that differs from ``commit_id`` (i.e. the user did
    not pin the bare SHA).  ``subdirectory`` carries the
    ``#subdirectory=`` fragment so an installer can locate the project
    inside the repo.

    ``bare_repo_url`` comes from ``parsed.repo_url``, which
    :meth:`VcsRequest.parse` has already separated from the ref and the
    fragment.  ``repo_url`` re-pins that bare URL to ``commit_id`` (the
    ``git+`` prefix, ``@<sha>``, and any ``#subdirectory=`` fragment) so
    the requirements.txt line installs the locked commit, not the ref
    the user supplied.
    """
    from nab_index.vcs import VcsRequest

    from ..lockfile import VcsPin

    if resolved_sha is None:
        msg = (
            f"{canonical}: VCS source pinned without a resolved commit SHA;"
            " materialize_vcs_source records the post-clone SHA before any"
            " version can be pinned, so this is an internal invariant"
            " violation"
        )
        raise MissingVcsCommitError(msg)
    parsed = VcsRequest.parse(source.url)
    # Keep the named ref only when it differs from the SHA (tag or branch case).
    requested_revision = (
        parsed.ref if parsed.ref and parsed.ref != resolved_sha else None
    )

    # Compose a pinned installable URL from the parsed pieces, not from source.url,
    # so credentials are stripped and the sha replaces any floating ref.
    bare_repo_url = _strip_userinfo(parsed.repo_url)
    repo_url = f"{parsed.scheme}+{bare_repo_url}@{resolved_sha}"
    if parsed.subdirectory:
        repo_url += f"#subdirectory={quote(parsed.subdirectory, safe='/')}"

    return VcsPin(
        name=canonical,
        version=str(version),
        repo_url=repo_url,
        bare_repo_url=bare_repo_url,
        commit_id=resolved_sha,
        subdirectory=parsed.subdirectory or None,
        requested_revision=requested_revision,
        vcs_type=parsed.scheme,
    )
