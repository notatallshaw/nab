"""Tests for FetchCoordinator with mocked HTTP."""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from nab_index.cache import NullCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import WheelFile
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.local_index import LocalIndexClient
from nab_index.multi_index import IndexConfig, MultiIndexClient
from nab_python.fetch import (
    FetchCoordinator,
    FetchKind,
    FetchRequest,
    IndexOverride,
    InMemoryIndex,
    _resolve_overrides,
)

if TYPE_CHECKING:
    import asyncio


def _coord(**kwargs: object) -> FetchCoordinator:
    """Build a FetchCoordinator wired to httpx so respx can mock it."""
    return FetchCoordinator(transport=HttpxAsyncTransport(), **kwargs)  # type: ignore[arg-type]


def _make_wheel(name: str = "foo", version: str = "1.0") -> WheelFile:
    """Build a minimal WheelFile for InMemoryIndex round-trip tests."""
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=False,
        upload_time=None,
    )


class TestInMemoryIndex:
    def test_listing_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_listing("foo") is None
        wheels = [_make_wheel("foo", v) for v in ("1.0", "2.0", "3.0")]
        idx.store_listing("foo", wheels)
        assert idx.get_listing("foo") == wheels

    def test_metadata_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_metadata("foo", "1.0") is None
        assert not idx.has_metadata("foo", "1.0")
        idx.store_metadata("foo", "1.0", "Metadata-Version: 2.1")
        assert idx.get_metadata("foo", "1.0") == "Metadata-Version: 2.1"
        assert idx.has_metadata("foo", "1.0")

    def test_store_metadata_none(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", None)
        assert idx.has_metadata("foo", "1.0")
        assert idx.get_metadata("foo", "1.0") is None

    def test_pending_event_set_on_listing(self) -> None:
        idx = InMemoryIndex()
        pending, existed = idx.get_or_create_pending("listing:foo")
        assert not existed
        assert not pending.event.is_set()
        wheels = [_make_wheel("foo")]
        idx.store_listing("foo", wheels)
        assert pending.event.is_set()
        assert pending.result == wheels

    def test_pending_event_set_on_metadata(self) -> None:
        idx = InMemoryIndex()
        pending, existed = idx.get_or_create_pending("metadata:foo:1.0")
        assert not existed
        idx.store_metadata("foo", "1.0", "text")
        assert pending.event.is_set()
        assert pending.result == "text"

    def test_get_or_create_existing(self) -> None:
        idx = InMemoryIndex()
        p1, existed1 = idx.get_or_create_pending("key")
        p2, existed2 = idx.get_or_create_pending("key")
        assert not existed1
        assert existed2
        assert p1 is p2

    def test_store_sdist_metadata_fires_sdist_pending(self) -> None:
        idx = InMemoryIndex()
        pending, _ = idx.get_or_create_pending("sdist:foo:1.0")
        assert not pending.event.is_set()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert pending.event.is_set()
        assert idx.get_metadata("foo", "1.0") == "PKG-INFO\n"

    def test_store_sdist_metadata_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", None)
        assert idx.has_metadata("foo", "1.0")
        assert idx.get_metadata("foo", "1.0") is None

    def test_sdist_archive_pending_event_fires(self) -> None:
        idx = InMemoryIndex()
        pending, _ = idx.get_or_create_pending("sdist-archive:foo:1.0")
        assert not pending.event.is_set()
        idx.store_sdist_archive("foo", "1.0", b"bytes")
        assert pending.event.is_set()
        assert pending.result == b"bytes"
        assert idx.get_sdist_archive("foo", "1.0") == b"bytes"

    def test_sdist_archive_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_archive("foo", "1.0", None)
        assert idx.get_sdist_archive("foo", "1.0") is None

    def test_parsed_metadata_roundtrip(self) -> None:
        """``store_parsed_metadata`` and ``get_parsed_metadata`` round-trip."""
        idx = InMemoryIndex()
        sentinel = object()
        assert idx.get_parsed_metadata("foo", "1.0") is None
        idx.store_parsed_metadata("foo", "1.0", sentinel)
        assert idx.get_parsed_metadata("foo", "1.0") is sentinel

    def test_parsed_metadata_per_version(self) -> None:
        """Each ``(package, version)`` pair has its own slot."""
        idx = InMemoryIndex()
        idx.store_parsed_metadata("foo", "1.0", "v1")
        idx.store_parsed_metadata("foo", "2.0", "v2")
        assert idx.get_parsed_metadata("foo", "1.0") == "v1"
        assert idx.get_parsed_metadata("foo", "2.0") == "v2"

    def test_resolved_sdist_metadata_roundtrip(self) -> None:
        """``store_resolved_sdist_metadata`` round-trips through ``get``."""
        idx = InMemoryIndex()
        sentinel = object()
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is None
        idx.store_resolved_sdist_metadata("foo", "1.0", sentinel)
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is sentinel

    def test_pop_parsed_metadata_invalidates_resolved(self) -> None:
        """Popping the raw parse drops the post-reconciliation entry too.

        Downstream code reads the reconciled value when raising the
        bundled-pyproject fallback or after a PEP 517 build; if the
        raw text is replaced (re-resolve), the reconciliation must be
        recomputed against the new text.
        """
        idx = InMemoryIndex()
        idx.store_parsed_metadata("foo", "1.0", "raw")
        idx.store_resolved_sdist_metadata("foo", "1.0", "resolved")
        idx.pop_parsed_metadata("foo", "1.0")
        assert idx.get_parsed_metadata("foo", "1.0") is None
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is None

    def test_listing_index_roundtrip(self) -> None:
        """``store_listing_index`` records the serving index name, and
        ``get_listing_index`` returns it on lookup or ``None`` when no
        index has served the package yet.
        """
        idx = InMemoryIndex()
        assert idx.get_listing_index("foo") is None
        idx.store_listing_index("foo", "primary")
        assert idx.get_listing_index("foo") == "primary"


LISTING_JSON = {
    "meta": {"api-version": "1.0"},
    "name": "testpkg",
    "files": [
        {
            "filename": "testpkg-1.0-py3-none-any.whl",
            "url": "https://files.example.com/testpkg-1.0-py3-none-any.whl",
            "requires-python": ">=3.8",
            "data-dist-info-metadata": {"sha256": "abc"},
        },
        {
            "filename": "testpkg-2.0-py3-none-any.whl",
            "url": "https://files.example.com/testpkg-2.0-py3-none-any.whl",
            "requires-python": ">=3.8",
            "data-dist-info-metadata": {"sha256": "def"},
        },
    ],
}

METADATA_TEXT = "Metadata-Version: 2.1\nName: testpkg\nVersion: 1.0\n"


class TestFetchCoordinator:
    def test_context_manager(self) -> None:
        with _coord() as coord:
            assert coord._started
        assert not coord._started

    def test_start_idempotent(self) -> None:
        coord = _coord()
        coord.start()
        thread1 = coord._thread
        coord.start()
        assert coord._thread is thread1
        coord.shutdown()

    @respx.mock
    def test_request_listing(self) -> None:
        respx.get("https://pypi.org/simple/testpkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        with _coord() as coord:
            event = coord.request_listing("testpkg")
            event.wait(timeout=5)
            listing = coord.index.get_listing("testpkg")
            assert listing is not None
            assert len(listing) >= 2

    @respx.mock
    def test_request_listing_cached(self) -> None:
        with _coord() as coord:
            coord.index.store_listing("cached", ["data"])
            event = coord.request_listing("cached")
            assert event.is_set()

    @respx.mock
    def test_request_listing_deduplicates(self) -> None:
        route = respx.get("https://pypi.org/simple/testpkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        with _coord() as coord:
            e1 = coord.request_listing("testpkg")
            e2 = coord.request_listing("testpkg")
            e1.wait(timeout=5)
            assert e1 is e2
            assert route.call_count == 1

    @respx.mock
    def test_request_metadata(self) -> None:
        respx.get("https://files.example.com/testpkg-1.0.whl.metadata").mock(
            return_value=httpx.Response(200, text=METADATA_TEXT)
        )
        with _coord() as coord:
            event = coord.request_metadata(
                "testpkg", "1.0", "https://files.example.com/testpkg-1.0.whl.metadata"
            )
            event.wait(timeout=5)
            text = coord.index.get_metadata("testpkg", "1.0")
            assert text == METADATA_TEXT

    @respx.mock
    def test_request_metadata_cached(self) -> None:
        with _coord() as coord:
            coord.index.store_metadata("pkg", "2.0", "cached")
            event = coord.request_metadata("pkg", "2.0", "https://x.com/m")
            assert event.is_set()

    @respx.mock
    def test_request_wheel_metadata_fetches_with_sentinel_key(self) -> None:
        """The per-wheel fetch caches under ``f"{version}#{filename}"``."""
        respx.get(
            "https://files.example.com/testpkg-1.0-py3-none-any.whl.metadata"
        ).mock(return_value=httpx.Response(200, text=METADATA_TEXT))
        with _coord() as coord:
            event = coord.request_wheel_metadata(
                "testpkg",
                "1.0",
                "testpkg-1.0-py3-none-any.whl",
                "https://files.example.com/testpkg-1.0-py3-none-any.whl.metadata",
            )
            event.wait(timeout=5)
            sentinel = "1.0#testpkg-1.0-py3-none-any.whl"
            assert coord.index.has_metadata("testpkg", sentinel)
            # The plain-version cache stays untouched; the per-wheel
            # fetch exists precisely so the resolver-time cache does
            # not get clobbered.
            assert not coord.index.has_metadata("testpkg", "1.0")

    @respx.mock
    def test_request_wheel_metadata_cached(self) -> None:
        """Repeating the per-wheel fetch returns an already-set event."""
        with _coord() as coord:
            sentinel = "2.0#pkg-2.0-py3-none-any.whl"
            coord.index.store_metadata("pkg", sentinel, "cached")
            event = coord.request_wheel_metadata(
                "pkg",
                "2.0",
                "pkg-2.0-py3-none-any.whl",
                "https://x.com/m",
            )
            assert event.is_set()

    @respx.mock
    def test_request_wheel_metadata_deduplicates(self) -> None:
        """A second concurrent per-wheel fetch reuses the in-flight event."""
        route = respx.get(
            "https://files.example.com/dup-1.0-py3-none-any.whl.metadata"
        ).mock(return_value=httpx.Response(200, text=METADATA_TEXT))
        with _coord() as coord:
            args = (
                "dup",
                "1.0",
                "dup-1.0-py3-none-any.whl",
                "https://files.example.com/dup-1.0-py3-none-any.whl.metadata",
            )
            e1 = coord.request_wheel_metadata(*args)
            e2 = coord.request_wheel_metadata(*args)
            assert e1 is e2
            e1.wait(timeout=5)
            assert route.call_count == 1

    @respx.mock
    def test_request_metadata_none_url(self) -> None:
        """Metadata request with None url stores None."""
        with _coord() as coord:
            # Put a request with None url directly on the queue
            from nab_python.fetch import FetchKind, FetchRequest

            pending, _ = coord.index.get_or_create_pending("metadata:pkg:1.0")
            coord._submit(
                FetchRequest(
                    kind=FetchKind.METADATA, package="pkg", version="1.0", url=None
                )
            )
            pending.event.wait(timeout=5)
            assert coord.index.has_metadata("pkg", "1.0")
            assert coord.index.get_metadata("pkg", "1.0") is None

    @respx.mock
    def test_request_metadata_batch(self) -> None:
        respx.get("https://f.com/a.metadata").mock(
            return_value=httpx.Response(200, text="meta-a")
        )
        respx.get("https://f.com/b.metadata").mock(
            return_value=httpx.Response(200, text="meta-b")
        )
        with _coord() as coord:
            results = coord.request_metadata_batch(
                [
                    ("pkg-a", "1.0", "https://f.com/a.metadata"),
                    ("pkg-b", "2.0", "https://f.com/b.metadata"),
                ]
            )
            assert len(results) == 2
            for _pkg, _ver, event in results:
                event.wait(timeout=5)
            assert coord.index.get_metadata("pkg-a", "1.0") == "meta-a"
            assert coord.index.get_metadata("pkg-b", "2.0") == "meta-b"

    @respx.mock
    def test_batch_skips_cached(self) -> None:
        with _coord() as coord:
            coord.index.store_metadata("cached", "1.0", "already")
            results = coord.request_metadata_batch(
                [
                    ("cached", "1.0", "https://f.com/c.metadata"),
                ]
            )
            assert len(results) == 1
            _pkg, _ver, event = results[0]
            assert event.is_set()

    @respx.mock
    def test_listing_triggers_metadata_prefetch(self) -> None:
        listing = {
            "meta": {"api-version": "1.0"},
            "name": "pkg",
            "files": [
                {
                    "filename": "pkg-3.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-3.0-py3-none-any.whl",
                    "data-dist-info-metadata": {"sha256": "x"},
                },
                {
                    "filename": "pkg-2.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-2.0-py3-none-any.whl",
                    "data-dist-info-metadata": {"sha256": "y"},
                },
                {
                    "filename": "pkg-1.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-1.0-py3-none-any.whl",
                    "data-dist-info-metadata": {"sha256": "z"},
                },
            ],
        }
        respx.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json=listing)
        )
        respx.get(url__regex=r".*\.whl\.metadata$").mock(
            return_value=httpx.Response(200, text="Metadata-Version: 2.1\n")
        )
        with _coord() as coord:
            event = coord.request_listing("pkg")
            event.wait(timeout=5)
            # Auto-prefetch should have fired for the newest 3 wheels
            # Give them a moment to complete
            import time

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if coord.index.has_metadata("pkg", "3.0"):
                    break
                time.sleep(0.01)
            assert coord.index.has_metadata("pkg", "3.0")

    @respx.mock
    def test_fetch_error_logged_not_raised(self) -> None:
        respx.get("https://pypi.org/simple/bad/").mock(return_value=httpx.Response(500))
        with _coord() as coord:
            event = coord.request_listing("bad")
            # The event won't be set because the fetch failed,
            # but the coordinator shouldn't crash
            event.wait(timeout=2)
            assert not coord._crashed

    @respx.mock
    def test_sdist_fetch_failure_records_empty(self) -> None:
        """When sdist extraction errors, store_sdist_metadata(None) unblocks
        any waiter and the coordinator does not crash."""
        respx.get("https://files.example.com/broken.tar.gz").mock(
            return_value=httpx.Response(500)
        )
        with _coord() as coord:
            event = coord.request_sdist(
                "broken", "1.0", "https://files.example.com/broken.tar.gz"
            )
            event.wait(timeout=5)
            assert not coord._crashed
            # store_sdist_metadata(None) was called via the failure handler.
            assert coord.index.get_metadata("broken", "1.0") is None

    def test_crashed_raises(self) -> None:
        coord = _coord()
        coord._crashed = True
        with pytest.raises(RuntimeError, match="crashed"):
            coord.request_listing("foo")

    @respx.mock
    def test_dispatch_single_and_batch(self) -> None:
        """Dispatch handles both single FetchRequest and list."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="ok"))
        with _coord() as coord:
            e1 = coord.request_metadata("a", "1", "https://f.com/a")
            results = coord.request_metadata_batch(
                [
                    ("b", "1", "https://f.com/b"),
                    ("c", "1", "https://f.com/c"),
                ]
            )
            e1.wait(timeout=5)
            for _, _, ev in results:
                ev.wait(timeout=5)
            assert coord.index.get_metadata("a", "1") == "ok"
            assert coord.index.get_metadata("b", "1") == "ok"
            assert coord.index.get_metadata("c", "1") == "ok"

    def test_shutdown_when_never_started(self) -> None:
        """shutdown() is a no-op when the coordinator was never started."""
        coord = _coord()
        assert coord._thread is None
        coord.shutdown()  # should not raise
        assert coord._thread is None

    def test_shutdown_with_thread_but_no_queue(self) -> None:
        """shutdown() handles thread set but queue not yet initialized."""
        coord = _coord()
        # Simulate the window between thread creation and queue setup.
        dummy = threading.Thread(target=lambda: None)
        dummy.start()
        dummy.join()
        coord._thread = dummy
        coord._async_q = None
        coord._loop = None
        coord.shutdown()
        assert coord._thread is None

    def test_run_loop_exception_sets_crashed(self) -> None:
        """If _async_fetcher raises, _run_loop catches it and sets _crashed."""
        coord = _coord()

        async def _boom() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        coord._async_fetcher = _boom  # type: ignore[assignment]
        coord._run_loop()
        assert coord._crashed is True

    @respx.mock
    def test_drain_queue_empty_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """QueueEmpty during drain loop breaks out of the loop."""
        import asyncio
        import time

        respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="ok"))

        triggered = threading.Event()

        class RiggedQueue(asyncio.Queue):
            """Queue that, once armed, lies once on empty() to force the race."""

            def __init__(self) -> None:
                super().__init__()
                self._origin_empty = super().empty
                self._origin_get_nowait = super().get_nowait
                self.armed = False

            def empty(self) -> bool:
                result = self._origin_empty()
                if result and self.armed:
                    self.armed = False
                    return False
                return result

            def get_nowait(self) -> FetchRequest | list[FetchRequest] | None:
                # Consult the real deque so the lie above can't trick us
                # into popping from an empty queue. Reaching this branch
                # is exactly the race the drain loop must handle.
                if self._origin_empty():
                    triggered.set()
                    raise asyncio.QueueEmpty
                return self._origin_get_nowait()

        monkeypatch.setattr("nab_python.fetch.asyncio.Queue", RiggedQueue)
        with _coord() as coord:
            real_dispatch = coord._dispatch

            def arm_after_dispatch(
                item: FetchRequest | list[FetchRequest],
                client: CachedAsyncSimpleClient | MultiIndexClient,
                sem: asyncio.Semaphore,
                tasks: set[asyncio.Task],
            ) -> None:
                real_dispatch(item, client, sem, tasks)
                coord._async_q.armed = True  # type: ignore[union-attr]

            coord._dispatch = arm_after_dispatch  # type: ignore[method-assign]
            coord.request_metadata("pkg", "1.0", "https://f.com/pkg")
            time.sleep(0.3)

        assert triggered.is_set()
        assert not coord._crashed

    @respx.mock
    def test_gather_pending_tasks_on_shutdown(self) -> None:
        """Pending tasks are gathered when shutdown sentinel arrives."""
        import time

        # Use a slow response so tasks are still pending when None arrives.
        async def slow_response(request: httpx.Request) -> httpx.Response:
            import asyncio as _asyncio

            await _asyncio.sleep(0.5)
            return httpx.Response(200, text="slow")

        respx.get(url__regex=r".*").mock(side_effect=slow_response)
        with _coord() as coord:
            # Submit requests that will take time to complete.
            for i in range(3):
                coord.request_metadata(f"pkg{i}", "1.0", f"https://f.com/pkg{i}")
            # Give the fetcher a moment to pick them up and start tasks.
            time.sleep(0.1)
        # Exiting the context manager calls shutdown. The gather on
        # line 274 runs because tasks exist when the main loop ends.
        assert not coord._crashed

    @respx.mock
    def test_shutdown_during_drain(self) -> None:
        """None sentinel found during drain loop cancels tasks and returns."""
        import asyncio as _asyncio

        async def slow_response(request: httpx.Request) -> httpx.Response:
            await _asyncio.sleep(10)
            return httpx.Response(200, text="slow")

        respx.get(url__regex=r".*").mock(side_effect=slow_response)

        coord = _coord()

        # Hook _dispatch to inject extra items + the shutdown sentinel
        # into the queue right after the first dispatch. The fetcher
        # thread will then drain those items and hit the None branch.
        real_dispatch = coord._dispatch
        injected = [False]

        def hooked_dispatch(
            item: FetchRequest | list[FetchRequest],
            client: CachedAsyncSimpleClient | MultiIndexClient,
            sem: asyncio.Semaphore,
            tasks: set[asyncio.Task],
        ) -> None:
            real_dispatch(item, client, sem, tasks)
            if not injected[0]:
                injected[0] = True
                queue = coord._async_q
                assert queue is not None
                queue.put_nowait(
                    FetchRequest(
                        kind=FetchKind.METADATA,
                        package="drain",
                        version="1.0",
                        url="https://f.com/drain",
                    )
                )
                queue.put_nowait(None)

        coord._dispatch = hooked_dispatch  # type: ignore[method-assign]
        coord.start()

        coord._submit(
            FetchRequest(
                kind=FetchKind.METADATA,
                package="first",
                version="1.0",
                url="https://f.com/first",
            )
        )

        thread = coord._thread
        assert thread is not None
        thread.join(timeout=5)
        assert not thread.is_alive(), "Fetcher thread should have stopped"
        coord._thread = None
        coord._started = False
        assert not coord._crashed

    @respx.mock
    def test_request_metadata_deduplicates(self) -> None:
        """Second request_metadata for the same key reuses the pending."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="meta"))
        with _coord() as coord:
            # Pre-create a pending so the second request finds it.
            coord.index.get_or_create_pending("metadata:pkg:1.0")
            e = coord.request_metadata("pkg", "1.0", "https://f.com/m")
            # Should reuse the existing pending (existed=True path).
            e.wait(timeout=5)

    @respx.mock
    def test_request_sdist(self) -> None:
        """request_sdist downloads, extracts PKG-INFO, stores it."""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg-1.0/PKG-INFO")
            data = b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=buf.getvalue())
        )
        with _coord() as coord:
            event = coord.request_sdist(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)
            assert "Name: pkg" in (coord.index.get_metadata("pkg", "1.0") or "")

    @respx.mock
    def test_request_sdist_stores_pyproject(self) -> None:
        """request_sdist stores pyproject.toml when present in the tarball."""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in (
                (
                    "pkg-1.0/PKG-INFO",
                    b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
                ),
                ("pkg-1.0/pyproject.toml", b'[project]\nname = "pkg"\n'),
            ):
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=buf.getvalue())
        )
        with _coord() as coord:
            event = coord.request_sdist(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)
            assert coord.index.get_sdist_pyproject("pkg", "1.0") is not None

    @respx.mock
    def test_request_sdist_deduplicates(self) -> None:
        """Second request_sdist for the same key reuses the pending."""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg-1.0/PKG-INFO")
            data = b"Metadata-Version: 2.1\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        respx.get(url__regex=r".*").mock(
            return_value=httpx.Response(200, content=buf.getvalue())
        )
        with _coord() as coord:
            coord.index.get_or_create_pending("sdist:pkg:1.0")
            e = coord.request_sdist("pkg", "1.0", "https://f.com/pkg-1.0.tar.gz")
            e.wait(timeout=5)

    @respx.mock
    def test_request_sdist_archive_stores_bytes(self) -> None:
        """request_sdist_archive downloads the full archive and stores it."""
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=b"archive-bytes"),
        )
        with _coord() as coord:
            event = coord.request_sdist_archive(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)
            assert coord.index.get_sdist_archive("pkg", "1.0") == b"archive-bytes"

    @respx.mock
    def test_request_sdist_archive_deduplicates(self) -> None:
        """Second request_sdist_archive for the same key reuses the pending."""
        respx.get(url__regex=r".*").mock(
            return_value=httpx.Response(200, content=b"archive"),
        )
        with _coord() as coord:
            coord.index.get_or_create_pending("sdist-archive:pkg:1.0")
            event = coord.request_sdist_archive(
                "pkg", "1.0", "https://f.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)

    @respx.mock
    def test_request_sdist_archive_404_stores_none(self) -> None:
        """A 404 leaves the archive slot at ``None`` so the waiter unblocks."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(404))
        with _coord() as coord:
            event = coord.request_sdist_archive(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)
            assert coord.index.get_sdist_archive("pkg", "1.0") is None

    def test_fetch_sdist_archive_null_url_stores_none(self) -> None:
        """A ``FetchRequest`` with ``url=None`` short-circuits to ``None``.

        Exercises the defensive branch in ``_fetch_sdist_archive`` that
        handles requests dispatched without a URL (parallel to
        ``_fetch_metadata``'s same-shape guard).
        """
        import asyncio

        from nab_python.fetch import FetchKind, FetchRequest, InMemoryIndex

        index = InMemoryIndex()

        async def _run() -> None:
            with _coord() as coord:
                coord.index = index
                await coord._fetch_sdist_archive(
                    client=None,
                    req=FetchRequest(
                        kind=FetchKind.SDIST_ARCHIVE,
                        package="pkg",
                        version="1.0",
                        url=None,
                    ),
                )

        asyncio.run(_run())
        assert index.get_sdist_archive("pkg", "1.0") is None

    @respx.mock
    def test_request_metadata_batch_deduplicates(self) -> None:
        """Batch request skips items with existing pending."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="meta"))
        with _coord() as coord:
            # Pre-create a pending for one of the batch items.
            coord.index.get_or_create_pending("metadata:a:1.0")
            results = coord.request_metadata_batch(
                [
                    ("a", "1.0", "https://f.com/a"),
                    ("b", "1.0", "https://f.com/b"),
                ]
            )
            for _, _, ev in results:
                ev.wait(timeout=5)


class TestFetchCoordinatorCache:
    @respx.mock
    def test_cache_dir_persists_listing(self, tmp_path: object) -> None:
        """When cache_dir is set, a listing fetch writes to disk."""
        from pathlib import Path

        from nab_index.cache import OnDiskCache

        cache_dir = tmp_path  # type: ignore[assignment]
        respx.get("https://pypi.org/simple/cached/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON, headers={"etag": "v1"})
        )
        with FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=Path(str(cache_dir)),
        ) as coord:
            event = coord.request_listing("cached")
            event.wait(timeout=5)
        # Disk cache populated.
        cache = OnDiskCache(Path(str(cache_dir)), "https://pypi.org/simple/")
        assert cache.get_simple("cached") is not None

    @respx.mock
    def test_offline_with_warm_cache_no_network(self, tmp_path: object) -> None:
        """Offline mode reads from disk without hitting the transport."""
        from pathlib import Path

        from nab_index.cache import CachePolicy, OnDiskCache

        cache_dir = Path(str(tmp_path))
        cache = OnDiskCache(cache_dir, "https://pypi.org/simple/")
        cache.put_simple(
            "testpkg",
            json.dumps(LISTING_JSON).encode(),
            CachePolicy(fetched_at=0, max_age=1, etag="x"),
        )
        # No respx route registered: any HTTP call would fail.
        with FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=cache_dir,
            offline=True,
        ) as coord:
            event = coord.request_listing("testpkg")
            event.wait(timeout=5)
            listing = coord.index.get_listing("testpkg")
            assert listing is not None
            assert len(listing) == 2

    @respx.mock
    def test_offline_with_cold_cache_records_empty(self, tmp_path: object) -> None:
        """Offline + cache miss: handler records empty listing, not crash."""
        from pathlib import Path

        cache_dir = Path(str(tmp_path))
        with FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=cache_dir,
            offline=True,
        ) as coord:
            event = coord.request_listing("missing")
            event.wait(timeout=5)
            listing = coord.index.get_listing("missing")
            # Empty list, not None: the handler caught OfflineError and
            # stored an empty listing so the resolver can proceed.
            assert listing == []
        assert not coord._crashed

    def test_explicit_cache_backend_takes_precedence(self) -> None:
        """A passed-in cache_backend wins over cache_dir."""
        from nab_index.cache import NullCache

        backend = NullCache()
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=None,
            cache_backend=backend,
        )
        try:
            assert coord._cache is backend
        finally:
            coord.shutdown()

    def test_no_cache_dir_uses_null_cache(self) -> None:
        """Default constructor wires a NullCache."""
        from nab_index.cache import NullCache

        coord = FetchCoordinator(transport=HttpxAsyncTransport())
        try:
            assert isinstance(coord._cache, NullCache)
        finally:
            coord.shutdown()


