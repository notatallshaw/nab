"""Channel-based async I/O coordinator for nab-python.

Mirrors uv's architecture: a sync resolver on the main thread sends
fetch requests through a queue to an async fetcher on a background
thread. An InMemoryIndex is shared state between both.

The cross-thread bridge is built from the standard library only:
the fetcher thread owns an ``asyncio.Queue`` and the sync side
schedules puts on the loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from packaging.utils import canonicalize_name as canonicalize_name_boundary

from nab_index.cache import CacheBackend, NullCache, OfflineError, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import SdistFile, WheelFile
from nab_index.lazy_wheel import RangeCapabilityMemo, RangeOutcome
from nab_index.local_index import LocalIndexClient, is_file_url, parse_file_url
from nab_index.multi_index import IndexConfig, MultiIndexClient
from nab_index.serialization import SimpleSerialization
from nab_index.transport import IDENTITY_HEADERS, raise_unless_ok

from ._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from typing_extensions import Self

__all__ = [
    "DEFAULT_INDEX_NAME",
    "DEFAULT_INDEX_URL",
    "FetchCoordinator",
    "FetchKind",
    "FetchRequest",
    "InMemoryIndex",
    "IndexRoute",
]


DEFAULT_INDEX_NAME = "pypi"
DEFAULT_INDEX_URL = "https://pypi.org/simple/"

# Maximum time the main thread waits for the fetcher thread to drain
# its queue and exit on :meth:`FetchCoordinator.shutdown`.
_COORDINATOR_JOIN_TIMEOUT_SECONDS = 10

if TYPE_CHECKING:
    from pathlib import Path

    from nab_index.transport import AsyncHttpTransport

logger = logging.getLogger(__name__)


class FetchKind(enum.Enum):
    """Distinguishes the kinds of fetches the coordinator handles."""

    LISTING = "listing"
    METADATA = "metadata"
    RANGE_METADATA = "range-metadata"
    SDIST = "sdist"
    SDIST_ARCHIVE = "sdist-archive"
    DIRECT_ARCHIVE = "direct-archive"


@dataclass(frozen=True, slots=True)
class IndexRoute:
    """Per-package index routing rule (a strict pin to one index).

    ``name`` is the package name (canonicalised internally).  ``index``
    is the *name* of an :class:`IndexConfig` declared in the
    coordinator's ordered list.  Routing decides where to fetch a
    package's listing before any version is known, so a route carries no
    version scope and no marker; the override layer guarantees at most one
    route per package.
    """

    name: str
    index: str


def _resolve_routes(routes: list[IndexRoute]) -> dict[str, str]:
    """Reduce routes to ``{canonical_name: index_name}``.

    At most one route exists per package (the override layer rejects two
    routes for one name at parse time), so this is a straight projection.
    """
    return {canonicalize_name(entry.name): entry.index for entry in routes}


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """A single fetch request, carried across the sync->async boundary."""

    kind: FetchKind
    package: str
    version: str | None = None
    url: str | None = None
    metadata_hash: tuple[str, str] | None = None
    sdist_hashes: tuple[tuple[str, str], ...] = ()
    wheel_hashes: tuple[tuple[str, str], ...] = ()


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None


def _metadata_key(package: str, version: str, metadata_url: str | None) -> str:
    """Return the pending key for one sidecar fetch.

    The URL is in the key so two wheels of a version do not share a request:
    a waiter is released by the artifact it asked for.
    """
    return f"metadata:{package}:{version}:{metadata_url}"


def _range_key(package: str, version: str, wheel_url: str) -> str:
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
            pending.result = materialised
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
        key = _metadata_key(package, version, metadata_url)
        slot = (package, version, metadata_url)
        with self._lock:
            self._write_metadata_slot(slot, data, from_sdist=False)
            pending = self._pending.get(key)
        if pending is not None:
            pending.result = data
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
        key = _metadata_key(package, version, metadata_url)
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
            pending.result = data
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
        key = _range_key(package, version, wheel_url)
        with self._lock:
            self._write_metadata_slot(
                (package, version, wheel_url), data, from_sdist=False
            )
            pending = self._pending.get(key)
        if pending is not None:
            pending.result = data
            pending.event.set()

    def store_range_absent(self, package: str, version: str, wheel_url: str) -> None:
        """Release a rung-4 waiter without writing a metadata slot.

        A range read that yielded no METADATA (ranges unsupported, no matching
        dist-info, an offline cold miss, a dead fetcher loop) is a rung miss,
        not an error: the pending fires so the provider reads ``None`` and steps
        to the sdist rung, which can still write the version-level slot.
        """
        key = _range_key(package, version, wheel_url)
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
        key = _range_key(package, version, wheel_url)
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
            pending.result = data
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

    def get_or_create_pending(self, key: str) -> tuple[_Pending, bool]:
        """Return (pending, already_existed)."""
        with self._lock:
            if key in self._pending:
                return self._pending[key], True
            pending = _Pending()
            self._pending[key] = pending
            return pending, False

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


# Queue items: single request, batch of requests, or None (shutdown).
_QueueItem = FetchRequest | list[FetchRequest] | None


class FetchCoordinator:
    """Bridges the sync provider and the async fetcher.

    Cross-thread communication uses an ``asyncio.Queue`` that lives
    in the fetcher thread's event loop. Sync callers schedule puts
    on the loop via ``loop.call_soon_threadsafe``. Batch submissions
    (lists) guarantee all items arrive together for concurrent
    processing.

    Use as a context manager:
        with FetchCoordinator(transport) as coordinator:
            ...
    """

    PREFETCH_METADATA_COUNT = 10

    def __init__(
        self,
        transport: AsyncHttpTransport,
        *,
        indexes: list[IndexConfig] | None = None,
        max_concurrency: int = 50,
        cache_dir: Path | None = None,
        cache_backend: CacheBackend | None = None,
        offline: bool = False,
        index_routes: list[IndexRoute] | None = None,
        on_fetch: Callable[[], None] | None = None,
    ) -> None:
        """Create a coordinator that wraps ``transport``.

        ``indexes`` is the ordered list of :class:`IndexConfig` records;
        order is significant (presence-based first-index walks them
        left-to-right).  When omitted, defaults to
        ``[IndexConfig("pypi", "https://pypi.org/simple/")]``.  Each
        index name must be unique across the list.

        ``index_routes`` adds per-package routing rules; an entry's
        ``index`` field names one of the configured indexes and pins that
        package's listing fetch to it.

        ``cache_backend`` wins over ``cache_dir`` if both are given;
        otherwise ``cache_dir`` enables a per-index :class:`OnDiskCache`
        and ``None`` falls back to a :class:`NullCache`.  Passing an
        explicit ``cache_backend`` together with more than one entry in
        ``indexes``, or with an index that pins its ``serialization``, is
        rejected: each of those needs its own cache.
        """
        if indexes is None:
            indexes = [IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL)]
        if not indexes:
            msg = "indexes must contain at least one IndexConfig"
            raise ValueError(msg)
        names = [idx.name for idx in indexes]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            msg = f"duplicate index names: {duplicates}"
            raise ValueError(msg)
        self.indexes = list(indexes)
        self._transport = transport
        self._max_concurrency = max_concurrency
        self._offline = offline
        if cache_backend is not None and len(indexes) > 1:
            msg = (
                "explicit cache_backend is incompatible with more than one"
                " index: pass cache_dir so each index gets its own cache."
            )
            raise ValueError(msg)
        if cache_backend is not None and any(
            idx.serialization is not SimpleSerialization.NEGOTIATE for idx in indexes
        ):
            msg = (
                "explicit cache_backend is incompatible with a pinned"
                " serialization: pass cache_dir so the pinned index gets a"
                " cache of its own."
            )
            raise ValueError(msg)
        if cache_backend is not None:
            self._cache: CacheBackend = cache_backend
        elif cache_dir is not None:
            self._cache = OnDiskCache(cache_dir, indexes[0].url)
        else:
            self._cache = NullCache()
        self._cache_dir = cache_dir
        self._index_routes = list(index_routes or [])
        # Progress hook: fired once per successful listing fetch, from the
        # fetcher thread.  ``None`` when nothing is watching (the common case).
        self._on_fetch = on_fetch
        self.index = InMemoryIndex()
        # Shared per-run range-capability memo.  Rebuilt fresh on the fetcher
        # loop in _async_fetcher so each run starts with empty per-netloc state;
        # built here too so the attribute is always a real memo for typing.
        self._range_memo = RangeCapabilityMemo()
        self._thread: threading.Thread | None = None
        self._started = False
        self._crashed = False
        self._crash_error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_q: asyncio.Queue[_QueueItem] | None = None
        self._queue_ready = threading.Event()

    @property
    def offline(self) -> bool:
        """Whether this run may read the cache only, never the network."""
        return self._offline

    def __enter__(self) -> Self:
        """Start the fetcher thread and return self."""
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """Shut the fetcher thread down on context exit."""
        self.shutdown()

    def start(self) -> None:
        """Start the fetcher thread (idempotent)."""
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="nab-fetcher",
        )
        self._thread.start()
        self._queue_ready.wait()

    def shutdown(self) -> None:
        """Signal the fetcher thread to exit and join it."""
        if self._thread is None:
            return
        self._submit(None)
        self._thread.join(timeout=_COORDINATOR_JOIN_TIMEOUT_SECONDS)
        self._thread = None
        self._started = False
        # Drop the dead loop so a later start() waits for the fresh one
        # instead of submitting to a closed loop.
        self._loop = None
        self._async_q = None
        self._queue_ready.clear()

    def _submit(self, item: _QueueItem) -> None:
        """Schedule ``item`` on the fetcher loop's queue from any thread.

        Requests the loop cannot take are recorded as failures so their
        waiters unblock instead of hanging.
        """
        loop = self._loop
        queue = self._async_q
        if loop is None or queue is None:
            self._refuse(item)
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            # call_soon_threadsafe raises RuntimeError once the loop is closed.
            self._refuse(item)

    def _refuse(self, item: _QueueItem) -> None:
        """Record a failure for each request in ``item``, unblocking waiters."""
        # None is the shutdown sentinel; it has no waiters.
        if item is None:
            return

        requests = item if isinstance(item, list) else [item]
        for req in requests:
            error = RuntimeError("Fetcher loop is not running, request refused")
            if req.kind is FetchKind.LISTING:
                self.index.store_listing_error(req.package, error)
                continue
            assert req.version is not None
            if req.kind is FetchKind.METADATA:
                self.index.store_metadata_error(
                    req.package, req.version, error, req.url
                )
            elif req.kind is FetchKind.RANGE_METADATA:
                # A dead loop is a rung miss: unblock the waiter and let it fall
                # through to the sdist rung, not a candidate-dropping error.
                assert req.url is not None
                self.index.store_range_absent(req.package, req.version, req.url)
            elif req.kind is FetchKind.SDIST:
                self.index.store_sdist_metadata_error(req.package, req.version, error)
            else:
                self.index.store_sdist_archive_error(req.package, req.version, error)

    def _record_crash(self, error: BaseException) -> None:
        """Record the failure that killed the fetcher thread."""
        self._crash_error = error
        self._crashed = True

    def _check_alive(self) -> None:
        if not self._crashed:
            return
        msg = f"Fetcher thread crashed: {self._crash_error}"
        raise RuntimeError(msg)

    def request_listing(self, package: str) -> threading.Event:
        """Request a listing fetch; return an event set when the result lands."""
        self._check_alive()
        if self.index.get_listing(package) is not None:
            done = threading.Event()
            done.set()
            return done
        key = f"listing:{package}"
        pending, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(FetchRequest(kind=FetchKind.LISTING, package=package))
        return pending.event

    def request_metadata(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> threading.Event:
        """Request the sidecar at ``url`` as the metadata for ``(package, version)``."""
        self._check_alive()
        if self.index.has_metadata(package, version, url):
            done = threading.Event()
            done.set()
            return done
        key = _metadata_key(package, version, url)
        pending, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(
                FetchRequest(
                    kind=FetchKind.METADATA,
                    package=package,
                    version=version,
                    url=url,
                    metadata_hash=metadata_hash,
                )
            )
        return pending.event

    def request_range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        wheel_hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        """Request a sidecar-less wheel's METADATA over an HTTP range read.

        This is rung 4: the ``range:`` pending key is per
        ``(package, version, wheel_url)``, so two provider threads asking for the
        same wheel enqueue a single read, while sibling sidecar-less wheels of
        one version (which a matrix picks per target and which can declare
        different dependencies) each get their own read, matching the sidecar
        path.  A warm cache hit is served inside the client, so there is no early
        cache check here.  ``wheel_hashes`` are the wheel's published digests; a
        full-body read is verified against them before its METADATA is used.
        """
        self._check_alive()
        key = _range_key(package, version, wheel_url)
        pending, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(
                FetchRequest(
                    kind=FetchKind.RANGE_METADATA,
                    package=package,
                    version=version,
                    url=wheel_url,
                    wheel_hashes=wheel_hashes,
                )
            )
        return pending.event

    def request_sdist(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        """Request sdist PKG-INFO extraction.

        Uses a separate ``sdist:`` pending key so it can fire even
        when a prior wheel metadata fetch already cached ``None``
        for the same ``(package, version)`` slot. The fetcher verifies the
        downloaded archive against ``sdist_hashes`` before extraction.
        """
        self._check_alive()
        key = f"sdist:{package}:{version}"
        pending, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(
                FetchRequest(
                    kind=FetchKind.SDIST,
                    package=package,
                    version=version,
                    url=url,
                    sdist_hashes=sdist_hashes,
                )
            )
        return pending.event

    def request_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        """Request the raw bytes of an sdist archive.

        Used by the :class:`~nab_python.provider.BuildPolicy.BUILD_REMOTE`
        path; the bytes are extracted to a temp dir and handed to a
        PEP 517 backend.  Stored under a separate pending key so it can
        run concurrently with a PKG-INFO fetch for the same version. The
        fetcher verifies the downloaded archive against ``sdist_hashes``
        before storing the bytes.
        """
        self._check_alive()
        key = f"sdist-archive:{package}:{version}"
        pending, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(
                FetchRequest(
                    kind=FetchKind.SDIST_ARCHIVE,
                    package=package,
                    version=version,
                    url=url,
                    sdist_hashes=sdist_hashes,
                )
            )
        return pending.event

    def request_direct_archive(
        self,
        package: str,
        version: str,
        url: str,
    ) -> threading.Event:
        """Request the bytes of an archive named by URL rather than by an index.

        Used by ``[[tool.nab.archive-sources]]``, whose URL is declared
        independently of every index, so the URL's own scheme decides how it is
        read.  The bytes land unverified in the same slot as
        :meth:`request_sdist_archive`; the caller checks the declared hashes.
        """
        self._check_alive()
        key = f"sdist-archive:{package}:{version}"
        pending, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(
                FetchRequest(
                    kind=FetchKind.DIRECT_ARCHIVE,
                    package=package,
                    version=version,
                    url=url,
                )
            )
        return pending.event

    def request_metadata_batch(
        self, items: list[tuple[str, str, str, tuple[str, str] | None]]
    ) -> list[tuple[str, str, threading.Event]]:
        """Submit a batch of metadata requests as a single queue item.

        Each item is ``(package, version, url, metadata_hash)``.  All
        requests in the batch reach the fetcher together so they are
        processed concurrently.
        """
        self._check_alive()
        results: list[tuple[str, str, threading.Event]] = []
        batch: list[FetchRequest] = []
        for package, version, url, metadata_hash in items:
            if self.index.has_metadata(package, version, url):
                done = threading.Event()
                done.set()
                results.append((package, version, done))
                continue
            key = _metadata_key(package, version, url)
            pending, existed = self.index.get_or_create_pending(key)
            if not existed:
                batch.append(
                    FetchRequest(
                        kind=FetchKind.METADATA,
                        package=package,
                        version=version,
                        url=url,
                        metadata_hash=metadata_hash,
                    )
                )
            results.append((package, version, pending.event))
        if batch:
            self._submit(batch)
        return results

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._async_fetcher())
        except Exception as exc:
            self._record_crash(exc)
            logger.exception("Fetcher thread crashed")

    def _build_client(
        self,
    ) -> CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient:
        """Return the index client for this run.

        Single-index configurations return a plain
        :class:`CachedAsyncSimpleClient` (or a :class:`LocalIndexClient`
        for a ``file:`` URL).  Multi-index configurations wire up a
        :class:`MultiIndexClient` whose underlying clients share the
        coordinator's transport but get their own per-URL cache.
        """
        override_map = _resolve_routes(self._index_routes)
        if len(self.indexes) == 1 and not override_map:
            return self._build_index_client(self.indexes[0])
        clients_by_name: dict[str, CachedAsyncSimpleClient | LocalIndexClient] = {}
        for cfg in self.indexes:
            clients_by_name[cfg.name] = self._build_index_client(cfg)
        order = [cfg.name for cfg in self.indexes]
        return MultiIndexClient(
            clients_by_name,
            order,
            override_map,
        )

    def _build_index_client(
        self,
        cfg: IndexConfig,
    ) -> CachedAsyncSimpleClient | LocalIndexClient:
        """Build a single index client for ``cfg``.

        A ``file:`` URL in either RFC 8089 spelling goes to
        :class:`LocalIndexClient` (no caching; the filesystem is the
        cache).  Everything else goes to :class:`CachedAsyncSimpleClient`
        with a per-URL :class:`OnDiskCache` when ``cache_dir`` is set.
        """
        if is_file_url(cfg.url):
            return LocalIndexClient(cfg.url)

        backend: CacheBackend
        if self._cache_dir is not None:
            backend = OnDiskCache(
                self._cache_dir, cfg.url, serialization=cfg.serialization
            )
        else:
            backend = self._cache
        return CachedAsyncSimpleClient(
            self._transport,
            backend,
            cfg.url,
            offline=self._offline,
            range_memo=self._range_memo,
            serialization=cfg.serialization,
        )

    async def _async_fetcher(self) -> None:
        # Fresh per-run memo, owned on this single loop thread, injected into
        # every client _build_client constructs below.
        self._range_memo = RangeCapabilityMemo()

        queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        sem = asyncio.Semaphore(self._max_concurrency)
        tasks: set[asyncio.Task] = set()

        try:
            client = self._build_client()
            self._loop = asyncio.get_running_loop()
            self._async_q = queue
        except BaseException as exc:
            # _loop and _async_q stay unset, so _submit refuses the request.
            self._record_crash(exc)
            raise
        finally:
            # start() waits on this event, so every path has to set it.
            self._queue_ready.set()

        try:
            stopping = False
            while not stopping:
                item = await queue.get()
                if item is None:
                    break

                self._dispatch(item, client, sem, tasks)

                # drain any extra queued items without blocking
                while not queue.empty():
                    try:
                        extra = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if extra is None:
                        # Fall through to the gather below instead of
                        # cancelling: a cancelled _handle never records a
                        # result, leaving its waiter's event unset forever.
                        stopping = True
                        break
                    self._dispatch(extra, client, sem, tasks)

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await client.aclose()

            # A file:// index client owns no transport, but a direct archive
            # fetch can still have opened one.  aclose is idempotent.
            await self._transport.aclose()

    def _dispatch(
        self,
        item: FetchRequest | list[FetchRequest],
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        sem: asyncio.Semaphore,
        tasks: set[asyncio.Task],
    ) -> None:
        """Create async tasks for a single request or a batch."""
        if isinstance(item, list):
            for req in item:
                task = asyncio.create_task(self._handle(client, req, sem))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        else:
            task = asyncio.create_task(self._handle(client, item, sem))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _handle(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
        sem: asyncio.Semaphore,
    ) -> None:
        async with sem:
            try:
                if req.kind is FetchKind.LISTING:
                    await self._fetch_listing(client, req)
                elif req.kind is FetchKind.METADATA:
                    await self._fetch_metadata(client, req)
                elif req.kind is FetchKind.SDIST:
                    await self._fetch_sdist(client, req)
                elif req.kind is FetchKind.SDIST_ARCHIVE:
                    await self._fetch_sdist_archive(client, req)
                elif req.kind is FetchKind.RANGE_METADATA:
                    await self._fetch_range_metadata(client, req)
                else:
                    await self._fetch_direct_archive(req)
            except OfflineError as exc:
                # Offline with a cold cache is an expected miss.
                logger.debug(
                    "Fetch failed: %s %s: %s", req.kind.value, req.package, exc
                )
                self._record_fetch_failure(client, req, exc)
            except Exception as exc:  # noqa: BLE001 - failures must unblock the waiter
                logger.warning(
                    "Fetch failed: %s %s: %s", req.kind.value, req.package, exc
                )
                self._record_fetch_failure(client, req, exc)

    def _record_fetch_failure(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
        exc: Exception,
    ) -> None:
        """Record a failed fetch so any waiter unblocks (else a deadlock).

        Offline with a cold cache is the one deliberate degradation: the
        artifact is recorded absent and the resolver works with what the cache
        holds. Any other failure is recorded as an error, because a file the
        listing advertised and the index then failed to serve is not the same as
        a file that does not exist.

        A listing that is already stored keeps it: the fetch succeeded, and the
        work that raised ran after the waiter woke, so recording an error would
        make the outcome depend on which thread reads the index first.
        """
        offline = isinstance(exc, OfflineError)

        if req.kind is FetchKind.LISTING:
            if offline:
                # Record the serving index before the empty listing fires the
                # pending event (see _fetch_listing).
                self._record_serving_index(client, req.package)
                self.index.store_listing(req.package, [], offline_miss=True)
            elif self.index.get_listing(req.package) is None:
                self.index.store_listing_error(req.package, exc)
            return

        assert req.version is not None
        self._record_versioned_failure(req, exc, offline=offline)

    def _record_versioned_failure(
        self, req: FetchRequest, exc: Exception, *, offline: bool
    ) -> None:
        """Record the failure of a version-scoped fetch, unblocking its waiter.

        Offline is the one deliberate degradation: the artifact is recorded
        absent so the resolver works with what the cache holds.  Any other
        failure is recorded as an error, because a file the listing advertised
        and the index then failed to serve is not one that does not exist.

        A declared archive is the exception: it is the package's only
        candidate, so there is nothing to degrade to and an offline miss is
        recorded as an error.
        """
        assert req.version is not None
        if req.kind is FetchKind.METADATA:
            if offline:
                self.index.store_metadata(req.package, req.version, None, req.url)
            else:
                self.index.store_metadata_error(req.package, req.version, exc, req.url)
        elif req.kind is FetchKind.RANGE_METADATA:
            assert req.url is not None
            if offline:
                # A cold offline miss is a rung miss: fall through to the sdist.
                self.index.store_range_absent(req.package, req.version, req.url)
            else:
                # A malformed blob or an unserveable advertised wheel fails
                # the resolve; record the per-wheel error.
                self.index.store_range_error(req.package, req.version, req.url, exc)
        elif req.kind is FetchKind.SDIST:
            if offline:
                self.index.store_sdist_metadata(req.package, req.version, None)
            else:
                self.index.store_sdist_metadata_error(req.package, req.version, exc)
        elif offline and req.kind is FetchKind.SDIST_ARCHIVE:
            self.index.store_sdist_archive(req.package, req.version, None)
        else:
            self.index.store_sdist_archive_error(req.package, req.version, exc)

    async def _fetch_listing(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
    ) -> None:
        files = await client.get_files(req.package)
        # Record the serving index before store_listing fires the pending
        # event: a waiter released by the event reads serving_index with no
        # further synchronisation to apply per-index policy to the listing
        # filter (mirrors the sdist pyproject store-before-fire ordering).
        self._record_serving_index(client, req.package)
        self.index.store_listing(
            req.package,
            files,
            unreadable_only=client.served_unreadable_only(req.package),
        )
        logger.debug("fetched listing: %s (%d files)", req.package, len(files))
        if self._on_fetch is not None:
            self._on_fetch()

        # Auto-prefetch metadata for the newest candidates (files are
        # oldest-first). One wheel per version: the first with a sidecar is the
        # one the provider picks for that version's metadata.
        first_wheel: dict[str, WheelFile] = {}
        for f in files:
            if isinstance(f, WheelFile) and f.has_metadata:
                first_wheel.setdefault(f.version, f)

        newest = list(first_wheel.values())[-self.PREFETCH_METADATA_COUNT :]
        for w in newest:
            url = w.metadata_url
            assert url is not None
            self.request_metadata(req.package, w.version, url, w.metadata_hash)

    def _record_serving_index(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        package: str,
    ) -> None:
        """Record which configured index served ``package``'s listing.

        Single-client configurations always serve from the lone
        configured index.  Multi-client configurations consult the
        router's per-package route cache; an empty cache (e.g. an
        index returned no listing on every walk) falls back to the
        first configured index name so consumers always see
        something.
        """
        if isinstance(client, MultiIndexClient):
            routed = client.route_for(package)
            name = routed if routed is not None else self.indexes[0].name
        else:
            name = self.indexes[0].name
        self.index.store_listing_index(package, name)

    async def _fetch_metadata(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
    ) -> None:
        assert req.version is not None
        if req.url is None:
            self.index.store_metadata(req.package, req.version, None)
            return
        text = await client.get_metadata_text(
            req.package, req.version, req.url, req.metadata_hash
        )
        self.index.store_metadata(req.package, req.version, text, req.url)
        logger.debug("fetched metadata: %s %s", req.package, req.version)

    async def _fetch_range_metadata(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
    ) -> None:
        """Recover a sidecar-less wheel's METADATA over an HTTP range read.

        A read that returns text stores it in the wheel's own slot; a read
        that returns none (ranges unsupported, no METADATA member) records the
        outcome and fires the waiter with no slot, so the provider steps to the
        sdist rung.  A malformed blob or an unserveable wheel URL raises out of
        the client and is caught by :meth:`_handle` as a fetch failure.
        """
        assert req.version is not None
        assert req.url is not None
        result = await client.get_range_metadata(
            req.package,
            req.version,
            req.url,
            canonicalize_name_boundary(req.package),
            req.wheel_hashes,
        )
        self.index.store_range_outcome(
            req.package, req.version, req.url, result.outcome
        )
        if result.text is None:
            self.index.store_range_absent(req.package, req.version, req.url)
        else:
            self.index.store_range_metadata(
                req.package, req.version, req.url, result.text
            )
        logger.debug(
            "range metadata: %s %s (%s)",
            req.package,
            req.version,
            result.outcome.value,
        )

    async def _fetch_sdist(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
    ) -> None:
        assert req.version is not None
        assert req.url is not None
        pkg_info, pyproject = await client.get_sdist_files(
            req.package, req.version, req.url, req.sdist_hashes
        )
        # Store pyproject.toml first: store_sdist_metadata fires the
        # pending event, and a released waiter reads the pyproject slot
        # with no further synchronisation.
        if pyproject is not None:
            self.index.store_sdist_pyproject(req.package, req.version, pyproject)
        self.index.store_sdist_metadata(req.package, req.version, pkg_info)

    async def _fetch_sdist_archive(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
    ) -> None:
        assert req.version is not None
        if req.url is None:
            self.index.store_sdist_archive(req.package, req.version, None)
            return
        data = await client.get_sdist_archive(
            req.package, req.version, req.url, req.sdist_hashes
        )
        self.index.store_sdist_archive(req.package, req.version, data)

    async def _fetch_direct_archive(self, req: FetchRequest) -> None:
        """Read an archive declared by URL, by that URL's own scheme."""
        assert req.version is not None
        assert req.url is not None

        if urlsplit(req.url).scheme == "file":
            data = parse_file_url(req.url).read_bytes()
        else:
            if self._offline:
                msg = f"archive fetch unavailable in offline mode ({req.url})"
                raise OfflineError(msg)
            response = await self._transport.get(req.url, headers=IDENTITY_HEADERS)
            raise_unless_ok(response, req.url)
            data = response.content

        self.index.store_sdist_archive(req.package, req.version, data)
