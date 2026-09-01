"""Tests for FetchCoordinator with mocked HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
import respx

from nab_index.cache import CachePolicy, NullCache, OfflineError, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient, SdistArchiveHold
from nab_index.client import (
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    SdistFile,
    SdistHashMismatchError,
    WheelFile,
)
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.lazy_wheel import RangeOutcome
from nab_index.local_index import LocalIndexClient
from nab_index.multi_index import IndexConfig, MultiIndexClient
from nab_index.parsed_listing import encode as encode_parsed
from nab_index.transport import HttpError, HttpResponse
from nab_project.fetch import (
    _WARM_SYNC_MIN_BLOB_BYTES,
    FetchCoordinator,
    FetchKind,
    FetchRequest,
    IndexRoute,
    InMemoryIndex,
    WarmSyncStats,
    _builds_remote_sdists,
    _resolve_routes,
)
from nab_project.inputs import ResolveInputs
from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import IndexOverride
from nab_provider.policy import BuildPolicy
from nab_provider.serialization import SimpleSerialization
from nab_provider.testing import pkg_override


@pytest.fixture
def no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exhaust the transport's retry budget on the first attempt, so no backoff."""
    monkeypatch.setattr("nab_index.httpx_async_transport.MAX_RETRIES", 0)


def _coord(**kwargs: object) -> FetchCoordinator:
    """Build a FetchCoordinator wired to httpx so respx can mock it.

    The overlap gate defaults off (threshold 0) so the serve-mechanism tests
    exercise the sync path independent of the blob-size gate, which has its own
    dedicated tests.
    """
    coord = FetchCoordinator(transport=HttpxAsyncTransport(), **kwargs)  # type: ignore[arg-type]
    coord._warm_sync_min_blob_bytes = 0
    return coord