class TestResolveOverrides:
    """Tests for the marker-aware override-resolution helper."""

    def test_no_overrides_no_op(self) -> None:
        assert _resolve_overrides([], None) == {}

    def test_no_marker_always_matches(self) -> None:
        result = _resolve_overrides(
            [IndexOverride("torch", "torch-cpu")],
            None,
        )
        assert result == {"torch": "torch-cpu"}

    def test_marker_matches_env(self) -> None:
        result = _resolve_overrides(
            [
                IndexOverride(
                    "torch",
                    "torch-cpu",
                    marker='platform_system == "Linux"',
                ),
            ],
            {"platform_system": "Linux"},
        )
        assert result == {"torch": "torch-cpu"}

    def test_marker_misses_env(self) -> None:
        result = _resolve_overrides(
            [
                IndexOverride(
                    "torch",
                    "torch-cpu",
                    marker='platform_system == "Linux"',
                ),
            ],
            {"platform_system": "Darwin"},
        )
        assert result == {}

    def test_first_match_wins(self) -> None:
        result = _resolve_overrides(
            [
                IndexOverride(
                    "torch",
                    "torch-cpu",
                    marker='platform_system == "Linux"',
                ),
                IndexOverride(
                    "torch",
                    "torch-rocm",
                ),
            ],
            {"platform_system": "Linux"},
        )
        assert result == {"torch": "torch-cpu"}

    def test_canonicalises_name(self) -> None:
        result = _resolve_overrides(
            [IndexOverride("My-Pkg", "alt")],
            None,
        )
        assert "my-pkg" in result

    def test_marker_required_but_no_env_drops(self) -> None:
        result = _resolve_overrides(
            [IndexOverride("torch", "alt", marker='os_name == "posix"')],
            None,
        )
        assert result == {}


