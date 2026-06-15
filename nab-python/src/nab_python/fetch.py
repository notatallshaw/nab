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

from nab_index.cache import CacheBackend, NullCache, OfflineError, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import SdistFile, WheelFile
from nab_index.local_index import LocalIndexClient
from nab_index.multi_index import IndexConfig, MultiIndexClient

from ._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Sequence

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
    """Distinguishes the four kinds of fetches the coordinator handles."""

    LISTING = "listing"
    METADATA = "metadata"
    SDIST = "sdist"
    SDIST_ARCHIVE = "sdist-archive"


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


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None


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
        self._metadata: dict[tuple[str, str], str | None] = {}
        # Keys whose ``_metadata`` slot was last written from an sdist
        # PKG-INFO rather than a wheel METADATA; readers need the
        # origin because only sdist deps go through the PEP 643 gate.
        self._metadata_from_sdist: set[tuple[str, str]] = set()
        self._sdist_pyproject: dict[tuple[str, str], str | None] = {}
        self._sdist_archives: dict[tuple[str, str], bytes | None] = {}
        self._pending: dict[str, _Pending] = {}

        # Parsed metadata is a pure function of the underlying text, so we
        # share it across tuple providers in universal mode.
        self._parsed_metadata: dict[tuple[str, str], Any] = {}

        # Post-reconciliation sdist metadata: the result after
        # PEP 643 dynamic deps have been resolved via the bundled
        # pyproject.toml fallback or a PEP 517 backend invocation.
        # Shared across tuples so universal mode does not re-augment
        # (or, more importantly, re-build) the same sdist N times.
        self._resolved_sdist_metadata: dict[tuple[str, str], Any] = {}

    def get_listing(self, package: str) -> list[WheelFile | SdistFile] | None:
        """Return the cached listing for ``package``, or ``None``."""
        with self._lock:
            return self._listings.get(package)

    def store_listing(
        self, package: str, data: Sequence[WheelFile | SdistFile]
    ) -> None:
        """Cache the listing for ``package`` and unblock any waiter.

        ``data`` is accepted as a Sequence (covariant) so callers can pass
        homogeneous ``list[WheelFile]`` lists; it is materialised into the
        internal ``list[WheelFile | SdistFile]`` cache.
        """
        key = f"listing:{package}"
        materialised = list(data)
        with self._lock:
            self._listings[package] = materialised
            pending = self._pending.get(key)
        if pending is not None:
            pending.result = materialised
            pending.event.set()

    def store_listing_error(self, package: str, error: BaseException) -> None:
        """Record a failed listing fetch and unblock any waiter.

        Distinct from ``store_listing([])``: an empty listing means the
        index served no files (a 404), while an error means the fetch
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

    def get_metadata(self, package: str, version: str) -> str | None:
        """Return cached metadata text, or ``None`` if not yet stored."""
        with self._lock:
            key = (package, version)
            if key in self._metadata:
                return self._metadata[key]
            return None

    def has_metadata(self, package: str, version: str) -> bool:
        """Return ``True`` once a metadata fetch has resolved (any value)."""
        with self._lock:
            return (package, version) in self._metadata

    def store_metadata(self, package: str, version: str, data: str | None) -> None:
        """Cache metadata text (or ``None`` for a failed fetch)."""
        key = f"metadata:{package}:{version}"
        with self._lock:
            self._metadata[(package, version)] = data
            self._metadata_from_sdist.discard((package, version))
            pending = self._pending.get(key)
        if pending is not None:
            pending.result = data
            pending.event.set()

    def store_sdist_metadata(
        self, package: str, version: str, data: str | None
    ) -> None:
        """Store sdist-derived PKG-INFO under the same metadata key.

        Wheel and sdist results land in the same ``_metadata`` slot
        because PKG-INFO is core-metadata-equivalent. The pending
        keys differ so a sdist request can run in parallel with (or
        after) a failed wheel metadata request.
        :meth:`metadata_from_sdist` reports which kind the slot holds.
        """
        key = f"sdist:{package}:{version}"
        with self._lock:
            self._metadata[(package, version)] = data
            self._metadata_from_sdist.add((package, version))
            pending = self._pending.get(key)
        if pending is not None:
            pending.result = data
            pending.event.set()

    def metadata_from_sdist(self, package: str, version: str) -> bool:
        """Return ``True`` when the metadata slot was last written from an sdist.

        The slot itself cannot distinguish wheel METADATA from sdist
        PKG-INFO; readers that apply the :pep:`643` dynamic-deps gate
        only to sdist values ask here for the current text's origin.
        """
        with self._lock:
            return (package, version) in self._metadata_from_sdist

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

    def get_or_create_pending(self, key: str) -> tuple[_Pending, bool]:
        """Return (pending, already_existed)."""
        with self._lock:
            if key in self._pending:
                return self._pending[key], True
            pending = _Pending()
            self._pending[key] = pending
            return pending, False

    def get_parsed_metadata(self, package: str, version: str) -> Any | None:
        """Return the cached parsed :class:`WheelMetadata` or ``None``.

        Unlike :meth:`get_metadata` (which returns the raw text),
        this returns the already-parsed dataclass.  Callers populate
        the cache by calling :meth:`store_parsed_metadata` after
        parsing the text once.
        """
        with self._lock:
            return self._parsed_metadata.get((package, version))

    def store_parsed_metadata(self, package: str, version: str, metadata: Any) -> None:
        """Cache a parsed :class:`WheelMetadata` for future tuple lookups.

        Safe across tuples because the parsed object is read-only and
        a pure function of the underlying text.  Per-tuple
        classification (marker eval, extras admission) happens
        above this cache.
        """
        with self._lock:
            self._parsed_metadata[(package, version)] = metadata

    def pop_parsed_metadata(self, package: str, version: str) -> Any | None:
        """Remove and return the cached parsed metadata for one key.

        Returns ``None`` when no entry was present.  Used by the
        re-resolve path so that overriding the raw text invalidates
        the parsed view that downstream metadata classification reads.
        """
        with self._lock:
            popped = self._parsed_metadata.pop((package, version), None)
            # An override invalidates the reconciled view too: the
            # post-augment / post-build metadata is downstream of the
            # raw parse and must not survive when the parse is replaced.
            self._resolved_sdist_metadata.pop((package, version), None)
            return popped

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
        ``indexes`` is rejected: each index needs its own cache.
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
        if cache_backend is not None:
            self._cache: CacheBackend = cache_backend
        elif cache_dir is not None:
            self._cache = OnDiskCache(cache_dir, indexes[0].url)
        else:
            self._cache = NullCache()
        self._cache_dir = cache_dir
        self._index_routes = list(index_routes or [])
        self.index = InMemoryIndex()
        self._thread: threading.Thread | None = None
        self._started = False
        self._crashed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_q: asyncio.Queue[_QueueItem] | None = None
        self._queue_ready = threading.Event()

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
        """Schedule ``item`` on the fetcher loop's queue from any thread."""
        loop = self._loop
        queue = self._async_q
        if loop is None or queue is None:
            return
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def _check_alive(self) -> None:
        if self._crashed:
            msg = "Fetcher thread crashed, see log for details"
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
        """Request a wheel-metadata fetch for ``(package, version)``."""
        self._check_alive()
        if self.index.has_metadata(package, version):
            done = threading.Event()
            done.set()
            return done
        key = f"metadata:{package}:{version}"
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

    def request_wheel_metadata(
        self,
        package: str,
        version: str,
        wheel_filename: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> threading.Event:
        """Request metadata for one specific wheel of ``(package, version)``.

        Used by the universal-resolution validation pass to fetch each
        tuple's chosen wheel metadata separately from the resolver's
        baseline.  Cached under the sentinel key ``f"{version}#{wheel_filename}"``
        so the resolver-time cache (keyed on plain ``version``) is
        untouched.  Goes through the same async transport and shares
        connection pooling.
        """
        self._check_alive()
        sentinel_version = f"{version}#{wheel_filename}"
        if self.index.has_metadata(package, sentinel_version):
            done = threading.Event()
            done.set()
            return done
        key = f"metadata:{package}:{sentinel_version}"
        pending, existed = self.index.get_or_create_pending(key)
        if not existed:
            self._submit(
                FetchRequest(
                    kind=FetchKind.METADATA,
                    package=package,
                    version=sentinel_version,
                    url=url,
                    metadata_hash=metadata_hash,
                )
            )
        return pending.event

    def request_sdist(self, package: str, version: str, url: str) -> threading.Event:
        """Request sdist PKG-INFO extraction.

        Uses a separate ``sdist:`` pending key so it can fire even
        when a prior wheel metadata fetch already cached ``None``
        for the same ``(package, version)`` slot.
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
                )
            )
        return pending.event

    def request_sdist_archive(
        self, package: str, version: str, url: str
    ) -> threading.Event:
        """Request the raw bytes of an sdist archive.

        Used by the :class:`~nab_python.provider.BuildPolicy.BUILD_REMOTE`
        path; the bytes are extracted to a temp dir and handed to a
        PEP 517 backend.  Stored under a separate pending key so it can
        run concurrently with a PKG-INFO fetch for the same version.
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
            if self.index.has_metadata(package, version):
                done = threading.Event()
                done.set()
                results.append((package, version, done))
                continue
            key = f"metadata:{package}:{version}"
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
        except Exception:
            logger.exception("Fetcher thread crashed")
            self._crashed = True

    def _build_client(
        self,
    ) -> CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient:
        """Return the index client for this run.

        Single-index configurations return a plain
        :class:`CachedAsyncSimpleClient` (or a :class:`LocalIndexClient`
        for ``file://``).  Multi-index configurations wire up a
        :class:`MultiIndexClient` whose underlying clients share the
        coordinator's transport but get their own per-URL cache.
        """
        override_map = _resolve_routes(self._index_routes)
        if len(self.indexes) == 1 and not override_map:
            cfg = self.indexes[0]
            return self._build_index_client(cfg.url)
        clients_by_name: dict[str, CachedAsyncSimpleClient | LocalIndexClient] = {}
        for cfg in self.indexes:
            clients_by_name[cfg.name] = self._build_index_client(cfg.url)
        order = [cfg.name for cfg in self.indexes]
        return MultiIndexClient(
            clients_by_name,
            order,
            override_map,
        )

    def _build_index_client(
        self,
        url: str,
    ) -> CachedAsyncSimpleClient | LocalIndexClient:
        """Build a single index client for ``url``.

        ``file://`` URLs go to :class:`LocalIndexClient` (no caching;
        the filesystem is the cache).  Everything else goes to
        :class:`CachedAsyncSimpleClient` with a per-URL
        :class:`OnDiskCache` when ``cache_dir`` is set.
        """
        if url.startswith("file://"):
            return LocalIndexClient(url)
        backend: CacheBackend
        if self._cache_dir is not None:
            backend = OnDiskCache(self._cache_dir, url)
        else:
            backend = self._cache
        return CachedAsyncSimpleClient(
            self._transport,
            backend,
            url,
            offline=self._offline,
        )

    async def _async_fetcher(self) -> None:
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        self._async_q = queue
        self._queue_ready.set()

        sem = asyncio.Semaphore(self._max_concurrency)
        tasks: set[asyncio.Task] = set()

        client = self._build_client()
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
                else:
                    await self._fetch_sdist_archive(client, req)
            except Exception as exc:
                logger.exception("Fetch failed: %s %s", req.kind.value, req.package)
                # Record a result so any waiter unblocks; without it the
                # resolver deadlocks on event.wait().
                if req.kind is FetchKind.LISTING:
                    # Offline + cold cache is a deliberate empty listing
                    # (the resolver proceeds with no candidates); any other
                    # failure is stored as an error so fetch_versions can
                    # surface it instead of reporting no candidates.
                    if isinstance(exc, OfflineError):
                        self.index.store_listing(req.package, [])
                        self._record_serving_index(client, req.package)
                    else:
                        self.index.store_listing_error(req.package, exc)
                else:
                    assert req.version is not None
                    if req.kind is FetchKind.METADATA:
                        self.index.store_metadata(req.package, req.version, None)
                    elif req.kind is FetchKind.SDIST:
                        self.index.store_sdist_metadata(req.package, req.version, None)
                    else:
                        self.index.store_sdist_archive(req.package, req.version, None)

    async def _fetch_listing(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
    ) -> None:
        files = await client.get_files(req.package)
        self.index.store_listing(req.package, files)
        self._record_serving_index(client, req.package)

        # auto-prefetch metadata for newest candidates (files are oldest-first)
        wheels_with_meta: list[tuple[WheelFile, str]] = [
            (f, f.metadata_url)
            for f in files
            if isinstance(f, WheelFile) and f.metadata_url is not None
        ]
        for w, metadata_url in wheels_with_meta[-self.PREFETCH_METADATA_COUNT :]:
            self.request_metadata(req.package, w.version, metadata_url, w.metadata_hash)

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
        self.index.store_metadata(req.package, req.version, text)

    async def _fetch_sdist(
        self,
        client: CachedAsyncSimpleClient | LocalIndexClient | MultiIndexClient,
        req: FetchRequest,
    ) -> None:
        assert req.version is not None
        assert req.url is not None
        pkg_info, pyproject = await client.get_sdist_files(
            req.package, req.version, req.url
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
        data = await client.get_sdist_archive(req.package, req.version, req.url)
        self.index.store_sdist_archive(req.package, req.version, data)
