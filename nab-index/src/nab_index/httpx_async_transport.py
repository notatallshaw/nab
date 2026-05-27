"""httpx-based async HTTP transport for nab-index."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING, Any

import httpx
import truststore

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
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _HttpxResponse:
        """Send a GET request."""
        try:
            response = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"GET {url} failed: {exc}"
            raise HttpError(msg) from exc
        return _HttpxResponse(response)

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.aclose()