class TestMultiIndexCoordinator:
    """Tests for FetchCoordinator with multiple indexes + overrides."""

    @respx.mock
    def test_secondary_index_consulted_on_primary_miss(self) -> None:
        """A package missing on the first index is fetched from the next."""
        respx.get("https://pypi.org/simple/torch/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"api-version": "1.0"},
                    "name": "torch",
                    "files": [],
                },
            )
        )
        respx.get("https://torch.example/cpu/torch/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"api-version": "1.0"},
                    "name": "torch",
                    "files": [
                        {
                            "filename": "torch-2.0-py3-none-any.whl",
                            "url": "https://torch.example/cpu/torch-2.0.whl",
                        },
                    ],
                },
            )
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            _coord(
                cache_dir=Path(tmp),
                indexes=[
                    IndexConfig("pypi", "https://pypi.org/simple/"),
                    IndexConfig("torch-cpu", "https://torch.example/cpu/"),
                ],
            ) as coord,
        ):
            event = coord.request_listing("torch")
            event.wait(timeout=5)
            listing = coord.index.get_listing("torch")
            assert listing is not None
            assert len(listing) == 1
            assert listing[0].filename == "torch-2.0-py3-none-any.whl"

    @respx.mock
    def test_index_override_strict_pin(self) -> None:
        """An override routes a package to a single index regardless of order."""
        respx.get("https://pypi.org/simple/torch/").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://torch.example/cpu/torch/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"api-version": "1.0"},
                    "name": "torch",
                    "files": [
                        {
                            "filename": "torch-2.0-py3-none-any.whl",
                            "url": "https://torch.example/cpu/torch-2.0.whl",
                        },
                    ],
                },
            )
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            _coord(
                cache_dir=Path(tmp),
                indexes=[
                    IndexConfig("pypi", "https://pypi.org/simple/"),
                    IndexConfig("torch-cpu", "https://torch.example/cpu/"),
                ],
                index_overrides=[IndexOverride("torch", "torch-cpu")],
            ) as coord,
        ):
            event = coord.request_listing("torch")
            event.wait(timeout=5)
            # The first index was never consulted for torch; the
            # override routed straight to torch-cpu.
            listing = coord.index.get_listing("torch")
            assert listing is not None
            assert len(listing) == 1

    def test_explicit_cache_backend_with_multi_index_raises(self) -> None:
        """cache_backend + multi-index is a config error."""
        with pytest.raises(ValueError, match="more than one"):
            FetchCoordinator(
                transport=HttpxAsyncTransport(),
                cache_backend=NullCache(),
                indexes=[
                    IndexConfig("pypi", "https://pypi.org/simple/"),
                    IndexConfig("alt", "https://x/"),
                ],
            )

    def test_default_indexes_is_pypi(self) -> None:
        """Without explicit indexes, the coordinator defaults to PyPI."""
        coord = FetchCoordinator(transport=HttpxAsyncTransport())
        try:
            client = coord._build_client()
            assert isinstance(client, CachedAsyncSimpleClient)
        finally:
            coord.shutdown()

    def test_duplicate_index_names_raises(self) -> None:
        """Duplicate index names are a config error."""
        with pytest.raises(ValueError, match="duplicate index names"):
            FetchCoordinator(
                transport=HttpxAsyncTransport(),
                indexes=[
                    IndexConfig("pypi", "https://pypi.org/simple/"),
                    IndexConfig("pypi", "https://other/"),
                ],
            )

    def test_empty_indexes_raises(self) -> None:
        """Empty indexes list is a config error."""
        with pytest.raises(ValueError, match="at least one"):
            FetchCoordinator(transport=HttpxAsyncTransport(), indexes=[])

    def test_local_index(self, tmp_path: Path) -> None:
        """A file:// index uses LocalIndexClient."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            indexes=[
                IndexConfig("pypi", "https://pypi.org/simple/"),
                IndexConfig("local", wheelhouse.as_uri()),
            ],
        )
        try:
            client = coord._build_client()
            assert isinstance(client, MultiIndexClient)
            assert isinstance(client._clients["local"], LocalIndexClient)
        finally:
            coord.shutdown()

    def test_single_index_short_circuit(self, tmp_path: Path) -> None:
        """Single index + no overrides returns a plain client."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[IndexConfig("custom", "https://custom.example/")],
        )
        try:
            client = coord._build_client()
            assert isinstance(client, CachedAsyncSimpleClient)
        finally:
            coord.shutdown()

    def test_override_targets_unknown_index_raises(self, tmp_path: Path) -> None:
        """Override referencing an undeclared index is rejected."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[IndexConfig("pypi", "https://pypi.org/simple/")],
            index_overrides=[IndexOverride("torch", "no-such-index")],
        )
        try:
            with pytest.raises(ValueError, match="unknown index names"):
                coord._build_client()
        finally:
            coord.shutdown()