def _wait_until(predicate: Callable[[], bool], timeout: float = 5) -> bool:
    """Poll ``predicate`` until it holds, and report whether it did.

    The coordinator finishes some work without setting the waiter's event, so
    waiting on the event itself would just spend the whole timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)

    # One last look, in case the work landed during the final sleep.
    return predicate()


class _FetcherDeath(BaseException):
    """A BaseException that is not an Exception, as asyncio.CancelledError is."""


class _FailingTransport:
    """Transport whose operations raise the given BaseException."""

    def __init__(self, failure: type[BaseException]) -> None:
        self._failure = failure

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> NoReturn:
        raise self._failure

    async def aclose(self) -> NoReturn:
        raise self._failure


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

    def test_offline_listing_miss_roundtrip(self) -> None:
        idx = InMemoryIndex()
        idx.store_listing("foo", [], offline_miss=True)
        assert idx.get_listing("foo") == []
        assert idx.is_offline_listing_miss("foo")

    def test_served_empty_listing_is_not_an_offline_miss(self) -> None:
        idx = InMemoryIndex()
        idx.store_listing("foo", [])
        assert not idx.is_offline_listing_miss("foo")

    def test_offline_listing_miss_fires_listing_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("listing:foo")
        idx.store_listing("foo", [], offline_miss=True)
        assert event.is_set()

    def test_the_yank_mark_is_set_before_the_listing_lands(self) -> None:
        """The yank mark lands first, so a page caught mid-store never reads absent."""
        idx = InMemoryIndex()
        readback: list[str] = []

        class _ReadOnPublish(dict[str, list[WheelFile | SdistFile]]):
            """Classify the page the instant its listing slot becomes readable."""

            def __setitem__(self, key: str, value: list[WheelFile | SdistFile]) -> None:
                super().__setitem__(key, value)
                assert idx.get_listing(key) == []
                readback.append(
                    "yanked" if idx.is_all_yanked_listing(key) else "absent"
                )

        idx._listings = _ReadOnPublish()
        idx.store_listing("foo", [], all_yanked=True)

        assert readback == ["yanked"]
        assert idx.is_all_yanked_listing("foo")

    def test_metadata_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_metadata("foo", "1.0") is None
        assert not idx.has_metadata("foo", "1.0")
        idx.store_metadata("foo", "1.0", "Metadata-Version: 2.1")
        assert idx.get_metadata("foo", "1.0") == "Metadata-Version: 2.1"
        assert idx.has_metadata("foo", "1.0")

    def test_metadata_slot_keeps_only_the_header_block(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata(
            "foo", "1.0", "Name: foo\nVersion: 1.0\n\nA long description.\n"
        )
        assert idx.get_metadata("foo", "1.0") == "Name: foo\nVersion: 1.0\n\n"

    def test_sdist_metadata_slot_keeps_only_the_header_block(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata(
            "foo", "1.0", "Name: foo\nVersion: 1.0\n\nA long description.\n"
        )
        assert idx.get_metadata("foo", "1.0") == "Name: foo\nVersion: 1.0\n\n"
        assert idx.metadata_from_sdist("foo", "1.0")

    def test_store_metadata_none(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", None)
        assert idx.has_metadata("foo", "1.0")

    def test_offline_metadata_miss_roundtrip(self) -> None:
        idx = InMemoryIndex()
        url = "https://files.example.com/foo-1.0.whl"
        assert not idx.is_offline_metadata_miss("foo", "1.0", url)
        idx.record_offline_metadata_miss("foo", "1.0", url)
        assert idx.is_offline_metadata_miss("foo", "1.0", url)
        assert not idx.is_offline_metadata_miss("foo", "2.0", url)

    def test_an_offline_miss_is_keyed_to_the_rung_that_skipped(self) -> None:
        idx = InMemoryIndex()
        idx.record_offline_metadata_miss(
            "foo", "1.0", "https://files.example.com/foo-1.0.whl"
        )
        assert not idx.is_offline_metadata_miss(
            "foo", "1.0", "https://files.example.com/foo-1.0.tar.gz"
        )
        assert not idx.is_offline_metadata_miss("foo", "1.0", None)

    def test_empty_metadata_slot_is_not_an_offline_miss(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", None)
        idx.store_sdist_metadata("foo", "1.0", None)
        assert not idx.is_offline_metadata_miss("foo", "1.0", None)

    def test_only_the_first_caller_claims_the_offline_warning(self) -> None:
        idx = InMemoryIndex()
        assert idx.claim_offline_metadata_warning("foo")
        assert not idx.claim_offline_metadata_warning("foo")
        assert idx.claim_offline_metadata_warning("bar")

    def test_metadata_error_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_metadata_error("foo", "1.0") is None
        error = MetadataHashMismatchError("metadata sha256 mismatch")
        idx.store_metadata_error("foo", "1.0", error)
        assert idx.get_metadata_error("foo", "1.0") is error
        assert idx.get_metadata("foo", "1.0") is None

    def test_store_metadata_error_fires_metadata_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.metadata")
        idx.store_metadata_error(
            "foo", "1.0", MetadataHashMismatchError("bad"), "https://f/a.metadata"
        )
        assert event.is_set()

    def test_store_metadata_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata_error("foo", "1.0", MetadataHashMismatchError("bad"))
        assert idx.get_metadata_error("foo", "1.0") is not None
        assert idx.get_metadata("foo", "1.0") is None

    def test_pending_event_set_on_listing(self) -> None:
        idx = InMemoryIndex()
        event, existed = idx.get_or_create_pending("listing:foo")
        assert not existed
        assert not event.is_set()
        wheels = [_make_wheel("foo")]
        idx.store_listing("foo", wheels)
        assert event.is_set()
        assert idx.get_listing("foo") == wheels

    def test_pending_event_set_on_metadata(self) -> None:
        idx = InMemoryIndex()
        event, existed = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.md")
        assert not existed
        idx.store_metadata("foo", "1.0", "text", metadata_url="https://f/a.md")
        assert event.is_set()
        assert idx.get_metadata_with_origin("foo", "1.0", "https://f/a.md") == (
            "text",
            False,
        )

    def test_sidecar_slot_answers_only_for_its_own_artifact(self) -> None:
        linux_url = "https://f/linux.whl.metadata"
        win_url = "https://f/win.whl.metadata"
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", "linux", metadata_url=linux_url)
        assert idx.has_metadata("foo", "1.0", linux_url)
        assert not idx.has_metadata("foo", "1.0", win_url)

    def test_a_positional_metadata_url_is_refused(self) -> None:
        """A transposed store raises instead of writing the URL as the text.

        ``data`` and ``metadata_url`` are both ``str | None``, so a positional
        ``metadata_url`` would take a swapped call and store the URL.
        """
        url = "https://f/linux.whl.metadata"
        idx = InMemoryIndex()
        with pytest.raises(TypeError):
            idx.store_metadata("foo", "1.0", url, "linux")  # type: ignore[call-arg]
        assert not idx.has_metadata("foo", "1.0", url)

    def test_sibling_sidecars_keep_their_own_text(self) -> None:
        """Two wheels of one version can declare different dependencies."""
        linux_url = "https://f/linux.whl.metadata"
        win_url = "https://f/win.whl.metadata"
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", "linux", metadata_url=linux_url)
        idx.store_metadata("foo", "1.0", "win", metadata_url=win_url)
        assert idx.get_metadata("foo", "1.0", linux_url) == "linux"
        assert idx.get_metadata("foo", "1.0", win_url) == "win"
        assert idx.get_metadata("foo", "1.0") is None

    def test_an_injected_override_answers_for_any_artifact(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", "override\n")
        assert idx.has_metadata("foo", "1.0", "https://f/win.whl.metadata")
        assert idx.get_metadata_with_origin(
            "foo", "1.0", "https://f/win.whl.metadata"
        ) == ("override\n", False)

    def test_sdist_pkg_info_does_not_answer_for_an_unfetched_sidecar(self) -> None:
        """A wheel that declares its own dependencies must fetch them.

        The sdist is a fallback for an artifact with no text of its own, so
        lending its PKG-INFO to a sidecar nobody has fetched yet would give
        the wheel the sdist's dependencies instead of the wheel's.
        """
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert not idx.has_metadata("foo", "1.0", "https://f/win.whl.metadata")
        assert idx.get_metadata_with_origin(
            "foo", "1.0", "https://f/win.whl.metadata"
        ) == (None, False)
        assert idx.get_metadata_with_origin("foo", "1.0") == ("PKG-INFO\n", True)

    def test_a_sidecar_that_was_not_served_falls_back_to_the_sdist(self) -> None:
        """An empty sidecar slot does not shadow the version-level text."""
        url = "https://f/a.whl.metadata"
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", None, metadata_url=url)
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert idx.has_metadata("foo", "1.0", url)
        assert idx.get_metadata_with_origin("foo", "1.0", url) == ("PKG-INFO\n", True)

    def test_has_metadata_is_false_on_an_empty_slot(self) -> None:
        idx = InMemoryIndex()
        assert not idx.has_metadata("foo", "1.0", "https://f/a.whl.metadata")

    def test_a_sidecars_integrity_error_answers_only_for_that_sidecar(self) -> None:
        linux_url = "https://f/linux.whl.metadata"
        win_url = "https://f/win.whl.metadata"
        idx = InMemoryIndex()
        error = MetadataHashMismatchError("metadata sha256 mismatch")
        idx.store_metadata_error("foo", "1.0", error, linux_url)
        assert idx.get_metadata_error("foo", "1.0", linux_url) is error
        assert idx.get_metadata_error("foo", "1.0", win_url) is None
        assert idx.get_metadata_error("foo", "1.0") is None

    def test_a_version_level_integrity_error_answers_for_any_artifact(self) -> None:
        idx = InMemoryIndex()
        error = SdistHashMismatchError("boom")
        idx.store_sdist_metadata_error("foo", "1.0", error)
        assert idx.get_metadata_error("foo", "1.0", "https://f/a.whl.metadata") is error

    def test_get_or_create_existing(self) -> None:
        idx = InMemoryIndex()
        first, existed1 = idx.get_or_create_pending("key")
        second, existed2 = idx.get_or_create_pending("key")
        assert not existed1
        assert existed2
        assert first is second

    def test_get_or_create_after_the_fetch_landed(self) -> None:
        """A published key still reports as existing, and its event is set."""
        idx = InMemoryIndex()
        idx.get_or_create_pending("listing:foo")
        idx.store_listing("foo", [])

        event, existed = idx.get_or_create_pending("listing:foo")

        assert existed
        assert event.is_set()

    def test_store_sdist_metadata_fires_sdist_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist:foo:1.0")
        assert not event.is_set()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert event.is_set()
        assert idx.get_metadata("foo", "1.0") == "PKG-INFO\n"

    def test_store_sdist_metadata_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", None)
        assert idx.has_metadata("foo", "1.0")
        assert idx.get_metadata("foo", "1.0") is None

    def test_store_sdist_metadata_error_fires_sdist_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist:foo:1.0")
        assert not event.is_set()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_metadata_error("foo", "1.0", err)
        assert event.is_set()
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
        event, _ = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.md")
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        idx.store_metadata("foo", "1.0", None, metadata_url="https://f/a.md")
        assert event.is_set()
        assert idx.get_metadata("foo", "1.0") == "PKG-INFO\n"
        assert idx.metadata_from_sdist("foo", "1.0")
        # The sidecar that resolved to nothing reads the kept text.
        assert idx.get_metadata_with_origin("foo", "1.0", "https://f/a.md") == (
            "PKG-INFO\n",
            True,
        )

    def test_metadata_none_after_sdist_none_stays_none(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", None)
        idx.store_metadata("foo", "1.0", None)
        assert idx.get_metadata("foo", "1.0") is None
        assert not idx.metadata_from_sdist("foo", "1.0")

    def test_sdist_archive_pending_event_fires(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist-archive:foo:1.0")
        assert not event.is_set()
        idx.store_sdist_archive("foo", "1.0", b"bytes")
        assert event.is_set()
        assert idx.get_sdist_archive("foo", "1.0") == b"bytes"

    def test_sdist_archive_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_archive("foo", "1.0", None)
        assert idx.get_sdist_archive("foo", "1.0") is None

    def test_store_sdist_archive_error_fires_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist-archive:foo:1.0")
        assert not event.is_set()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_archive_error("foo", "1.0", err)
        assert event.is_set()
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
        assert idx.get_parsed_metadata("foo", "1.0", "METADATA") is None
        idx.store_parsed_metadata("foo", "1.0", sentinel, "METADATA")
        assert idx.get_parsed_metadata("foo", "1.0", "METADATA") is sentinel

    def test_parsed_metadata_per_version(self) -> None:
        """Each ``(package, version)`` pair has its own slot."""
        idx = InMemoryIndex()
        idx.store_parsed_metadata("foo", "1.0", "v1", "TEXT-1")
        idx.store_parsed_metadata("foo", "2.0", "v2", "TEXT-2")
        assert idx.get_parsed_metadata("foo", "1.0", "TEXT-1") == "v1"
        assert idx.get_parsed_metadata("foo", "2.0", "TEXT-2") == "v2"

    def test_parsed_metadata_answers_only_for_its_own_text(self) -> None:
        """The sdist's parse is not served to a reader holding wheel METADATA.

        Both kinds write one ``(package, version)`` slot, so the wheel's
        PEP 658 sidecar can replace PKG-INFO a previous tuple already parsed.
        """
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        idx.store_parsed_metadata("foo", "1.0", "sdist-parse", "PKG-INFO")
        idx.store_metadata("foo", "1.0", "METADATA")
        assert idx.get_parsed_metadata("foo", "1.0", "METADATA") is None

    def test_get_metadata_with_origin_reports_the_last_write(self) -> None:
        """Text and origin come back together, from whichever kind wrote last."""
        idx = InMemoryIndex()
        assert idx.get_metadata_with_origin("foo", "1.0") == (None, False)
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        assert idx.get_metadata_with_origin("foo", "1.0") == ("PKG-INFO", True)
        idx.store_metadata("foo", "1.0", "METADATA")
        assert idx.get_metadata_with_origin("foo", "1.0") == ("METADATA", False)

    def test_resolved_sdist_metadata_roundtrip(self) -> None:
        """``store_resolved_sdist_metadata`` round-trips through ``get``."""
        idx = InMemoryIndex()
        sentinel = object()
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is None
        idx.store_resolved_sdist_metadata("foo", "1.0", sentinel)
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is sentinel

    def test_metadata_rewrite_drops_the_reconciled_sdist_view(self) -> None:
        """Replacing the raw text drops the post-reconciliation entry too.

        The reconciled record is the PKG-INFO in the slot plus the bundled
        pyproject.toml or a PEP 517 build, so it cannot outlive that text.
        """
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        idx.store_resolved_sdist_metadata("foo", "1.0", "resolved")
        idx.store_metadata("foo", "1.0", "METADATA")
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is None

    def test_restoring_the_same_text_keeps_the_reconciled_sdist_view(self) -> None:
        """Re-storing identical text is not a rewrite, so the entry survives."""
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        idx.store_resolved_sdist_metadata("foo", "1.0", "resolved")
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        assert idx.get_resolved_sdist_metadata("foo", "1.0") == "resolved"

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
        assert coord.index.get_metadata("second", "1.0", "https://f.com/second") == "ok"

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
    def test_on_fetch_callback_fires_per_listing(self) -> None:
        respx.get("https://pypi.org/simple/testpkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        calls: list[int] = []
        with _coord(on_fetch=lambda: calls.append(1)) as coord:
            event = coord.request_listing("testpkg")
            event.wait(timeout=5)
        assert calls == [1]

    @respx.mock
    def test_listing_survives_a_failure_raised_after_it_is_stored(self) -> None:
        """A failure raised after the listing is stored records no error."""
        respx.get("https://pypi.org/simple/testpkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )

        def on_fetch() -> None:
            msg = "progress sink closed"
            raise RuntimeError(msg)

        with _coord(on_fetch=on_fetch) as coord:
            assert coord.request_listing("testpkg").wait(timeout=5)

        assert coord.index.get_listing("testpkg") is not None
        assert coord.index.get_listing_error("testpkg") is None

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
            claimed, _ = coord.index.get_or_create_pending("listing:testpkg")
            event = coord.request_listing("testpkg")
            assert event is claimed
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
            text = coord.index.get_metadata(
                "testpkg", "1.0", "https://files.example.com/testpkg-1.0.whl.metadata"
            )
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
            from nab_project.fetch import FetchKind, FetchRequest

            coord.index.get_or_create_pending("metadata:pkg:1.0")
            coord._submit(
                FetchRequest(
                    kind=FetchKind.METADATA, package="pkg", version="1.0", url=None
                )
            )

            assert _wait_until(lambda: coord.index.has_metadata("pkg", "1.0"))
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
            a_meta = coord.index.get_metadata(
                "pkg-a", "1.0", "https://f.com/a.metadata"
            )
            b_meta = coord.index.get_metadata(
                "pkg-b", "2.0", "https://f.com/b.metadata"
            )
            assert a_meta == "meta-a"
            assert b_meta == "meta-b"

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
            # Oldest-first; the prefetch takes the newest wheel.
            "files": [
                {
                    "filename": "pkg-1.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-1.0-py3-none-any.whl",
                    "dist-info-metadata": {"sha256": digest},
                },
                {
                    "filename": "pkg-2.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-2.0-py3-none-any.whl",
                    "dist-info-metadata": {"sha256": digest},
                },
                {
                    "filename": "pkg-3.0-py3-none-any.whl",
                    "url": "https://f.com/pkg-3.0-py3-none-any.whl",
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
            # Wait for the async prefetch of the newest wheel's sidecar.
            sidecar = "https://f.com/pkg-3.0-py3-none-any.whl.metadata"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if coord.index.has_metadata("pkg", "3.0", sidecar):
                    break
                time.sleep(0.01)
            assert coord.index.has_metadata("pkg", "3.0", sidecar)

    @respx.mock
    def test_listing_prefetch_derives_urls_only_for_submitted_wheels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sidecar URL is derived only for the wheels the prefetch submits."""
        listing = {
            "meta": {"api-version": "1.0"},
            "name": "pkg",
            "files": [
                {
                    "filename": f"pkg-{n}.0-py3-none-any.whl",
                    "url": f"https://f.com/pkg-{n}.0-py3-none-any.whl",
                    "core-metadata": True,
                }
                for n in range(1, 16)
            ],
        }
        respx.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json=listing)
        )
        respx.get(url__regex=r".*\.whl\.metadata$").mock(
            return_value=httpx.Response(200, text="Metadata-Version: 2.1\n")
        )

        derived: list[str] = []
        original = WheelFile.metadata_url.fget
        assert original is not None

        def counting(wheel: WheelFile) -> str | None:
            derived.append(wheel.filename)
            return original(wheel)

        monkeypatch.setattr(WheelFile, "metadata_url", property(counting))

        sidecar = "https://f.com/pkg-15.0-py3-none-any.whl.metadata"
        with _coord() as coord:
            coord.request_listing("pkg").wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not coord.index.has_metadata(
                "pkg", "15.0", sidecar
            ):
                time.sleep(0.01)
            assert coord.index.has_metadata("pkg", "15.0", sidecar)

        # Only the newest version's wheel, not all 15.
        assert derived == ["pkg-15.0-py3-none-any.whl"]

    def test_prefetch_after_listing_enqueues_the_newest_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The extracted tail picks the newest version's first sidecar wheel."""

        def _wheel(
            version: str, *, build: str = "", has_meta: bool = True
        ) -> WheelFile:
            """A wheel for ``version``; ``build`` also varies the sidecar hash."""
            tag = f"-{build}" if build else ""
            name = f"pkg-{version}{tag}-py3-none-any.whl"
            return WheelFile(
                filename=name,
                url=f"https://f.com/{name}",
                version=version,
                requires_python=None,
                has_metadata=has_meta,
                upload_time=None,
                metadata_hash=("sha256", f"h{version}{tag}") if has_meta else None,
            )

        # Oldest-first, with more versions than the prefetch takes.
        files: list[WheelFile | SdistFile] = []
        for n in range(1, 16):
            files.append(_wheel(f"{n}.0"))

        # The three the tail must pass over.
        files.append(_wheel("15.0", build="1"))  # second wheel of 15.0
        files.append(_wheel("16.0", has_meta=False))  # no sidecar
        files.append(
            SdistFile(
                filename="pkg-17.0.tar.gz",
                url="https://f.com/pkg-17.0.tar.gz",
                version="17.0",
                requires_python=None,
                upload_time=None,
            )
        )

        calls: list[tuple[object, ...]] = []

        def _spy(*args: object, **kwargs: object) -> threading.Event:
            calls.append(args)
            done = threading.Event()
            done.set()
            return done

        coord = _coord()
        monkeypatch.setattr(coord, "request_metadata", _spy)
        coord._prefetch_metadata_after_listing("pkg", files)

        assert calls == [
            (
                "pkg",
                "15.0",
                "https://f.com/pkg-15.0-py3-none-any.whl.metadata",
                ("sha256", "h15.0"),
            )
        ]

    @respx.mock
    def test_listing_entry_with_unsplittable_url_is_dropped(self) -> None:
        """Only the entry whose URL urllib cannot split is dropped."""
        listing = {
            "meta": {"api-version": "1.0"},
            "name": "pkg",
            "files": [
                {
                    "filename": "pkg-1.0-py3-none-any.whl",
                    "url": "https://[2001:db8::1/pkg-1.0-py3-none-any.whl",
                    "core-metadata": True,
                },
                *(
                    {
                        "filename": f"pkg-{n}.0-py3-none-any.whl",
                        "url": f"https://f.com/pkg-{n}.0-py3-none-any.whl",
                        "core-metadata": True,
                    }
                    for n in (2, 3)
                ),
            ],
        }

        respx.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json=listing)
        )
        respx.get(url__regex=r".*\.whl\.metadata$").mock(
            return_value=httpx.Response(200, text="Metadata-Version: 2.1\n")
        )

        with _coord() as coord:
            assert coord.request_listing("pkg").wait(timeout=5)

        # Read after the fetcher thread joins, so the prefetch that follows
        # store_listing has finished.
        files = coord.index.get_listing("pkg")
        assert files is not None
        assert [f.version for f in files] == ["2.0", "3.0"]
        assert coord.index.get_listing_error("pkg") is None

        newest = "https://f.com/pkg-3.0-py3-none-any.whl.metadata"
        _, newest_prefetched = coord.index.get_or_create_pending(
            f"metadata:pkg:3.0:{newest}"
        )
        assert newest_prefetched

        older = "https://f.com/pkg-2.0-py3-none-any.whl.metadata"
        _, older_prefetched = coord.index.get_or_create_pending(
            f"metadata:pkg:2.0:{older}"
        )
        assert not older_prefetched

    @respx.mock
    def test_fetch_error_logged_not_raised(self) -> None:
        respx.get("https://pypi.org/simple/bad/").mock(return_value=httpx.Response(500))
        with _coord() as coord:
            event = coord.request_listing("bad")
            # The event won't be set because the fetch failed,
            # but the coordinator shouldn't crash
            event.wait(timeout=2)
            assert not coord._crashed

    @pytest.mark.parametrize(
        ("kind", "request_fetch"),
        [
            ("listing", lambda coord: coord.request_listing("bad")),
            (
                "metadata",
                lambda coord: coord.request_metadata(
                    "bad", "1.0", "https://files.example.com/bad-1.0.whl.metadata"
                ),
            ),
            (
                "sdist",
                lambda coord: coord.request_sdist(
                    "bad", "1.0", "https://files.example.com/bad-1.0.tar.gz"
                ),
            ),
            (
                "sdist-archive",
                lambda coord: coord.request_sdist_archive(
                    "bad", "1.0", "https://files.example.com/bad-1.0.tar.gz"
                ),
            ),
        ],
    )
    @respx.mock
    def test_fetch_failure_warns_with_cause_and_no_traceback(
        self,
        kind: str,
        request_fetch: Callable[[FetchCoordinator], threading.Event],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed fetch warns with its cause, and no traceback."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(500))

        with (
            caplog.at_level(logging.DEBUG, logger="nab_project.fetch"),
            _coord() as coord,
        ):
            request_fetch(coord).wait(timeout=5)

        failures = [
            r
            for r in caplog.records
            if r.name == "nab_project.fetch" and r.msg.startswith("Fetch failed")
        ]
        assert len(failures) == 1
        assert failures[0].levelno == logging.WARNING
        assert failures[0].exc_info is None
        assert failures[0].args[0] == kind
        assert "500" in failures[0].getMessage()

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
    def test_sdist_fetch_failure_records_error(self, no_retries: None) -> None:
        """A 5xx on an sdist records an error, not an archive without PKG-INFO."""
        respx.get("https://files.example.com/broken.tar.gz").mock(
            return_value=httpx.Response(500)
        )
        with _coord() as coord:
            event = coord.request_sdist(
                "broken", "1.0", "https://files.example.com/broken.tar.gz"
            )
            event.wait(timeout=5)
            assert not coord._crashed
            assert coord.index.get_metadata("broken", "1.0") is None
            error = coord.index.get_metadata_error("broken", "1.0")
            assert isinstance(error, HttpError)

    @respx.mock
    def test_metadata_fetch_failure_records_error(self, no_retries: None) -> None:
        """A 5xx on a sidecar records an error, not an absent sidecar."""
        sidecar = "https://files.example.com/broken-1.0.whl.metadata"
        respx.get(sidecar).mock(return_value=httpx.Response(500))
        with _coord() as coord:
            event = coord.request_metadata("broken", "1.0", sidecar)
            event.wait(timeout=5)
            assert not coord._crashed
            assert coord.index.get_metadata("broken", "1.0") is None
            error = coord.index.get_metadata_error("broken", "1.0", sidecar)
            assert isinstance(error, HttpError)

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
            claimed, _ = coord.index.get_or_create_pending(f"metadata:pkg:1.0:{url}")
            coord._submit(
                FetchRequest(
                    kind=FetchKind.METADATA,
                    package="pkg",
                    version="1.0",
                    url=url,
                )
            )

            assert claimed.wait(timeout=5)
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
            sidecar = "https://files.example.com/tampered-1.0.whl.metadata"
            assert coord.index.get_metadata("tampered", "1.0", sidecar) is None
            error = coord.index.get_metadata_error("tampered", "1.0", sidecar)
            assert isinstance(error, MetadataHashMismatchError)

    def test_crashed_raises(self) -> None:
        coord = _coord()
        coord._record_crash(ValueError("boom"))
        with pytest.raises(RuntimeError, match="crashed: boom"):
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
            assert coord.index.get_metadata("a", "1", "https://f.com/a") == "ok"
            assert coord.index.get_metadata("b", "1", "https://f.com/b") == "ok"
            assert coord.index.get_metadata("c", "1", "https://f.com/c") == "ok"

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

    @pytest.mark.timeout(30)
    @pytest.mark.parametrize("failure", [_FetcherDeath, KeyboardInterrupt])
    def test_run_loop_records_a_base_exception_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
    ) -> None:
        """A BaseException out of the loop is recorded, then ends the thread."""
        escaped: list[BaseException] = []

        def _collect(args: threading.ExceptHookArgs) -> None:
            assert args.exc_value is not None
            escaped.append(args.exc_value)

        monkeypatch.setattr(threading, "excepthook", _collect)
        coord = FetchCoordinator(transport=_FailingTransport(failure))  # type: ignore[arg-type]

        coord.start()
        coord.shutdown()

        assert coord._crashed
        assert isinstance(coord._crash_error, failure)
        assert escaped == [coord._crash_error]
        with pytest.raises(RuntimeError, match="Fetcher thread crashed"):
            coord.request_listing("foo")

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

    def test_start_holds_until_client_is_built(self) -> None:
        """start() does not return while the index client is still being built."""
        building = threading.Event()
        release = threading.Event()
        started = threading.Event()

        class _BlockingBuildCoordinator(FetchCoordinator):
            def _build_client(self) -> NoReturn:
                building.set()
                release.wait(timeout=5)
                msg = "index client build failed"
                raise ValueError(msg)

        coord = _BlockingBuildCoordinator(transport=HttpxAsyncTransport())

        def _start() -> None:
            coord.start()
            started.set()

        starter = threading.Thread(target=_start)
        starter.start()

        try:
            assert building.wait(timeout=5)
            assert not coord._queue_ready.is_set()

            release.set()
            assert started.wait(timeout=5)
            assert coord._crashed
            assert coord._async_q is None
            with pytest.raises(RuntimeError, match="index client build failed"):
                coord.request_listing("foo")
        finally:
            release.set()
            starter.join(timeout=5)
            coord.shutdown()

    def test_non_local_file_index_error_names_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected index URL reaches the caller in the crash error."""
        monkeypatch.setattr(sys, "platform", "linux")
        coord = _coord(indexes=[IndexConfig("local", "file://wheels")])
        try:
            coord.start()
            assert coord._crashed
            with pytest.raises(RuntimeError, match="file://wheels"):
                coord.request_listing("foo")
        finally:
            coord.shutdown()

    def _dead_loop_coord(self) -> FetchCoordinator:
        """Return a coordinator whose loop is dead, crash flag cleared."""
        coord = _coord(index_routes=[IndexRoute(name="foo", index="missing")])
        coord.start()
        assert coord._thread is not None
        coord._thread.join(timeout=5)
        coord._crashed = False
        return coord

    def test_refused_listing_submit_releases_waiter(self) -> None:
        """A listing request refused by a closed loop fails instead of hanging."""
        coord = self._dead_loop_coord()
        event = coord.request_listing("foo")
        assert event.is_set()
        assert isinstance(coord.index.get_listing_error("foo"), RuntimeError)

    def test_refused_submit_releases_all_request_kinds(self) -> None:
        """Refused metadata, sdist, archive, and batch requests all fail loudly."""
        coord = self._dead_loop_coord()
        meta = coord.request_metadata("a", "1.0", "https://f.com/a")
        sdist = coord.request_sdist("b", "1.0", "https://f.com/b.tar.gz")
        archive = coord.request_sdist_archive("c", "1.0", "https://f.com/c.tar.gz")
        batch = coord.request_metadata_batch([("d", "1.0", "https://f.com/d", None)])

        assert meta.is_set()
        assert sdist.is_set()
        assert archive.is_set()
        assert batch[0][2].is_set()

        meta_error = coord.index.get_metadata_error("a", "1.0", "https://f.com/a")
        batch_error = coord.index.get_metadata_error("d", "1.0", "https://f.com/d")
        assert isinstance(meta_error, RuntimeError)
        assert isinstance(coord.index.get_metadata_error("b", "1.0"), RuntimeError)
        assert isinstance(coord.index.get_sdist_archive_error("c", "1.0"), RuntimeError)
        assert isinstance(batch_error, RuntimeError)

    def test_submit_to_closed_loop_releases_waiter(self) -> None:
        """A request reaching the loop after it closed fails instead of hanging."""

        class _DispatchFailureCoordinator(FetchCoordinator):
            def _dispatch(
                self,
                item: FetchRequest | list[FetchRequest],
                client: object,
                sem: asyncio.Semaphore,
                tasks: set[asyncio.Task],
            ) -> NoReturn:
                msg = "dispatch failed"
                raise RuntimeError(msg)

        coord = _DispatchFailureCoordinator(transport=HttpxAsyncTransport())
        coord.start()
        coord.request_listing("foo")

        assert coord._thread is not None
        coord._thread.join(timeout=5)
        assert coord._crashed
        assert coord._loop is not None

        # Clear the flag so the next request gets past _check_alive to _submit.
        coord._crashed = False
        event = coord.request_listing("bar")
        assert event.is_set()
        assert isinstance(coord.index.get_listing_error("bar"), RuntimeError)

        coord.shutdown()

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

        monkeypatch.setattr("nab_project.fetch.asyncio.Queue", RiggedQueue)
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
        assert coord.index.get_metadata("first", "1.0", "https://f.com/first") == "slow"
        assert coord.index.get_metadata("drain", "1.0", "https://f.com/drain") == "slow"

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
        assert coord.index.get_metadata("pkg", "1.0", "https://f.com/pkg") == "slow"

    @respx.mock
    def test_request_metadata_deduplicates(self) -> None:
        """Second request_metadata for the same key reuses the pending."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="meta"))
        with _coord() as coord:
            claimed, _ = coord.index.get_or_create_pending(
                "metadata:pkg:1.0:https://f.com/m"
            )
            event = coord.request_metadata("pkg", "1.0", "https://f.com/m")

            # Deduplicating means handing back the pending's own event. Nothing
            # was submitted for it, so nothing ever sets it.
            assert event is claimed
            assert not event.is_set()

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

        pyproject_at_event: list[Mapping[str, Any] | None] = []

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
        assert pyproject_at_event == [{"project": {"name": "pkg"}}]

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
                serving_at_event.append(self.get_listing_index(package))
                super().store_listing(
                    package,
                    data,
                    offline_miss=offline_miss,
                    unreadable_only=unreadable_only,
                    unreachable_only=unreachable_only,
                    no_usable_file=no_usable_file,
                    all_yanked=all_yanked,
                    zip_sdists=zip_sdists,
                )

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
            claimed, _ = coord.index.get_or_create_pending("sdist:pkg:1.0")
            event = coord.request_sdist("pkg", "1.0", "https://f.com/pkg-1.0.tar.gz")

            assert event is claimed
            assert not event.is_set()

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
            claimed, _ = coord.index.get_or_create_pending("sdist-archive:pkg:1.0")
            event = coord.request_sdist_archive(
                "pkg", "1.0", "https://f.com/pkg-1.0.tar.gz"
            )

            assert event is claimed
            assert not event.is_set()

    @respx.mock
    def test_request_sdist_archive_404_records_error(self) -> None:
        """A 404 on an archive records an error and unblocks the waiter."""
        respx.get(url__regex=r".*").mock(return_value=httpx.Response(404))
        with _coord() as coord:
            event = coord.request_sdist_archive(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)
            assert coord.index.get_sdist_archive("pkg", "1.0") is None
            error = coord.index.get_sdist_archive_error("pkg", "1.0")
            assert isinstance(error, HttpError)

    @respx.mock
    @pytest.mark.skipif(
        not hasattr(tarfile, "data_filter"),
        reason="sdist extraction requires the tar data filter (PEP 706)",
    )
    def test_request_built_metadata_builds_the_downloaded_sdist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The build rung downloads, extracts, and stores what the backend said.

        Only the backend invocation is faked: the archive is really fetched
        through the transport and really extracted, so the member is exercised
        the way the provider reaches it.
        """
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            body = b'[project]\nname = "pkg"\nversion = "1.0"\n'
            info = tarfile.TarInfo(name="pkg-1.0/pyproject.toml")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        respx.get("https://files.example.com/pkg-1.0.tar.gz").mock(
            return_value=httpx.Response(200, content=buf.getvalue()),
        )

        built = WheelMetadata(
            name="pkg",
            version=Version("1.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
        )
        seen: dict[str, object] = {}

        def fake_build(_path: Path, **kwargs: object) -> WheelMetadata:
            seen.update(kwargs)
            return built

        monkeypatch.setattr("nab_project.build_backend.extract_metadata", fake_build)
        config = ResolveInputs()
        with _coord(build_config=config) as coord:
            event = coord.request_built_metadata(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.tar.gz", ()
            )
            assert event.wait(timeout=5)
            assert coord.index.get_built_metadata("pkg", "1.0") is built
        assert seen == {"config": config, "offline": False}

    def test_request_direct_archive_deduplicates(self, tmp_path: Path) -> None:
        """A direct archive already in flight hands back its pending event."""
        archive = tmp_path / "pkg-1.0.tar.gz"
        archive.write_bytes(b"archive")
        with _coord() as coord:
            claimed, _ = coord.index.get_or_create_pending("sdist-archive:pkg:digest")
            event = coord.request_direct_archive("pkg", "digest", archive.as_uri())

            assert event is claimed
            assert coord.index.get_sdist_archive("pkg", "digest") is None

    def test_offline_direct_archive_records_the_offline_error(self) -> None:
        """An offline miss on a declared archive is recorded as an error.

        An index artifact recorded absent lets the resolver skip that version;
        a declared archive is the package's only candidate, so there is no
        version to skip to.
        """
        with _coord(offline=True) as coord:
            event = coord.request_direct_archive(
                "pkg", "digest", "https://files.example.com/pkg-1.0.tar.gz"
            )
            event.wait(timeout=5)
            assert coord.index.get_sdist_archive("pkg", "digest") is None
            error = coord.index.get_sdist_archive_error("pkg", "digest")
            assert isinstance(error, OfflineError)

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

        from nab_project.fetch import FetchKind, FetchRequest, InMemoryIndex

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
            claimed, _ = coord.index.get_or_create_pending(
                "metadata:a:1.0:https://f.com/a"
            )
            results = coord.request_metadata_batch(
                [
                    ("a", "1.0", "https://f.com/a", None),
                    ("b", "1.0", "https://f.com/b", None),
                ]
            )
            events = {package: event for package, _version, event in results}

            assert events["a"] is claimed
            assert not events["a"].is_set()

            # The item without a pending is still fetched.
            assert events["b"].wait(timeout=5)
            assert coord.index.get_metadata("b", "1.0", "https://f.com/b") == "meta"


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
            assert coord.index.is_offline_listing_miss("missing")
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
            linux_text = coord.index.get_metadata("foo", "1.0", linux_url)
            assert linux_text == linux_body.decode()

        # A later run over the same cache dir, in an environment whose
        # compatible wheel is the win_amd64 one.
        with _coord(cache_dir=tmp_path) as coord:
            coord.request_metadata("foo", "1.0", win_url, win_hash).wait(timeout=5)
            assert coord.index.get_metadata_error("foo", "1.0", win_url) is None
            assert coord.index.get_metadata("foo", "1.0", win_url) == win_body.decode()

        assert linux_route.call_count == 1
        assert win_route.call_count == 1

    @respx.mock
    def test_offline_with_cold_cache_records_absent_artifacts(
        self, tmp_path: Path
    ) -> None:
        """Offline with a cache miss records the artifact absent, not as an error.

        Offline is the one failure the resolver works around: it resolves from
        what the cache holds, so an uncached artifact is skipped.  Each
        metadata rung marks the skip alongside the empty slot.
        """
        with FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coord:
            meta = coord.request_metadata(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.whl.metadata"
            )
            sdist = coord.request_sdist(
                "pkg", "2.0", "https://files.example.com/pkg-2.0.tar.gz"
            )
            archive = coord.request_sdist_archive(
                "pkg", "3.0", "https://files.example.com/pkg-3.0.tar.gz"
            )
            assert meta.wait(timeout=5)
            assert sdist.wait(timeout=5)
            assert archive.wait(timeout=5)

            assert coord.index.get_metadata("pkg", "1.0") is None
            assert coord.index.get_metadata_error("pkg", "1.0") is None
            assert coord.index.get_metadata("pkg", "2.0") is None
            assert coord.index.get_metadata_error("pkg", "2.0") is None
            assert coord.index.get_sdist_archive("pkg", "3.0") is None
            assert coord.index.get_sdist_archive_error("pkg", "3.0") is None
            assert coord.index.is_offline_metadata_miss(
                "pkg", "1.0", "https://files.example.com/pkg-1.0.whl.metadata"
            )
            assert coord.index.is_offline_metadata_miss(
                "pkg", "2.0", "https://files.example.com/pkg-2.0.tar.gz"
            )
        assert not coord._crashed

    @respx.mock
    def test_offline_with_cold_cache_logs_at_debug(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Offline + cache miss is an expected result, so it stays at debug."""
        cache_dir = Path(str(tmp_path))

        with (
            caplog.at_level(logging.DEBUG, logger="nab_project.fetch"),
            FetchCoordinator(
                transport=HttpxAsyncTransport(),
                cache_dir=cache_dir,
                offline=True,
            ) as coord,
        ):
            event = coord.request_listing("missing")
            event.wait(timeout=5)
            assert coord.index.get_listing("missing") == []

        failures = [
            r
            for r in caplog.records
            if r.name == "nab_project.fetch" and r.msg.startswith("Fetch failed")
        ]
        assert len(failures) == 1
        assert failures[0].levelno == logging.DEBUG
        assert failures[0].exc_info is None

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
            assert coord.request_listing("torch").wait(timeout=30)
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

    def test_explicit_cache_backend_with_a_pin_raises(self) -> None:
        """cache_backend + a pinned serialization is a config error."""
        with pytest.raises(ValueError, match="pinned serialization"):
            FetchCoordinator(
                transport=HttpxAsyncTransport(),
                cache_backend=NullCache(),
                indexes=[
                    IndexConfig(
                        "art",
                        "https://art.example/",
                        serialization=SimpleSerialization.JSON,
                    )
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

    def test_local_zip_sdist_reaches_the_store(self, tmp_path: Path) -> None:
        """A ``.zip`` sdist leaves no record, so its release rides beside them.

        The wheel keeps the listing non-empty, which is the case the
        package-level unreadable-format flag cannot report.
        """
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        (wheelhouse / "foo-1.0.zip").write_bytes(b"")

        with _coord(indexes=[IndexConfig("local", wheelhouse.as_uri())]) as coord:
            coord.request_listing("foo").wait(timeout=5)
            listing = coord.index.get_listing("foo")
            assert listing is not None
            assert [f.filename for f in listing] == ["foo-1.0-py3-none-any.whl"]
            assert not coord.index.is_unreadable_only_listing("foo")
            assert coord.index.zip_sdist_versions("foo") == frozenset({"1.0"})

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

    def test_local_index_without_authority(self, tmp_path: Path) -> None:
        """A file:/path index URL without an authority is still a local index."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        url = wheelhouse.as_uri().replace("file://", "file:", 1)
        assert not url.startswith("file://")

        coord = _coord(indexes=[IndexConfig("local", url)])
        try:
            assert isinstance(coord._build_client(), LocalIndexClient)
        finally:
            coord.shutdown()

        with _coord(indexes=[IndexConfig("local", url)]) as coord:
            coord.request_listing("foo").wait(timeout=5)
            listing = coord.index.get_listing("foo")
            assert coord.index.get_listing_error("foo") is None
            assert listing is not None
            assert [f.filename for f in listing] == ["foo-1.0-py3-none-any.whl"]

    def test_local_index_with_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``file:`` index URL lists a flat wheelhouse."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        wheel = wheelhouse / "foo-1.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        monkeypatch.chdir(tmp_path)

        with _coord(indexes=[IndexConfig("local", "file:wheelhouse")]) as coord:
            coord.request_listing("foo").wait(timeout=5)
            listing = coord.index.get_listing("foo")
            assert coord.index.get_listing_error("foo") is None
            assert listing is not None
            assert [f.url for f in listing] == [wheel.as_uri()]

    def test_unparseable_index_url_is_not_local(self, tmp_path: Path) -> None:
        """An index URL urlsplit cannot parse falls through to the remote client."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")

        with _coord(
            indexes=[
                IndexConfig("local", wheelhouse.as_uri()),
                # An unterminated IPv6 bracket has been a urlsplit ValueError
                # for far longer than a bracketed IPv4 address, which only
                # started raising after the oldest 3.10 nab supports.
                IndexConfig("bad", "https://[::1/simple/"),
            ],
        ) as coord:
            coord.request_listing("foo").wait(timeout=5)
            assert coord.index.get_listing_error("foo") is None
            assert coord.index.get_listing_index("foo") == "local"

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

    def test_pin_reaches_the_client_and_its_cache(self, tmp_path: Path) -> None:
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[
                IndexConfig(
                    "custom",
                    "https://custom.example/",
                    serialization=SimpleSerialization.JSON,
                )
            ],
        )
        try:
            client = coord._build_client()
            assert isinstance(client, CachedAsyncSimpleClient)
            assert client._serialization is SimpleSerialization.JSON
            backend = client._cache
            assert isinstance(backend, OnDiskCache)
            assert backend._simple_dir.name.endswith("-json")
        finally:
            coord.shutdown()

    def test_each_index_keeps_its_own_pin(self, tmp_path: Path) -> None:
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[
                IndexConfig(
                    "a", "https://a.example/", serialization=SimpleSerialization.JSON
                ),
                IndexConfig(
                    "b", "https://b.example/", serialization=SimpleSerialization.HTML
                ),
            ],
        )
        try:
            client = coord._build_client()
            assert isinstance(client, MultiIndexClient)
            first, second = client._clients["a"], client._clients["b"]
            assert isinstance(first, CachedAsyncSimpleClient)
            assert isinstance(second, CachedAsyncSimpleClient)
            assert first._serialization is SimpleSerialization.JSON
            assert second._serialization is SimpleSerialization.HTML
            first_cache, second_cache = first._cache, second._cache
            assert isinstance(first_cache, OnDiskCache)
            assert isinstance(second_cache, OnDiskCache)
            assert first_cache._simple_dir.name.endswith("-json")
            assert second_cache._simple_dir.name.endswith("-html")
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

    def test_cache_floor_single_index(self, tmp_path: Path) -> None:
        """A floor keyed by the single index sets the client's window."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[IndexConfig("custom", "https://custom.example/")],
            index_cache_floors={"custom": 42},
        )
        try:
            client = coord._build_client()
            assert isinstance(client, CachedAsyncSimpleClient)
            assert client._min_fresh_seconds == 42
        finally:
            coord.shutdown()

    def test_cache_floor_single_index_absent(self, tmp_path: Path) -> None:
        """A floor keyed by another index leaves this client's window None."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[IndexConfig("custom", "https://custom.example/")],
            index_cache_floors={"other": 42},
        )
        try:
            client = coord._build_client()
            assert isinstance(client, CachedAsyncSimpleClient)
            assert client._min_fresh_seconds is None
        finally:
            coord.shutdown()

    def test_cache_floor_multi_index(self, tmp_path: Path) -> None:
        """Each index's client gets its own floor by name; absent means None."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[
                IndexConfig("pypi", "https://pypi.org/simple/"),
                IndexConfig("torch-cpu", "https://torch.example/cpu/"),
            ],
            index_cache_floors={"pypi": 99},
        )
        try:
            client = coord._build_client()
            assert isinstance(client, MultiIndexClient)
            pypi = client._clients["pypi"]
            torch = client._clients["torch-cpu"]
            assert isinstance(pypi, CachedAsyncSimpleClient)
            assert isinstance(torch, CachedAsyncSimpleClient)
            assert pypi._min_fresh_seconds == 99
            assert torch._min_fresh_seconds is None
        finally:
            coord.shutdown()

    def test_cache_floor_default_none(self, tmp_path: Path) -> None:
        """Without index_cache_floors the client's window is None."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[IndexConfig("custom", "https://custom.example/")],
        )
        try:
            client = coord._build_client()
            assert isinstance(client, CachedAsyncSimpleClient)
            assert client._min_fresh_seconds is None
        finally:
            coord.shutdown()

    def test_cache_floor_local_index_inert(self, tmp_path: Path) -> None:
        """A floor on a file:// index builds a LocalIndexClient unaffected."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            indexes=[
                IndexConfig("pypi", "https://pypi.org/simple/"),
                IndexConfig("local", wheelhouse.as_uri()),
            ],
            index_cache_floors={"local": 3600},
        )
        try:
            client = coord._build_client()
            assert isinstance(client, MultiIndexClient)
            assert isinstance(client._clients["local"], LocalIndexClient)
        finally:
            coord.shutdown()

    def test_parsed_cache_stats_shared_across_index_clients(
        self, tmp_path: Path
    ) -> None:
        """Every per-index client shares the coordinator's parsed-cache sink."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path / "cache",
            indexes=[
                IndexConfig("pypi", "https://pypi.org/simple/"),
                IndexConfig("alt", "https://alt.example/"),
            ],
        )
        try:
            client = coord._build_client()
            assert isinstance(client, MultiIndexClient)
            stats = coord.parsed_cache_stats
            subclients = list(client._clients.values())
            for sub in subclients:
                assert isinstance(sub, CachedAsyncSimpleClient)
                assert sub._parsed_stats is stats
            # Increments through the separate index clients total on one sink.
            subclients[0]._parsed_stats.hit += 1
            subclients[1]._parsed_stats.miss += 1
            assert (stats.hit, stats.miss, stats.rebuild) == (1, 1, 0)
        finally:
            coord.shutdown()

    def test_parsed_cache_stats_shared_on_single_index(self, tmp_path: Path) -> None:
        """A single-index client shares the coordinator's sink too."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),
            cache_dir=tmp_path,
            indexes=[IndexConfig("custom", "https://custom.example/")],
        )
        try:
            client = coord._build_client()
            assert isinstance(client, CachedAsyncSimpleClient)
            assert client._parsed_stats is coord.parsed_cache_stats
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
        """Wait for the prefetch of the wheel the listing publishes first."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not coord.index.has_metadata(
            "foo", "1.0", f"https://f.example/{_SIBLING_LINUX}.metadata"
        ):
            time.sleep(0.01)

    @respx.mock
    def test_listing_prefetch_takes_the_wheel_the_provider_will_pick(self) -> None:
        """The prefetch fetches one wheel per version: the first with a sidecar."""
        linux, win = self._routes()
        with _coord() as coord:
            coord.request_listing("foo").wait(timeout=5)
            self._await_metadata(coord)
            text = coord.index.get_metadata(
                "foo", "1.0", f"https://f.example/{_SIBLING_LINUX}.metadata"
            )
            assert text == _SIBLING_LINUX_BODY.decode()
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
            assert coord.index.get_metadata_error("foo", "1.0", win_url) is None
            win_text = coord.index.get_metadata("foo", "1.0", win_url)
            assert win_text == _SIBLING_WIN_BODY.decode()

        assert linux.call_count == 1
        assert win.call_count == 1


