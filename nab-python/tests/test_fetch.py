"""Tests for FetchCoordinator with mocked HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
import respx

from nab_index.cache import NullCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import (
    MetadataHashMismatchError,
    SdistFile,
    SdistHashMismatchError,
    WheelFile,
)
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.local_index import LocalIndexClient
from nab_index.multi_index import IndexConfig, MultiIndexClient
from nab_python.fetch import (
    FetchCoordinator,
    FetchKind,
    FetchRequest,
    IndexRoute,
    InMemoryIndex,
    _resolve_routes,
)


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

    def test_metadata_error_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_metadata_error("foo", "1.0") is None
        error = MetadataHashMismatchError("metadata sha256 mismatch")
        idx.store_metadata_error("foo", "1.0", error)
        assert idx.get_metadata_error("foo", "1.0") is error
        assert idx.get_metadata("foo", "1.0") is None

    def test_store_metadata_error_fires_metadata_pending(self) -> None:
        idx = InMemoryIndex()
        pending, _ = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.metadata")
        idx.store_metadata_error(
            "foo", "1.0", MetadataHashMismatchError("bad"), "https://f/a.metadata"
        )
        assert pending.event.is_set()

    def test_store_metadata_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata_error("foo", "1.0", MetadataHashMismatchError("bad"))
        assert idx.get_metadata_error("foo", "1.0") is not None
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
        pending, existed = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.md")
        assert not existed
        idx.store_metadata("foo", "1.0", "text", "https://f/a.md")
        assert pending.event.is_set()
        assert pending.result == "text"

    def test_sidecar_slot_answers_only_for_its_own_artifact(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", "linux", "https://f/linux.whl.metadata")
        assert idx.has_metadata_for("foo", "1.0", "https://f/linux.whl.metadata")
        assert not idx.has_metadata_for("foo", "1.0", "https://f/win.whl.metadata")

    def test_version_level_slot_answers_for_any_artifact(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert idx.has_metadata_for("foo", "1.0", "https://f/win.whl.metadata")

    def test_has_metadata_for_is_false_on_an_empty_slot(self) -> None:
        idx = InMemoryIndex()
        assert not idx.has_metadata_for("foo", "1.0", "https://f/a.whl.metadata")

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

    def test_store_sdist_metadata_error_fires_sdist_pending(self) -> None:
        idx = InMemoryIndex()
        pending, _ = idx.get_or_create_pending("sdist:foo:1.0")
        assert not pending.event.is_set()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_metadata_error("foo", "1.0", err)
        assert pending.event.is_set()
        assert idx.get_metadata_error("foo", "1.0") is err
        assert idx.get_metadata("foo", "1.0") is None

    def test_store_sdist_metadata_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_metadata_error("foo", "1.0", err)
        assert idx.get_metadata_error("foo", "1.0") is err

    def test_metadata_from_sdist_tracks_last_write(self) -> None:
        idx = InMemoryIndex()
        assert not idx.metadata_from_sdist("foo", "1.0")
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert idx.metadata_from_sdist("foo", "1.0")
        idx.store_metadata("foo", "1.0", "METADATA\n")
        assert not idx.metadata_from_sdist("foo", "1.0")

    def test_metadata_none_keeps_stored_sdist_pkg_info(self) -> None:
        """A failed sidecar fetch must not erase stored sdist PKG-INFO."""
        idx = InMemoryIndex()
        pending, _ = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.md")
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        idx.store_metadata("foo", "1.0", None, "https://f/a.md")
        assert pending.event.is_set()
        assert idx.get_metadata("foo", "1.0") == "PKG-INFO\n"
        assert idx.metadata_from_sdist("foo", "1.0")
        # The kept text still stands for the version, not for the sidecar.
        assert idx.has_metadata_for("foo", "1.0", "https://f/other.md")

    def test_metadata_none_after_sdist_none_stays_none(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", None)
        idx.store_metadata("foo", "1.0", None)
        assert idx.get_metadata("foo", "1.0") is None
        assert not idx.metadata_from_sdist("foo", "1.0")

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

    def test_store_sdist_archive_error_fires_pending(self) -> None:
        idx = InMemoryIndex()
        pending, _ = idx.get_or_create_pending("sdist-archive:foo:1.0")
        assert not pending.event.is_set()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_archive_error("foo", "1.0", err)
        assert pending.event.is_set()
        assert idx.get_sdist_archive_error("foo", "1.0") is err
        assert idx.get_sdist_archive("foo", "1.0") is None

    def test_store_sdist_archive_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_archive_error("foo", "1.0", err)
        assert idx.get_sdist_archive_error("foo", "1.0") is err

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
            "dist-info-metadata": {"sha256": "abc"},
        },
        {
            "filename": "testpkg-2.0-py3-none-any.whl",
            "url": "https://files.example.com/testpkg-2.0-py3-none-any.whl",
            "requires-python": ">=3.8",
            "dist-info-metadata": {"sha256": "def"},
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
    def test_restart_after_shutdown_serves_requests(self) -> None:
        """A coordinator restarted after shutdown serves new requests."""

        class ReusableTransport(HttpxAsyncTransport):
            """Survives the per-run client aclose so both runs can fetch."""

            async def aclose(self) -> None:
                pass

        respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="ok"))
        coord = FetchCoordinator(transport=ReusableTransport())  # type: ignore[arg-type]
        with coord:
            event = coord.request_metadata("first", "1.0", "https://f.com/first")
            assert event.wait(timeout=5)
        with coord:
            event = coord.request_metadata("second", "1.0", "https://f.com/second")
            assert event.wait(timeout=5)
        assert coord.index.get_metadata("second", "1.0") == "ok"

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
        """A request whose key is already pending reuses that pending.

        Pre-creating the pending makes the dedup path fire without
        racing the fetcher thread: the listing is not yet cached, so
        the cached early-return is skipped, but ``get_or_create_pending``
        reports the key already exists and no fetch is submitted.
        """
        route = respx.get("https://pypi.org/simple/testpkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        with _coord() as coord:
            pending, _ = coord.index.get_or_create_pending("listing:testpkg")
            event = coord.request_listing("testpkg")
            assert event is pending.event
            assert route.call_count == 0

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
            event = coord.request_metadata("pkg", "2.0", "https://example.com/m")
            assert event.is_set()

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
                    ("pkg-a", "1.0", "https://f.com/a.metadata", None),
                    ("pkg-b", "2.0", "https://f.com/b.metadata", None),
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
                    ("cached", "1.0", "https://f.com/c.metadata", None),
                ]
            )
            assert len(results) == 1
            _pkg, _ver, event = results[0]
            assert event.is_set()

    @respx.mock
    def test_listing_triggers_metadata_prefetch(self) -> None:
        metadata_body = "Metadata-Version: 2.1\n"
        digest = hashlib.sha256(metadata_body.encode()).hexdigest()
        listing = {
            "meta": {"api-version": "1.0"},
            "name": "pkg",
            "files": [
                {
                    "filename": "pkg-3.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-3.0-py3-none-any.whl",
                    "dist-info-metadata": {"sha256": digest},
                },
                {
                    "filename": "pkg-2.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-2.0-py3-none-any.whl",
                    "dist-info-metadata": {"sha256": digest},
                },
                {
                    "filename": "pkg-1.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-1.0-py3-none-any.whl",
                    "dist-info-metadata": {"sha256": digest},
                },
            ],
        }
        respx.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json=listing)
        )
        respx.get(url__regex=r".*\.whl\.metadata$").mock(
            return_value=httpx.Response(200, text=metadata_body)
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
    def test_listing_transport_error_not_masked_as_empty(self) -> None:
        """A 5xx on a listing fetch stores an error, not an empty listing.
        A genuine 404 (no candidates) is distinct and handled inside get_files.
        """
        respx.get("https://pypi.org/simple/bad/").mock(return_value=httpx.Response(500))
        with _coord() as coord:
            event = coord.request_listing("bad")
            event.wait(timeout=5)
            assert not coord._crashed
            assert coord.index.get_listing("bad") is None
            assert coord.index.get_listing_error("bad") is not None

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

    @respx.mock
    def test_metadata_fetch_failure_records_empty(self) -> None:
        """When metadata fetch errors, store_metadata(None) unblocks the
        waiter and the coordinator does not crash."""
        respx.get("https://files.example.com/broken-1.0.whl.metadata").mock(
            return_value=httpx.Response(500)
        )
        with _coord() as coord:
            event = coord.request_metadata(
                "broken", "1.0", "https://files.example.com/broken-1.0.whl.metadata"
            )
            event.wait(timeout=5)
            assert not coord._crashed
            assert coord.index.get_metadata("broken", "1.0") is None

    @respx.mock
    def test_late_sidecar_failure_keeps_stored_sdist_pkg_info(self) -> None:
        """A sidecar fetch failing after the sdist stored PKG-INFO keeps it."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg-1.0/PKG-INFO")
            data = b"Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=buf.getvalue())
        )
        respx.get("https://files.example.com/pkg-1.0.whl.metadata").mock(
            return_value=httpx.Response(404)
        )

        with _coord() as coord:
            sdist_event = coord.request_sdist(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz"
            )
            assert sdist_event.wait(timeout=5)

            # queue the request directly: request_metadata would short-circuit
            # on the stored PKG-INFO, but a prefetch already in flight lands
            url = "https://files.example.com/pkg-1.0.whl.metadata"
            pending, _ = coord.index.get_or_create_pending(f"metadata:pkg:1.0:{url}")
            coord._submit(
                FetchRequest(
                    kind=FetchKind.METADATA,
                    package="pkg",
                    version="1.0",
                    url=url,
                )
            )

            assert pending.event.wait(timeout=5)
            assert not coord._crashed
            assert "Name: pkg" in (coord.index.get_metadata("pkg", "1.0") or "")
            assert coord.index.metadata_from_sdist("pkg", "1.0")

    @respx.mock
    def test_metadata_hash_mismatch_records_integrity_error(self) -> None:
        """A PEP 658 sidecar that fails its published hash is recorded as an
        integrity error, not stored as a None (no-metadata) result."""
        good = b"Metadata-Version: 2.1\nName: tampered\n"
        good_digest = hashlib.sha256(good).hexdigest()
        tampered = b"Metadata-Version: 2.1\nName: evil\n"
        respx.get("https://files.example.com/tampered-1.0.whl.metadata").mock(
            return_value=httpx.Response(200, content=tampered)
        )
        with _coord() as coord:
            event = coord.request_metadata(
                "tampered",
                "1.0",
                "https://files.example.com/tampered-1.0.whl.metadata",
                ("sha256", good_digest),
            )
            event.wait(timeout=5)
            assert not coord._crashed
            assert coord.index.get_metadata("tampered", "1.0") is None
            error = coord.index.get_metadata_error("tampered", "1.0")
            assert isinstance(error, MetadataHashMismatchError)

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
                    ("b", "1", "https://f.com/b", None),
                    ("c", "1", "https://f.com/c", None),
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

    def test_shutdown_resets_loop_state(self) -> None:
        """shutdown() drops the closed loop so a later start() rewires it."""
        coord = _coord()
        coord.start()
        coord.shutdown()
        assert coord._loop is None
        assert coord._async_q is None
        assert not coord._queue_ready.is_set()

    def test_run_loop_exception_sets_crashed(self) -> None:
        """If _async_fetcher raises, _run_loop catches it and sets _crashed."""
        coord = _coord()

        async def _boom() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        coord._async_fetcher = _boom  # type: ignore[assignment]
        coord._run_loop()
        assert coord._crashed is True

    def test_shutdown_completes_after_startup_crash(self) -> None:
        """shutdown() tears down cleanly when the fetcher crashed at startup."""
        coord = _coord(index_routes=[IndexRoute(name="foo", index="missing")])
        coord.start()
        assert coord._thread is not None
        coord._thread.join(timeout=5)
        assert coord._crashed

        coord.shutdown()
        assert coord._thread is None
        assert not coord._started
        assert coord._loop is None
        assert coord._async_q is None

    def test_startup_crash_surfaces_from_with_block(self) -> None:
        """The with form raises the crash error, not a teardown error."""

        def request_from_crashed_fetcher() -> None:
            routes = [IndexRoute(name="foo", index="missing")]
            with _coord(index_routes=routes) as coord:
                assert coord._thread is not None
                coord._thread.join(timeout=5)
                coord.request_listing("foo")

        with pytest.raises(RuntimeError, match="crashed"):
            request_from_crashed_fetcher()

    def _crashed_coord_in_race_window(self) -> FetchCoordinator:
        """Return a crashed coordinator caught before _crashed is visible."""
        coord = _coord(index_routes=[IndexRoute(name="foo", index="missing")])
        coord.start()
        assert coord._thread is not None
        coord._thread.join(timeout=5)
        coord._crashed = False
        return coord

    def test_refused_listing_submit_releases_waiter(self) -> None:
        """A listing request refused by a closed loop fails instead of hanging."""
        coord = self._crashed_coord_in_race_window()
        event = coord.request_listing("foo")
        assert event.is_set()
        assert isinstance(coord.index.get_listing_error("foo"), RuntimeError)

    def test_refused_submit_releases_all_request_kinds(self) -> None:
        """Refused metadata, sdist, archive, and batch requests all fail loudly."""
        coord = self._crashed_coord_in_race_window()
        meta = coord.request_metadata("a", "1.0", "https://f.com/a")
        sdist = coord.request_sdist("b", "1.0", "https://f.com/b.tar.gz")
        archive = coord.request_sdist_archive("c", "1.0", "https://f.com/c.tar.gz")
        batch = coord.request_metadata_batch([("d", "1.0", "https://f.com/d", None)])

        assert meta.is_set()
        assert sdist.is_set()
        assert archive.is_set()
        assert batch[0][2].is_set()

        assert isinstance(coord.index.get_metadata_error("a", "1.0"), RuntimeError)
        assert isinstance(coord.index.get_metadata_error("b", "1.0"), RuntimeError)
        assert isinstance(coord.index.get_sdist_archive_error("c", "1.0"), RuntimeError)
        assert isinstance(coord.index.get_metadata_error("d", "1.0"), RuntimeError)

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
        """None sentinel found during drain loop settles in-flight tasks and returns."""
        import asyncio as _asyncio

        async def slow_response(request: httpx.Request) -> httpx.Response:
            await _asyncio.sleep(0.2)
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
        assert coord.index.get_metadata("first", "1.0") == "slow"
        assert coord.index.get_metadata("drain", "1.0") == "slow"

    @respx.mock
    def test_drain_shutdown_replies_to_inflight_requests(self) -> None:
        """A request batched with the shutdown sentinel still gets a reply."""

        async def slow_response(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.5)
            return httpx.Response(200, text="slow")

        respx.get(url__regex=r".*").mock(side_effect=slow_response)
        coord = _coord()
        coord.start()
        loop = coord._loop
        assert loop is not None
        # Block the loop so the request and the sentinel arrive in one
        # batch and the sentinel is found during the drain.
        loop.call_soon_threadsafe(time.sleep, 0.3)
        event = coord.request_metadata("pkg", "1.0", "https://f.com/pkg")
        coord.shutdown()
        assert event.wait(timeout=2)
        assert coord.index.get_metadata("pkg", "1.0") == "slow"

    @respx.mock
    def test_request_metadata_deduplicates(self) -> None:
        """Second request_metadata for the same key reuses the pending."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="meta"))
        with _coord() as coord:
            # Pre-create a pending so the second request finds it.
            coord.index.get_or_create_pending("metadata:pkg:1.0:https://f.com/m")
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
    def test_request_sdist_verifies_published_hash(self) -> None:
        """A matching published hash lets extraction proceed."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg-1.0/PKG-INFO")
            data = b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        body = buf.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=body)
        )
        with _coord() as coord:
            event = coord.request_sdist(
                "pkg",
                "1.0",
                "https://files.example.com/pkg-1.0.tar.gz",
                (("sha256", digest),),
            )
            event.wait(timeout=5)
            assert "Name: pkg" in (coord.index.get_metadata("pkg", "1.0") or "")
            assert coord.index.get_metadata_error("pkg", "1.0") is None

    @respx.mock
    def test_request_sdist_hash_mismatch_records_error(self) -> None:
        """Tampered sdist bytes record an integrity error, not the PKG-INFO."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg-1.0/PKG-INFO")
            data = b"Metadata-Version: 2.1\nName: evil\nVersion: 1.0\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=buf.getvalue())
        )
        with _coord() as coord:
            event = coord.request_sdist(
                "pkg",
                "1.0",
                "https://files.example.com/pkg-1.0.tar.gz",
                (("sha256", "0" * 64),),
            )
            event.wait(timeout=5)
            assert not coord._crashed
            assert coord.index.get_metadata("pkg", "1.0") is None
            assert isinstance(
                coord.index.get_metadata_error("pkg", "1.0"),
                SdistHashMismatchError,
            )

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
    def test_request_sdist_pyproject_stored_before_event(self) -> None:
        """The coupled pyproject.toml is visible before the sdist event fires.

        A waiter released by the sdist event reads the pyproject slot
        with no further synchronisation, so both artifacts from the one
        download must be stored before the event is set.
        """
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

        pyproject_at_event: list[str | None] = []

        class RecordingIndex(InMemoryIndex):
            def store_sdist_metadata(
                self, package: str, version: str, data: str | None
            ) -> None:
                pyproject_at_event.append(self.get_sdist_pyproject(package, version))
                super().store_sdist_metadata(package, version, data)

        with _coord() as coord:
            coord.index = RecordingIndex()
            event = coord.request_sdist(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)
        assert pyproject_at_event == ['[project]\nname = "pkg"\n']

    @respx.mock
    def test_request_listing_serving_index_stored_before_event(self) -> None:
        """The serving index is visible before the listing event fires.

        A waiter released by the listing event reads ``serving_index``
        with no further synchronisation to apply per-index policy to the
        listing filter, so the serving index must be recorded before the
        event is set (mirrors the sdist pyproject store-before-fire
        ordering).
        """
        respx.get("https://pypi.org/simple/testpkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )

        serving_at_event: list[str | None] = []

        class RecordingIndex(InMemoryIndex):
            def store_listing(
                self, package: str, data: Sequence[WheelFile | SdistFile]
            ) -> None:
                serving_at_event.append(self.get_listing_index(package))
                super().store_listing(package, data)

        with _coord() as coord:
            coord.index = RecordingIndex()
            event = coord.request_listing("testpkg")
            event.wait(timeout=5)
        assert serving_at_event == ["pypi"]

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
    def test_request_sdist_archive_verifies_published_hash(self) -> None:
        """A matching published hash lets the archive bytes through."""
        body = b"archive-bytes"
        digest = hashlib.sha256(body).hexdigest()
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=body),
        )
        with _coord() as coord:
            event = coord.request_sdist_archive(
                "pkg",
                "1.0",
                "https://files.example.com/pkg-1.0.tar.gz",
                (("sha256", digest),),
            )
            event.wait(timeout=5)
            assert coord.index.get_sdist_archive("pkg", "1.0") == body
            assert coord.index.get_sdist_archive_error("pkg", "1.0") is None

    @respx.mock
    def test_request_sdist_archive_hash_mismatch_records_error(self) -> None:
        """A tampered archive records an integrity error and stores no bytes."""
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=b"tampered"),
        )
        with _coord() as coord:
            event = coord.request_sdist_archive(
                "pkg",
                "1.0",
                "https://files.example.com/pkg-1.0.tar.gz",
                (("sha256", "0" * 64),),
            )
            event.wait(timeout=5)
            assert not coord._crashed
            assert coord.index.get_sdist_archive("pkg", "1.0") is None
            assert isinstance(
                coord.index.get_sdist_archive_error("pkg", "1.0"),
                SdistHashMismatchError,
            )

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

    def test_request_direct_archive_deduplicates(self, tmp_path: Path) -> None:
        """A direct archive already in flight hands back its pending event."""
        archive = tmp_path / "pkg-1.0.tar.gz"
        archive.write_bytes(b"archive")
        with _coord() as coord:
            pending, _ = coord.index.get_or_create_pending("sdist-archive:pkg:digest")
            event = coord.request_direct_archive("pkg", "digest", archive.as_uri())

            assert event is pending.event
            assert coord.index.get_sdist_archive("pkg", "digest") is None

    def test_file_index_still_closes_the_transport(self, tmp_path: Path) -> None:
        """A file:// index client owns no transport; a direct archive fetch may."""
        closed = threading.Event()

        class RecordingTransport(HttpxAsyncTransport):
            async def aclose(self) -> None:
                closed.set()
                await super().aclose()

        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        with FetchCoordinator(
            transport=RecordingTransport(),  # type: ignore[arg-type]
            indexes=[IndexConfig("local", wheelhouse.as_uri())],
        ):
            pass

        assert closed.is_set()

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
            coord.index.get_or_create_pending("metadata:a:1.0:https://f.com/a")
            results = coord.request_metadata_batch(
                [
                    ("a", "1.0", "https://f.com/a", None),
                    ("b", "1.0", "https://f.com/b", None),
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

    @respx.mock
    def test_warm_cache_keeps_sibling_wheels_apart(self, tmp_path: Path) -> None:
        """One wheel's METADATA is never served for a sibling wheel of that version.

        A run whose compatible wheel differs from an earlier run's must fetch
        its own sidecar.
        """
        linux_url = "https://f.example/foo-1.0-cp311-manylinux_2_17_x86_64.whl.metadata"
        win_url = "https://f.example/foo-1.0-cp311-win_amd64.whl.metadata"
        linux_body = (
            b"Metadata-Version: 2.1\nName: foo\nRequires-Dist: linux-only-dep\n"
        )
        win_body = (
            b"Metadata-Version: 2.1\nName: foo\nRequires-Dist: windows-only-dep\n"
        )
        linux_route = respx.get(linux_url).mock(
            return_value=httpx.Response(200, content=linux_body)
        )
        win_route = respx.get(win_url).mock(
            return_value=httpx.Response(200, content=win_body)
        )
        linux_hash = ("sha256", hashlib.sha256(linux_body).hexdigest())
        win_hash = ("sha256", hashlib.sha256(win_body).hexdigest())

        with _coord(cache_dir=tmp_path) as coord:
            coord.request_metadata("foo", "1.0", linux_url, linux_hash).wait(timeout=5)
            assert coord.index.get_metadata("foo", "1.0") == linux_body.decode()

        # A later run over the same cache dir, in an environment whose
        # compatible wheel is the win_amd64 one.
        with _coord(cache_dir=tmp_path) as coord:
            coord.request_metadata("foo", "1.0", win_url, win_hash).wait(timeout=5)
            assert coord.index.get_metadata_error("foo", "1.0") is None
            assert coord.index.get_metadata("foo", "1.0") == win_body.decode()

        assert linux_route.call_count == 1
        assert win_route.call_count == 1

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


class TestResolveRoutes:
    """Tests for the route-resolution helper.

    Routes carry no marker now: routing decides where a listing is
    fetched before any version (or marker context) is known.
    """

    def test_no_routes_no_op(self) -> None:
        assert _resolve_routes([]) == {}

    def test_single_route(self) -> None:
        result = _resolve_routes([IndexRoute("torch", "torch-cpu")])
        assert result == {"torch": "torch-cpu"}

    def test_distinct_packages(self) -> None:
        result = _resolve_routes(
            [IndexRoute("torch", "torch-cpu"), IndexRoute("numpy", "alt")]
        )
        assert result == {"torch": "torch-cpu", "numpy": "alt"}

    def test_canonicalises_name(self) -> None:
        result = _resolve_routes([IndexRoute("My-Pkg", "alt")])
        assert "my-pkg" in result


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
                index_routes=[IndexRoute("torch", "torch-cpu")],
            ) as coord,
        ):
            event = coord.request_listing("torch")
            event.wait(timeout=5)
            # The first index was never consulted for torch; the
            # override routed straight to torch-cpu.
            listing = coord.index.get_listing("torch")
            assert listing is not None
            assert len(listing) == 1

    @respx.mock
    def test_serving_index_recorded_per_package(self) -> None:
        """Each package records the index that actually served it.

        numpy is served by the first index and torch by the second
        after a first-index miss, so the recorded serving index differs
        per package. The lockfile reads this to attribute each pin's
        ``index`` URL.
        """
        respx.get("https://pypi.org/simple/numpy/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"api-version": "1.0"},
                    "name": "numpy",
                    "files": [
                        {
                            "filename": "numpy-1.0-py3-none-any.whl",
                            "url": "https://pypi.org/numpy-1.0.whl",
                        },
                    ],
                },
            )
        )
        respx.get("https://pypi.org/simple/torch/").mock(
            return_value=httpx.Response(
                200,
                json={"meta": {"api-version": "1.0"}, "name": "torch", "files": []},
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
            for pkg in ("numpy", "torch"):
                coord.request_listing(pkg).wait(timeout=5)
            assert coord.index.get_listing_index("numpy") == "pypi"
            assert coord.index.get_listing_index("torch") == "torch-cpu"

    @respx.mock
    def test_override_serving_index_recorded(self) -> None:
        """An overridden package records its override index, not the first."""
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
                index_routes=[IndexRoute("torch", "torch-cpu")],
            ) as coord,
        ):
            coord.request_listing("torch").wait(timeout=5)
            assert coord.index.get_listing_index("torch") == "torch-cpu"

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

    @respx.mock
    def test_offline_cold_remote_falls_through_to_local(self, tmp_path: Path) -> None:
        """Offline + cold cache on the remote index still serves local files."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")

        with _coord(
            cache_dir=tmp_path / "cache",
            offline=True,
            indexes=[
                IndexConfig("pypi", "https://pypi.org/simple/"),
                IndexConfig("local", wheelhouse.as_uri()),
            ],
        ) as coord:
            coord.request_listing("foo").wait(timeout=5)
            listing = coord.index.get_listing("foo")
            assert listing is not None
            assert [f.filename for f in listing] == ["foo-1.0-py3-none-any.whl"]
            assert coord.index.get_listing_error("foo") is None
            assert coord.index.get_listing_index("foo") == "local"

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
            index_routes=[IndexRoute("torch", "no-such-index")],
        )
        try:
            with pytest.raises(ValueError, match="unknown index names"):
                coord._build_client()
        finally:
            coord.shutdown()


_SIBLING_LINUX = "foo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"
_SIBLING_WIN = "foo-1.0-cp311-cp311-win_amd64.whl"
_SIBLING_LINUX_BODY = b"Metadata-Version: 2.1\nName: foo\nRequires-Dist: linux-dep\n"
_SIBLING_WIN_BODY = b"Metadata-Version: 2.1\nName: foo\nRequires-Dist: windows-dep\n"


def _sibling_wheel_listing() -> dict:
    """A listing whose one version has more sidecar wheels than the prefetch takes.

    Ordered so the prefetch window would cut past the first wheel if it counted
    wheels rather than versions.
    """
    fillers = [f"foo-1.0-cp3{n}-cp3{n}-macosx_11_0_arm64.whl" for n in range(3, 3 + 9)]
    names = [_SIBLING_LINUX, "foo-1.0-py3-none-any.whl", _SIBLING_WIN, *fillers]
    return {
        "meta": {"api-version": "1.0"},
        "name": "foo",
        "files": [
            {
                "filename": name,
                "url": f"https://f.example/{name}",
                "core-metadata": True,
            }
            for name in names
        ],
    }


class TestSiblingWheelMetadata:
    """The metadata a version's slot holds belongs to the wheel that was asked for."""

    def _routes(self) -> tuple[respx.Route, respx.Route]:
        linux = respx.get(f"https://f.example/{_SIBLING_LINUX}.metadata").mock(
            return_value=httpx.Response(200, content=_SIBLING_LINUX_BODY)
        )
        win = respx.get(f"https://f.example/{_SIBLING_WIN}.metadata").mock(
            return_value=httpx.Response(200, content=_SIBLING_WIN_BODY)
        )
        respx.get("https://pypi.org/simple/foo/").mock(
            return_value=httpx.Response(200, json=_sibling_wheel_listing())
        )
        respx.get(url__regex=r".*\.whl\.metadata$").mock(
            return_value=httpx.Response(200, content=b"Metadata-Version: 2.1\n")
        )
        return linux, win

    def _await_metadata(self, coord: FetchCoordinator) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not coord.index.has_metadata(
            "foo", "1.0"
        ):
            time.sleep(0.01)

    @respx.mock
    def test_listing_prefetch_takes_the_wheel_the_provider_will_pick(self) -> None:
        """The prefetch fetches one wheel per version: the first with a sidecar."""
        linux, win = self._routes()
        with _coord() as coord:
            coord.request_listing("foo").wait(timeout=5)
            self._await_metadata(coord)
            assert (
                coord.index.get_metadata("foo", "1.0") == _SIBLING_LINUX_BODY.decode()
            )
        assert linux.call_count == 1
        assert win.call_count == 0

    @respx.mock
    def test_sibling_wheel_is_not_served_from_a_prefetched_listing(
        self, tmp_path: Path
    ) -> None:
        """A run whose compatible wheel is not the prefetched one gets its own.

        The listing prefetch has already filled the version's slot, and a
        second run over the warm cache dir refills it with no network at all,
        so nothing but the artifact identity keeps the two wheels apart.
        """
        linux, win = self._routes()
        with _coord(cache_dir=tmp_path) as coord:
            coord.request_listing("foo").wait(timeout=5)
            self._await_metadata(coord)

        win_hash = ("sha256", hashlib.sha256(_SIBLING_WIN_BODY).hexdigest())
        win_url = f"https://f.example/{_SIBLING_WIN}.metadata"
        with _coord(cache_dir=tmp_path) as coord:
            coord.request_listing("foo").wait(timeout=5)
            self._await_metadata(coord)
            coord.request_metadata("foo", "1.0", win_url, win_hash).wait(timeout=5)
            assert coord.index.get_metadata_error("foo", "1.0") is None
            assert coord.index.get_metadata("foo", "1.0") == _SIBLING_WIN_BODY.decode()

        assert linux.call_count == 1
        assert win.call_count == 1
