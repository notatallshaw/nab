"""Shared store for fetched package data.

The fetcher writes here and the provider reads, so the store is the one
object both sides of the fetch port agree on.  It is stdlib-only: no
network, no filesystem, no import of the index client at runtime.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_index.client import SdistFile, WheelFile
    from nab_index.lazy_wheel import RangeOutcome

__all__ = [
    "InMemoryIndex",
    "metadata_pending_key",
    "range_pending_key",
]


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)


def metadata_pending_key(package: str, version: str, metadata_url: str | None) -> str:
    """Return the pending key for one sidecar fetch.

    The URL is in the key so two wheels of a version do not share a request:
    a waiter is released by the artifact it asked for.
    """
    return f"metadata:{package}:{version}:{metadata_url}"


def range_pending_key(package: str, version: str, wheel_url: str) -> str:
    """Return the pending key for one range read.

    The wheel URL is in the key so sibling sidecar-less wheels of a version do
    not share a request: sibling wheels can declare different dependencies, so a
    waiter is released by the wheel it asked for, matching the sidecar path.
    """
    return f"range:{package}:{version}:{wheel_url}"


class InMemoryIndex:
    """Thread-safe storage for fetched package data.

    The async fetcher writes here; the sync provider reads.
    """

    def __init__(self) -> None:
        """Create an empty index."""
        self._lock = threading.Lock()
        self._listings: dict[str, list[WheelFile | SdistFile]] = {}
        self._listing_errors: dict[str, BaseException] = {}
        self._listing_indexes: dict[str, str] = {}
        # Packages whose empty listing stands for an index skipped offline.
        self._offline_listing_misses: set[str] = set()
        # Packages whose empty listing stands for a page of formats nab cannot read.
        self._unreadable_only_listings: set[str] = set()
        # Metadata text is keyed by the artifact it came from: the sidecar URL
        # for a wheel's METADATA, or None for text that stands for the version
        # itself (sdist PKG-INFO, an injected override).  Two wheels of one
        # version can declare different dependencies, so a reader asks for the
        # artifact its own target would install.
        self._metadata: dict[tuple[str, str, str | None], str | None] = {}
        self._metadata_errors: dict[tuple[str, str, str | None], BaseException] = {}
        # Empty metadata slots that stand for a rung skipped offline, keyed by
        # that rung's URL.
        self._offline_metadata_misses: set[tuple[str, str, str]] = set()
        # Packages whose offline skips have already been warned about.
        self._offline_metadata_warned: set[str] = set()
        # Versions whose version-level slot was written from an sdist PKG-INFO;
        # readers need the origin because only sdist deps go through the
        # PEP 643 gate.
        self._metadata_from_sdist: set[tuple[str, str]] = set()
        self._sdist_pyproject: dict[tuple[str, str], str | None] = {}
        self._sdist_archives: dict[tuple[str, str], bytes | None] = {}
        self._sdist_archive_errors: dict[tuple[str, str], BaseException] = {}
        # The mechanical outcome of a rung-4 range read, per wheel URL, for
        # the provider's tier accounting.  Discovered in nab-index, recorded
        # here; keyed like the read itself so sibling wheels of one version
        # do not overwrite each other's outcome.
        self._range_outcomes: dict[tuple[str, str, str], RangeOutcome] = {}
        self._pending: dict[str, _Pending] = {}

        # Parsed metadata is a pure function of the underlying text, so it
        # is shared across the per-target providers of one resolve.  Entries
        # are ``(source_text, parsed)``: one version can have several texts
        # (sibling wheels, an sdist), so a parse only answers for the text it
        # parsed.
        self._parsed_metadata: dict[tuple[str, str], tuple[str, Any]] = {}

        # Post-reconciliation sdist metadata: the result after
        # PEP 643 dynamic deps have been resolved via the bundled
        # pyproject.toml fallback or a PEP 517 backend invocation.
        # Shared across targets so a matrix does not re-augment (or, more
        # importantly, re-build) the same sdist once per tuple.
        self._resolved_sdist_metadata: dict[tuple[str, str], Any] = {}

    def get_listing(self, package: str) -> list[WheelFile | SdistFile] | None:
        """Return the cached listing for ``package``, or ``None``."""
        with self._lock:
            return self._listings.get(package)

    def store_listing(
        self,
        package: str,
        data: Sequence[WheelFile | SdistFile],
        *,
        offline_miss: bool = False,
        unreadable_only: bool = False,
    ) -> None:
        """Cache the listing for ``package`` and unblock any waiter.

        ``data`` is accepted as a Sequence (covariant) so callers can pass
        homogeneous ``list[WheelFile]`` lists; it is materialised into the
        internal ``list[WheelFile | SdistFile]`` cache.

        ``offline_miss`` marks the empty listing as an index skipped offline
        rather than one that served no files.  ``unreadable_only`` marks it
        as a page whose every file is in a format nab does not read.
        """
        key = f"listing:{package}"
        materialised = list(data)
        with self._lock:
            self._listings[package] = materialised
            if offline_miss:
                self._offline_listing_misses.add(package)
            if unreadable_only:
                self._unreadable_only_listings.add(package)
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def is_offline_listing_miss(self, package: str) -> bool:
        """Whether ``package``'s empty listing is an offline cold-cache miss."""
        with self._lock:
            return package in self._offline_listing_misses

    def is_unreadable_only_listing(self, package: str) -> bool:
        """Whether ``package``'s empty listing held only unreadable formats."""
        with self._lock:
            return package in self._unreadable_only_listings

    def store_listing_error(self, package: str, error: BaseException) -> None:
        """Record a failed listing fetch and unblock any waiter.

        Distinct from ``store_listing([])``: an empty listing means the
        index served nothing nab could read, while an error means the fetch
        itself failed. ``fetch_versions`` re-raises the error instead of
        reporting the package as having no candidates.
        """
        key = f"listing:{package}"
        with self._lock:
            self._listing_errors[package] = error
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def get_listing_error(self, package: str) -> BaseException | None:
        """Return the recorded listing fetch error for ``package``, or ``None``."""
        with self._lock:
            return self._listing_errors.get(package)

    def store_listing_index(self, package: str, index_name: str) -> None:
        """Record which configured index served ``package``."""
        with self._lock:
            self._listing_indexes[package] = index_name

    def get_listing_index(self, package: str) -> str | None:
        """Return the configured index name that served ``package``, or ``None``."""
        with self._lock:
            return self._listing_indexes.get(package)

    def _read_metadata(
        self, package: str, version: str, metadata_url: str | None
    ) -> tuple[str | None, bool]:
        """Return the text answering for ``metadata_url`` and its origin.

        Caller holds the lock.  The artifact's own slot wins, then the
        version-level one.  An sdist's PKG-INFO answers for an artifact whose
        own read returned nothing, but not for one nobody has read yet:
        lending it there would give a wheel that declares its own dependencies
        the sdist's.  An injected override has no artifact behind it and
        answers for any.
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
        """Return cached metadata text, or ``None`` if not yet stored."""
        with self._lock:
            return self._read_metadata(package, version, metadata_url)[0]

    def has_metadata(
        self, package: str, version: str, metadata_url: str | None = None
    ) -> bool:
        """Return ``True`` once a fetch answering for ``metadata_url`` resolved.

        Any value counts, including the ``None`` of a sidecar that was not
        served.  It tracks :meth:`_read_metadata`, so a fetch skipped on the
        strength of it leaves the reader the same text a fetch would have.
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

        A wheel's METADATA and an sdist's PKG-INFO can both stand for one
        version, and only sdist text goes through the :pep:`643` dynamic-deps
        gate, so text and origin are read together under one lock.
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
        """Write one metadata slot. Caller holds the lock.

        Reconciled sdist metadata is derived from the version-level text, so
        replacing that text drops it.  The parsed cache carries the text it
        parsed and needs no eviction.
        """
        package, version, metadata_url = slot
        if metadata_url is None:
            if self._metadata.get(slot) != data:
                self._resolved_sdist_metadata.pop((package, version), None)
            if from_sdist:
                self._metadata_from_sdist.add((package, version))
            else:
                self._metadata_from_sdist.discard((package, version))
        self._metadata[slot] = data

    def store_metadata(
        self,
        package: str,
        version: str,
        data: str | None,
        metadata_url: str | None = None,
    ) -> None:
        """Cache metadata text (or ``None`` for a failed fetch).

        ``metadata_url`` is the sidecar the text came from; ``None`` stores the
        text as standing for the version rather than for one artifact.

        A ``data`` of ``None`` means no PEP 658 sidecar arrived and readers
        fall back to the sdist.  It lands in the sidecar's own slot, so it
        cannot erase sdist PKG-INFO the version-level slot already holds.
        """
        key = metadata_pending_key(package, version, metadata_url)
        slot = (package, version, metadata_url)
        with self._lock:
            self._write_metadata_slot(slot, data, from_sdist=False)
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def store_metadata_error(
        self,
        package: str,
        version: str,
        error: BaseException,
        metadata_url: str | None = None,
    ) -> None:
        """Record a failed metadata fetch and unblock waiters.

        Distinct from ``store_metadata(None)``: ``None`` means no PEP 658
        sidecar arrived and the resolver may fall back to the sdist; an error
        means an advertised sidecar could not be fetched or failed its published
        hash, so the resolve must not fall through.
        """
        key = metadata_pending_key(package, version, metadata_url)
        with self._lock:
            self._metadata_errors[(package, version, metadata_url)] = error
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

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

        The skip writes the same empty slot a metadata-less artifact writes, so
        without the mark the two read alike.  ``url`` keys it to one rung of the
        ladder: a rung skipped is no claim about an artifact a later rung read.
        """
        with self._lock:
            self._offline_metadata_misses.add((package, version, url))

    def is_offline_metadata_miss(
        self, package: str, version: str, url: str | None
    ) -> bool:
        """Whether the metadata fetch at ``url`` was skipped offline."""
        with self._lock:
            return (package, version, url) in self._offline_metadata_misses

    def claim_offline_metadata_warning(self, package: str) -> bool:
        """Whether the caller owns ``package``'s one offline-skip warning.

        True for the first caller only.  The targets of a run share this index
        but each builds its own :class:`~nab_python.provider.Provider`, so the
        state lives here to hold the report to one per package.
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
        rather than for one artifact and answers a read that names no
        artifact.  The pending key differs from a wheel's so an sdist request
        can run in parallel with (or after) a failed wheel metadata request.
        :meth:`metadata_from_sdist` reports which kind the version-level slot
        holds.
        """
        key = f"sdist:{package}:{version}"
        with self._lock:
            self._write_metadata_slot((package, version, None), data, from_sdist=True)
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def store_sdist_metadata_error(
        self, package: str, version: str, error: BaseException
    ) -> None:
        """Record a failed sdist fetch and unblock the sdist waiter.

        Distinct from ``store_sdist_metadata(None)``: ``None`` means the archive
        yielded no PKG-INFO; an error means the archive could not be fetched or
        failed its published hash, so the resolve must abort rather than fall
        through.
        """
        key = f"sdist:{package}:{version}"
        with self._lock:
            self._metadata_errors[(package, version, None)] = error
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def metadata_from_sdist(self, package: str, version: str) -> bool:
        """Return ``True`` when the version-level slot was written from an sdist.

        The slot itself cannot distinguish wheel METADATA from sdist
        PKG-INFO; readers that apply the :pep:`643` dynamic-deps gate
        only to sdist values ask here for the current text's origin.
        """
        with self._lock:
            return (package, version) in self._metadata_from_sdist

    def store_range_metadata(
        self, package: str, version: str, wheel_url: str, data: str
    ) -> None:
        """Store range-recovered wheel METADATA in the wheel's own slot.

        The text is authoritative wheel METADATA, so it lands in the
        ``(package, version, wheel_url)`` slot with ``from_sdist=False`` and
        stays off the :pep:`643` dynamic-deps gate.  Keying by the wheel URL,
        like the sidecar path, keeps sibling sidecar-less wheels of one version
        independent: a matrix target that picks one wheel never reads another
        wheel's dependencies.  Firing the ``range:`` pending releases the
        provider thread blocked on rung 4.
        """
        key = range_pending_key(package, version, wheel_url)
        with self._lock:
            self._write_metadata_slot(
                (package, version, wheel_url), data, from_sdist=False
            )
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def store_range_absent(self, package: str, version: str, wheel_url: str) -> None:
        """Release a rung-4 waiter without writing a metadata slot.

        A range read that yielded no METADATA (ranges unsupported, no matching
        dist-info, an offline cold miss, a dead fetcher loop) is a rung miss,
        not an error: the pending fires so the provider reads ``None`` and steps
        to the sdist rung, which can still write the version-level slot.
        """
        key = range_pending_key(package, version, wheel_url)
        with self._lock:
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def store_range_error(
        self, package: str, version: str, wheel_url: str, error: BaseException
    ) -> None:
        """Record a failed range read as a per-wheel error and unblock rung 4.

        Distinct from :meth:`store_range_absent`: a malformed-UTF-8 METADATA
        blob, or a wheel URL the index advertised and then could not serve,
        fails the resolve rather than falling through to the sdist, mirroring
        :meth:`store_metadata_error` for an advertised sidecar.  The error
        lands in the ``(package, version, wheel_url)`` slot the provider reads
        for that wheel.  The ``range:`` pending fires so the waiter unblocks.
        """
        key = range_pending_key(package, version, wheel_url)
        with self._lock:
            self._metadata_errors[(package, version, wheel_url)] = error
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

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
        with self._lock:
            return self._range_outcomes.get((package, version, wheel_url))

    def store_sdist_pyproject(self, package: str, version: str, data: str) -> None:
        """Store sdist-derived pyproject.toml text for static-metadata fallback.

        The fetcher writes both PKG-INFO and pyproject.toml when an
        sdist is downloaded. Provider code reads this slot when
        PKG-INFO marks dependencies as :pep:`643` Dynamic.  No
        ``None`` slot is written: missing pyproject.toml is
        indistinguishable from never-fetched at the read path.
        """
        with self._lock:
            self._sdist_pyproject[(package, version)] = data

    def get_sdist_pyproject(self, package: str, version: str) -> str | None:
        """Return sdist pyproject.toml text or ``None`` if absent or unfetched."""
        with self._lock:
            return self._sdist_pyproject.get((package, version))

    def store_sdist_archive(
        self, package: str, version: str, data: bytes | None
    ) -> None:
        """Cache sdist archive bytes (or ``None`` for a failed fetch)."""
        key = f"sdist-archive:{package}:{version}"
        with self._lock:
            self._sdist_archives[(package, version)] = data
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def get_sdist_archive(self, package: str, version: str) -> bytes | None:
        """Return cached sdist archive bytes, or ``None`` if absent or failed."""
        with self._lock:
            return self._sdist_archives.get((package, version))

    def store_sdist_archive_error(
        self, package: str, version: str, error: BaseException
    ) -> None:
        """Record a failed sdist-archive fetch and unblock the waiter.

        Kept in its own slot rather than ``store_sdist_archive(None)`` so the
        ``BUILD_REMOTE`` path can tell an archive the index never offered (skip
        the version) from one it advertised and then failed to serve (abort the
        resolve).
        """
        key = f"sdist-archive:{package}:{version}"
        with self._lock:
            self._sdist_archive_errors[(package, version)] = error
            pending = self._pending.get(key)
        if pending is not None:
            pending.event.set()

    def get_sdist_archive_error(
        self, package: str, version: str
    ) -> BaseException | None:
        """Return a recorded sdist-archive fetch error, or ``None``."""
        with self._lock:
            return self._sdist_archive_errors.get((package, version))

    def get_or_create_pending(self, key: str) -> tuple[threading.Event, bool]:
        """Return ``key``'s waitable event, and whether it already existed.

        One fetch is asked for by the speculative prefetch, by the scan batch
        and by the read, so a later caller is handed the first caller's event
        and waits on the request already in flight.
        """
        with self._lock:
            if key in self._pending:
                return self._pending[key].event, True
            pending = _Pending()
            self._pending[key] = pending
            return pending.event, False

    def get_parsed_metadata(
        self, package: str, version: str, source_text: str
    ) -> Any | None:
        """Return the cached parse of ``source_text``, or ``None``.

        Unlike :meth:`get_metadata` (which returns the raw text), this
        returns the already-parsed dataclass.  A parse of any other text is
        a miss: wheel METADATA and sdist PKG-INFO share one
        ``(package, version)`` slot and either can replace the other
        mid-resolve, so a hit on the key alone could hand back the deps of
        the artifact the caller is not holding.
        """
        with self._lock:
            entry = self._parsed_metadata.get((package, version))
            if entry is None or entry[0] != source_text:
                return None
            return entry[1]

    def store_parsed_metadata(
        self, package: str, version: str, metadata: Any, source_text: str
    ) -> None:
        """Cache the parse of ``source_text`` for future tuple lookups.

        Safe across tuples because the parsed object is read-only and
        a pure function of ``source_text``.  Per-tuple classification
        (marker eval, extras admission) happens above this cache.
        """
        with self._lock:
            self._parsed_metadata[(package, version)] = (source_text, metadata)

    def get_resolved_sdist_metadata(self, package: str, version: str) -> Any | None:
        """Return cached post-reconciliation sdist metadata or ``None``.

        The cached value is the result of
        :func:`nab_python._provider.metadata_resolver.resolve_dynamic_sdist`:
        either a :pep:`621` pyproject augmentation or a PEP 517 backend
        invocation.  Both branches are deterministic functions of the
        sdist content under nab's build inputs, so the value is shared
        across universal-mode tuples to avoid duplicate work.
        """
        with self._lock:
            return self._resolved_sdist_metadata.get((package, version))

    def store_resolved_sdist_metadata(
        self, package: str, version: str, metadata: Any
    ) -> None:
        """Cache reconciled sdist metadata for cross-tuple reuse."""
        with self._lock:
            self._resolved_sdist_metadata[(package, version)] = metadata
