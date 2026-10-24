"""Exercise httpx2's real client with an in-memory network transport."""

from __future__ import annotations

import asyncio
import gzip
import ssl
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
import truststore

from nab_index.client import AsyncSimpleClient
from nab_index.httpx2_async_transport import Httpx2AsyncTransport
from nab_index.retry_limits import MAX_REDIRECTS, MAX_RETRIES
from nab_index.transport import IDENTITY_HEADERS, USER_AGENT, HttpError

Connect = Callable[[Callable[[httpx2.Request], httpx2.Response]], Httpx2AsyncTransport]


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry delays without waiting."""
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("nab_index.httpx2_async_transport.asyncio.sleep", sleep)
    return delays


@pytest.fixture
def connect(
    monkeypatch: pytest.MonkeyPatch,
) -> Connect:
    """Attach a request handler while preserving real client configuration."""
    client_type = httpx2.AsyncClient

    def make(
        handler: Callable[[httpx2.Request], httpx2.Response],
    ) -> Httpx2AsyncTransport:
        def client(**kwargs: Any) -> httpx2.AsyncClient:
            assert kwargs["http2"] is True
            assert kwargs["follow_redirects"] is True
            assert kwargs["max_redirects"] == MAX_REDIRECTS
            assert isinstance(kwargs["verify"], truststore.SSLContext)
            return client_type(transport=httpx2.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx2, "AsyncClient", client)
        return Httpx2AsyncTransport()

    return make


def _response(
    status: int = 200, body: bytes = b"ok", headers: dict[str, str] | None = None
) -> httpx2.Response:
    """Return an unread response so the adapter must consume the raw stream."""
    return httpx2.Response(status, stream=httpx2.ByteStream(body), headers=headers)


def test_response_and_redirect(connect: Connect) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/":
            return _response(302, headers={"Location": "/final"})
        return _response(body=b'{"ok":true}', headers={"ETag": "abc"})

    transport = connect(handle)

    async def run() -> None:
        try:
            response = await transport.get("https://example.com/")
            assert response.status_code == 200
            assert response.url == "https://example.com/final"
            assert response.headers["etag"] == "abc"
            assert response.content == b'{"ok":true}'
            assert response.text == '{"ok":true}'
            assert response.json() == {"ok": True}
            response.raise_for_status()
        finally:
            await transport.aclose()

        assert transport._client is not None
        assert transport._client.is_closed

    asyncio.run(run())

    assert len(requests) == 2
    assert requests[0].headers["User-Agent"] == USER_AGENT
    assert requests[0].headers["Accept-Encoding"] == "gzip"


@pytest.mark.parametrize(
    "status", [408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 527]
)
def test_status_retry(connect: Connect, slept: list[float], status: int) -> None:
    replies = iter([_response(status, headers={"Retry-After": "1"}), _response()])
    transport = connect(lambda request: next(replies))

    async def run() -> None:
        try:
            assert (await transport.get("https://example.com/")).status_code == 200
        finally:
            await transport.aclose()

    asyncio.run(run())
    assert slept == [1.0]


@pytest.mark.parametrize("status", [304, 404, 503])
def test_final_status_body(connect: Connect, slept: list[float], status: int) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(status)

    transport = connect(handle)

    async def run() -> None:
        try:
            response = await transport.get("https://example.com/")
            assert response.status_code == status
            assert response.content == b"ok"
            if status >= 400:
                with pytest.raises(HttpError, match=f"HTTP {status}"):
                    response.raise_for_status()
            else:
                response.raise_for_status()
        finally:
            await transport.aclose()

    asyncio.run(run())
    expected = MAX_RETRIES if status == 503 else 0
    assert len(slept) == expected
    assert len(requests) == expected + 1


@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("failure", ["connection", "gzip"])
def test_retry_failures(
    connect: Connect, slept: list[float], recover: bool, failure: str
) -> None:
    calls = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if recover and calls == 2:
            return _response(
                body=gzip.compress(b"ok"), headers={"Content-Encoding": "gzip"}
            )
        if failure == "connection":
            raise httpx2.ConnectError("connection lost")
        return _response(
            body=gzip.compress(b"ok")[:-4], headers={"Content-Encoding": "gzip"}
        )

    transport = connect(handle)

    async def run() -> None:
        try:
            if recover:
                assert (await transport.get("https://example.com/")).content == b"ok"
            else:
                with pytest.raises(HttpError, match="GET https://example.com/ failed"):
                    await transport.get("https://example.com/")
        finally:
            await transport.aclose()

    asyncio.run(run())
    assert calls == (2 if recover else MAX_RETRIES + 1)
    assert len(slept) == calls - 1


@pytest.mark.parametrize("attribute", ["__cause__", "__context__"])
def test_certificate_failure_is_not_retried(
    connect: Connect, slept: list[float], attribute: str
) -> None:
    certificate = ssl.SSLCertVerificationError("untrusted issuer")
    error = httpx2.ConnectError("certificate rejected")
    setattr(error, attribute, certificate)
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise error

    client = AsyncSimpleClient(connect(handle))

    async def run() -> None:
        try:
            with pytest.raises(HttpError, match="certificate rejected") as caught:
                await client.get_metadata_text("https://example.com/demo.whl.metadata")
            assert caught.value.__cause__ is error
            assert getattr(caught.value.__cause__, attribute) is certificate
        finally:
            await client.aclose()

    asyncio.run(run())
    assert len(requests) == 1
    assert slept == []


@pytest.mark.parametrize("recover", [True, False])
def test_ordinary_tls_failure_is_retried(
    connect: Connect, slept: list[float], recover: bool
) -> None:
    metadata = "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
    errors: list[httpx2.ConnectError] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        if recover and len(errors) == 1:
            return _response(body=metadata.encode())
        error = httpx2.ConnectError("connection lost", request=request)
        errors.append(error)
        raise error from ssl.SSLError("TLS EOF")

    client = AsyncSimpleClient(connect(handle))

    async def run() -> None:
        try:
            if recover:
                text = await client.get_metadata_text(
                    "https://example.com/demo.whl.metadata"
                )
                assert text == metadata
            else:
                with pytest.raises(HttpError, match="connection lost") as caught:
                    await client.get_metadata_text(
                        "https://example.com/demo.whl.metadata"
                    )
                assert caught.value.__cause__ is errors[-1]
                assert isinstance(caught.value.__cause__.__cause__, ssl.SSLError)
        finally:
            await client.aclose()

    asyncio.run(run())
    assert len(errors) == (1 if recover else MAX_RETRIES + 1)
    assert len(slept) == (1 if recover else MAX_RETRIES)


@pytest.mark.parametrize(
    "error",
    [
        httpx2.TooManyRedirects("loop"),
        httpx2.UnsupportedProtocol("scheme"),
        httpx2.InvalidURL("url"),
        UnicodeError("hostname"),
    ],
)
def test_permanent_failure(
    connect: Connect, slept: list[float], error: Exception
) -> None:
    calls = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise error

    transport = connect(handle)

    async def run() -> None:
        try:
            with pytest.raises(HttpError) as caught:
                await transport.get("https://example.com/")
            assert caught.value.__cause__ is error
        finally:
            await transport.aclose()

    asyncio.run(run())
    assert calls == 1
    assert slept == []


def test_identity_keeps_raw_bytes(connect: Connect) -> None:
    body = gzip.compress(b"archive")

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["User-Agent"] == USER_AGENT
        return _response(body=body, headers={"Content-Encoding": "gzip"})

    transport = connect(handle)

    async def run() -> None:
        try:
            response = await transport.get(
                "https://example.com/", headers=IDENTITY_HEADERS
            )
            assert response.content == body
        finally:
            await transport.aclose()

    asyncio.run(run())


def test_cancellation_propagates(connect: Connect, slept: list[float]) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise asyncio.CancelledError

    transport = connect(handle)

    async def run() -> None:
        try:
            with pytest.raises(asyncio.CancelledError):
                await transport.get("https://example.com/")
        finally:
            await transport.aclose()

    asyncio.run(run())
    assert slept == []