_RANGE_META = b"Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n\nBody.\n"
_RANGE_META_HEADERS = "Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n\n"
_RANGE_URL = "https://files.example.org/packages/widget-1.0-py3-none-any.whl"


def _build_range_wheel(
    *,
    dist_info: str | None = "widget-1.0.dist-info",
    metadata: bytes | None = _RANGE_META,
) -> bytes:
    """Build a small wheel zip in memory for range-reader round trips."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("widget/__init__.py", b"value = 1\n")
        if dist_info is not None:
            if metadata is not None:
                zf.writestr(f"{dist_info}/METADATA", metadata)
            zf.writestr(f"{dist_info}/WHEEL", b"Wheel-Version: 1.0\n")
    return buf.getvalue()


def _parse_range(value: str) -> tuple[str, int, int]:
    body = value.removeprefix("bytes=")
    if body.startswith("-"):
        return ("suffix", int(body[1:]), 0)
    start, _, end = body.partition("-")
    return ("absolute", int(start), int(end))


class _FakeRangeResponse:
    def __init__(self, status: int, headers: dict[str, str], content: bytes) -> None:
        self._status = status
        self._headers = headers
        self._content = content

    @property
    def status_code(self) -> int:
        return self._status

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8")

    def json(self) -> object:
        return json.loads(self._content)

    def raise_for_status(self) -> None:
        if self._status >= 400:
            msg = f"HTTP {self._status}"
            raise HttpError(msg)


class FakeRangeTransport:
    """Serve wheel bytes over ranges per a named server-quirk mode."""

    def __init__(self, mode: str, wheel_bytes: bytes) -> None:
        self.mode = mode
        self.wheel = wheel_bytes
        self.total = len(wheel_bytes)
        self.requests: list[str] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeRangeResponse:
        await asyncio.sleep(0)
        headers = headers or {}
        rng = headers["Range"]
        self.requests.append(rng)
        return self._respond(rng)

    async def aclose(self) -> None:
        return None

    def _partial(
        self, start: int, end: int, *, gzip: bool = False
    ) -> _FakeRangeResponse:
        end = min(end, self.total - 1)
        data = self.wheel[start : end + 1]
        headers = {"content-range": f"bytes {start}-{end}/{self.total}"}
        if gzip:
            headers["content-encoding"] = "gzip"
        return _FakeRangeResponse(206, headers, data)

    def _respond(self, rng: str) -> _FakeRangeResponse:
        kind, a, b = _parse_range(rng)
        if self.mode == "well_behaved":
            if kind == "suffix":
                return self._partial(max(0, self.total - a), self.total - 1)
            return self._partial(a, b)
        if self.mode == "gzip_range":
            if kind == "suffix":
                return self._partial(max(0, self.total - a), self.total - 1, gzip=True)
            return self._partial(a, b, gzip=True)
        if self.mode == "error_500":
            return _FakeRangeResponse(500, {}, b"")
        msg = f"unknown mode {self.mode}"
        raise AssertionError(msg)


_RANGE_URL_A = "https://files.example.org/packages/widget-1.0-cp312-linux.whl"
_RANGE_URL_B = "https://files.example.org/packages/widget-1.0-cp312-macos.whl"


class TestRangeMetadataIndex:
    def test_store_range_metadata_fires_pending(self) -> None:
        idx = InMemoryIndex()
        key = f"range:widget:1.0:{_RANGE_URL_A}"
        event, _ = idx.get_or_create_pending(key)
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_A, "META")
        assert event.is_set()
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) == "META"
        assert not idx.metadata_from_sdist("widget", "1.0")

    def test_store_range_metadata_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_A, "META")
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) == "META"

    def test_sibling_wheels_keep_independent_range_slots(self) -> None:
        idx = InMemoryIndex()
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_A, "META_A")
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_B, "META_B")
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) == "META_A"
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_B) == "META_B"

    def test_store_range_absent_fires_pending_without_slot(self) -> None:
        idx = InMemoryIndex()
        key = f"range:widget:1.0:{_RANGE_URL_A}"
        event, _ = idx.get_or_create_pending(key)
        idx.store_range_absent("widget", "1.0", _RANGE_URL_A)
        assert event.is_set()
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) is None

    def test_store_range_absent_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_range_absent("widget", "1.0", _RANGE_URL_A)
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) is None

    def test_store_range_error_fires_pending_and_records(self) -> None:
        idx = InMemoryIndex()
        key = f"range:widget:1.0:{_RANGE_URL_A}"
        event, _ = idx.get_or_create_pending(key)
        error = MalformedSimpleResponseError("bad")
        idx.store_range_error("widget", "1.0", _RANGE_URL_A, error)
        assert event.is_set()
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_A) is error
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) is None

    def test_store_range_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        error = HttpError("boom")
        idx.store_range_error("widget", "1.0", _RANGE_URL_A, error)
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_A) is error

    def test_sibling_range_error_spares_the_other_wheel(self) -> None:
        idx = InMemoryIndex()
        error = HttpError("boom")
        idx.store_range_error("widget", "1.0", _RANGE_URL_A, error)
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_B, "META_B")
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_A) is error
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_B) is None
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_B) == "META_B"

    def test_range_outcome_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_range_outcome("widget", "1.0", _RANGE_URL_A) is None
        idx.store_range_outcome("widget", "1.0", _RANGE_URL_A, RangeOutcome.PARTIAL)
        assert (
            idx.get_range_outcome("widget", "1.0", _RANGE_URL_A) is RangeOutcome.PARTIAL
        )
        # Keyed like the read itself: a sibling wheel's slot stays empty.
        assert idx.get_range_outcome("widget", "1.0", _RANGE_URL_B) is None


def _crashed_range_coord() -> FetchCoordinator:
    """Return a coordinator whose loop is dead, crash flag cleared."""
    coord = _coord(index_routes=[IndexRoute(name="foo", index="missing")])
    coord.start()
    assert coord._thread is not None
    coord._thread.join(timeout=5)
    coord._crashed = False
    return coord


class TestRangeMetadataCoordinator:
    def test_single_flight_one_enqueue(self) -> None:
        """A second request for a read still in flight shares its event."""
        release = threading.Event()

        class _HeldRangeTransport(FakeRangeTransport):
            """Hold every range read until the test releases it."""

            async def get(
                self, url: str, *, headers: dict[str, str] | None = None
            ) -> _FakeRangeResponse:
                await asyncio.to_thread(release.wait, 5)
                return await super().get(url, headers=headers)

        transport = _HeldRangeTransport("well_behaved", _build_range_wheel())
        with FetchCoordinator(transport=transport) as coord:  # type: ignore[arg-type]
            submitted: list[FetchRequest] = []
            orig = coord._submit

            def spy(item: object) -> None:
                if (
                    isinstance(item, FetchRequest)
                    and item.kind is FetchKind.RANGE_METADATA
                ):
                    submitted.append(item)
                orig(item)  # type: ignore[arg-type]

            coord._submit = spy  # type: ignore[method-assign, assignment]
            e1 = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            e2 = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            assert e1 is e2

            release.set()
            assert e1.wait(timeout=5)
            assert len(submitted) == 1
            assert (
                coord.index.get_metadata("widget", "1.0", _RANGE_URL)
                == _RANGE_META_HEADERS
            )

    def test_distinct_wheel_urls_enqueue_separately(self) -> None:
        transport = FakeRangeTransport("well_behaved", _build_range_wheel())
        other_url = _RANGE_URL.replace("py3-none-any", "cp312-cp312-macosx")
        with FetchCoordinator(transport=transport) as coord:  # type: ignore[arg-type]
            submitted: list[FetchRequest] = []
            orig = coord._submit

            def spy(item: object) -> None:
                if (
                    isinstance(item, FetchRequest)
                    and item.kind is FetchKind.RANGE_METADATA
                ):
                    submitted.append(item)
                orig(item)  # type: ignore[arg-type]

            coord._submit = spy  # type: ignore[method-assign, assignment]
            e1 = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            e2 = coord.request_range_metadata("widget", "1.0", other_url)
            assert e1 is not e2
            assert e1.wait(timeout=5)
            assert e2.wait(timeout=5)
            assert len(submitted) == 2

    def test_handler_stores_text(self) -> None:
        transport = FakeRangeTransport("well_behaved", _build_range_wheel())
        with FetchCoordinator(transport=transport) as coord:  # type: ignore[arg-type]
            event = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            assert event.wait(timeout=5)
            assert (
                coord.index.get_metadata("widget", "1.0", _RANGE_URL)
                == _RANGE_META_HEADERS
            )
            assert not coord.index.metadata_from_sdist("widget", "1.0")
            assert coord.index.get_range_outcome("widget", "1.0", _RANGE_URL) in (
                RangeOutcome.PARTIAL,
                RangeOutcome.FULL_BODY,
            )

    def test_handler_stores_absent_on_missing(self) -> None:
        transport = FakeRangeTransport(
            "well_behaved", _build_range_wheel(dist_info=None)
        )
        with FetchCoordinator(transport=transport) as coord:  # type: ignore[arg-type]
            event = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            assert event.wait(timeout=5)
            assert coord.index.get_metadata("widget", "1.0", _RANGE_URL) is None
            assert coord.index.get_metadata_error("widget", "1.0", _RANGE_URL) is None
            assert (
                coord.index.get_range_outcome("widget", "1.0", _RANGE_URL)
                is RangeOutcome.MISSING
            )

    def test_handler_stores_absent_on_unsupported(self) -> None:
        transport = FakeRangeTransport("gzip_range", _build_range_wheel())
        with FetchCoordinator(transport=transport) as coord:  # type: ignore[arg-type]
            event = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            assert event.wait(timeout=5)
            assert coord.index.get_metadata("widget", "1.0", _RANGE_URL) is None
            assert (
                coord.index.get_range_outcome("widget", "1.0", _RANGE_URL)
                is RangeOutcome.UNSUPPORTED
            )

    def test_handler_stores_error_on_bad_utf8(self) -> None:
        transport = FakeRangeTransport(
            "well_behaved", _build_range_wheel(metadata=b"\xff\xfe not utf-8")
        )
        with FetchCoordinator(transport=transport) as coord:  # type: ignore[arg-type]
            event = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            assert event.wait(timeout=5)
            assert coord.index.get_metadata("widget", "1.0", _RANGE_URL) is None
            error = coord.index.get_metadata_error("widget", "1.0", _RANGE_URL)
            assert isinstance(error, MalformedSimpleResponseError)

    def test_handler_stores_error_on_transport_failure(self) -> None:
        transport = FakeRangeTransport("error_500", _build_range_wheel())
        with FetchCoordinator(transport=transport) as coord:  # type: ignore[arg-type]
            event = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            assert event.wait(timeout=5)
            assert coord.index.get_metadata("widget", "1.0", _RANGE_URL) is None
            error = coord.index.get_metadata_error("widget", "1.0", _RANGE_URL)
            assert isinstance(error, HttpError)

    def test_offline_cold_stores_absent(self) -> None:
        transport = FakeRangeTransport("well_behaved", _build_range_wheel())
        with FetchCoordinator(transport=transport, offline=True) as coord:  # type: ignore[arg-type]
            event = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
            assert event.wait(timeout=5)
            assert coord.index.get_metadata("widget", "1.0", _RANGE_URL) is None
            assert coord.index.get_metadata_error("widget", "1.0", _RANGE_URL) is None
            assert transport.requests == []
        assert not coord._crashed

    def test_refused_request_releases_waiter(self) -> None:
        coord = _crashed_range_coord()
        event = coord.request_range_metadata("widget", "1.0", _RANGE_URL)
        assert event.is_set()
        assert coord.index.get_metadata("widget", "1.0", _RANGE_URL) is None
        assert coord.index.get_metadata_error("widget", "1.0", _RANGE_URL) is None


_PYPI = "https://pypi.org/simple/"


class _RaiseOnGetTransport:
    """A transport whose ``get`` raises: proves a warm hit touches no network."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        msg = f"unexpected network fetch: {url}"
        raise HttpError(msg)

    async def aclose(self) -> None:
        return None


