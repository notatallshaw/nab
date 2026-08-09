"""Tests for the async HTTP transports and FetchCoordinator transport injection."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import itertools
import json
import ssl
import sys
import tarfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urljoin

import httpx
import pytest
import respx
import truststore
import urllib3
from urllib3.util.retry import RequestHistory

from nab_index.cache import NullCache, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import (
    AsyncSimpleClient,
    MalformedSimpleResponseError,
    SdistFile,
    WheelFile,
    _extract_sdist_files,
)
from nab_index.httpx_async_transport import HttpxAsyncTransport, _HttpxResponse
from nab_index.retry import (
    GET_RETRY,
    MAX_REDIRECTS,
    MAX_RETRIES,
    RETRY_STATUSES,
    next_delay,
)
from nab_index.transport import (
    DEFAULT_HEADERS,
    IDENTITY_HEADERS,
    USER_AGENT,
    AsyncHttpTransport,
    ContentDecodingError,
    HttpError,
    HttpResponse,
    UnserveableUrlError,
    accepts_gzip,
    decode_body,
    raise_for_error_status,
)
from nab_index.urllib3_async_transport import (
    Urllib3AsyncTransport,
    _SSLContext,
    _Urllib3Response,
)
from nab_python.fetch import FetchCoordinator

LISTING_JSON = {
    "meta": {"api-version": "1.0"},
    "name": "pkg",
    "files": [
        {
            "filename": "pkg-1.0-py3-none-any.whl",
            "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
            "dist-info-metadata": {"sha256": "abc"},
        },
    ],
}

LISTING_BODY = b'{"files": []}'
GZIP_BODY = gzip.compress(LISTING_BODY)
# Cut after the 10-byte gzip header but before the trailer: a mid-transfer
# connection drop whose Content-Length matches the bytes that did arrive.
TRUNCATED_GZIP_BODY = GZIP_BODY[: len(GZIP_BODY) // 2]

# Only the first is rejected up front; the rest raise out of urllib3's conversions.
UNPARSEABLE_RETRY_AFTERS = [
    pytest.param("soon", id="not-a-date"),
    pytest.param("Wed, 31 Dec 10000 23:59:59 GMT", id="year-past-datetime"),
    pytest.param("9" * 4301, id="digits-past-int-limit"),
    pytest.param(f"Wed, {'9' * 400} Dec 2020 00:00:00 GMT", id="day-past-float"),
]


class _StubIndex(ThreadingHTTPServer):
    """Loopback index that serves each queued status once, then 200.

    A queued 3xx redirects to ``/redirected/``, whose response pops the
    next queued status like any other request.
    """

    statuses: list[int]
    seen: list[str]
    retry_after: str | None


class _StubIndexHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, _StubIndex)
        self.server.seen.append(self.path)
        status = self.server.statuses.pop(0) if self.server.statuses else 200

        body = b"ok"
        self.send_response(status)
        if 300 <= status < 400:
            self.send_header("Location", "/redirected/")
        if self.server.retry_after is not None:
            self.send_header("Retry-After", self.server.retry_after)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Drop the handler's stderr access log."""


@contextmanager
def _stub_index(
    statuses: list[int], retry_after: str | None = None
) -> Iterator[_StubIndex]:
    server = _StubIndex(("127.0.0.1", 0), _StubIndexHandler)
    server.statuses = list(statuses)
    server.seen = []
    server.retry_after = retry_after
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _GzipStubIndex(ThreadingHTTPServer):
    """Loopback index that serves each queued gzip body once, then GZIP_BODY."""

    bodies: list[bytes]
    encoding: str
    seen: list[str]


class _GzipStubIndexHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, _GzipStubIndex)
        self.server.seen.append(self.path)
        body = self.server.bodies.pop(0) if self.server.bodies else GZIP_BODY

        self.send_response(200)
        self.send_header("Content-Encoding", self.server.encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Drop the handler's stderr access log."""


@contextmanager
def _gzip_stub_index(
    bodies: list[bytes], encoding: str = "gzip"
) -> Iterator[_GzipStubIndex]:
    server = _GzipStubIndex(("127.0.0.1", 0), _GzipStubIndexHandler)
    server.bodies = list(bodies)
    server.encoding = encoding
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _ArtifactStubIndex(ThreadingHTTPServer):
    """Loopback index that labels a body from its filename.

    ``mimetypes.guess_type("demo-1.0.tar.gz")`` reports a gzip encoding, so a
    static file server sends the archive's own bytes under
    ``Content-Encoding: gzip`` whatever the request asked for.
    """

    body: bytes
    accept_encoding: list[str | None]


class _ArtifactStubIndexHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, _ArtifactStubIndex)
        self.server.accept_encoding.append(self.headers.get("Accept-Encoding"))
        body = self.server.body

        self.send_response(200)
        self.send_header("Content-Type", "application/x-tar")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Drop the handler's stderr access log."""


@contextmanager
def _artifact_stub_index(body: bytes) -> Iterator[_ArtifactStubIndex]:
    server = _ArtifactStubIndex(("127.0.0.1", 0), _ArtifactStubIndexHandler)
    server.body = body
    server.accept_encoding = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _UserAgentStubIndex(ThreadingHTTPServer):
    """Loopback index that records each request's User-Agent."""

    user_agents: list[str | None]


class _UserAgentStubIndexHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, _UserAgentStubIndex)
        self.server.user_agents.append(self.headers.get("User-Agent"))

        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Drop the handler's stderr access log."""


@contextmanager
def _user_agent_stub_index() -> Iterator[_UserAgentStubIndex]:
    server = _UserAgentStubIndex(("127.0.0.1", 0), _UserAgentStubIndexHandler)
    server.user_agents = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _MovedIndex(ThreadingHTTPServer):
    """Loopback index that moves its project page, mapping path to Location.

    ``transient`` queues statuses served once each after the hops are done,
    for a retryable blip between the final redirect and the body.
    """

    hops: dict[str, str]
    body: bytes
    seen: list[str]
    transient: list[int]


class _MovedIndexHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, _MovedIndex)
        self.server.seen.append(self.path)
        location = self.server.hops.get(self.path)
        if location is not None:
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.server.transient:
            self.send_response(self.server.transient.pop(0))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.pypi.simple.v1+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Drop the handler's stderr access log."""


@contextmanager
def _moved_index(
    hops: dict[str, str], body: bytes, transient: Sequence[int] = ()
) -> Iterator[_MovedIndex]:
    server = _MovedIndex(("127.0.0.1", 0), _MovedIndexHandler)
    server.hops = dict(hops)
    server.body = body
    server.seen = []
    server.transient = list(transient)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the httpx transport's backoff sleeps instead of taking them."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(
        "nab_index.httpx_async_transport.asyncio.sleep",
        fake_sleep,
    )
    return delays


@pytest.fixture
def thread_slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the urllib3 transport's truncation-retry sleeps instead of taking them."""
    delays: list[float] = []
    monkeypatch.setattr(
        "nab_index.urllib3_async_transport.time.sleep",
        delays.append,
    )
    return delays


def _requests_before_giving_up(classes: Sequence[str]) -> int:
    """Requests urllib3 issues for one URL whose failures cycle through ``classes``."""
    failures: dict[str, dict[str, Any]] = {
        "status": {"response": urllib3.HTTPResponse(status=503)},
        "connect": {"error": urllib3.exceptions.ConnectTimeoutError()},
    }

    retry = GET_RETRY
    issued = 0

    # Bounded so a policy that never exhausts fails the assertion instead of hanging.
    for name in itertools.islice(itertools.cycle(classes), 50):
        issued += 1
        try:
            retry = retry.increment("GET", "/pkg/", **failures[name])
        except urllib3.exceptions.MaxRetryError:
            break

    return issued


