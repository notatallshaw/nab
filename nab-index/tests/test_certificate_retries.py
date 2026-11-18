"""Certificate verification failures terminate transport retries."""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest
import urllib3

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.retry import GET_RETRY, is_certificate_error
from nab_index.transport import HttpError


@pytest.mark.parametrize("attribute", ["__cause__", "__context__"])
def test_certificate_error_through_wrapper(attribute: str) -> None:
    error = httpx.ConnectError("certificate rejected")
    setattr(error, attribute, ssl.SSLCertVerificationError("untrusted issuer"))
    assert is_certificate_error(error)


def test_certificate_error_without_wrapper() -> None:
    assert is_certificate_error(ssl.SSLCertVerificationError("expired"))


@pytest.mark.parametrize(
    "error", [None, OSError("connection lost"), ssl.SSLError("TLS EOF")]
)
def test_other_errors_are_not_certificate_failures(error: BaseException | None) -> None:
    assert not is_certificate_error(error)


def test_cyclic_exception_chain_terminates() -> None:
    error = OSError("first")
    other = OSError("second")
    error.__cause__ = other
    other.__context__ = error
    assert not is_certificate_error(error)


def test_explicit_cause_takes_precedence() -> None:
    error = OSError("request failed")
    error.__cause__ = OSError("connection lost")
    error.__context__ = ssl.SSLCertVerificationError("earlier unrelated failure")
    assert not is_certificate_error(error)


def test_urllib3_stops_at_wrapped_certificate_error() -> None:
    certificate = ssl.SSLCertVerificationError("untrusted issuer")
    error = urllib3.exceptions.SSLError(certificate)
    error.__cause__ = certificate
    with pytest.raises(urllib3.exceptions.MaxRetryError) as caught:
        GET_RETRY.increment("GET", "/simple/numpy/", error=error)
    assert caught.value.reason is error
    assert caught.value.__cause__ is error


def test_urllib3_retains_transient_tls_retry() -> None:
    error = urllib3.exceptions.SSLError("TLS EOF")
    retry = GET_RETRY.increment("GET", "/simple/numpy/", error=error)
    assert not retry.is_exhausted()
    assert retry.other == GET_RETRY.other - 1


def test_httpx_stops_at_wrapped_certificate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = httpx.ConnectError("certificate rejected")
    error.__cause__ = ssl.SSLCertVerificationError("untrusted issuer")
    attempts: list[httpx.Request] = []

    def fail_handshake(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise error

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_handshake))

    def make_client(**kwargs: object) -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(
        "nab_index.httpx_async_transport.httpx.AsyncClient", make_client
    )

    async def unexpected_sleep(delay: float) -> None:
        pytest.fail(f"certificate failure scheduled a {delay} second retry")

    monkeypatch.setattr(
        "nab_index.httpx_async_transport.asyncio.sleep", unexpected_sleep
    )

    async def request() -> None:
        transport = HttpxAsyncTransport(http2=False)
        try:
            await transport.get("https://example.com/simple/numpy/")
        finally:
            await transport.aclose()

    with pytest.raises(HttpError, match="certificate rejected") as caught:
        asyncio.run(request())
    assert caught.value.__cause__ is error
    assert len(attempts) == 1