def _sync_sdist(version: str = "1.0", name: str = "pkg") -> SdistFile:
    """A minimal SdistFile for warm parsed-listing round trips."""
    return SdistFile(
        filename=f"{name}-{version}.tar.gz",
        url=f"https://f.example/{name}-{version}.tar.gz",
        version=version,
        requires_python=None,
        upload_time=None,
    )


def _sync_wheel(version: str = "1.0", name: str = "pkg") -> WheelFile:
    """A minimal sidecar-bearing WheelFile for warm-hit prefetch round trips."""
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://f.example/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        metadata_hash=None,
    )


def _warm_parsed(
    cache: OnDiskCache,
    package: str,
    files: Sequence[WheelFile | SdistFile],
    *,
    body: bytes | None = None,
    fresh: bool = True,
    blob: bool = True,
    digest_override: str | None = None,
    zip_sdists: frozenset[str] = frozenset(),
) -> None:
    """Write a policy sidecar, raw body, and parsed blob for ``package``.

    ``fresh`` controls the freshness window; ``blob`` omits the parsed blob;
    ``digest_override`` binds the blob to a foreign body so ``decode`` misses;
    ``zip_sdists`` seeds the releases the blob says the parse dropped.
    """
    if body is None:
        body = json.dumps(LISTING_JSON).encode()
    now = int(time.time())
    if fresh:
        policy = CachePolicy(fetched_at=now, max_age=3600, etag="x")
    else:
        policy = CachePolicy(fetched_at=now - 10_000, max_age=1, etag="x")
    digest = cache.put_simple(package, body, policy)
    if blob:
        bound = digest_override if digest_override is not None else digest
        cache.put_simple_parsed(package, encode_parsed(list(files), bound, zip_sdists))