def _assert_jittered_backoff_schedule(delays: list[float]) -> None:
    """The full three-retry schedule: immediate, then 0.5 and 1.0 plus jitter."""
    assert len(delays) == MAX_RETRIES
    assert delays[0] == 0.0
    assert 0.5 <= delays[1] < 0.5 + GET_RETRY.backoff_jitter
    assert 1.0 <= delays[2] < 1.0 + GET_RETRY.backoff_jitter


class TestRetryPolicy:
    """The retry policy both transports share."""

    def test_transient_status_is_retried_without_retry_after(self) -> None:
        """A bare 5xx/429 is retried; urllib3's default only retries with Retry-After."""
        assert all(
            GET_RETRY.is_retry("GET", status, has_retry_after=False)
            for status in RETRY_STATUSES
        )

    def test_client_error_is_not_retried(self) -> None:
        """A 404 (absent package) and a 403 are the index's answer."""
        assert not GET_RETRY.is_retry("GET", 404, has_retry_after=False)
        assert not GET_RETRY.is_retry("GET", 403, has_retry_after=False)

    def test_request_timeout_is_retried(self) -> None:
        """A 408 says the server gave up waiting, so the client may repeat the GET."""
        assert 408 in RETRY_STATUSES
        assert GET_RETRY.is_retry("GET", 408, has_retry_after=False)
        assert GET_RETRY.is_retry("GET", 408, has_retry_after=True)

    def test_cloudflare_transient_5xx_is_retried(self) -> None:
        """Cloudflare's 520-524 and 527 origin errors are blips, so they retry."""
        for status in (520, 521, 522, 523, 524, 527):
            assert status in RETRY_STATUSES
            assert GET_RETRY.is_retry("GET", status, has_retry_after=False)

    def test_budget_is_bounded(self) -> None:
        """Every failure class is capped, so nothing retries forever."""
        assert GET_RETRY.total is None
        assert GET_RETRY.connect == MAX_RETRIES
        assert GET_RETRY.read == MAX_RETRIES
        assert GET_RETRY.status == MAX_RETRIES
        assert GET_RETRY.other == MAX_RETRIES

    def test_each_failure_class_has_its_own_budget(self) -> None:
        """Each class carries its own MAX_RETRIES, so mixed failures retry for longer."""
        assert _requests_before_giving_up(["status"]) == MAX_RETRIES + 1
        assert _requests_before_giving_up(["connect"]) == MAX_RETRIES + 1
        assert _requests_before_giving_up(["status", "connect"]) == 2 * MAX_RETRIES + 1

    def test_redirects_have_their_own_budget(self) -> None:
        """A redirect is not a failure, so it never spends a transient retry."""
        assert GET_RETRY.redirect == MAX_REDIRECTS

    def test_exhausted_status_retries_keep_the_response(self) -> None:
        """A persistent 503 surfaces as HTTP 503, not as a retry error."""
        assert GET_RETRY.raise_on_status is False

    def test_backoff_grows_between_attempts(self) -> None:
        """The first retry is immediate, then urllib3's exponential schedule plus jitter."""
        assert next_delay(1) == 0.0
        assert 0.5 <= next_delay(2) < 0.5 + GET_RETRY.backoff_jitter
        assert 1.0 <= next_delay(3) < 1.0 + GET_RETRY.backoff_jitter

    def test_backoff_carries_jitter_to_desynchronize_retries(self) -> None:
        """Repeated backoff for one failure count spreads across the jitter window."""
        assert GET_RETRY.backoff_jitter > 0.0
        delays = {next_delay(2) for _ in range(200)}
        assert len(delays) > 1
        assert all(0.5 <= d < 0.5 + GET_RETRY.backoff_jitter for d in delays)

    def test_retry_after_overrides_backoff(self) -> None:
        assert next_delay(1, "2") == 2.0

    def test_retry_after_is_bounded(self) -> None:
        """A long Retry-After cannot park the resolve."""
        assert next_delay(1, "3600") == 10.0
        assert GET_RETRY.get_retry_after(_urllib3_response(503, "3600")) == 10.0

    @pytest.mark.parametrize("retry_after", UNPARSEABLE_RETRY_AFTERS)
    def test_unparseable_retry_after_falls_back_to_backoff(
        self, retry_after: str
    ) -> None:
        assert 0.5 <= next_delay(2, retry_after) < 0.5 + GET_RETRY.backoff_jitter
        assert GET_RETRY.get_retry_after(_urllib3_response(503, retry_after)) is None

    def test_absent_retry_after_falls_back_to_backoff(self) -> None:
        assert GET_RETRY.get_retry_after(_urllib3_response(503, None)) is None

    def test_bound_survives_urllib3s_retry_copies(self) -> None:
        """urllib3 clones the policy per attempt, so the bound must clone with it."""
        retry = GET_RETRY.increment(method="GET", url="/pkg/", error=OSError("boom"))
        assert retry.get_retry_after(_urllib3_response(503, "3600")) == 10.0


def _urllib3_response(status: int, retry_after: str | None) -> urllib3.BaseHTTPResponse:
    response = MagicMock(spec=urllib3.BaseHTTPResponse)
    response.status = status
    response.headers = {} if retry_after is None else {"Retry-After": retry_after}
    return response


class TestRaiseForErrorStatus:
    """Which statuses name the URL, and which only name the moment."""

    @pytest.mark.parametrize("status", [200, 203, 204, 301, 304, 399])
    def test_status_under_400_does_not_raise(self, status: int) -> None:
        assert raise_for_error_status(status, "https://example.com/") is None

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
    def test_non_retried_client_error_is_a_verdict_on_the_url(
        self, status: int
    ) -> None:
        """A 4xx the retry policy leaves alone raises the narrow subclass."""
        assert status not in RETRY_STATUSES
        with pytest.raises(UnserveableUrlError, match=f"HTTP {status} for"):
            raise_for_error_status(status, "https://example.com/pkg.metadata")

    @pytest.mark.parametrize("status", sorted(RETRY_STATUSES))
    def test_retried_status_stays_a_bare_http_error(self, status: int) -> None:
        """A status the retry policy calls a blip must not read as a verdict.

        Includes the two transient client errors, 408 and 429, which are 4xx
        but say nothing about the URL.
        """
        with pytest.raises(HttpError) as excinfo:
            raise_for_error_status(status, "https://example.com/pkg.metadata")
        assert not isinstance(excinfo.value, UnserveableUrlError)

    @pytest.mark.parametrize("status", [501, 505])
    def test_unretried_server_error_stays_a_bare_http_error(self, status: int) -> None:
        """A 5xx is the server's state, never a verdict on the URL."""
        with pytest.raises(HttpError) as excinfo:
            raise_for_error_status(status, "https://example.com/pkg.metadata")
        assert not isinstance(excinfo.value, UnserveableUrlError)


class TestDecodeBody:
    """The body decoding both transports share."""

    def test_no_encoding_passes_through(self) -> None:
        assert decode_body(LISTING_BODY, None) == LISTING_BODY

    def test_identity_passes_through(self) -> None:
        assert decode_body(LISTING_BODY, "identity") == LISTING_BODY

    def test_unadvertised_coding_passes_through(self) -> None:
        """Only gzip is decoded; the transports never advertise anything else."""
        assert decode_body(LISTING_BODY, "br") == LISTING_BODY

    def test_gzip_decodes(self) -> None:
        assert decode_body(GZIP_BODY, "gzip") == LISTING_BODY

    def test_gzip_value_is_case_insensitive(self) -> None:
        assert decode_body(GZIP_BODY, " GZip ") == LISTING_BODY

    def test_x_gzip_decodes(self) -> None:
        assert decode_body(GZIP_BODY, "x-gzip") == LISTING_BODY
        assert decode_body(GZIP_BODY, " X-GZip ") == LISTING_BODY

    def test_multi_member_gzip_decodes(self) -> None:
        body = GZIP_BODY + gzip.compress(b" and more")
        assert decode_body(body, "gzip") == LISTING_BODY + b" and more"

    def test_empty_gzip_body_passes_through(self) -> None:
        """A bodiless response (a 304) may still carry Content-Encoding."""
        assert decode_body(b"", "gzip") == b""

    def test_truncated_gzip_raises(self) -> None:
        with pytest.raises(ContentDecodingError, match="truncated or corrupt"):
            decode_body(TRUNCATED_GZIP_BODY, "gzip")

    def test_corrupt_gzip_raises(self) -> None:
        with pytest.raises(ContentDecodingError, match="truncated or corrupt"):
            decode_body(b"not gzip at all", "gzip")


