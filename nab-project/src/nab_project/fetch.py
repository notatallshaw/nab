"""Channel-based async I/O coordinator for nab-project.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from packaging.utils import canonicalize_name as canonicalize_name_boundary

from nab_index.cache import CacheBackend, NullCache, OfflineError, OnDiskCache
from nab_index.cached_client import (
    CachedAsyncSimpleClient,
    ParsedCacheStats,
    SdistArchiveHold,
    read_fresh_parsed_listing,
)
from nab_index.lazy_wheel import RangeCapabilityMemo
from nab_index.local_index import LocalIndexClient, is_file_url, parse_file_url
from nab_index.multi_index import MultiIndexClient
from nab_index.transport import IDENTITY_HEADERS, raise_unless_ok
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.metadata import static_project_from_table
from nab_provider.policy import BuildPolicy
from nab_provider.records import (
    DEFAULT_INDEX_NAME,
    DEFAULT_INDEX_URL,
    IndexConfig,
    SdistFile,
    WheelFile,
)
from nab_provider.serialization import SimpleSerialization
from nab_provider.store import InMemoryIndex, metadata_pending_key, range_pending_key

from ._build_remote import build_remote_sdist
from ._sources import materialize_source
from ._toml import parse_pyproject_table

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from typing_extensions import Self

    from nab_index.parsed_listing import ParsedListing

__all__ = [
    "DEFAULT_INDEX_NAME",
    "DEFAULT_INDEX_URL",
    "FetchCoordinator",
    "FetchKind",
    "FetchRequest",
    "InMemoryIndex",
    "IndexRoute",
    "WarmSyncStats",
    "index_cache_floors",
    "index_routes",
]


# Maximum time the main thread waits for the fetcher thread to drain
# its queue and exit on :meth:`FetchCoordinator.shutdown`.
# How many fetches a resolve opens at once when its caller names no bound.
DEFAULT_MAX_CONCURRENCY = 50

_COORDINATOR_JOIN_TIMEOUT_SECONDS = 10

# A warm listing whose parsed blob is smaller than this is declined to the
# async path instead of served inline: serving a small blob inline exposes
# main-thread selection work with no round trip to offset it, while a large
# blob's materialize dominates that overhead.
_WARM_SYNC_MIN_BLOB_BYTES = 64 * 1024

if TYPE_CHECKING:
    from pathlib import Path

    from nab_index.transport import AsyncHttpTransport
    from nab_provider.metadata import WheelMetadata
    from nab_provider.policy import SourceRequest

    from .inputs import ResolveInputs

logger = logging.getLogger(__name__)


def _done_event() -> threading.Event:
    """Return an already-set event, for a request answered without waiting."""
    event = threading.Event()
    event.set()
    return event


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


def index_routes(inputs: ResolveInputs) -> list[IndexRoute]:
    """Project the routing package overrides into coordinator :class:`IndexRoute`s.

    Each per-package override that sets ``index`` contributes one route,
    keyed by its bare package name.  A routing entry always uses a
    bare-name requirement (parse-time guarantee), and the parse-time
    non-overlap check forbids two routes for one package, so the resulting
    route map has at most one entry per name.
    """
    return [
        IndexRoute(name=override.name, index=override.index)
        for override in inputs.package_overrides
        if override.index is not None
    ]


def index_cache_floors(inputs: ResolveInputs) -> dict[str, int]:
    """Project per-index cache-freshness floors, keyed by index name."""
    return {
        name: override.assume_fresh_seconds
        for name, override in inputs.index_overrides.items()
        if override.assume_fresh_seconds is not None
    }


def _builds_remote_sdists(inputs: ResolveInputs | None) -> bool:
    """Whether ``inputs`` names ``build-remote`` anywhere.

    Coarser than :meth:`~nab_provider.provider.Provider.effective_build_policy`,
    which decides per version. Holding sdist archives is a whole-run decision,
    so an upper bound on what could reach a build is enough.
    """
    if inputs is None:
        return False
    if inputs.build_policy is BuildPolicy.BUILD_REMOTE:
        return True
    overrides = (*inputs.package_overrides, *inputs.index_overrides.values())
    return any(o.build_policy is BuildPolicy.BUILD_REMOTE for o in overrides)


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
class WarmSyncStats:
    """Counters for the synchronous warm-hit listing path.

    A sync hit bumps ``listing_hits``; a decline bumps ``listing_declines`` and
    one reason sub-counter, then runs the async path. Written only from the main
    resolver thread, so the counters need no lock.
    """

    listing_hits: int = 0
    listing_declines: int = 0
    declined_ineligible: int = 0
    declined_no_policy: int = 0
    declined_stale_online: int = 0
    # No blob, or one the read helper would not serve: a foreign build, a
    # non-binding digest, corruption, or a blob holding no records.
    declined_no_blob: int = 0
    declined_small_blob: int = 0


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

    PREFETCH_METADATA_COUNT = 1

    def __init__(  # noqa: PLR0913 - the per-index knobs a coordinator wires up
        self,
        transport: AsyncHttpTransport,
        *,
        indexes: list[IndexConfig] | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        cache_dir: Path | None = None,
        cache_backend: CacheBackend | None = None,
        offline: bool = False,
        index_routes: list[IndexRoute] | None = None,
        index_cache_floors: Mapping[str, int] | None = None,
        on_fetch: Callable[[], None] | None = None,
        build_config: ResolveInputs | None = None,
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

        ``index_cache_floors`` maps an index name to a read-time
        freshness floor in seconds, passed to that index's cached client
        as ``min_fresh_seconds``.  Indexes absent from the map, and the
        ``file://`` local client, get no floor.

        ``build_config`` is the settings a :pep:`517` build runs under;
        a caller that resolves without building leaves it ``None``.

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
            # Serialization-partitioned like the per-index backend
            # _build_index_client builds, so the warm-sync probe reads the same
            # directory the fetcher's client writes.
            self._cache = OnDiskCache(
                cache_dir, indexes[0].url, serialization=indexes[0].serialization
            )
        else:
            self._cache = NullCache()
        self._cache_dir = cache_dir
        self._build_config = build_config
        self._index_routes = list(index_routes or [])
        self._index_cache_floors = dict(index_cache_floors or {})
        # The sync warm-hit path serves one shape only: a single non-file index
        # over an OnDiskCache, unrouted, so the serving index is always
        # indexes[0] and the probe reads the same backend the fetcher's client
        # reads. Everything else declines to the async path.
        self._sync_listing_enabled = (
            len(self.indexes) == 1
            and not self._index_routes
            and not is_file_url(self.indexes[0].url)
            and isinstance(self._cache, OnDiskCache)
        )
        # Progress hook: fired once per successful listing fetch, from the
        # fetcher thread.  ``None`` when nothing is watching (the common case).
        self._on_fetch = on_fetch
        self.index = InMemoryIndex()
        # Shared per-run range-capability memo.  Rebuilt fresh on the fetcher
        # loop in _async_fetcher so each run starts with empty per-netloc state;
        # built here too so the attribute is always a real memo for typing.
        self._range_memo = RangeCapabilityMemo()
        # Shared per-run parsed-listing cache counters, injected into every
        # index client the same way; rebuilt on the fetcher loop so each run
        # starts at zero.
        self._parsed_cache_stats = ParsedCacheStats()
        # Only a resolve that can build a remote sdist holds archives, so
        # nothing else pays the memory.
        self._holds_sdist_archives = _builds_remote_sdists(build_config)
        self._sdist_archive_hold: SdistArchiveHold | None = None
        self._warm_sync_stats = WarmSyncStats()
        self._warm_sync_min_blob_bytes = _WARM_SYNC_MIN_BLOB_BYTES
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

    @property
    def parsed_cache_stats(self) -> ParsedCacheStats:
        """Parsed-listing cache counters for this run, shared by every index client.

        Read after a run to confirm a warm resolve serves parsed blobs. The sink
        is rebuilt at the start of each fetcher loop, so the counts reflect the
        most recent run.
        """
        return self._parsed_cache_stats

    @property
    def warm_sync_stats(self) -> WarmSyncStats:
        """Synchronous warm-hit listing counters, reset at each ``start()``."""
        return self._warm_sync_stats

    def start(self) -> None:
        """Start the fetcher thread (idempotent)."""
        if self._started:
            return
        self._started = True
        self._warm_sync_stats = WarmSyncStats()
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

    def _post_to_loop(self, callback: Callable[..., object], *args: object) -> bool:
        """Hand ``callback`` to the fetcher loop from any thread.

        Returns ``False`` when there is no live loop (never started, or closed
        between the read and the post) so the caller can run the work inline.
        """
        loop = self._loop
        if loop is None:
            return False
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            # call_soon_threadsafe raises RuntimeError once the loop is closed.
            return False
        return True

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

    def _try_listing_sync(self, package: str) -> ParsedListing | None:
        """Read a fresh parsed listing on the caller's thread, or ``None``.

        ``read_fresh_parsed_listing`` never raises, so the caller's pending is
        never stranded. On a decline the policy is re-read to attribute a reason
        counter rather than threading it out of the pure helper.
        """
        stats = self._warm_sync_stats
        if not self._sync_listing_enabled:
            stats.declined_ineligible += 1
            return None
        parsed = read_fresh_parsed_listing(self._cache, package, offline=self._offline)
        if parsed is not None:
            return parsed
        policy = self._cache.get_simple_policy(package)
        if policy is None:
            stats.declined_no_policy += 1
        elif not (policy.is_fresh() or self._offline):
            stats.declined_stale_online += 1
        else:
            stats.declined_no_blob += 1
        return None

    def _overlap_gate_admits(self, package: str) -> bool:
        """Whether a warm listing may be served inline, or must go async.

        A parsed blob below ``_WARM_SYNC_MIN_BLOB_BYTES`` declines to the async
        path. A blob whose size cannot be stat'd, or a run where the sync path is
        disabled, is admitted and left to ``_try_listing_sync`` to classify.
        """
        if not self._sync_listing_enabled:
            return True
        size = self._cache.get_simple_parsed_size(package)
        if size is None:
            return True
        return size >= self._warm_sync_min_blob_bytes

    def request_listing(
        self, package: str, *, speculative: bool = False
    ) -> threading.Event:
        """Request a listing fetch; return an event set when the result lands.

        The single-flight pending is claimed first: an existing pending means
        another party owns fulfillment, so its event is joined and the cache is
        never probed. Only the pending's creator probes; a fresh parsed hit is
        served inline, and every other outcome declines to the async fetch, which
        owns every cache write and self-heal.

        ``speculative`` callers skip the sync probe and dispatch async, so their
        read work overlaps resolver CPU on the fetcher thread; only blocking
        critical-path callers serve inline.
        """
        self._check_alive()
        if self.index.get_listing(package) is not None:
            done = threading.Event()
            done.set()
            return done
        key = f"listing:{package}"
        event, existed = self.index.get_or_create_pending(key)
        if existed:
            return event
        if not speculative:
            if self._overlap_gate_admits(package):
                parsed = self._try_listing_sync(package)
                if parsed is not None:
                    records = parsed.files
                    # Store the serving index before store_listing fires the
                    # pending, matching the async path's ordering. The tail runs
                    # on the fetcher loop, or inline when the loop is gone.
                    self.index.store_listing_index(package, self.indexes[0].name)
                    self.index.store_listing(
                        package, records, zip_sdists=parsed.zip_sdists
                    )
                    self._warm_sync_stats.listing_hits += 1
                    if not self._post_to_loop(self._run_listing_tail, package, records):
                        self._run_listing_tail(package, records)
                    return event
            else:
                self._warm_sync_stats.declined_small_blob += 1
            self._warm_sync_stats.listing_declines += 1
        self._submit(FetchRequest(kind=FetchKind.LISTING, package=package))
        return event

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
        key = metadata_pending_key(package, version, url)
        event, existed = self.index.get_or_create_pending(key)
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
        return event

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
        key = range_pending_key(package, version, wheel_url)
        event, existed = self.index.get_or_create_pending(key)
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
        return event

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
        event, existed = self.index.get_or_create_pending(key)
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
        return event

    def request_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        """Request the raw bytes of an sdist archive.

        Used by the :attr:`~nab_provider.policy.BuildPolicy.BUILD_REMOTE`
        path.  Stored under a separate pending key so it can run
        concurrently with a PKG-INFO fetch for the same version. The
        fetcher verifies the downloaded archive against ``sdist_hashes``
        before storing the bytes.
        """
        self._check_alive()
        key = f"sdist-archive:{package}:{version}"
        event, existed = self.index.get_or_create_pending(key)
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
        return event

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
        event, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(
                FetchRequest(
                    kind=FetchKind.DIRECT_ARCHIVE,
                    package=package,
                    version=version,
                    url=url,
                )
            )
        return event

    def request_source_listing(self, request: SourceRequest) -> threading.Event:
        """Materialise a declared source and store what it declared.

        Run inline rather than on the fetcher thread: an archive source waits
        on :meth:`request_direct_archive`, which the fetcher thread serves, and
        a dynamic source runs a build backend.
        """
        self._check_alive()
        self.index.store_source(
            request.package,
            materialize_source(self, request, self._build_config),
        )
        return _done_event()

    def request_built_metadata(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...],
    ) -> threading.Event:  # pragma: no cover (tar data filter)
        """Build the sdist at ``url`` and store the METADATA it produced.

        Run inline, for the same reason as :meth:`request_source_listing`: the
        build waits on :meth:`request_sdist_archive`, which the fetcher thread
        serves.
        """
        self._check_alive()
        built: WheelMetadata = build_remote_sdist(
            self, package, version, url, sdist_hashes, self._build_config
        )
        self.index.store_built_metadata(package, version, built)
        return _done_event()

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
                results.append((package, version, _done_event()))
                continue
            key = metadata_pending_key(package, version, url)
            event, existed = self.index.get_or_create_pending(key)
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
            results.append((package, version, event))
        if batch:
            self._submit(batch)
        return results

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._async_fetcher())
        except BaseException as exc:
            self._record_crash(exc)
            logger.exception("Fetcher thread crashed")
            # A KeyboardInterrupt or a SystemExit still ends the thread.
            if not isinstance(exc, Exception):
                raise

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
        Any freshness floor registered for ``cfg.name`` is passed as
        ``min_fresh_seconds``.
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
            min_fresh_seconds=self._index_cache_floors.get(cfg.name),
            parsed_stats=self._parsed_cache_stats,
            sdist_archive_hold=self._sdist_archive_hold,
        )

    async def _async_fetcher(self) -> None:
        # Fresh per-run memo, owned on this single loop thread, injected into
        # every client _build_client constructs below.
        self._range_memo = RangeCapabilityMemo()
        # Fresh per-run parsed-listing counters, injected the same way, so a
        # reused coordinator starts each run at zero.
        self._parsed_cache_stats = ParsedCacheStats()
        # Held archives are capped at the fetcher's in-flight width; a build
        # past that downloads its own archive.
        self._sdist_archive_hold = (
            SdistArchiveHold(self._max_concurrency)
            if self._holds_sdist_archives
            else None
        )

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

            # Nothing can take from the hold once the loop is gone.
            if self._sdist_archive_hold is not None:
                self._sdist_archive_hold.clear()

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

        A metadata rung marks the skip before it stores, so a waiter released
        by the store reads the mark and not a bare empty slot.
        """
        assert req.version is not None
        if req.kind is FetchKind.METADATA:
            if offline:
                assert req.url is not None
                self.index.record_offline_metadata_miss(
                    req.package, req.version, req.url
                )
                self.index.store_metadata(
                    req.package, req.version, None, metadata_url=req.url
                )
            else:
                self.index.store_metadata_error(req.package, req.version, exc, req.url)
        elif req.kind is FetchKind.RANGE_METADATA:
            assert req.url is not None
            if offline:
                # A cold offline miss is a rung miss: fall through to the sdist.
                self.index.record_offline_metadata_miss(
                    req.package, req.version, req.url
                )
                self.index.store_range_absent(req.package, req.version, req.url)
            else:
                # A malformed blob or an unserveable advertised wheel fails
                # the resolve; record the per-wheel error.
                self.index.store_range_error(req.package, req.version, req.url, exc)
        elif req.kind is FetchKind.SDIST:
            if offline:
                assert req.url is not None
                self.index.record_offline_metadata_miss(
                    req.package, req.version, req.url
                )
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
            unreachable_only=client.served_unreachable_only(req.package),
            no_usable_file=client.served_no_usable_file(req.package),
            all_yanked=client.served_all_yanked(req.package),
            zip_sdists=client.served_zip_sdists(req.package),
        )
        logger.debug("fetched listing: %s (%d files)", req.package, len(files))
        if self._on_fetch is not None:
            self._on_fetch()
        self._prefetch_metadata_after_listing(req.package, files)

    def _prefetch_metadata_after_listing(
        self, package: str, files: Sequence[WheelFile | SdistFile]
    ) -> None:
        """Enqueue metadata for the newest candidates of a stored listing.

        Assumes a listing is oldest-first and keeps a version's files together,
        which nab does not enforce. An index that interleaves versions warms an
        older release, costing a request rather than a wrong resolve.

        One wheel per version: the first with a sidecar is the one the provider
        picks for that version's metadata. The backwards walk assigns
        unconditionally, so that first wheel is the one left in place.
        """
        wanted = self.PREFETCH_METADATA_COUNT
        newest: dict[str, WheelFile] = {}
        for f in reversed(files):
            if not (isinstance(f, WheelFile) and f.has_metadata):
                continue
            if f.version not in newest and len(newest) == wanted:
                break
            newest[f.version] = f

        for w in reversed(newest.values()):
            url = w.metadata_url
            assert url is not None
            self.request_metadata(package, w.version, url, w.metadata_hash)

    def _run_listing_tail(
        self, package: str, records: Sequence[WheelFile | SdistFile]
    ) -> None:
        """Run the post-listing tail on the fetcher loop: tick then prefetch.

        Mirrors the tail of the async ``_fetch_listing``. A failure is swallowed
        rather than turned into a listing error: the pending has already fired,
        so the served listing must not be overwritten.
        """
        try:
            if self._on_fetch is not None:
                self._on_fetch()
            self._prefetch_metadata_after_listing(package, records)
        except Exception as exc:  # noqa: BLE001
            logger.warning("listing prefetch tail failed: %s: %s", package, exc)

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

        The router also says whether a routing override chose that index,
        which the name alone cannot: an empty walk records the first
        configured index too.  A route over a lone index holds no other
        index back, so it is not recorded as a pin.
        """
        if isinstance(client, MultiIndexClient):
            routed = client.route_for(package)
            name = routed if routed is not None else self.indexes[0].name
            pinned = len(self.indexes) > 1 and client.pinned_index(package) is not None
        else:
            name = self.indexes[0].name
            pinned = False
        self.index.store_listing_index(package, name, pinned=pinned)

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
        self.index.store_metadata(req.package, req.version, text, metadata_url=req.url)
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
            table = parse_pyproject_table(pyproject)
            self.index.store_sdist_pyproject(req.package, req.version, table)
            self._release_archive_if_deps_are_static(req.package, req.version, table)
        self.index.store_sdist_metadata(req.package, req.version, pkg_info)

    def _release_archive_if_deps_are_static(
        self, package: str, version: str, table: Mapping[str, Any] | None
    ) -> None:
        """Release a held archive when ``table`` already declares the deps.

        The hold exists for a build, and the metadata ladder builds only when
        the bundled ``[project]`` table cannot supply the dependencies, so a
        table that can means no build will ask for this archive.  A ``None``
        table is a pyproject that did not parse and declares nothing, so the
        archive stays held.
        """
        if self._sdist_archive_hold is None or table is None:
            return
        if static_project_from_table(table) is not None:
            self._sdist_archive_hold.take(package, version)

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