def _spy_submit(coord: FetchCoordinator) -> list[object]:
    """Record every ``_submit`` item, still delegating to the real submit."""
    calls: list[object] = []
    original = coord._submit

    def wrapper(item: object) -> None:
        calls.append(item)
        original(item)  # type: ignore[arg-type]

    coord._submit = wrapper  # type: ignore[method-assign]
    return calls


def _listing_submits(calls: Sequence[object]) -> list[object]:
    """The recorded submits that are LISTING requests."""
    return [
        item
        for item in calls
        if isinstance(item, FetchRequest) and item.kind is FetchKind.LISTING
    ]


class TestWarmSyncListingPath:
    """The synchronous warm-hit fast path for ``request_listing`` (C5, S-ALL)."""

    def test_eligibility_gate_single_index_ondisk(self, tmp_path: Path) -> None:
        """The gate is on for a single non-file index over an OnDiskCache."""
        coord = _coord(cache_dir=tmp_path)
        try:
            assert coord._sync_listing_enabled is True
        finally:
            coord.shutdown()

    def test_eligibility_off_for_null_cache(self) -> None:
        coord = _coord()
        try:
            assert coord._sync_listing_enabled is False
        finally:
            coord.shutdown()

    def test_eligibility_off_for_multi_index(self, tmp_path: Path) -> None:
        coord = _coord(
            cache_dir=tmp_path,
            indexes=[
                IndexConfig("pypi", _PYPI),
                IndexConfig("alt", "https://alt.example/"),
            ],
        )
        try:
            assert coord._sync_listing_enabled is False
        finally:
            coord.shutdown()

    def test_eligibility_off_for_file_index(self, tmp_path: Path) -> None:
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        coord = _coord(
            cache_dir=tmp_path / "cache",
            indexes=[IndexConfig("local", wheelhouse.as_uri())],
        )
        try:
            assert coord._sync_listing_enabled is False
        finally:
            coord.shutdown()

    def test_eligibility_off_for_bare_file_url(self, tmp_path: Path) -> None:
        """The other RFC 8089 spelling is a file index too, so the gate is off."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        coord = _coord(
            cache_dir=tmp_path / "cache",
            indexes=[IndexConfig("local", f"file:{wheelhouse}")],
        )
        try:
            assert coord._sync_listing_enabled is False
        finally:
            coord.shutdown()

    def test_probe_cache_matches_a_pinned_serializations_dir(
        self, tmp_path: Path
    ) -> None:
        """The probe reads the serialization-partitioned dir the client writes."""
        cfg = IndexConfig("pypi", _PYPI, serialization=SimpleSerialization.HTML)
        coord = _coord(cache_dir=tmp_path, indexes=[cfg])
        try:
            pinned = OnDiskCache(tmp_path, _PYPI, serialization=cfg.serialization)
            _warm_parsed(pinned, "pkg", [_sync_sdist("1.0")])
            assert coord._sync_listing_enabled is True
            assert coord._try_listing_sync("pkg") is not None
        finally:
            coord.shutdown()

    def test_probe_ignores_the_unpinned_dir_under_a_pin(self, tmp_path: Path) -> None:
        """A negotiated entry never answers for an index pinned to one form."""
        cfg = IndexConfig("pypi", _PYPI, serialization=SimpleSerialization.HTML)
        coord = _coord(cache_dir=tmp_path, indexes=[cfg])
        try:
            _warm_parsed(OnDiskCache(tmp_path, _PYPI), "pkg", [_sync_sdist("1.0")])
            assert coord._try_listing_sync("pkg") is None
        finally:
            coord.shutdown()

    def test_eligibility_off_for_routed(self, tmp_path: Path) -> None:
        coord = _coord(
            cache_dir=tmp_path,
            index_routes=[IndexRoute(name="pkg", index="pypi")],
        )
        try:
            assert coord._sync_listing_enabled is False
        finally:
            coord.shutdown()

    @respx.mock
    def test_warm_hit_serves_without_listing_fetch(self, tmp_path: Path) -> None:
        """A fresh parsed hit is served inline: no LISTING submit, prefetch fired."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_sdist("1.0"), _sync_sdist("2.0")]
        _warm_parsed(cache, "pkg", files)

        prefetched: list[tuple[str, object]] = []

        with _coord(cache_dir=tmp_path) as coord:
            coord._prefetch_metadata_after_listing = (  # type: ignore[method-assign]
                lambda package, records: prefetched.append((package, list(records)))
            )
            calls = _spy_submit(coord)
            event = coord.request_listing("pkg")

            assert event.is_set()
            assert coord.index.get_listing("pkg") == files
            assert coord.index.get_listing_index("pkg") == "pypi"
            assert _listing_submits(calls) == []
            assert coord.warm_sync_stats.listing_hits == 1
            assert coord.warm_sync_stats.listing_declines == 0
            coord.shutdown()
            assert prefetched == [("pkg", files)]

    @respx.mock
    def test_warm_hit_fires_progress_hook(self, tmp_path: Path) -> None:
        """A sync hit ticks ``on_fetch`` once, matching the async path's count."""
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "pkg", [_sync_sdist("1.0")])

        ticks: list[int] = []
        with _coord(cache_dir=tmp_path, on_fetch=lambda: ticks.append(1)) as coord:
            coord.request_listing("pkg")
            coord.shutdown()
            assert ticks == [1]
            assert coord.warm_sync_stats.listing_hits == 1

    @respx.mock
    def test_warm_hit_no_network(self, tmp_path: Path) -> None:
        """A warm hit touches no transport at all."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_sdist("1.0")]
        _warm_parsed(cache, "pkg", files)

        coord = FetchCoordinator(
            transport=_RaiseOnGetTransport(),  # type: ignore[arg-type]
            cache_dir=tmp_path,
        )
        coord._warm_sync_min_blob_bytes = 0
        with coord:
            coord._prefetch_metadata_after_listing = (  # type: ignore[method-assign]
                lambda package, records: None
            )
            event = coord.request_listing("pkg")
            assert event.is_set()
            assert coord.index.get_listing("pkg") == files
            assert not coord._crashed

    @respx.mock
    def test_preexisting_pending_joins_without_probe(self, tmp_path: Path) -> None:
        """A pending key joins the existing event: no probe, store, or submit."""
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "pkg", [_sync_sdist("1.0")])

        with _coord(cache_dir=tmp_path) as coord:
            probed: list[str] = []

            def _probe(package: str) -> None:
                probed.append(package)

            coord._try_listing_sync = _probe  # type: ignore[method-assign]
            calls = _spy_submit(coord)
            claimed, _ = coord.index.get_or_create_pending("listing:pkg")

            event = coord.request_listing("pkg")

            assert event is claimed
            assert probed == []
            assert _listing_submits(calls) == []
            assert coord.index.get_listing("pkg") is None
            assert coord.warm_sync_stats.listing_hits == 0
            assert coord.warm_sync_stats.listing_declines == 0

    @respx.mock
    def test_decline_stale_online(self, tmp_path: Path) -> None:
        """A stale entry online declines to async and revalidates."""
        respx.get(f"{_PYPI}pkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON, headers={"etag": "v2"})
        )
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "pkg", [_sync_sdist("1.0")], fresh=False)

        with _coord(cache_dir=tmp_path) as coord:
            calls = _spy_submit(coord)
            coord.request_listing("pkg").wait(timeout=5)

            assert coord.index.get_listing("pkg") is not None
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_stale_online == 1
            assert coord.warm_sync_stats.listing_declines == 1
            assert coord.warm_sync_stats.listing_hits == 0

    @respx.mock
    def test_decline_no_policy_cold(self, tmp_path: Path) -> None:
        """A cold cache has no policy: decline to async fetch."""
        respx.get(f"{_PYPI}pkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        with _coord(cache_dir=tmp_path) as coord:
            calls = _spy_submit(coord)
            coord.request_listing("pkg").wait(timeout=5)

            assert coord.index.get_listing("pkg") is not None
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_no_policy == 1
            assert coord.warm_sync_stats.listing_declines == 1

    @respx.mock
    def test_decline_no_blob(self, tmp_path: Path) -> None:
        """A fresh policy with no parsed blob declines; async rebuilds from body."""
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "testpkg", [_sync_sdist("1.0")], blob=False)

        with _coord(cache_dir=tmp_path) as coord:
            calls = _spy_submit(coord)
            coord.request_listing("testpkg").wait(timeout=5)

            assert coord.index.get_listing("testpkg") is not None
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_no_blob == 1
            assert coord.warm_sync_stats.listing_declines == 1

    def test_gate_default_threshold(self, tmp_path: Path) -> None:
        """A fresh coordinator carries the module default blob-size threshold."""
        coord = FetchCoordinator(
            transport=HttpxAsyncTransport(),  # type: ignore[arg-type]
            cache_dir=tmp_path,
        )
        try:
            assert coord._warm_sync_min_blob_bytes == _WARM_SYNC_MIN_BLOB_BYTES
        finally:
            coord.shutdown()

    def test_gate_admits_when_sync_disabled(self) -> None:
        """The gate admits (defers to the eligibility gate) when sync is off."""
        coord = _coord()
        try:
            assert coord._sync_listing_enabled is False
            assert coord._overlap_gate_admits("pkg") is True
        finally:
            coord.shutdown()

    @respx.mock
    def test_gate_declines_small_blob_to_async(self, tmp_path: Path) -> None:
        """A parsed blob below the threshold declines to the async path."""
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "pkg", [_sync_sdist("1.0")])

        with _coord(cache_dir=tmp_path) as coord:
            coord._warm_sync_min_blob_bytes = 10**9
            calls = _spy_submit(coord)
            coord.request_listing("pkg").wait(timeout=5)

            assert coord.index.get_listing("pkg") is not None
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.listing_hits == 0
            assert coord.warm_sync_stats.declined_small_blob == 1
            assert coord.warm_sync_stats.listing_declines == 1

    @respx.mock
    def test_gate_admits_blob_at_threshold(self, tmp_path: Path) -> None:
        """A parsed blob at or above the threshold serves inline."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_sdist("1.0")]
        _warm_parsed(cache, "pkg", files)
        size = cache.get_simple_parsed_size("pkg")
        assert size is not None

        with _coord(cache_dir=tmp_path) as coord:
            coord._warm_sync_min_blob_bytes = size
            calls = _spy_submit(coord)
            event = coord.request_listing("pkg")

            assert event.is_set()
            assert coord.index.get_listing("pkg") == files
            assert _listing_submits(calls) == []
            assert coord.warm_sync_stats.listing_hits == 1
            assert coord.warm_sync_stats.declined_small_blob == 0

    @respx.mock
    def test_gate_admits_when_blob_size_unknown(self, tmp_path: Path) -> None:
        """A missing blob has no size, so the gate admits and _try declines."""
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "pkg", [_sync_sdist("1.0")], blob=False)
        assert cache.get_simple_parsed_size("pkg") is None

        with _coord(cache_dir=tmp_path) as coord:
            coord._warm_sync_min_blob_bytes = 10**9
            coord.request_listing("pkg").wait(timeout=5)

            assert coord.warm_sync_stats.declined_small_blob == 0
            assert coord.warm_sync_stats.declined_no_blob == 1
            assert coord.warm_sync_stats.listing_declines == 1

    @respx.mock
    def test_decline_digest_mismatch(self, tmp_path: Path) -> None:
        """A blob bound to a foreign body declines; the sync path never rebuilds."""
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(
            cache,
            "testpkg",
            [_sync_sdist("1.0")],
            digest_override="0" * 64,
        )

        with _coord(cache_dir=tmp_path) as coord:
            calls = _spy_submit(coord)
            coord.request_listing("testpkg").wait(timeout=5)

            assert coord.index.get_listing("testpkg") is not None
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_no_blob == 1
            assert not coord._crashed

    @respx.mock
    def test_decline_multi_index(self, tmp_path: Path) -> None:
        """A multi-index config is ineligible; it declines to async."""
        respx.get(f"{_PYPI}pkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "pkg", [_sync_sdist("1.0")])

        with _coord(
            cache_dir=tmp_path,
            indexes=[
                IndexConfig("pypi", _PYPI),
                IndexConfig("alt", "https://alt.example/"),
            ],
        ) as coord:
            calls = _spy_submit(coord)
            coord.request_listing("pkg").wait(timeout=5)

            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_ineligible == 1
            assert coord.warm_sync_stats.listing_declines == 1

    @respx.mock
    def test_decline_file_index(self, tmp_path: Path) -> None:
        """A file:// index is ineligible; it declines to the local async path."""
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        with _coord(
            cache_dir=tmp_path / "cache",
            indexes=[IndexConfig("local", wheelhouse.as_uri())],
        ) as coord:
            calls = _spy_submit(coord)
            coord.request_listing("pkg").wait(timeout=5)

            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_ineligible == 1

    @respx.mock
    def test_decline_null_cache(self) -> None:
        """No cache dir means a NullCache: ineligible, declines to async."""
        respx.get(f"{_PYPI}pkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        with _coord() as coord:
            calls = _spy_submit(coord)
            coord.request_listing("pkg").wait(timeout=5)

            assert coord.index.get_listing("pkg") is not None
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_ineligible == 1

    @respx.mock
    def test_decline_offline_cold_stores_empty(self, tmp_path: Path) -> None:
        """Offline with a cold cache declines (no policy) and records empty."""
        with _coord(cache_dir=tmp_path, offline=True) as coord:
            calls = _spy_submit(coord)
            coord.request_listing("missing").wait(timeout=5)

            assert coord.index.get_listing("missing") == []
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.declined_no_policy == 1
            assert not coord._crashed

    def test_start_resets_stats(self, tmp_path: Path) -> None:
        """``start`` zeroes the warm-sync counters for a reused coordinator."""
        coord = _coord(cache_dir=tmp_path)
        try:
            coord.start()
            coord._warm_sync_stats.listing_hits = 7
            coord._warm_sync_stats.declined_no_blob = 3
            coord.shutdown()
            coord.start()
            assert coord.warm_sync_stats == WarmSyncStats()
        finally:
            coord.shutdown()

    @respx.mock
    def test_speculative_skips_probe_and_routes_async(self, tmp_path: Path) -> None:
        """A speculative call skips the sync probe and dispatches async.

        S-CRIT keeps speculative listing reads on the fetcher thread so their
        read+parse work overlaps resolver CPU; only blocking critical-path
        callers serve the warm cache inline.
        """
        cache = OnDiskCache(tmp_path, _PYPI)
        _warm_parsed(cache, "pkg", [_sync_sdist("1.0")])

        with _coord(cache_dir=tmp_path) as coord:
            probed: list[str] = []
            coord._try_listing_sync = probed.append  # type: ignore[method-assign]
            calls = _spy_submit(coord)

            coord.request_listing("pkg", speculative=True).wait(timeout=5)

            assert probed == []
            assert len(_listing_submits(calls)) == 1
            assert coord.warm_sync_stats.listing_hits == 0
            assert coord.warm_sync_stats.listing_declines == 0

    @respx.mock
    def test_default_call_serves_synchronously(self, tmp_path: Path) -> None:
        """The default (critical-path) call still serves the warm cache inline."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_sdist("1.0")]
        _warm_parsed(cache, "pkg", files)

        with _coord(cache_dir=tmp_path) as coord:
            coord._prefetch_metadata_after_listing = (  # type: ignore[method-assign]
                lambda package, records: None
            )
            calls = _spy_submit(coord)

            event = coord.request_listing("pkg")

            assert event.is_set()
            assert coord.index.get_listing("pkg") == files
            assert _listing_submits(calls) == []
            assert coord.warm_sync_stats.listing_hits == 1

    def test_warm_hit_stores_the_releases_the_parse_dropped(
        self, tmp_path: Path
    ) -> None:
        """The blob carries them, so an inline serve reports them as a fetch does.

        Nothing else on this path reads the body a ``.zip`` sdist was listed in.
        """
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_sdist("1.0")]
        _warm_parsed(cache, "pkg", files, zip_sdists=frozenset({"1.0"}))

        with _coord(cache_dir=tmp_path) as coord:
            coord._prefetch_metadata_after_listing = (  # type: ignore[method-assign]
                lambda package, records: None
            )
            calls = _spy_submit(coord)

            coord.request_listing("pkg").wait(timeout=5)

            assert _listing_submits(calls) == []
            assert coord.warm_sync_stats.listing_hits == 1
            assert coord.index.zip_sdist_versions("pkg") == frozenset({"1.0"})

    def test_skew_reader_never_serves_torn_pair(self, tmp_path: Path) -> None:
        """Interleaved writer/reader: every non-None probe is a bound pair."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files_a: list[WheelFile | SdistFile] = [_sync_sdist("1.0")]
        files_b: list[WheelFile | SdistFile] = [_sync_sdist("2.0")]
        _warm_parsed(cache, "pkg", files_a)

        coord = _coord(cache_dir=tmp_path)
        stop = threading.Event()
        errors: list[BaseException] = []
        results: list[object] = []

        def writer() -> None:
            toggle = False
            while not stop.is_set():
                try:
                    if toggle:
                        _warm_parsed(cache, "pkg", files_b)
                        _warm_parsed(cache, "pkg", files_a, blob=False)
                    else:
                        _warm_parsed(cache, "pkg", files_a)
                        _warm_parsed(cache, "pkg", files_b, blob=False)
                except PermissionError:
                    # Windows refuses os.replace onto a file the reader has open;
                    # retry. Production single-flight keeps a sync read and a
                    # fetcher write off the same file, so this race is harness-only.
                    continue
                toggle = not toggle

        def reader() -> None:
            try:
                for _ in range(3000):
                    results.append(coord._try_listing_sync("pkg"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        try:
            w = threading.Thread(target=writer)
            r = threading.Thread(target=reader)
            w.start()
            r.start()
            r.join(timeout=30)
            stop.set()
            w.join(timeout=5)

            assert not errors
            for value in results:
                assert value is None or value.files in (files_a, files_b)
        finally:
            stop.set()
            coord.shutdown()


class TestWarmSyncTailOffload:
    """The post-listing tail runs on the fetcher loop, not the caller thread."""

    @respx.mock
    def test_sync_hit_runs_tail_off_the_caller_thread(self, tmp_path: Path) -> None:
        """The newest-version selection walk executes on the fetcher thread only."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_wheel("1.0"), _sync_wheel("2.0")]
        _warm_parsed(cache, "pkg", files)

        with _coord(cache_dir=tmp_path) as coord:
            fetcher_ident = coord._thread.ident
            caller_ident = threading.get_ident()

            tail_idents: list[int | None] = []
            done = threading.Event()
            original = coord._prefetch_metadata_after_listing

            def _record(package: str, records: object) -> None:
                tail_idents.append(threading.get_ident())
                original(package, records)  # type: ignore[arg-type]
                done.set()

            coord._prefetch_metadata_after_listing = _record  # type: ignore[method-assign]

            meta_idents: list[int] = []

            def _spy_meta(*args: object, **kwargs: object) -> threading.Event:
                meta_idents.append(threading.get_ident())
                ev = threading.Event()
                ev.set()
                return ev

            coord.request_metadata = _spy_meta  # type: ignore[method-assign]

            calls = _spy_submit(coord)
            event = coord.request_listing("pkg")

            assert event.is_set()
            assert coord.index.get_listing("pkg") == files
            assert _listing_submits(calls) == []
            assert done.wait(timeout=5)

            assert tail_idents == [fetcher_ident]
            assert fetcher_ident != caller_ident
            assert meta_idents
            assert all(ident == fetcher_ident for ident in meta_idents)
            assert caller_ident not in meta_idents

    @respx.mock
    def test_offloaded_tail_prefetches_metadata(self, tmp_path: Path) -> None:
        """The offloaded tail enqueues the newest version's metadata and it lands."""
        respx.get(url__regex=r".*\.whl\.metadata$").mock(
            return_value=httpx.Response(200, text="Metadata-Version: 2.1\n")
        )
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_wheel("1.0"), _sync_wheel("2.0")]
        _warm_parsed(cache, "pkg", files)

        with _coord(cache_dir=tmp_path) as coord:
            done = threading.Event()
            original = coord._prefetch_metadata_after_listing

            def _wrap(package: str, records: object) -> None:
                original(package, records)  # type: ignore[arg-type]
                done.set()

            coord._prefetch_metadata_after_listing = _wrap  # type: ignore[method-assign]
            calls = _spy_submit(coord)

            coord.request_listing("pkg")
            assert done.wait(timeout=5)

            meta_submits = {
                (item.package, item.version, item.url)
                for item in calls
                if isinstance(item, FetchRequest) and item.kind is FetchKind.METADATA
            }
            sidecar = "https://f.example/pkg-2.0-py3-none-any.whl.metadata"
            assert meta_submits == {("pkg", "2.0", sidecar)}

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not coord.index.has_metadata(
                "pkg", "2.0", sidecar
            ):
                time.sleep(0.01)
            assert coord.index.has_metadata("pkg", "2.0", sidecar)

    @respx.mock
    def test_dead_loop_runs_tail_inline(self, tmp_path: Path) -> None:
        """No live loop: the tail is a last-resort inline run on the caller."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_sdist("1.0")]
        _warm_parsed(cache, "pkg", files)

        # An unstarted coordinator has no loop, so _post_to_loop declines.
        coord = _coord(cache_dir=tmp_path)
        caller_ident = threading.get_ident()
        idents: list[int] = []
        coord._prefetch_metadata_after_listing = (  # type: ignore[method-assign]
            lambda package, records: idents.append(threading.get_ident())
        )
        try:
            event = coord.request_listing("pkg")

            assert event.is_set()
            assert coord.index.get_listing("pkg") == files
            assert idents == [caller_ident]
            assert coord.warm_sync_stats.listing_hits == 1
            assert not coord._crashed
        finally:
            coord.shutdown()

    def test_post_to_loop_false_on_closed_loop(self, tmp_path: Path) -> None:
        """A closed loop makes call_soon_threadsafe raise; the post declines."""
        coord = _coord(cache_dir=tmp_path)
        loop = asyncio.new_event_loop()
        loop.close()
        coord._loop = loop
        try:
            assert coord._post_to_loop(lambda: None) is False
        finally:
            coord._loop = None
            coord.shutdown()

    @respx.mock
    def test_offloaded_tail_exception_logged_and_listing_survives(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising tail logs WARNING and leaves the served listing intact."""
        cache = OnDiskCache(tmp_path, _PYPI)
        files: list[WheelFile | SdistFile] = [_sync_sdist("1.0")]
        _warm_parsed(cache, "pkg", files)

        ran = threading.Event()

        def _boom(package: str, records: object) -> None:
            ran.set()
            raise RuntimeError("boom")

        with _coord(cache_dir=tmp_path) as coord:
            coord._prefetch_metadata_after_listing = _boom  # type: ignore[method-assign]
            with caplog.at_level(logging.WARNING):
                event = coord.request_listing("pkg")
                assert ran.wait(timeout=5)
                coord.shutdown()

            assert event.is_set()
            assert coord.index.get_listing("pkg") == files

        assert any(
            "listing prefetch tail failed" in record.getMessage()
            for record in caplog.records
        )


class _SdistFilesClient:
    """A client whose PKG-INFO read returns a fixed pair of files."""

    def __init__(self, pyproject: str | None) -> None:
        self._pyproject = pyproject

    async def get_sdist_files(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str | None, str | None]:
        return ("Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n", self._pyproject)


class TestSdistArchiveHolding:
    """Which resolves hold an sdist archive after reading its PKG-INFO."""

    @pytest.mark.parametrize(
        ("config", "holds"),
        [
            (None, False),
            (ResolveInputs(), False),
            (ResolveInputs(build_policy=BuildPolicy.BUILD_REMOTE), True),
            (
                ResolveInputs(
                    package_overrides=(
                        pkg_override("foo", build_policy=BuildPolicy.BUILD_REMOTE),
                    )
                ),
                True,
            ),
            (
                ResolveInputs(
                    index_overrides={
                        "pypi": IndexOverride(build_policy=BuildPolicy.BUILD_REMOTE)
                    }
                ),
                True,
            ),
            (
                ResolveInputs(
                    package_overrides=(
                        pkg_override("foo", build_policy=BuildPolicy.NEVER),
                    )
                ),
                False,
            ),
        ],
    )
    def test_build_remote_anywhere_in_the_config_holds(
        self, config: ResolveInputs | None, holds: bool
    ) -> None:
        assert _builds_remote_sdists(config) is holds

    def _fetched_with_pyproject(
        self, pyproject: str | None, config: ResolveInputs | None
    ) -> tuple[FetchCoordinator, SdistArchiveHold]:
        """Fetch one version's sdist files with its archive already held.

        The hold is attached to the coordinator only when ``config`` would give
        it one, so it comes back untouched for a resolve that holds nothing.
        Returns the coordinator and the hold.
        """
        coord = _coord(build_config=config)
        hold = SdistArchiveHold()
        hold.put("pkg", "1.0", b"archive bytes")
        coord._sdist_archive_hold = hold if coord._holds_sdist_archives else None

        asyncio.run(
            coord._fetch_sdist(
                client=_SdistFilesClient(pyproject),  # type: ignore[arg-type]
                req=FetchRequest(
                    kind=FetchKind.SDIST,
                    package="pkg",
                    version="1.0",
                    url="https://f.example/pkg-1.0.tar.gz",
                ),
            )
        )
        return coord, hold

    def test_a_static_pyproject_releases_the_archive(self) -> None:
        """A pyproject that declares the deps means the version never builds."""
        config = ResolveInputs(build_policy=BuildPolicy.BUILD_REMOTE)
        coord, hold = self._fetched_with_pyproject(
            '[project]\nname = "pkg"\ndependencies = []\n', config
        )

        assert hold.take("pkg", "1.0") is None
        assert coord.index.get_sdist_pyproject("pkg", "1.0") is not None

    def test_a_dynamic_pyproject_keeps_the_archive(self) -> None:
        """A table that defers its deps leaves the build's bytes in place."""
        config = ResolveInputs(build_policy=BuildPolicy.BUILD_REMOTE)
        _, hold = self._fetched_with_pyproject(
            '[project]\nname = "pkg"\ndynamic = ["dependencies"]\n', config
        )

        assert hold.take("pkg", "1.0") == b"archive bytes"

    def test_an_sdist_without_a_pyproject_keeps_the_archive(self) -> None:
        config = ResolveInputs(build_policy=BuildPolicy.BUILD_REMOTE)
        _, hold = self._fetched_with_pyproject(None, config)

        assert hold.take("pkg", "1.0") == b"archive bytes"

    def test_an_unparseable_pyproject_keeps_the_archive_and_the_pkg_info(self) -> None:
        """A pyproject that will not parse reads as one the sdist never shipped.

        The source here starts with a UTF-8 BOM, which tomli rejects.
        """
        config = ResolveInputs(build_policy=BuildPolicy.BUILD_REMOTE)
        coord, hold = self._fetched_with_pyproject(
            '\ufeff[project]\nname = "pkg"\ndependencies = []\n', config
        )

        assert hold.take("pkg", "1.0") == b"archive bytes"
        assert coord.index.get_sdist_pyproject("pkg", "1.0") is None
        assert coord.index.get_metadata("pkg", "1.0") == (
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
        )

    def test_a_resolve_that_holds_nothing_still_stores_the_pyproject(self) -> None:
        """The release is skipped when there is no hold to release from."""
        coord, hold = self._fetched_with_pyproject(
            '[project]\nname = "pkg"\ndependencies = []\n', None
        )

        assert coord._sdist_archive_hold is None
        assert hold.take("pkg", "1.0") == b"archive bytes"
        assert coord.index.get_sdist_pyproject("pkg", "1.0") is not None

    def _sdist_bytes(self) -> bytes:
        """A gzipped sdist carrying a PKG-INFO and no pyproject.toml."""
        body = b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg-1.0/PKG-INFO")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        return buf.getvalue()

    @respx.mock
    def test_a_build_remote_run_reads_the_archive_once(self) -> None:
        """The build's archive is the PKG-INFO read's own download."""
        archive = self._sdist_bytes()
        url = "https://files.example.com/pkg-1.0.tar.gz"
        route = respx.get(url).mock(return_value=httpx.Response(200, content=archive))
        config = ResolveInputs(build_policy=BuildPolicy.BUILD_REMOTE)

        with _coord(build_config=config) as coord:
            coord.request_sdist("pkg", "1.0", url).wait(timeout=5)
            coord.request_sdist_archive("pkg", "1.0", url).wait(timeout=5)

            assert coord.index.get_sdist_archive("pkg", "1.0") == archive
            assert route.call_count == 1

    @respx.mock
    def test_a_run_that_cannot_build_reads_the_archive_twice(self) -> None:
        """A config without build-remote holds nothing, so the build downloads."""
        archive = self._sdist_bytes()
        url = "https://files.example.com/pkg-1.0.tar.gz"
        route = respx.get(url).mock(return_value=httpx.Response(200, content=archive))

        with _coord() as coord:
            coord.request_sdist("pkg", "1.0", url).wait(timeout=5)
            coord.request_sdist_archive("pkg", "1.0", url).wait(timeout=5)

            assert coord.index.get_sdist_archive("pkg", "1.0") == archive
            assert route.call_count == 2

    def test_the_fetcher_loop_drops_what_it_still_holds(self) -> None:
        """Nothing takes from the hold once the loop is gone."""
        config = ResolveInputs(build_policy=BuildPolicy.BUILD_REMOTE)
        coord = _coord(build_config=config)
        coord.start()

        hold = coord._sdist_archive_hold
        assert hold is not None
        hold.put("pkg", "1.0", b"archive bytes")

        coord.shutdown()

        assert hold.take("pkg", "1.0") is None
