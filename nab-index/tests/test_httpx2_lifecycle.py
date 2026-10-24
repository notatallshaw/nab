"""Use the real HTTPX2 client to check lazy setup and transport closure."""

from __future__ import annotations

import asyncio
import builtins
import ssl

import httpx2
import pytest

from nab_index import httpx2_async_transport as module
from nab_index.httpx2_async_transport import Httpx2AsyncTransport
from nab_index.retry_limits import MAX_REDIRECTS
from nab_index.transport import HttpError


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.AsyncClient]:
    """Record actual clients while replacing their network with a fixed response."""
    actual_client = httpx2.AsyncClient
    created: list[httpx2.AsyncClient] = []

    def response(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=httpx2.ByteStream(request.url.path.encode()))

    def create(
        *,
        http2: bool,
        follow_redirects: bool,
        max_redirects: int,
        verify: ssl.SSLContext,
    ) -> httpx2.AsyncClient:
        assert follow_redirects
        assert max_redirects == MAX_REDIRECTS

        client = actual_client(
            http2=http2,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            verify=verify,
            transport=httpx2.MockTransport(response),
        )
        created.append(client)
        return client

    monkeypatch.setattr(httpx2, "AsyncClient", create)
    return created


def test_unused_close_does_not_construct_a_client(
    clients: list[httpx2.AsyncClient],
) -> None:
    transport = Httpx2AsyncTransport()

    assert not clients

    async def run() -> None:
        await transport.aclose()
        await transport.aclose()
        with pytest.raises(HttpError):
            await transport.get("https://example.com/closed")

    asyncio.run(run())

    assert not clients


@pytest.mark.parametrize("http2", [False, True])
def test_requests_reuse_one_client_and_close_it(
    clients: list[httpx2.AsyncClient], *, http2: bool
) -> None:
    transport = Httpx2AsyncTransport(http2=http2)

    assert not clients

    async def run() -> None:
        try:
            first, second = await asyncio.gather(
                transport.get("https://example.com/first"),
                transport.get("https://example.com/second"),
            )
            assert (first.content, second.content) == (b"/first", b"/second")
            assert len(clients) == 1
            assert not clients[0].is_closed
        finally:
            await transport.aclose()

        assert clients[0].is_closed

        with pytest.raises(HttpError):
            await transport.get("https://example.com/after-close")

    asyncio.run(run())

    assert len(clients) == 1


@pytest.mark.parametrize("http2", [False, True])
@pytest.mark.parametrize("module_name", ["httpx2", "h2"])
def test_optional_dependencies_are_checked_before_any_request(
    monkeypatch: pytest.MonkeyPatch, module_name: str, *, http2: bool
) -> None:
    actual_import = builtins.__import__

    def import_module(name: str, *args: object, **kwargs: object) -> object:
        if name == module_name:
            raise ImportError(name)
        return actual_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    if module_name == "httpx2" or http2:
        with pytest.raises(ImportError):
            Httpx2AsyncTransport(http2=True)
    else:
        transport = Httpx2AsyncTransport(http2=False)
        asyncio.run(transport.aclose())


def test_client_construction_failure_is_a_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**_options: object) -> httpx2.AsyncClient:
        msg = "invalid client configuration"
        raise ValueError(msg)

    monkeypatch.setattr(httpx2, "AsyncClient", unavailable)
    transport = Httpx2AsyncTransport()

    async def run() -> None:
        try:
            with pytest.raises(HttpError):
                await transport.get("https://example.com/")
        finally:
            await transport.aclose()

    asyncio.run(run())


def test_ssl_context_is_also_deferred(
    monkeypatch: pytest.MonkeyPatch, clients: list[httpx2.AsyncClient]
) -> None:
    actual_context = module.truststore.SSLContext
    created: list[ssl.SSLContext] = []

    def context(protocol: int) -> ssl.SSLContext:
        value = actual_context(protocol)
        created.append(value)
        return value

    monkeypatch.setattr(module.truststore, "SSLContext", context)
    transport = Httpx2AsyncTransport()

    assert not created

    async def run() -> None:
        try:
            await transport.get("https://example.com/first")
            await transport.get("https://example.com/second")
        finally:
            await transport.aclose()

    asyncio.run(run())

    assert len(created) == len(clients) == 1
