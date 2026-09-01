"""Shared store for fetched package data.

No network, no filesystem, no import of the index client at runtime.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from nab_provider.metadata import metadata_header_block

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from nab_provider.records import RangeOutcome, SdistFile, WheelFile

    from .policy import SourceMaterialization

__all__ = [
    "InMemoryIndex",
    "metadata_pending_key",
    "range_pending_key",
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

        self._listings: dict[str, list[WheelFile | SdistFile]] = {}
        self._listing_errors: dict[str, BaseException] = {}
        self._listing_indexes: dict[str, str] = {}
        # Packages a routing override sent to one index, which left the
        # other configured indexes unasked.
        self._pinned_listings: set[str] = set()
        # Packages whose empty listing stands for an index skipped offline.
        self._offline_listing_misses: set[str] = set()
        # Packages whose empty listing stands for a page of formats nab cannot read.
        self._unreadable_only_listings: set[str] = set()
        # Packages whose empty listing stands for a page of links nab cannot reach.
        self._unreachable_only_listings: set[str] = set()
        # Packages whose empty listing stands for a page nab kept no file off.
        self._no_usable_file_listings: set[str] = set()
        # Packages whose empty listing stands for a page of yanked files.
        self._all_yanked_listings: set[str] = set()
        # Versions an index served as a ``.zip`` sdist, which the listing parse
        # drops, so no record of them reaches ``_listings``.
        self._zip_sdists: dict[str, frozenset[str]] = {}

        # Metadata text is keyed by the artifact it came from: the sidecar URL
        # for a wheel's METADATA, or None for text that stands for the version
        # itself, such as an sdist's PKG-INFO.  Each is cut to its header block
        # on the way in.
        self._metadata: dict[tuple[str, str, str | None], str | None] = {}
        self._metadata_errors: dict[tuple[str, str, str | None], BaseException] = {}
        # Versions whose version-level slot was written from an sdist PKG-INFO;
        # only sdist deps go through the PEP 643 gate.
        self._metadata_from_sdist: set[tuple[str, str]] = set()

        # Empty metadata slots that stand for a rung skipped offline, keyed by
        # that rung's URL.
        self._offline_metadata_misses: set[tuple[str, str, str]] = set()
        # Packages whose offline skips have already been warned about.
        self._offline_metadata_warned: set[str] = set()

        self._sdist_pyproject: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        self._sdist_archives: dict[tuple[str, str], bytes | None] = {}
        self._sdist_archive_errors: dict[tuple[str, str], BaseException] = {}

        # The mechanical outcome of a rung-4 range read, per wheel URL, for
        # the provider's tier accounting.
        self._range_outcomes: dict[tuple[str, str, str], RangeOutcome] = {}

        # A parse is a pure function of its text, so ``(source_text, parsed)``
        # entries are shared across the per-target providers of one resolve.
        self._parsed_metadata: dict[tuple[str, str], tuple[str, Any]] = {}

        # Sdist metadata after PEP 643 dynamic deps have been resolved from the
        # bundled pyproject.toml or a PEP 517 backend.  Shared across targets so
        # a matrix does not rebuild one sdist per tuple.
        self._resolved_sdist_metadata: dict[tuple[str, str], Any] = {}

        # What a host made of a declared source, and of a remote sdist build.
        self._sources: dict[str, SourceMaterialization] = {}
        self._built_metadata: dict[tuple[str, str], Any] = {}

    @contextmanager
    def _publishing(self, key: str) -> Iterator[None]:
        """Hold the lock for a store write, then wake ``key``'s waiter.

        The wake is outside the lock, and a body that raises skips it.

        A published key stays in the map so it keeps deduplicating, but holds
        the shared set event rather than one of its own.
        """
        with self._lock:
            yield
            pending = self._pending.get(key)
            if pending is not None:
                self._pending[key] = _PUBLISHED

        if pending is not None:
            pending.set()

    def get_listing(self, package: str) -> list[WheelFile | SdistFile] | None:
        """Return the cached listing for ``package``, or ``None``."""
        return self._listings.get(package)

    def store_listing(
        self,
        package: str,
        data: Sequence[WheelFile | SdistFile],
        *,
        offline_miss: bool = False,
        unreadable_only: bool = False,
        unreachable_only: bool = False,
        no_usable_file: bool = False,
        all_yanked: bool = False,
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

        ``zip_sdists`` names the releases served as a ``.zip`` sdist, which
        nothing in ``data`` records.  It replaces whatever a prior store left,
        so re-storing a listing without one clears it.
        """
        key = f"listing:{package}"
        materialised = list(data)
        with self._publishing(key):
            # The marks land first, so an unlocked reader sees them with the listing.
            if offline_miss:
                self._offline_listing_misses.add(package)
            if unreadable_only:
                self._unreadable_only_listings.add(package)
            if unreachable_only:
                self._unreachable_only_listings.add(package)
            if no_usable_file:
                self._no_usable_file_listings.add(package)
            if all_yanked:
                self._all_yanked_listings.add(package)
            if zip_sdists:
                self._zip_sdists[package] = zip_sdists
            else:
                self._zip_sdists.pop(package, None)

            self._listings[package] = materialised

    def is_offline_listing_miss(self, package: str) -> bool:
        """Whether ``package``'s empty listing is an offline cold-cache miss."""
        return package in self._offline_listing_misses

    def is_unreadable_only_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing held only unreadable formats."""
        return package in self._unreadable_only_listings

    def is_unreachable_only_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing held only links nab cannot reach."""
        return package in self._unreachable_only_listings

    def is_no_usable_file_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing named files nab kept none of."""
        return package in self._no_usable_file_listings

    def is_all_yanked_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing held files and yanked every one."""
        return package in self._all_yanked_listings

    def zip_sdist_versions(self, package: str) -> frozenset[str]:
        """Versions of ``package`` an index served as a ``.zip`` sdist."""
        return self._zip_sdists.get(package, frozenset())

    def store_listing_error(self, package: str, error: BaseException) -> None:
        """Record a failed listing fetch and unblock any waiter.

        Distinct from ``store_listing([])``: an empty listing means the index
        served nothing nab could read; an error means the fetch failed.
        """
        key = f"listing:{package}"
        with self._publishing(key):
            self._listing_errors[package] = error

    def get_listing_error(self, package: str) -> BaseException | None:
        """Return ``package``'s recorded listing fetch error, or ``None``."""
        return self._listing_errors.get(package)

    def store_listing_index(
        self, package: str, index_name: str, *, pinned: bool = False
    ) -> None:
        """Record which configured index served ``package``.

        ``pinned`` marks ``index_name`` as an index a routing override
        chose, which left the other configured indexes unasked.
        """
        with self._lock:
            self._listing_indexes[package] = index_name
            if pinned:
                self._pinned_listings.add(package)

    def get_listing_index(self, package: str) -> str | None:
        """Return the configured index name that served ``package``, or ``None``."""
        return self._listing_indexes.get(package)

    def is_pinned_listing(self, package: str) -> bool:
        """Whether a routing override kept other indexes from serving ``package``."""
        return package in self._pinned_listings

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
                if text is not None:
                    return (text, False)
            elif (package, version) in self._metadata_from_sdist:
                return (None, False)
        version_level = self._metadata.get((package, version, None))
        return (version_level, (package, version) in self._metadata_from_sdist)

    def get_metadata(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> str | None:
        """Return the cached header block, or ``None`` if not yet stored."""
        with self._lock:
            return self._read_metadata(package, version, metadata_url)[0]

    def has_metadata(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> bool:
        """Return ``True`` once a fetch answering for ``metadata_url`` resolved.

        Any value counts, including the ``None`` of an unserved sidecar.  The
        precedence is :meth:`_read_metadata`'s.
        """
        with self._lock:
            if (
                metadata_url is not None
                and (package, version, metadata_url) in self._metadata
            ):
                return True
            return (package, version, None) in self._metadata and (
                metadata_url is None
                or (package, version) not in self._metadata_from_sdist
            )

    def get_metadata_with_origin(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> tuple[str | None, bool]:
        """Return the metadata text for ``metadata_url`` and its sdist origin.

        Both come off one lock, so the text and its origin cannot disagree.
        """
        with self._lock:
            return self._read_metadata(package, version, metadata_url)

    def _write_metadata_slot(
        self,
        slot: tuple[str, str, str | None],
        data: str | None,
        *,
        from_sdist: bool,
    ) -> None:
        """Write one metadata slot, cut to its header block. Caller holds the lock.

        Reconciled sdist metadata is derived from the version-level text, so
        replacing that text drops it.
        """
        package, version, metadata_url = slot
        text = None if data is None else metadata_header_block(data)
        if metadata_url is None:
            if self._metadata.get(slot) != text:
                self._resolved_sdist_metadata.pop((package, version), None)
            if from_sdist:
                self._metadata_from_sdist.add((package, version))
            else:
                self._metadata_from_sdist.discard((package, version))

        # The text lands last, so an unlocked reader never pairs it with sdist
        # metadata derived from the text it replaced.
        self._metadata[slot] = text

    def store_metadata(
        self,
        package: str,
        version: str,
        data: str | None,
        *,
        metadata_url: str | None = None,
    ) -> None:
        """Cache the header block of ``data``, or ``None`` when no sidecar was served.

        ``metadata_url`` is the sidecar the text came from; ``None`` stores it
        as standing for the version rather than one artifact.  It is
        keyword-only: it and ``data`` are both ``str | None``, so a transposed
        call would type-check.

        A ``data`` of ``None`` lands in the sidecar's own slot, so it cannot
        erase sdist PKG-INFO from the version-level one.
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
            self._metadata_errors[(package, version, metadata_url)] = error

    def get_metadata_error(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> BaseException | None:
        """Return a recorded metadata fetch error, or ``None``.

        The artifact's own error wins, then a version-level one.
        """
        with self._lock:
            if metadata_url is not None:
                error = self._metadata_errors.get((package, version, metadata_url))
                if error is not None:
                    return error
            return self._metadata_errors.get((package, version, None))

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
        """Store sdist-derived PKG-INFO in the version-level metadata slot.

        PKG-INFO is core-metadata-equivalent, so it stands for the version
        rather than one artifact.  Its pending key differs from a wheel's, so
        an sdist request can run alongside or after a failed wheel request.
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
            self._metadata_errors[(package, version, None)] = error

    def metadata_from_sdist(self, package: str, version: str) -> bool:
        """Return ``True`` when the version-level slot was written from an sdist."""
        return (package, version) in self._metadata_from_sdist

    def store_range_metadata(
        self, package: str, version: str, wheel_url: str, data: str
    ) -> None:
        """Store range-recovered wheel METADATA in the wheel's own slot.

        The text is authoritative wheel METADATA, so it is stored with
        ``from_sdist=False`` and stays off the :pep:`643` dynamic-deps gate.
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
            pass

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
            self._metadata_errors[(package, version, wheel_url)] = error

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
        """Store an sdist's parsed pyproject.toml for static-metadata fallback.

        The host parses the TOML on the way in, so the store needs no TOML
        library.  ``None`` reads the same as never-fetched.
        """
        with self._lock:
            self._sdist_pyproject[(package, version)] = data

    def get_sdist_pyproject(
        self, package: str, version: str
    ) -> Mapping[str, Any] | None:
        """Return the parsed sdist pyproject, or ``None`` if absent or unfetched."""
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
        return self._sdist_archives.get((package, version))

    def store_sdist_archive_error(
        self, package: str, version: str, error: BaseException
    ) -> None:
        """Record a failed sdist-archive fetch and unblock the waiter.

        Kept in its own slot rather than ``store_sdist_archive(None)`` so
        ``BUILD_REMOTE`` can tell an archive the index never offered (skip the
        version) from one it advertised and failed to serve (abort).
        """
        key = f"sdist-archive:{package}:{version}"
        with self._publishing(key):
            self._sdist_archive_errors[(package, version)] = error

    def get_sdist_archive_error(
        self, package: str, version: str
    ) -> BaseException | None:
        """Return a recorded sdist-archive fetch error, or ``None``."""
        return self._sdist_archive_errors.get((package, version))

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
    ) -> Any | None:
        """Return the cached parse of ``source_text``, or ``None``.

        A parse of any other text is a miss: wheel METADATA and sdist PKG-INFO
        share one ``(package, version)`` slot, so a key-only hit could hand
        back another artifact's deps.
        """
        entry = self._parsed_metadata.get((package, version))
        if entry is None or entry[0] != source_text:
            return None
        return entry[1]

    def store_parsed_metadata(
        self, package: str, version: str, metadata: Any, source_text: str
    ) -> None:
        """Cache the parse of ``source_text``."""
        with self._lock:
            self._parsed_metadata[(package, version)] = (source_text, metadata)

    def get_resolved_sdist_metadata(self, package: str, version: str) -> Any | None:
        """Return cached post-reconciliation sdist metadata or ``None``.

        The cached value is what
        :func:`nab_provider._provider.metadata_resolver.resolve_dynamic_sdist`
        returned.
        """
        return self._resolved_sdist_metadata.get((package, version))

    def store_resolved_sdist_metadata(
        self, package: str, version: str, metadata: Any
    ) -> None:
        """Cache reconciled sdist metadata for cross-tuple reuse."""
        with self._lock:
            self._resolved_sdist_metadata[(package, version)] = metadata

    def store_source(self, package: str, result: SourceMaterialization) -> None:
        """Record what a host made of ``package``'s declared source."""
        with self._lock:
            self._sources[package] = result

    def get_source(self, package: str) -> SourceMaterialization | None:
        """Return ``package``'s materialised source, or ``None``."""
        return self._sources.get(package)

    def store_built_metadata(self, package: str, version: str, metadata: Any) -> None:
        """Record the METADATA a host's :pep:`517` build produced."""
        with self._lock:
            self._built_metadata[(package, version)] = metadata

    def get_built_metadata(self, package: str, version: str) -> Any | None:
        """Return built METADATA for ``(package, version)``, or ``None``.

        Unlike :meth:`get_resolved_sdist_metadata`, this is what the build
        declared, before the provider checks it against the candidate.
        """
        return self._built_metadata.get((package, version))