class TestAcceptsGzip:
    """The Accept-Encoding parsing that decides whether a body is decoded."""

    def test_requested_gzip(self) -> None:
        assert accepts_gzip({"Accept-Encoding": "gzip"})

    def test_requested_identity(self) -> None:
        assert not accepts_gzip({"Accept-Encoding": "identity"})

    def test_value_is_case_insensitive(self) -> None:
        assert accepts_gzip({"Accept-Encoding": "GZip"})

    def test_name_is_case_insensitive(self) -> None:
        assert accepts_gzip({"accept-encoding": "gzip"})
        assert not accepts_gzip({"ACCEPT-ENCODING": "identity"})

    def test_caller_override_replaces_the_transport_default(self) -> None:
        """A transport merges the caller's headers over its own ``Accept-Encoding``."""
        assert not accepts_gzip(
            {"Accept-Encoding": "gzip", "accept-encoding": "identity"}
        )

    def test_zero_quality_is_a_refusal(self) -> None:
        """``q=0`` says the coding is not acceptable."""
        assert not accepts_gzip({"Accept-Encoding": "identity, gzip;q=0"})

    def test_quality_above_zero_is_a_request(self) -> None:
        assert accepts_gzip({"Accept-Encoding": "gzip; q=0.5, identity"})

    def test_unparseable_quality_is_a_refusal(self) -> None:
        assert not accepts_gzip({"Accept-Encoding": "gzip;q=high"})

    def test_absent_header(self) -> None:
        assert not accepts_gzip({"Accept": "application/json"})


class TestFetchCoordinatorTransport:
    @respx.mock
    def test_explicit_transport(self) -> None:
        """Coordinator routes fetches through whatever transport is passed in."""
        respx.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        with FetchCoordinator(transport=HttpxAsyncTransport()) as coord:
            event = coord.request_listing("pkg")
            event.wait(timeout=5)
            assert coord.index.get_listing("pkg") is not None


