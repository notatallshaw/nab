"""Shared store for fetched package data.

No network, no filesystem, no import of the index client at runtime.
"""

from __future__ import annotations

__lazy_modules__ = (
    "hashlib",
    "nab_provider.metadata",
    "packaging.utils",
)

import threading
from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING, Any, NamedTuple

from packaging.utils import canonicalize_version

from .metadata import metadata_header_block, metadata_without_description

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from .metadata import WheelMetadata
    from .records import RangeOutcome, SdistFile, WheelFile

__all__ = [
    "InMemoryIndex",
    "WheelMetadataContext",
    "metadata_pending_key",
    "range_pending_key",
    "sdist_artifact_key",
]


# Every published key shares this one set event rather than keeping its own.
_PUBLISHED = threading.Event()
_PUBLISHED.set()


def metadata_pending_key(package: str, version: str, metadata_url: str | None) -> str:
    """Return the pending key for one sidecar fetch.

    The URL is in the key so two wheels of a version do not share a request.
    """
    return f"metadata:{package}:{version}:{metadata_url}"


def range_pending_key(package: str, version: str, wheel_url: str) -> str:
    """Return the pending key for one range read.

    The wheel URL is in the key: sibling sidecar-less wheels of a version can
    declare different dependencies.
    """
    return f"range:{package}:{version}:{wheel_url}"


def sdist_artifact_key(version: str, url: str) -> str:
    """Key an sdist by its normalized version and archive URL."""
    version = canonicalize_version(version)
    return "artifact-" + sha256(f"{version}\0{url}".encode()).hexdigest()


