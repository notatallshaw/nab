"""httpx-based async HTTP transport for nab-index."""

from __future__ import annotations

import asyncio
import ssl
from typing import TYPE_CHECKING, Any

import httpx
import truststore

from .retry import MAX_REDIRECTS, MAX_RETRIES, RETRY_STATUSES, next_delay
from .transport import HttpError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "HttpxAsyncTransport",
]


class _HttpxResponse:
    """Adapter that converts httpx's status error to the transport's HttpError.

    httpx.Response already matches the HttpResponse protocol; only
    raise_for_status needs translating so callers see one error type.
    """

    __slots__ = ("_response",)

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def content(self) -> bytes:
        return self._response.content

    @property
    def text(self) -> str:
        return self._response.text

    def json(self) -> Any:
        return self._response.json()

    def raise_for_status(self) -> None:
        try:
            self._response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HttpError(str(exc)) from exc


class HttpxAsyncTransport:
    """Async HTTP transport using httpx.

    HTTP/2 is enabled by default for connection multiplexing.
    """

    def __init__(self, *, http2: bool = True) -> None:
        """Create a transport."""
        self._client = httpx.AsyncClient(
            http2=http2,
            # httpx defaults this off, unlike urllib3 and pip; the Simple
            # API relies on redirects (canonicalising URLs, mirrors, CDNs).
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _HttpxResponse:
        """Send a GET request, retrying a blip.

        httpx has no retry machinery beyond reconnecting, so the shared policy
        runs in this loop rather than in the client.
        """
        failures = 0

        while True:
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                failures += 1
                if failures > MAX_RETRIES:
                    msg = f"GET {url} failed: {exc}"
                    raise HttpError(msg) from exc
                delay = next_delay(failures)
            except Exception as exc:
                # httpx raises InvalidURL and lets idna hostname errors escape
                # its HTTPError hierarchy.  A URL httpx cannot even issue is not
                # a blip, so it is wrapped and raised rather than retried.
                msg = f"GET {url} failed: {exc}"
                raise HttpError(msg) from exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    return _HttpxResponse(response)
                failures += 1
                # Out of budget: hand back the status the index served, which is
                # what the urllib3 backend does with raise_on_status=False.
                if failures > MAX_RETRIES:
                    return _HttpxResponse(response)
                delay = next_delay(failures, response.headers.get("Retry-After"))

            await asyncio.sleep(delay)

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.aclose()
