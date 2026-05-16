"""Build a :class:`LockInput` from a finished resolve.

The provider's caches still hold the listings the resolver consumed
when this runs, so artefact hashes and per-file Requires-Python can
be read directly without a second fetch.  This module also owns the
``read_lockfile_anchor`` helper used by ``nab lock`` to keep
``P<n>D`` durations stable across re-locks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, overload
from urllib.parse import urlsplit, urlunsplit

import tomli

from nab_index.client import SdistFile, WheelFile

from .._vendor.packaging.pylock import Pylock, PylockValidationError
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
    "build_lock_input_from_provider",
    "read_lockfile_anchor",
    "read_lockfile_packages",
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

    def effective_dist_policy(self, canonical_name: str, /) -> DistPolicy:
        """Return the effective :class:`DistPolicy` for ``canonical_name``."""
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
    raw = data.get("tool", {}).get("nab", {}).get("created-at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
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
    """Return ``url`` with any ``user:password@`` userinfo removed.

    Lockfiles are committed to version control, so an index or VCS
    URL carrying embedded credentials must not be written verbatim.
    Only the userinfo is dropped: host case and port are preserved
    and a ``git+`` scheme prefix is left intact.  A no-op for URLs
    without credentials.
    """
    parts = urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=netloc))


def build_lock_input_from_provider(
    provider: LockInputProvider,
    pins: Mapping[str, Version],
    *,
    requires_python: str | None = None,
    extras: Sequence[str] = (),
    dependency_groups: Sequence[str] = (),
    default_groups: Sequence[str] = (),
    created_by: str = "nab",
    indexes: Sequence[IndexConfig] = (),
) -> LockInput:
    """Build a :class:`LockInput` from a finished resolve.

    ``provider`` is the :class:`Provider` that drove the resolve;
    its caches still hold the listings the resolver consumed.  ``pins``
    is the canonical-name -> :class:`Version` mapping returned by the
    resolver after extras keys have been stripped.

    ``dependency_groups`` lists the PEP 735 groups whose requirements
    were folded into this resolve; ``default_groups`` is the subset
    that a default install (no ``--group`` flag) should apply.

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
    )


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
    URL.  When ``indexes`` is empty or the route is unknown the URL
    falls back to the default Simple-API root.

    Under :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL` the
    package's wheels stayed in ``versions_cache`` as a possible
    metadata source for the resolver; only the sdist is emitted
    into the lock so installers download and build that archive.
    """
    from ..fetch import DEFAULT_INDEX_URL
    from ..lockfile import IndexPin, SdistArtifact, WheelArtifact
    from ..provider import DistPolicy

    files = list(provider.dist_files_for(canonical, version))
    if provider.effective_dist_policy(canonical) is DistPolicy.SDIST_INSTALL:
        files = [f for f in files if not isinstance(f, WheelFile)]

    wheels = tuple(
        _build_artifact(canonical, f, WheelArtifact)
        for f in files
        if isinstance(f, WheelFile)
    )
    sdist_file = next((f for f in files if isinstance(f, SdistFile)), None)
    sdist = (
        _build_artifact(canonical, sdist_file, SdistArtifact)
        if sdist_file is not None
        else None
    )
    requires_python = _common_requires_python(files)
    serving_name = provider.coordinator.index.get_listing_index(canonical)
    by_name = {ix.name: ix.url for ix in indexes}

    if serving_name is not None and serving_name in by_name:
        index_url = by_name[serving_name]
    elif indexes:
        index_url = indexes[0].url
    else:
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
    canonical: str,
    source: WheelFile | SdistFile,
    cls: type[WheelArtifact],
) -> WheelArtifact: ...
@overload
def _build_artifact(
    canonical: str,
    source: WheelFile | SdistFile,
    cls: type[SdistArtifact],
) -> SdistArtifact: ...
def _build_artifact(
    canonical: str,
    source: WheelFile | SdistFile,
    cls: type[WheelArtifact | SdistArtifact],
) -> WheelArtifact | SdistArtifact:
    hashes = _filter_acceptable_hashes(canonical, source.filename, source.hashes)
    return cls(
        filename=source.filename,
        url=source.url,
        hashes=hashes,
        size=source.size,
        upload_time=_parse_upload_time(source.upload_time),
        local_path=source.local_path,
    )


def _parse_upload_time(raw: str | None) -> datetime | None:
    """Parse an index ``upload-time`` string to a ``datetime``.

    Accepts the RFC 3339 form the Simple/JSON API serves (``Z`` or an
    explicit offset).  Returns ``None`` when the field is absent or
    unparseable; the timestamp is informational, so a bad value is
    dropped rather than fatal.
    """
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _filter_acceptable_hashes(
    canonical: str, filename: str, hashes: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    """Return the subset of ``hashes`` whose algorithm is consumable.

    Pip's hash-checking mode and PEP 751 both accept any of sha256,
    sha384, or sha512; nab forwards every recorded entry so consumers
    can pick.  Raise :class:`MissingHashError` when none of the
    acceptable algorithms are present.
    """
    from ..lockfile import ACCEPTED_HASH_ALGORITHMS

    accepted = tuple(
        (algo, digest)
        for algo, digest in sorted(hashes)
        if algo in ACCEPTED_HASH_ALGORITHMS
    )
    if not accepted:
        algos = sorted({algo for algo, _ in hashes})
        msg = (
            f"{canonical}: artefact {filename!r} has no acceptable hash"
            f" (need one of {list(ACCEPTED_HASH_ALGORITHMS)!r}, got {algos!r})"
        )
        raise MissingHashError(msg)
    return accepted


def _common_requires_python(files: Iterable[WheelFile | SdistFile]) -> str | None:
    """Return a single Requires-Python value if all files agree, else ``None``."""
    seen: set[str] = set()
    for f in files:
        if f.requires_python is not None:
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

    Prefer ``resolved_sha`` (the post-clone SHA recorded on the
    provider) over the URL's ``@<ref>``: annotated tags and floating
    refs only resolve to a commit after the clone runs.  Fall back to
    the URL ref when the source was never materialised.

    ``requested_revision`` is the URL's ``@<ref>``, kept only when it
    is a named ref that differs from ``commit_id`` (i.e. the user did
    not pin the bare SHA).  An absent or unparseable ref leaves it
    ``None``.
    """
    from nab_index.vcs import VcsCloneError, VcsRequest

    from ..lockfile import VcsPin

    try:
        url_ref = VcsRequest.parse(source.url).ref
    except VcsCloneError:
        url_ref = ""
    commit_id = resolved_sha if resolved_sha is not None else url_ref
    requested_revision = url_ref if url_ref and url_ref != commit_id else None
    return VcsPin(
        name=canonical,
        version=str(version),
        repo_url=_strip_userinfo(source.url),
        commit_id=commit_id,
        requested_revision=requested_revision,
    )