def _project_table_only(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``data`` cut to ``[project]``, empty unless that key holds a table."""
    project = data.get("project")
    return {"project": project} if isinstance(project, dict) else {}


class WheelMetadataContext:
    """Identify one package listing and serving index for wheel metadata sharing."""

    __slots__ = ()


class _ListingResult(NamedTuple):
    """Files or a fetch error, with the diagnostics from that same lookup."""

    files: list[WheelFile | SdistFile] | None = None
    error: BaseException | None = None
    offline_miss: bool = False
    unreadable_only: bool = False
    unreachable_only: bool = False
    no_usable_file: bool = False
    all_yanked: bool = False
    zip_sdists: frozenset[str] = frozenset()


_NO_LISTING = _ListingResult()


class InMemoryIndex:
    """Thread-safe slots for fetched data, plus the events readers wait on.

    The async fetcher writes; the sync provider reads.

    A read of a single dict or set runs without the lock: the keys hash and
    compare in C, so one lookup cannot interleave with a write.  Writers put the
    marks that qualify a slot in place before the slot itself.  The lock still
    covers every write, every read spanning two slots, and the test-and-set
    accessors.
    """

    def __init__(self) -> None:
        """Create an empty index."""
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}

        self._listings: dict[str, _ListingResult] = {}
        # Publish the index name and whether the route was pinned together.
        self._listing_indexes: dict[str, tuple[str, bool]] = {}

        # Metadata text is keyed by the artifact it came from: the sidecar URL
        # for a wheel's METADATA, or None for text that stands for the version
        # itself, such as an sdist's PKG-INFO.  Each is cut to its header block
        # less the description on the way in.
        self._metadata: dict[
            tuple[str, str, str | None], str | BaseException | None
        ] = {}
        # Versions whose version-level slot was written from an sdist PKG-INFO;
        # only sdist deps go through the PEP 643 check.
        self._metadata_from_sdist: set[tuple[str, str]] = set()
        # The distinct texts a release's slots hold, so equal ones share a str.
        self._slot_texts: dict[tuple[str, str], tuple[str, ...]] = {}

        # Empty metadata slots that stand for a rung skipped offline, keyed by
        # that rung's URL.
        self._offline_metadata_misses: set[tuple[str, str, str]] = set()
        # Packages whose offline skips have already been warned about.
        self._offline_metadata_warned: set[str] = set()

        self._sdist_pyproject: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        self._sdist_archives: dict[tuple[str, str], bytes | BaseException | None] = {}

        # The mechanical outcome of a rung-4 range read, per wheel URL, for
        # the provider's tier accounting.
        self._range_outcomes: dict[tuple[str, str, str], RangeOutcome] = {}

        # Share each text's parse across the target providers of one resolve.
        self._parsed_metadata: dict[
            tuple[str, str], tuple[tuple[str, WheelMetadata], ...]
        ] = {}
        self._consistent_wheel_metadata: dict[str, dict[str, tuple[str, str]]] = {}
        self._consistent_metadata_contexts: dict[str, WheelMetadataContext] = {}

        # Sdist metadata reconciled with declarations from its bundled pyproject.toml.
        self._resolved_sdist_metadata: dict[tuple[str, str], WheelMetadata] = {}

        # Raw build output is checked against each target before use.
        self._built_metadata: dict[tuple[str, str], WheelMetadata] = {}

    @contextmanager
    def _publishing(self, key: str) -> Generator[None, None, None]:
        """Hold the lock for a store write, then wake ``key``'s waiter.

        The wake is outside the lock, and a body that raises skips it.

        A published key stays in the map so it keeps deduplicating, but holds
        the shared set event rather than one of its own.
        """
        with self._lock:
            yield  # noqa: RUF075 - a failed write must not publish or wake waiters
            pending = self._pending.get(key)
            if pending is not None:
                self._pending[key] = _PUBLISHED

        if pending is not None:
            pending.set()

    def get_listing(self, package: str) -> list[WheelFile | SdistFile] | None:
        """Return the cached listing for ``package``, or ``None``."""
        return self._listings.get(package, _NO_LISTING).files

    def store_listing(  # noqa: PLR0913 - independent diagnostics accompany one listing
        self,
        package: str,
        data: Sequence[WheelFile | SdistFile],
        *,
        offline_miss: bool = False,
        unreadable_only: bool = False,
        unreachable_only: bool = False,
        no_usable_file: bool = False,
        all_yanked: bool = False,
        has_yanked_files: bool | None = None,
        zip_sdists: frozenset[str] = frozenset(),
    ) -> None:
        """Cache the listing for ``package`` and unblock any waiter.

        ``data`` is a Sequence so callers can pass a ``list[WheelFile]``.

        ``offline_miss`` marks an empty listing as an index skipped offline
        rather than one that served no files; ``unreadable_only`` marks it as
        a page of formats nab does not read; ``unreachable_only`` marks it as
        a page whose every link nab cannot reach; ``no_usable_file`` marks it
        as a page that named files nab kept none of; ``all_yanked`` marks it
        as a page whose every file is yanked.

        ``has_yanked_files`` may be False when a parser has already proved
        the listing has no withdrawals. Otherwise leave it unset to derive
        the sparse withdrawal index from the records.

        ``zip_sdists`` names the releases served as a ``.zip`` sdist, which
        nothing in ``data`` records.  It replaces whatever a prior store left,
        so re-storing a listing without one clears it.
        """
        materialised = list(data)
        withdrawn = getattr(data, "withdrawn_versions", None)
        if has_yanked_files is None:
            withdrawn = {file.version for file in materialised if file.yanked}
            has_yanked_files = bool(withdrawn)
        if has_yanked_files or withdrawn:
            # Ordinary listings do not need the withdrawal record classes.
            from .yanking import YankedListing  # noqa: PLC0415

            materialised = YankedListing(materialised, withdrawn_versions=withdrawn)
        result = _ListingResult(
            files=materialised,
            offline_miss=offline_miss,
            unreadable_only=unreadable_only,
            unreachable_only=unreachable_only,
            no_usable_file=no_usable_file,
            all_yanked=all_yanked,
            zip_sdists=zip_sdists,
        )
        self._publish_listing(package, result)

    def _publish_listing(self, package: str, result: _ListingResult) -> None:
        with self._publishing(f"listing:{package}"):
            self._consistent_wheel_metadata.pop(package, None)
            self._consistent_metadata_contexts.pop(package, None)
            self._listings[package] = result

    def is_offline_listing_miss(self, package: str) -> bool:
        """Whether ``package``'s empty listing is an offline cold-cache miss."""
        return self._listings.get(package, _NO_LISTING).offline_miss

    def is_unreadable_only_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing held only unreadable formats."""
        return self._listings.get(package, _NO_LISTING).unreadable_only

    def is_unreachable_only_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing held only links nab cannot reach."""
        return self._listings.get(package, _NO_LISTING).unreachable_only

    def is_no_usable_file_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing named files nab kept none of."""
        return self._listings.get(package, _NO_LISTING).no_usable_file

    def is_all_yanked_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing held files and yanked every one."""
        return self._listings.get(package, _NO_LISTING).all_yanked

    def zip_sdist_versions(self, package: str) -> frozenset[str]:
        """Versions of ``package`` an index served as a ``.zip`` sdist."""
        return self._listings.get(package, _NO_LISTING).zip_sdists

    def store_listing_error(self, package: str, error: BaseException) -> None:
        """Record a failed listing fetch and unblock any waiter.

        Distinct from ``store_listing([])``: an empty listing means the index
        served nothing nab could read; an error means the fetch failed.
        """
        self._publish_listing(package, _ListingResult(error=error))

    def get_listing_error(self, package: str) -> BaseException | None:
        """Return ``package``'s recorded listing fetch error, or ``None``."""
        return self._listings.get(package, _NO_LISTING).error

    def store_listing_index(
        self, package: str, index_name: str, *, pinned: bool = False
    ) -> None:
        """Record which configured index served ``package``.

        ``pinned`` marks ``index_name`` as an index a routing override
        chose, which left the other configured indexes unasked.
        """
        with self._lock:
            if self.get_listing_index(package) != index_name:
                self._consistent_wheel_metadata.pop(package, None)
                self._consistent_metadata_contexts.pop(package, None)
            self._listing_indexes[package] = (index_name, pinned)

    def get_listing_index(self, package: str) -> str | None:
        """Return the configured index name that served ``package``."""
        route = self._listing_indexes.get(package)
        return route[0] if route is not None else None

    def pinned_listing_index(self, package: str) -> str | None:
        """Return the index name when a routing override excluded the other indexes."""
        route = self._listing_indexes.get(package)
        if route is None:
            return None
        name, pinned = route
        return name if pinned else None

    def _read_metadata(
        self, package: str, version: str, metadata_url: str | None
    ) -> tuple[str | None, bool]:
        """Return the text answering for ``metadata_url`` and its origin.

        Caller holds the lock.  The artifact's own slot wins, then the
        version-level one.  An sdist's PKG-INFO answers for an artifact whose
        own read returned nothing, but not for one nobody has read yet, which
        would hand a wheel the sdist's dependencies instead of its own.
        Version-level text from anywhere else answers for any artifact.
        """
        if metadata_url is not None:
            slot = (package, version, metadata_url)
            if slot in self._metadata:
                text = self._metadata[slot]
                if isinstance(text, BaseException):
                    return (None, False)
                if text is not None:
                    return (text, False)
            elif (package, version) in self._metadata_from_sdist:
                return (None, False)
        version_level = self._metadata.get((package, version, None))
        if isinstance(version_level, BaseException):
            return (None, False)
        return (version_level, (package, version) in self._metadata_from_sdist)

    def get_metadata(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> str | None:
        """Return the cached header block less the description, or ``None``."""
        with self._lock:
            return self._read_metadata(package, version, metadata_url)[0]

    def has_metadata(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> bool:
        """Return ``True`` once a fetch answering for ``metadata_url`` resolved.

        Text, an absent sidecar and a fetch error are all completed results.
        A caller can read the error with :meth:`get_metadata_error`.
        """
        with self._lock:
            if (
                metadata_url is not None
                and (package, version, metadata_url) in self._metadata
            ):
                return True
            release = (package, version, None)
            if metadata_url is None:
                return release in self._metadata
            # A release-wide error does not complete a different wheel's own fetch.
            return (
                release in self._metadata
                and (package, version) not in self._metadata_from_sdist
                and not isinstance(self._metadata[release], BaseException)
            )

    def get_metadata_with_origin(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> tuple[str | None, bool]:
        """Return the metadata text for ``metadata_url`` and its sdist origin.

        Both come off one lock, so the text and its origin cannot disagree.
        """
        with self._lock:
            return self._read_metadata(package, version, metadata_url)

    def consistent_metadata_context(
        self, package: str, files: Sequence[WheelFile | SdistFile]
    ) -> WheelMetadataContext | None:
        """Return the sharing token for this raw listing and its serving index."""
        with self._lock:
            if self.get_listing(package) is not files:
                return None

            context = self._consistent_metadata_contexts.get(package)
            if context is None:
                context = WheelMetadataContext()
                self._consistent_metadata_contexts[package] = context
            return context

    def get_consistent_wheel_metadata(
        self, package: str, version: str, *, context: WheelMetadataContext | None
    ) -> str | None:
        """Return shared release metadata while its source is unchanged."""
        with self._lock:
            if (
                context is None
                or self._consistent_metadata_contexts.get(package) is not context
            ):
                return None

            sources = self._consistent_wheel_metadata.get(package)
            if sources is None:
                return None

            representative = sources.get(version)
            if representative is None:
                return None

            # A replacement or fetch error invalidates the shared source.
            url, text = representative
            if self._metadata.get((package, version, url)) != text or isinstance(
                self._metadata.get((package, version, None)), BaseException
            ):
                sources.pop(version)
                return None

            return text

    def store_consistent_wheel_metadata(
        self,
        package: str,
        version: str,
        url: str,
        text: str,
        *,
        context: WheelMetadataContext | None,
    ) -> None:
        """Remember the first validated wheel source for an opted-in release."""
        with self._lock:
            # Publish only to the context that admitted this wheel's metadata.
            if (
                context is not None
                and self._consistent_metadata_contexts.get(package) is context
                and self._metadata.get((package, version, url)) == text
            ):
                sources = self._consistent_wheel_metadata.setdefault(package, {})
                sources.setdefault(version, (url, text))

    def _share_slot_text(self, release: tuple[str, str], text: str) -> str:
        """Return the str already held for ``release`` equal to ``text``, or hold it.

        Caller holds the lock.  ``release`` is ``(package, version)``.  A matrix
        writes one slot per target of a release, and targets whose wheels
        declare the same headers would otherwise hold a copy of the text each.
        """
        held = self._slot_texts.get(release, ())
        for seen in held:
            if seen == text:
                return seen
        self._slot_texts[release] = (*held, text)
        return text

    def _write_metadata_slot(
        self,
        slot: tuple[str, str, str | None],
        data: str | BaseException | None,
        *,
        from_sdist: bool,
    ) -> None:
        """Write a metadata slot while holding the lock.

        Replacing version-level text drops metadata derived from it.
        """
        package, version, metadata_url = slot
        result = data
        if isinstance(data, str):
            result = self._share_slot_text(
                (package, version),
                metadata_without_description(metadata_header_block(data)),
            )

        if metadata_url is None:
            if isinstance(result, BaseException) or self._metadata.get(slot) != result:
                self._resolved_sdist_metadata.pop((package, version), None)
            if from_sdist:
                self._metadata_from_sdist.add((package, version))
            else:
                self._metadata_from_sdist.discard((package, version))

        # Publish after clearing any sdist metadata derived from the previous text.
        self._metadata[slot] = result

    def store_metadata(
        self,
        package: str,
        version: str,
        data: str | None,
        *,
        metadata_url: str | None = None,
    ) -> None:
        """Cache metadata without its description.

        ``metadata_url`` selects an artifact slot. ``None`` data records a
        missing sidecar without replacing version-level PKG-INFO.
        """
        key = metadata_pending_key(package, version, metadata_url)
        slot = (package, version, metadata_url)
        with self._publishing(key):
            self._write_metadata_slot(slot, data, from_sdist=False)

    def store_metadata_error(
        self,
        package: str,
        version: str,
        error: BaseException,
        metadata_url: str | None = None,
    ) -> None:
        """Record a failed metadata fetch and unblock waiters.

        Where ``store_metadata(None)`` says no sidecar arrived and the resolver
        may fall back to the sdist, an error says an advertised sidecar failed
        to fetch or failed its published hash, so it must not fall through.
        """
        key = metadata_pending_key(package, version, metadata_url)
        with self._publishing(key):
            self._write_metadata_slot(
                (package, version, metadata_url), error, from_sdist=False
            )

    def get_metadata_error(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> BaseException | None:
        """Return a recorded metadata fetch error, or ``None``.

        The artifact's own error wins, then a version-level one.
        """
        with self._lock:
            if metadata_url is not None:
                result = self._metadata.get((package, version, metadata_url))
                if isinstance(result, BaseException):
                    return result
            result = self._metadata.get((package, version, None))
            return result if isinstance(result, BaseException) else None

    def record_offline_metadata_miss(
        self, package: str, version: str, url: str
    ) -> None:
        """Mark the metadata fetch at ``url`` as one offline mode skipped.

        The skip writes the same empty slot a metadata-less artifact writes.
        ``url`` keys the mark to one rung: a rung skipped is no claim about an
        artifact a later rung read.
        """
        with self._lock:
            self._offline_metadata_misses.add((package, version, url))

    def is_offline_metadata_miss(
        self, package: str, version: str, url: str | None
    ) -> bool:
        """Whether the metadata fetch at ``url`` was skipped offline."""
        return (package, version, url) in self._offline_metadata_misses

    def claim_offline_metadata_warning(self, package: str) -> bool:
        """Whether the caller owns ``package``'s one offline-skip warning.

        True for the first caller only.  Targets of a run share this index but
        each builds its own :class:`~nab_provider.provider.Provider`, so the
        state lives here.
        """
        with self._lock:
            if package in self._offline_metadata_warned:
                return False
            self._offline_metadata_warned.add(package)
            return True

    def store_sdist_metadata(
        self, package: str, version: str, data: str | None
    ) -> None:
        """Store PKG-INFO using the archive's cache identity.

        Indexed sdists pass ``sdist_artifact_key(actual_version, url)`` as
        ``version``. The separate pending key lets an sdist request run
        alongside or after a failed wheel request.
        """
        key = f"sdist:{package}:{version}"
        with self._publishing(key):
            self._write_metadata_slot((package, version, None), data, from_sdist=True)

    def store_sdist_metadata_error(
        self, package: str, version: str, error: BaseException
    ) -> None:
        """Record a failed sdist fetch and unblock the sdist waiter.

        Distinct from ``store_sdist_metadata(None)``: ``None`` means the archive
        yielded no PKG-INFO; an error means it failed to fetch or failed its
        published hash, so the resolve aborts.
        """
        key = f"sdist:{package}:{version}"
        with self._publishing(key):
            self._write_metadata_slot((package, version, None), error, from_sdist=False)

    def metadata_from_sdist(self, package: str, version: str) -> bool:
        """Return whether the supplied cache key holds sdist-derived metadata."""
        return (package, version) in self._metadata_from_sdist

    def store_range_metadata(
        self, package: str, version: str, wheel_url: str, data: str
    ) -> None:
        """Store range-recovered wheel METADATA in the wheel's own slot.

        The text is authoritative wheel METADATA, so it uses
        ``from_sdist=False`` and skips the :pep:`643` check.
        """
        key = range_pending_key(package, version, wheel_url)
        with self._publishing(key):
            self._write_metadata_slot(
                (package, version, wheel_url), data, from_sdist=False
            )

    def store_range_absent(self, package: str, version: str, wheel_url: str) -> None:
        """Release a rung-4 waiter without writing a metadata slot.

        A range read that came back empty (ranges unsupported, no METADATA
        member, an offline cold miss, a dead fetcher loop) is a rung miss, not
        an error, so the read falls through to the sdist rung.
        """
        key = range_pending_key(package, version, wheel_url)
        with self._publishing(key):
            self._metadata.pop((package, version, wheel_url), None)

    def store_range_error(
        self, package: str, version: str, wheel_url: str, error: BaseException
    ) -> None:
        """Record a failed range read as a per-wheel error and unblock rung 4.

        Distinct from :meth:`store_range_absent`: a malformed-UTF-8 METADATA
        blob, or a wheel URL the index advertised and could not serve, fails
        the resolve rather than falling through to the sdist.
        """
        key = range_pending_key(package, version, wheel_url)
        with self._publishing(key):
            self._write_metadata_slot(
                (package, version, wheel_url), error, from_sdist=False
            )

    def store_range_outcome(
        self, package: str, version: str, wheel_url: str, outcome: RangeOutcome
    ) -> None:
        """Record the mechanical outcome of a range read for tier accounting."""
        with self._lock:
            self._range_outcomes[(package, version, wheel_url)] = outcome

    def get_range_outcome(
        self, package: str, version: str, wheel_url: str
    ) -> RangeOutcome | None:
        """Return the recorded range-read outcome, or ``None`` if none ran."""
        return self._range_outcomes.get((package, version, wheel_url))

    def store_sdist_pyproject(
        self, package: str, version: str, data: Mapping[str, Any] | None
    ) -> None:
        """Store the pyproject cut to ``[project]`` for static-metadata fallback.

        The host parses the TOML on the way in, so the store needs no TOML
        library.  ``None`` reads the same as never-fetched.
        """
        stored = None if data is None else _project_table_only(data)
        with self._lock:
            self._sdist_pyproject[(package, version)] = stored

    def get_sdist_pyproject(
        self, package: str, version: str
    ) -> Mapping[str, Any] | None:
        """Return the ``[project]`` cut, or ``None`` if absent or unfetched."""
        return self._sdist_pyproject.get((package, version))

    def store_sdist_archive(
        self, package: str, version: str, data: bytes | None
    ) -> None:
        """Cache sdist archive bytes, or ``None`` when no archive was fetched."""
        key = f"sdist-archive:{package}:{version}"
        with self._publishing(key):
            self._sdist_archives[(package, version)] = data

    def get_sdist_archive(self, package: str, version: str) -> bytes | None:
        """Return cached sdist archive bytes, or ``None`` if absent or unfetched."""
        result = self._sdist_archives.get((package, version))
        return result if isinstance(result, bytes) else None

    def store_sdist_archive_error(
        self, package: str, version: str, error: BaseException
    ) -> None:
        """Record a failed sdist-archive fetch and unblock the waiter.

        Callers use the error to distinguish a failed download from an archive
        the index never offered.
        """
        key = f"sdist-archive:{package}:{version}"
        with self._publishing(key):
            self._sdist_archives[(package, version)] = error

    def get_sdist_archive_error(
        self, package: str, version: str
    ) -> BaseException | None:
        """Return a recorded sdist-archive fetch error, or ``None``."""
        result = self._sdist_archives.get((package, version))
        return result if isinstance(result, BaseException) else None

    def get_or_create_pending(self, key: str) -> tuple[threading.Event, bool]:
        """Return ``key``'s waitable event, and whether it already existed.

        The speculative prefetch, the scan batch and the read can all ask for
        one fetch, so a later caller waits on the request in flight.

        A key whose fetch already landed hands back the shared set event.
        """
        with self._lock:
            event = self._pending.get(key)
            if event is not None:
                return event, True

            event = threading.Event()
            self._pending[key] = event
            return event, False

    def get_parsed_metadata(
        self, package: str, version: str, source_text: str
    ) -> WheelMetadata | None:
        """Return the parse cached for ``source_text``, if any."""
        for text, metadata in self._parsed_metadata.get((package, version), ()):
            if text == source_text:
                return metadata
        return None

    def store_parsed_metadata(
        self, package: str, version: str, metadata: WheelMetadata, source_text: str
    ) -> None:
        """Cache a text's parse beside the version's other parses.

        Replacing the tuple keeps unlocked reads atomic.
        """
        key = (package, version)
        with self._lock:
            entries = self._parsed_metadata.get(key, ())
            self._parsed_metadata[key] = (*entries, (source_text, metadata))

    def get_resolved_sdist_metadata(
        self, package: str, version: str
    ) -> WheelMetadata | None:
        """Return sdist metadata reconciled with its bundled project declarations."""
        return self._resolved_sdist_metadata.get((package, version))

    def store_resolved_sdist_metadata(
        self, package: str, version: str, metadata: WheelMetadata
    ) -> None:
        """Cache reconciled sdist metadata for cross-tuple reuse."""
        with self._lock:
            self._resolved_sdist_metadata[(package, version)] = metadata

    def store_built_metadata(
        self, package: str, artifact_key: str, metadata: WheelMetadata
    ) -> None:
        """Record the METADATA a host's :pep:`517` build produced."""
        with self._lock:
            self._built_metadata[(package, artifact_key)] = metadata

    def get_built_metadata(
        self, package: str, artifact_key: str
    ) -> WheelMetadata | None:
        """Return built METADATA for ``(package, artifact_key)``, or ``None``.

        Unlike :meth:`get_resolved_sdist_metadata`, this is what the build
        declared, before the provider checks it against the candidate.
        """
        return self._built_metadata.get((package, artifact_key))