class TestHttpxAsyncTransport:
    @respx.mock
    def test_get_returns_response_adapter(self) -> None:
        respx.get("https://example.com/").mock(
            return_value=httpx.Response(200, json={"a": 1}, headers={"etag": "abc"})
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                resp = await transport.get("https://example.com/")
                resp.raise_for_status()
                return resp
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        assert isinstance(resp, _HttpxResponse)
        assert resp.status_code == 200
        assert resp.headers["etag"] == "abc"
        assert resp.json() == {"a": 1}
        assert resp.content == b'{"a":1}'
        assert resp.text == '{"a":1}'

    @respx.mock
    def test_raise_for_status_converts_status_error(self) -> None:
        respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                resp = await transport.get("https://example.com/missing")
                resp.raise_for_status()
            finally:
                await transport.aclose()

        with pytest.raises(HttpError):
            asyncio.run(go())

    @respx.mock
    def test_get_follows_redirects(self) -> None:
        """httpx follows a 3xx redirect, like the urllib3 backend and pip/uv."""
        respx.get("https://example.com/simple/pkg").mock(
            return_value=httpx.Response(
                301, headers={"Location": "https://example.com/simple/pkg/"}
            )
        )
        respx.get("https://example.com/simple/pkg/").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                resp = await transport.get("https://example.com/simple/pkg")
                resp.raise_for_status()
                return resp
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @respx.mock
    def test_get_follows_redirects_past_the_transient_retry_budget(self) -> None:
        """A redirect chain longer than MAX_RETRIES is still followed."""
        chain = MAX_RETRIES + 2
        for n in range(chain):
            respx.get(f"https://example.com/r/{n}").mock(
                return_value=httpx.Response(302, headers={"Location": f"/r/{n + 1}"})
            )
        respx.get(f"https://example.com/r/{chain}").mock(
            return_value=httpx.Response(200)
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/r/0")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        resp.raise_for_status()
        assert resp.status_code == 200

    @respx.mock
    def test_get_gives_up_on_a_redirect_loop(self, slept: list[float]) -> None:
        """A redirect loop raises rather than being retried."""
        route = respx.get("https://example.com/loop").mock(
            return_value=httpx.Response(302, headers={"Location": "/loop"})
        )

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                await transport.get("https://example.com/loop")
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match="redirects"):
            asyncio.run(go())
        assert route.call_count == MAX_REDIRECTS + 1
        assert slept == []

    def test_client_takes_the_shared_redirect_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AsyncClient follows redirects with the policy's budget."""
        cls = MagicMock()
        monkeypatch.setattr("nab_index.httpx_async_transport.httpx.AsyncClient", cls)
        HttpxAsyncTransport()
        assert cls.call_args.kwargs["follow_redirects"] is True
        assert cls.call_args.kwargs["max_redirects"] == MAX_REDIRECTS

    @respx.mock
    def test_get_wraps_connection_error(self) -> None:
        respx.get("https://example.com/").mock(side_effect=httpx.ConnectError("boom"))

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                await transport.get("https://example.com/")
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match="GET https://example.com/ failed"):
            asyncio.run(go())

    @pytest.mark.parametrize(
        "raised",
        [
            httpx.InvalidURL("Invalid IDNA hostname"),
            UnicodeError("idna codepoint not allowed"),
        ],
    )
    def test_get_wraps_non_httperror_from_request(
        self, raised: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-HTTPError raised while issuing the request maps to HttpError.

        A URL httpx cannot even issue is not a blip, so it is raised on the
        first attempt rather than spending the retry budget on it.
        """
        attempts = 0

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)

            def boom(*args: object, **kwargs: object) -> object:
                nonlocal attempts
                attempts += 1
                raise raised

            monkeypatch.setattr(transport._client, "stream", boom)
            try:
                await transport.get("https://bad.example/simple/pkg/")
            finally:
                await transport.aclose()

        with pytest.raises(
            HttpError, match="GET https://bad.example/simple/pkg/ failed"
        ):
            asyncio.run(go())

        assert attempts == 1

    def test_uses_truststore_ssl_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AsyncClient gets a truststore SSLContext via verify=."""
        cls = MagicMock()
        monkeypatch.setattr("nab_index.httpx_async_transport.httpx.AsyncClient", cls)
        HttpxAsyncTransport()
        verify = cls.call_args.kwargs["verify"]
        assert isinstance(verify, truststore.SSLContext)

    @respx.mock
    def test_get_retries_a_transient_status(self, slept: list[float]) -> None:
        """A bare 503 is a blip, so ask again before believing it."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        resp.raise_for_status()
        assert resp.status_code == 200
        assert route.call_count == 2
        assert slept == [0.0]

    @pytest.mark.parametrize("status", [520, 521, 522, 523, 524, 527])
    @respx.mock
    def test_get_retries_a_cloudflare_transient_status(
        self, status: int, slept: list[float]
    ) -> None:
        """A Cloudflare 52x origin error is a blip, so ask again before believing it."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[httpx.Response(status), httpx.Response(200, json={"ok": True})]
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        resp.raise_for_status()
        assert resp.status_code == 200
        assert route.call_count == 2
        assert slept == [0.0]

    @respx.mock
    def test_get_retries_a_request_timeout(self, slept: list[float]) -> None:
        """A 408 is a blip, so ask again before believing it."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[httpx.Response(408), httpx.Response(200, json={"ok": True})]
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        resp.raise_for_status()
        assert resp.status_code == 200
        assert route.call_count == 2
        assert slept == [0.0]

    @respx.mock
    def test_get_retries_a_transport_error(self, slept: list[float]) -> None:
        """A dropped connection is retried, as it is on the urllib3 backend."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[httpx.ConnectError("boom"), httpx.Response(200)]
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        assert asyncio.run(go()).status_code == 200
        assert route.call_count == 2
        assert slept == [0.0]

    @respx.mock
    def test_get_honours_a_bounded_retry_after(self, slept: list[float]) -> None:
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "3600"}),
                httpx.Response(200),
            ]
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        assert asyncio.run(go()).status_code == 200
        assert route.call_count == 2
        assert slept == [10.0]

    @pytest.mark.parametrize("retry_after", UNPARSEABLE_RETRY_AFTERS)
    @respx.mock
    def test_get_backs_off_when_retry_after_does_not_parse(
        self, retry_after: str, slept: list[float]
    ) -> None:
        """An unparseable Retry-After must not cost the retry budget."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": retry_after})]
            * (MAX_RETRIES + 1)
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        assert route.call_count == MAX_RETRIES + 1
        _assert_jittered_backoff_schedule(slept)
        with pytest.raises(HttpError, match="429"):
            resp.raise_for_status()

    @respx.mock
    def test_get_gives_up_on_a_persistent_transient_status(
        self, slept: list[float]
    ) -> None:
        """The budget is bounded, and the caller still sees the 503."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[httpx.Response(503)] * (MAX_RETRIES + 1)
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        assert route.call_count == MAX_RETRIES + 1
        _assert_jittered_backoff_schedule(slept)
        with pytest.raises(HttpError, match="503"):
            resp.raise_for_status()

    @respx.mock
    def test_get_gives_up_on_a_persistent_transport_error(
        self, slept: list[float]
    ) -> None:
        route = respx.get("https://example.com/pkg").mock(
            side_effect=httpx.ConnectError("boom")
        )

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match="GET https://example.com/pkg failed"):
            asyncio.run(go())
        assert route.call_count == MAX_RETRIES + 1
        _assert_jittered_backoff_schedule(slept)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/x.tar.gz",
            "gopher://example.com/x",
            "not-a-url",
            "://nohost/path",
        ],
    )
    def test_get_does_not_retry_an_unsupported_scheme(
        self, url: str, slept: list[float], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A URL httpx cannot issue is permanent, so it is raised on the first try."""
        attempts = 0

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            real_stream = transport._client.stream

            def counting_stream(*args: object, **kwargs: object) -> object:
                nonlocal attempts
                attempts += 1
                return real_stream(*args, **kwargs)

            monkeypatch.setattr(transport._client, "stream", counting_stream)
            try:
                await transport.get(url)
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match=f"GET {url} failed"):
            asyncio.run(go())
        assert attempts == 1
        assert slept == []

    @respx.mock
    def test_get_does_not_retry_a_client_error(self, slept: list[float]) -> None:
        """A 404 is the index's answer, so it is fetched once."""
        route = respx.get("https://example.com/missing").mock(
            return_value=httpx.Response(404)
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/missing")
            finally:
                await transport.aclose()

        assert asyncio.run(go()).status_code == 404
        assert route.call_count == 1
        assert slept == []

    @respx.mock
    def test_get_decodes_a_complete_gzip_body(self) -> None:
        respx.get("https://example.com/pkg").mock(
            return_value=httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, content=GZIP_BODY
            )
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        assert resp.content == LISTING_BODY
        assert resp.text == LISTING_BODY.decode()
        assert resp.json() == {"files": []}

    @respx.mock
    def test_get_decodes_a_body_labelled_x_gzip(self) -> None:
        """An index may label a gzip body with the coding's older name."""
        respx.get("https://example.com/pkg").mock(
            return_value=httpx.Response(
                200, headers={"Content-Encoding": "x-gzip"}, content=GZIP_BODY
            )
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        assert asyncio.run(go()).json() == {"files": []}

    @respx.mock
    def test_get_retries_a_truncated_gzip_body(self, slept: list[float]) -> None:
        """A gzip body cut before its trailer is retried like a dropped connection."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[
                httpx.Response(
                    200,
                    headers={"Content-Encoding": "gzip"},
                    content=TRUNCATED_GZIP_BODY,
                ),
                httpx.Response(
                    200, headers={"Content-Encoding": "gzip"}, content=GZIP_BODY
                ),
            ]
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                return await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        assert resp.status_code == 200
        assert resp.content == LISTING_BODY
        assert route.call_count == 2
        assert slept == [0.0]

    @respx.mock
    def test_get_gives_up_on_a_persistent_truncated_gzip_body(
        self, slept: list[float]
    ) -> None:
        """Truncation retries stop after MAX_RETRIES and raise HttpError."""
        route = respx.get("https://example.com/pkg").mock(
            side_effect=[
                httpx.Response(
                    200,
                    headers={"Content-Encoding": "gzip"},
                    content=TRUNCATED_GZIP_BODY,
                )
            ]
            * (MAX_RETRIES + 1)
        )

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                await transport.get("https://example.com/pkg")
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match="GET https://example.com/pkg failed"):
            asyncio.run(go())
        assert route.call_count == MAX_RETRIES + 1
        _assert_jittered_backoff_schedule(slept)

    @respx.mock
    def test_get_requests_gzip_without_caller_headers(self) -> None:
        route = respx.get("https://example.com/").mock(return_value=httpx.Response(200))

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                await transport.get("https://example.com/")
            finally:
                await transport.aclose()

        asyncio.run(go())
        assert route.calls[0].request.headers["Accept-Encoding"] == "gzip"

    @respx.mock
    def test_get_lets_caller_override_accept_encoding(self) -> None:
        route = respx.get("https://example.com/").mock(return_value=httpx.Response(200))

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                await transport.get(
                    "https://example.com/", headers={"Accept-Encoding": "identity"}
                )
            finally:
                await transport.aclose()

        asyncio.run(go())
        assert route.calls[0].request.headers["Accept-Encoding"] == "identity"


_REQUESTED_URL = "https://example.com/simple/pkg/"


def _unredirected_response(status: int) -> MagicMock:
    """A urllib3 response that followed no redirect, so it carries no history."""
    response = MagicMock(spec=urllib3.BaseHTTPResponse)
    response.status = status
    response.retries = None
    return response


def _redirected_response(status: int, hops: list[tuple[str, str]]) -> MagicMock:
    """A urllib3 response whose retry history holds ``(hop url, raw Location)``."""
    response = MagicMock(spec=urllib3.BaseHTTPResponse)
    response.status = status
    response.retries = urllib3.Retry(
        history=tuple(
            RequestHistory("GET", url, None, 301, location) for url, location in hops
        )
    )
    return response


class TestUrllib3AsyncTransport:
    def _fake_pool(self, body: bytes, status: int = 200) -> MagicMock:
        fake_response = _unredirected_response(status)
        fake_response.data = body
        fake_response.headers = urllib3.HTTPHeaderDict()
        pool = MagicMock(spec=urllib3.PoolManager)
        pool.request.return_value = fake_response
        return pool

    def test_get_returns_response_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = self._fake_pool(b"world")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> tuple[bytes, str]:
            transport = Urllib3AsyncTransport()
            try:
                resp = await transport.get("https://example.com/", headers={"k": "v"})
                resp.raise_for_status()
                return resp.content, resp.text
            finally:
                await transport.aclose()

        content, text = asyncio.run(go())
        assert content == b"world"
        assert text == "world"
        pool.request.assert_called_once_with(
            "GET",
            "https://example.com/",
            headers={**DEFAULT_HEADERS, "k": "v"},
            timeout=5.0,
            retries=GET_RETRY,
            decode_content=False,
        )
        pool.clear.assert_called_once()

    def test_get_wraps_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A urllib3 transport error surfaces as the transport-contract HttpError."""
        pool = MagicMock(spec=urllib3.PoolManager)
        pool.request.side_effect = urllib3.exceptions.MaxRetryError(pool, "https://x/")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get("https://x/")
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match="GET https://x/ failed"):
            asyncio.run(go())

    def test_get_wraps_malformed_ipv6_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redirect to a malformed-IPv6 Location maps to HttpError."""
        pool = MagicMock(spec=urllib3.PoolManager)

        def follow_redirect(method: str, url: str, **kwargs: object) -> object:
            return urljoin(url, "https://[::1/pkg/")

        pool.request.side_effect = follow_redirect
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get("https://example.com/simple/pkg/")
            finally:
                await transport.aclose()

        with pytest.raises(
            HttpError, match="GET https://example.com/simple/pkg/ failed"
        ) as excinfo:
            asyncio.run(go())
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_get_requests_gzip_without_caller_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get() advertises gzip even when the caller passes no headers.

        urllib3 sits on stdlib http.client, which emits
        ``Accept-Encoding: identity`` when no Accept-Encoding header is
        supplied, telling the server not to compress.
        """
        pool = self._fake_pool(b"{}")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get("https://example.com/")
            finally:
                await transport.aclose()

        asyncio.run(go())
        pool.request.assert_called_once_with(
            "GET",
            "https://example.com/",
            headers=DEFAULT_HEADERS,
            timeout=5.0,
            retries=GET_RETRY,
            decode_content=False,
        )

    def test_get_lets_caller_override_accept_encoding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller-supplied Accept-Encoding overrides the gzip default."""
        pool = self._fake_pool(b"{}")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get(
                    "https://example.com/", headers={"Accept-Encoding": "identity"}
                )
            finally:
                await transport.aclose()

        asyncio.run(go())
        pool.request.assert_called_once_with(
            "GET",
            "https://example.com/",
            headers={**DEFAULT_HEADERS, **IDENTITY_HEADERS},
            timeout=5.0,
            retries=GET_RETRY,
            decode_content=False,
        )

    def test_get_applies_bounded_default_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default request carries a finite timeout."""
        pool = self._fake_pool(b"{}")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get("https://example.com/")
            finally:
                await transport.aclose()

        asyncio.run(go())
        assert pool.request.call_args.kwargs["timeout"] == 5.0

    def test_get_forwards_custom_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller-chosen timeout reaches the underlying request."""
        pool = self._fake_pool(b"{}")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport(timeout=1.5)
            try:
                await transport.get("https://example.com/")
            finally:
                await transport.aclose()

        asyncio.run(go())
        assert pool.request.call_args.kwargs["timeout"] == 1.5

    def test_get_retries_a_bare_transient_status(self) -> None:
        """One 503 with no Retry-After must not read as the index's answer."""
        with _stub_index([503]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/pkg-1.0.whl.metadata"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            resp.raise_for_status()
            assert resp.status_code == 200
            assert len(index.seen) == 2

    @pytest.mark.parametrize("status", [520, 521, 522, 523, 524, 527])
    def test_get_retries_a_cloudflare_transient_status(self, status: int) -> None:
        """One Cloudflare 52x origin error with no Retry-After must not end the fetch."""
        with _stub_index([status]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/pkg-1.0.whl.metadata"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            resp.raise_for_status()
            assert resp.status_code == 200
            assert len(index.seen) == 2

    def test_get_retries_a_request_timeout(self) -> None:
        """One 408 must not end the fetch."""
        with _stub_index([408]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            resp.raise_for_status()
            assert resp.status_code == 200
            assert len(index.seen) == 2

    def test_get_follows_redirects_past_the_transient_retry_budget(self) -> None:
        """A redirect chain longer than MAX_RETRIES is still followed."""
        with _stub_index([302] * (MAX_RETRIES + 2)) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            resp.raise_for_status()
            assert resp.status_code == 200
            assert len(index.seen) == MAX_RETRIES + 3

    def test_redirect_leaves_the_transient_retry_budget_intact(self) -> None:
        """The redirect target still gets the full transient retry budget."""
        with _stub_index([302, *[503] * MAX_RETRIES]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            resp.raise_for_status()
            assert resp.status_code == 200
            assert index.seen == ["/pkg/", *["/redirected/"] * (MAX_RETRIES + 1)]

    def test_get_follows_the_full_redirect_budget(self) -> None:
        with _stub_index([302] * MAX_REDIRECTS) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            assert resp.status_code == 200
            assert len(index.seen) == MAX_REDIRECTS + 1

    def test_get_gives_up_on_a_redirect_loop(self) -> None:
        """The redirect budget is finite, so a loop ends in an error."""
        with _stub_index([302] * (MAX_REDIRECTS + 1)) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> None:
                transport = Urllib3AsyncTransport()
                try:
                    await transport.get(url)
                finally:
                    await transport.aclose()

            with pytest.raises(HttpError, match="too many redirects"):
                asyncio.run(go())
            assert len(index.seen) == MAX_REDIRECTS + 1

    def test_get_gives_up_on_a_persistent_transient_status(self) -> None:
        """The budget is bounded, and the caller still sees the 503."""
        with _stub_index([503] * (MAX_RETRIES + 1)) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            assert len(index.seen) == MAX_RETRIES + 1
            with pytest.raises(HttpError, match="HTTP 503"):
                resp.raise_for_status()

    def test_get_backs_off_when_retry_after_does_not_parse(self) -> None:
        """An unparseable Retry-After must not cost the retry budget."""
        with _stub_index(
            [429] * (MAX_RETRIES + 1), retry_after="Wed, 31 Dec 10000 23:59:59 GMT"
        ) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            assert len(index.seen) == MAX_RETRIES + 1
            with pytest.raises(HttpError, match="HTTP 429"):
                resp.raise_for_status()

    def test_get_does_not_retry_a_client_error(self) -> None:
        """A 404 is the index's answer, so it is fetched once."""
        with _stub_index([404]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            assert asyncio.run(go()).status_code == 404
            assert len(index.seen) == 1

    def test_get_decodes_a_complete_gzip_body(self) -> None:
        with _gzip_stub_index([]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            resp.raise_for_status()
            assert resp.content == LISTING_BODY
            assert len(index.seen) == 1

    def test_get_decodes_a_body_labelled_x_gzip(self) -> None:
        """An index may label a gzip body with the coding's older name."""
        with _gzip_stub_index([], encoding="x-gzip") as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            assert asyncio.run(go()).content == LISTING_BODY

    def test_get_retries_a_truncated_gzip_body(self, thread_slept: list[float]) -> None:
        """A gzip body cut before its trailer is retried like a dropped connection."""
        with _gzip_stub_index([TRUNCATED_GZIP_BODY]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> _Urllib3Response:
                transport = Urllib3AsyncTransport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            resp = asyncio.run(go())
            resp.raise_for_status()
            assert resp.status_code == 200
            assert resp.content == LISTING_BODY
            assert len(index.seen) == 2
            assert thread_slept == [0.0]

    def test_get_gives_up_on_a_persistent_truncated_gzip_body(
        self, thread_slept: list[float]
    ) -> None:
        """Truncation retries stop after MAX_RETRIES and raise HttpError."""
        with _gzip_stub_index([TRUNCATED_GZIP_BODY] * (MAX_RETRIES + 1)) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> None:
                transport = Urllib3AsyncTransport()
                try:
                    await transport.get(url)
                finally:
                    await transport.aclose()

            with pytest.raises(HttpError, match=f"GET {url} failed"):
                asyncio.run(go())
            assert len(index.seen) == MAX_RETRIES + 1
            _assert_jittered_backoff_schedule(thread_slept)

    def test_response_json(self) -> None:
        fake = _unredirected_response(200)
        body = json.dumps({"a": 1}).encode()
        assert _Urllib3Response(fake, body, _REQUESTED_URL).json() == {"a": 1}

    def test_response_raise_for_status(self) -> None:
        fake = _unredirected_response(404)
        with pytest.raises(HttpError, match=f"404 for {_REQUESTED_URL}"):
            _Urllib3Response(fake, b"", _REQUESTED_URL).raise_for_status()

    def test_response_raise_for_status_names_the_redirect_target(self) -> None:
        fake = _redirected_response(500, [(_REQUESTED_URL, "/moved/")])
        with pytest.raises(HttpError, match="500 for https://example.com/moved/"):
            _Urllib3Response(fake, b"", _REQUESTED_URL).raise_for_status()

    def test_response_raise_for_status_ok(self) -> None:
        fake = _unredirected_response(200)
        _Urllib3Response(fake, b"", _REQUESTED_URL).raise_for_status()  # no exception

    def test_response_status_code_and_headers(self) -> None:
        fake = _unredirected_response(304)
        fake.headers = {"etag": "abc"}
        adapter = _Urllib3Response(fake, b"", _REQUESTED_URL)
        assert adapter.status_code == 304
        assert adapter.headers["etag"] == "abc"

    def test_url_without_a_redirect_is_the_requested_url(self) -> None:
        fake = _unredirected_response(200)
        assert _Urllib3Response(fake, b"", _REQUESTED_URL).url == _REQUESTED_URL

    def test_url_resolves_a_relative_location_against_its_own_hop(self) -> None:
        """Each Location is verbatim, so the chain is walked, not just the first hop."""
        fake = _redirected_response(
            200,
            [
                (_REQUESTED_URL, "https://mirror.example.com/pypi/simple/pkg/"),
                ("https://mirror.example.com/pypi/simple/pkg/", "moved/"),
            ],
        )
        adapter = _Urllib3Response(fake, b"", _REQUESTED_URL)
        assert adapter.url == "https://mirror.example.com/pypi/simple/pkg/moved/"

    def test_url_ignores_a_retried_status(self) -> None:
        """urllib3 records a status retry with the request path and no Location."""
        fake = MagicMock(spec=urllib3.BaseHTTPResponse)
        fake.status = 200
        fake.retries = urllib3.Retry(
            history=(RequestHistory("GET", "/simple/pkg/", None, 503, None),)
        )
        assert _Urllib3Response(fake, b"", _REQUESTED_URL).url == _REQUESTED_URL

    def test_uses_truststore_ssl_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each per-thread PoolManager gets a truststore SSLContext."""
        captured: dict[str, Any] = {}

        def fake_pool_manager(**kw: Any) -> MagicMock:
            captured.update(kw)
            return MagicMock(spec=urllib3.PoolManager)

        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            fake_pool_manager,
        )
        # Pools are created lazily per worker thread, so build one.
        Urllib3AsyncTransport()._pool()
        assert isinstance(captured["ssl_context"], truststore.SSLContext)

    def test_pool_is_per_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each thread gets its own PoolManager so the truststore context is unshared."""
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: MagicMock(spec=urllib3.PoolManager),
        )
        transport = Urllib3AsyncTransport()
        pools = [
            asyncio.run(asyncio.to_thread(transport._pool)),
            asyncio.run(asyncio.to_thread(transport._pool)),
        ]
        # Distinct worker threads -> distinct pools; all tracked for aclose.
        assert pools[0] is not pools[1]
        assert set(map(id, pools)) <= set(map(id, transport._pools))

    def test_ssl_context_satisfies_urllib3_cert_check(self) -> None:
        """``_SSLContext`` returns a non-empty CA count for urllib3-future.

        urllib3-future calls ``cert_store_stats()`` to decide whether to
        load default certs; truststore raises ``NotImplementedError``
        there, so the subclass returns a non-zero ``x509_ca`` count.
        """
        ctx = _SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        stats = ctx.cert_store_stats()
        assert stats["x509_ca"] >= 1


class TestAsyncSimpleClient:
    """Tests for nab-index's AsyncSimpleClient via a faked transport."""

    class _FakeResponse:
        def __init__(
            self,
            body: bytes,
            status: int = 200,
            headers: Mapping[str, str] | None = None,
            url: str = "",
        ) -> None:
            self._content = body
            self._status = status
            self._headers = headers or {}
            self._url = url

        @property
        def status_code(self) -> int:
            return self._status

        @property
        def url(self) -> str:
            return self._url

        @property
        def headers(self) -> Mapping[str, str]:
            return self._headers

        @property
        def content(self) -> bytes:
            return self._content

        @property
        def text(self) -> str:
            return self._content.decode()

        def json(self) -> object:
            return json.loads(self.text)

        def raise_for_status(self) -> None:
            if self._status >= 400:
                msg = f"status {self._status}"
                raise RuntimeError(msg)

    class _FakeTransport:
        def __init__(
            self,
            body: bytes,
            status: int = 200,
            headers: Mapping[str, str] | None = None,
        ) -> None:
            self._body = body
            self._status = status
            self._headers = headers
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        async def get(
            self, url: str, *, headers: dict[str, str] | None = None
        ) -> TestAsyncSimpleClient._FakeResponse:
            self.calls.append((url, headers))
            return TestAsyncSimpleClient._FakeResponse(
                self._body, self._status, self._headers, url
            )

        async def aclose(self) -> None:
            return None

    def test_get_files(self) -> None:
        body = json.dumps(LISTING_JSON).encode()
        transport = self._FakeTransport(body)

        async def go() -> list:
            async with AsyncSimpleClient(transport, "https://pypi.org/simple/") as c:
                return await c.get_files("pkg")

        files = asyncio.run(go())
        assert len(files) == 1
        assert transport.calls[0][0] == "https://pypi.org/simple/pkg/"
        assert transport.calls[0][1] == {
            "Accept": (
                "application/vnd.pypi.simple.v1+json, "
                "application/vnd.pypi.simple.v1+html;q=0.2, "
                "text/html;q=0.01"
            )
        }

    def test_get_files_reads_html_listing(self) -> None:
        body = (
            b"<!DOCTYPE html>\n<html>\n  <body>\n"
            b'    <a href="pkg-1.0-py3-none-any.whl">pkg-1.0-py3-none-any.whl</a>\n'
            b"  </body>\n</html>\n"
        )
        transport = self._FakeTransport(body, headers={"Content-Type": "text/html"})

        async def go() -> list:
            async with AsyncSimpleClient(transport, "https://pypi.org/simple/") as c:
                return await c.get_files("pkg")

        files = asyncio.run(go())
        assert [f.url for f in files] == [
            "https://pypi.org/simple/pkg/pkg-1.0-py3-none-any.whl"
        ]

    def test_get_files_oversized_int_raises_clean(self) -> None:
        """A ``size`` too long to convert must not escape as a raw ValueError."""
        oversized = "9" * (sys.get_int_max_str_digits() + 1)
        listing = {
            "meta": {"api-version": "1.1"},
            "name": "pkg",
            "files": [
                {
                    "filename": "pkg-1.0-py3-none-any.whl",
                    "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
                    "size": "PLACEHOLDER",
                },
            ],
        }

        # json.dumps hits the same limit writing the int out, so splice it in.
        body = json.dumps(listing).replace('"PLACEHOLDER"', oversized).encode()
        transport = self._FakeTransport(body)

        async def go() -> list:
            async with AsyncSimpleClient(transport, "https://pypi.org/simple/") as c:
                return await c.get_files("pkg")

        with pytest.raises(
            MalformedSimpleResponseError, match="malformed Simple-API"
        ) as caught:
            asyncio.run(go())
        assert isinstance(caught.value, HttpError)

    def test_get_files_404_returns_empty(self) -> None:
        transport = self._FakeTransport(b"not found", status=404)

        async def go() -> list:
            async with AsyncSimpleClient(transport, "https://pypi.org/simple/") as c:
                return await c.get_files("absent")

        assert asyncio.run(go()) == []

    def test_get_metadata_text(self) -> None:
        transport = self._FakeTransport(b"Metadata-Version: 2.1\n")

        async def go() -> str:
            async with AsyncSimpleClient(transport) as c:
                return await c.get_metadata_text("https://example.com/pkg.metadata")

        assert asyncio.run(go()) == "Metadata-Version: 2.1\n"


def _build_tarball(members: list[tuple[str, bytes | None]]) -> bytes:
    """Build a tar.gz with the given (name, data-or-None) members.

    ``data is None`` produces a directory entry rather than a file.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SDIST_BODY = _build_tarball(
    [
        ("demo-1.0/PKG-INFO", b"Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"),
        ("demo-1.0/pyproject.toml", b"[project]\nname = 'demo'\n"),
    ]
)
SDIST_SHA256 = hashlib.sha256(SDIST_BODY).hexdigest()

TRANSPORTS = [
    pytest.param(Urllib3AsyncTransport, id="urllib3"),
    pytest.param(lambda: HttpxAsyncTransport(http2=False), id="httpx"),
]


class TestArtifactContentEncoding:
    """An artifact body is hashed as served, so it is never content-decoded."""

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_download_returns_the_served_bytes(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _artifact_stub_index(SDIST_BODY) as server:
            url = f"http://127.0.0.1:{server.server_port}/demo-1.0.tar.gz"

            async def go() -> bytes:
                async with AsyncSimpleClient(make_transport()) as client:
                    return await client.download(url)

            data = asyncio.run(go())

        assert data == SDIST_BODY
        assert hashlib.sha256(data).hexdigest() == SDIST_SHA256
        assert server.accept_encoding == ["identity"]

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_sdist_files_clear_the_published_hash(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _artifact_stub_index(SDIST_BODY) as server:
            url = f"http://127.0.0.1:{server.server_port}/demo-1.0.tar.gz"

            async def go() -> tuple[str | None, str | None]:
                async with CachedAsyncSimpleClient(
                    make_transport(), NullCache()
                ) as client:
                    return await client.get_sdist_files(
                        "demo", "1.0", url, (("sha256", SDIST_SHA256),)
                    )

            pkg_info, pyproject = asyncio.run(go())

        assert pkg_info is not None
        assert "Name: demo" in pkg_info
        assert pyproject == "[project]\nname = 'demo'\n"
        assert server.accept_encoding == ["identity"]

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_sdist_archive_clears_the_published_hash(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _artifact_stub_index(SDIST_BODY) as server:
            url = f"http://127.0.0.1:{server.server_port}/demo-1.0.tar.gz"

            async def go() -> bytes:
                async with CachedAsyncSimpleClient(
                    make_transport(), NullCache()
                ) as client:
                    return await client.get_sdist_archive(
                        "demo", "1.0", url, (("sha256", SDIST_SHA256),)
                    )

            assert asyncio.run(go()) == SDIST_BODY

        assert server.accept_encoding == ["identity"]

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_direct_archive_keeps_the_served_bytes(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        """The ``archive-sources`` path, whose caller checks the declared hash."""
        with (
            _artifact_stub_index(SDIST_BODY) as server,
            FetchCoordinator(transport=make_transport()) as coord,
        ):
            url = f"http://127.0.0.1:{server.server_port}/demo-1.0.tar.gz"
            coord.request_direct_archive("demo", SDIST_SHA256, url).wait(timeout=10)
            assert coord.index.get_sdist_archive("demo", SDIST_SHA256) == SDIST_BODY

        assert server.accept_encoding == ["identity"]


class TestUnfollowedRedirectAcrossBackends:
    """Both backends treat a 300 they did not follow the same way.

    ``raise_for_status`` is the 4xx/5xx line, so it clears the 300 on either
    backend; the body-reading calls are what reject it.
    """

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_raise_for_status_clears_a_300(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _stub_index([300]) as index:
            url = f"http://127.0.0.1:{index.server_port}/pkg/"

            async def go() -> HttpResponse:
                transport = make_transport()
                try:
                    return await transport.get(url)
                finally:
                    await transport.aclose()

            response = asyncio.run(go())

        assert response.status_code == 300
        response.raise_for_status()

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_metadata_sidecar_300_raises(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _stub_index([300]) as index:
            url = f"http://127.0.0.1:{index.server_port}/demo-1.0.whl.metadata"

            async def go() -> str:
                async with CachedAsyncSimpleClient(
                    make_transport(), NullCache()
                ) as client:
                    return await client.get_metadata_text("demo", "1.0", url)

            with pytest.raises(HttpError, match="300"):
                asyncio.run(go())


class TestUserAgentAcrossBackends:
    """Both backends send the same User-Agent."""

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_get_sends_the_user_agent(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _user_agent_stub_index() as index:
            url = f"http://127.0.0.1:{index.server_port}/simple/pkg/"

            async def go() -> None:
                transport = make_transport()
                try:
                    await transport.get(url)
                finally:
                    await transport.aclose()

            asyncio.run(go())

        assert index.user_agents == [USER_AGENT]

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_accept_encoding_override_keeps_the_user_agent(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _user_agent_stub_index() as index:
            url = f"http://127.0.0.1:{index.server_port}/files/pkg-1.0.whl"

            async def go() -> None:
                transport = make_transport()
                try:
                    await transport.get(url, headers=IDENTITY_HEADERS)
                finally:
                    await transport.aclose()

            asyncio.run(go())

        assert index.user_agents == [USER_AGENT]


RELATIVE_LISTING = (
    b'{"meta": {"api-version": "1.1"}, "name": "pkg", "files": ['
    b'{"filename": "pkg-1.0-py3-none-any.whl", "url": "pkg-1.0-py3-none-any.whl", '
    b'"hashes": {"sha256": "' + b"0" * 64 + b'"}, "core-metadata": true}]}'
)


class TestMovedProjectPage:
    """A relative file URL resolves against the page the index redirected to.

    RFC 3986 section 5.1.3: the base is the URL the representation was
    retrieved from, which after a redirect is the target rather than the
    requested URL.
    """

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_relative_file_url_resolves_against_the_redirect_target(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _moved_index(
            {"/simple/pkg/": "/pypi/simple/pkg/"}, RELATIVE_LISTING
        ) as index:
            root = f"http://127.0.0.1:{index.server_port}"

            async def go() -> list[WheelFile | SdistFile]:
                async with AsyncSimpleClient(
                    make_transport(), f"{root}/simple/"
                ) as client:
                    return await client.get_files("pkg")

            (wheel,) = asyncio.run(go())
            assert index.seen == ["/simple/pkg/", "/pypi/simple/pkg/"]

        assert isinstance(wheel, WheelFile)
        assert wheel.url == f"{root}/pypi/simple/pkg/pkg-1.0-py3-none-any.whl"
        assert wheel.metadata_url == f"{wheel.url}.metadata"

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_relative_location_resolves_against_the_hop_that_served_it(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        """The second hop's ``Location`` is relative to the first hop's target."""
        with _moved_index(
            {"/simple/pkg/": "/pypi/simple/", "/pypi/simple/": "pkg/"},
            RELATIVE_LISTING,
        ) as index:
            root = f"http://127.0.0.1:{index.server_port}"

            async def go() -> list[WheelFile | SdistFile]:
                async with AsyncSimpleClient(
                    make_transport(), f"{root}/simple/"
                ) as client:
                    return await client.get_files("pkg")

            (wheel,) = asyncio.run(go())

        assert wheel.url == f"{root}/pypi/simple/pkg/pkg-1.0-py3-none-any.whl"

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_redirect_target_survives_a_warm_cache_hit(
        self, make_transport: Callable[[], AsyncHttpTransport], tmp_path: Path
    ) -> None:
        with _moved_index(
            {"/simple/pkg/": "/pypi/simple/pkg/"}, RELATIVE_LISTING
        ) as index:
            root = f"http://127.0.0.1:{index.server_port}"
            index_url = f"{root}/simple/"

            async def go() -> list[WheelFile | SdistFile]:
                cache = OnDiskCache(tmp_path, index_url)
                async with CachedAsyncSimpleClient(
                    make_transport(), cache, index_url
                ) as client:
                    return await client.get_files("pkg")

            (cold,) = asyncio.run(go())
            (warm,) = asyncio.run(go())
            assert index.seen == ["/simple/pkg/", "/pypi/simple/pkg/"]

        assert warm.url == cold.url
        assert warm.url == f"{root}/pypi/simple/pkg/pkg-1.0-py3-none-any.whl"


class TestRetriedProjectPage:
    """A retried status is not a redirect, so it leaves the base alone.

    urllib3 puts a status, connect, or read retry in the same history as a
    redirect, recorded with the request path rather than an absolute URL.
    """

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_relative_file_url_survives_a_retried_status(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _moved_index({}, RELATIVE_LISTING, transient=[503]) as index:
            root = f"http://127.0.0.1:{index.server_port}"

            async def go() -> list[WheelFile | SdistFile]:
                async with AsyncSimpleClient(
                    make_transport(), f"{root}/simple/"
                ) as client:
                    return await client.get_files("pkg")

            (wheel,) = asyncio.run(go())
            assert index.seen == ["/simple/pkg/", "/simple/pkg/"]

        assert isinstance(wheel, WheelFile)
        assert wheel.url == f"{root}/simple/pkg/pkg-1.0-py3-none-any.whl"
        assert wheel.metadata_url == f"{wheel.url}.metadata"

    @pytest.mark.parametrize("make_transport", TRANSPORTS)
    def test_status_retried_after_a_redirect_keeps_the_redirect_target(
        self, make_transport: Callable[[], AsyncHttpTransport]
    ) -> None:
        with _moved_index(
            {"/simple/pkg/": "/pypi/simple/pkg/"}, RELATIVE_LISTING, transient=[503]
        ) as index:
            root = f"http://127.0.0.1:{index.server_port}"

            async def go() -> list[WheelFile | SdistFile]:
                async with AsyncSimpleClient(
                    make_transport(), f"{root}/simple/"
                ) as client:
                    return await client.get_files("pkg")

            (wheel,) = asyncio.run(go())

            # Backends resume at different points, so pin the outcome, not the order.
            assert index.transient == []
            assert index.seen[-1] == "/pypi/simple/pkg/"

        assert wheel.url == f"{root}/pypi/simple/pkg/pkg-1.0-py3-none-any.whl"


class TestExtractSdistFiles:
    """Tests for ``_extract_sdist_files``: PKG-INFO + pyproject.toml extraction."""

    def test_returns_pkg_info_text(self) -> None:
        body = _build_tarball(
            [("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n")]
        )
        pkg_info, pyproject = _extract_sdist_files(body)
        assert pkg_info is not None
        assert "Name: pkg" in pkg_info
        assert pyproject is None

    def test_iterates_past_non_pkg_info_members(self) -> None:
        body = _build_tarball(
            [
                ("pkg-1.0/setup.py", b"# setup"),
                ("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\n"),
            ]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info == "Metadata-Version: 2.1\n"

    def test_skips_directory_named_pkg_info(self) -> None:
        """A depth-1 directory named PKG-INFO yields no metadata text."""
        body = _build_tarball(
            [
                ("pkg-1.0/PKG-INFO", None),
                ("pkg-1.0/setup.py", b"# something"),
            ]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info is None

    def test_ignores_pkg_info_below_top_level(self) -> None:
        """A PKG-INFO buried below the conventional ``<name>-<version>/`` is ignored."""
        body = _build_tarball(
            [("pkg-1.0/sub/PKG-INFO", b"Metadata-Version: 2.1\nName: deep\n")]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info is None

    def test_ignores_pyproject_below_top_level(self) -> None:
        """A pyproject.toml buried below the top level is ignored."""
        body = _build_tarball(
            [("pkg-1.0/sub/pyproject.toml", b"[project]\nname = 'deep'\n")]
        )
        _, pyproject = _extract_sdist_files(body)
        assert pyproject is None

    def test_ignores_pkg_info_at_archive_root(self) -> None:
        """A PKG-INFO at archive root never shadows the real one under the root dir."""
        body = _build_tarball(
            [
                ("PKG-INFO", b"Metadata-Version: 2.1\nName: stray\n"),
                ("realpkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: realpkg\n"),
            ]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info is not None
        assert "Name: realpkg" in pkg_info

    def test_drops_metadata_when_top_level_dir_is_ambiguous(self) -> None:
        """Two sibling top-level dirs each with a PKG-INFO has no canonical root."""
        body = _build_tarball(
            [
                ("zzz/PKG-INFO", b"Metadata-Version: 2.1\nName: wrong\n"),
                ("realpkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: realpkg\n"),
                ("realpkg-1.0/pyproject.toml", b"[project]\nname = 'realpkg'\n"),
            ]
        )
        pkg_info, pyproject = _extract_sdist_files(body)
        assert pkg_info is None
        assert pyproject is None

    def test_pyproject_must_share_pkg_info_top_level_dir(self) -> None:
        """A pyproject.toml from a sibling dir does not pair with the real PKG-INFO."""
        body = _build_tarball(
            [
                ("realpkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: realpkg\n"),
                ("other-2.0/pyproject.toml", b"[project]\nname = 'other'\n"),
            ]
        )
        pkg_info, pyproject = _extract_sdist_files(body)
        assert pkg_info is not None
        assert "Name: realpkg" in pkg_info
        assert pyproject is None

    def test_first_member_wins_within_one_root(self) -> None:
        """A duplicate basename in the same root keeps the first occurrence."""
        body = _build_tarball(
            [
                ("realpkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: first\n"),
                ("realpkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: second\n"),
            ]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info is not None
        assert "Name: first" in pkg_info

    def test_pairs_pkg_info_and_pyproject_from_same_root(self) -> None:
        """PKG-INFO and pyproject.toml under one root pair normally."""
        body = _build_tarball(
            [
                ("realpkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: realpkg\n"),
                ("realpkg-1.0/pyproject.toml", b"[project]\nname = 'realpkg'\n"),
            ]
        )
        pkg_info, pyproject = _extract_sdist_files(body)
        assert pkg_info is not None
        assert "Name: realpkg" in pkg_info
        assert pyproject == "[project]\nname = 'realpkg'\n"

    def test_returns_none_on_tar_error(self) -> None:
        assert _extract_sdist_files(b"not-a-tarball") == (None, None)

    def test_returns_none_when_pkg_info_missing(self) -> None:
        body = _build_tarball([("pkg-1.0/setup.py", b"# nothing")])
        pkg_info, pyproject = _extract_sdist_files(body)
        assert pkg_info is None
        assert pyproject is None

    @pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
    def test_returns_none_when_pkg_info_is_a_broken_link(
        self, link_type: bytes
    ) -> None:
        """A PKG-INFO that is a link to an absent member is treated as absent."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            directory = tarfile.TarInfo("pkg-1.0")
            directory.type = tarfile.DIRTYPE
            tar.addfile(directory)
            link = tarfile.TarInfo("pkg-1.0/PKG-INFO")
            link.type = link_type
            link.linkname = "pkg-1.0/absent"
            tar.addfile(link)
        assert _extract_sdist_files(buf.getvalue()) == (None, None)

    @pytest.mark.parametrize(
        "links",
        [
            [("pkg-1.0/PKG-INFO", "PKG-INFO")],
            [("pkg-1.0/PKG-INFO", "other"), ("pkg-1.0/other", "PKG-INFO")],
        ],
        ids=["self", "two-member-cycle"],
    )
    def test_returns_none_when_pkg_info_symlinks_form_a_cycle(
        self, links: list[tuple[str, str]]
    ) -> None:
        """A PKG-INFO symlink cycle never resolves, so it is treated as absent."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            directory = tarfile.TarInfo("pkg-1.0")
            directory.type = tarfile.DIRTYPE
            tar.addfile(directory)
            for name, linkname in links:
                link = tarfile.TarInfo(name)
                link.type = tarfile.SYMTYPE
                link.linkname = linkname
                tar.addfile(link)
        assert _extract_sdist_files(buf.getvalue()) == (None, None)
