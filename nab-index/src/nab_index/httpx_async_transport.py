"""httpx-based async HTTP transport for nab-index."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

import httpx
import truststore

if TYPE_CHECKING:
    from .transport import HttpResponse

__all__ = [
    "HttpxAsyncTransport",
]


class HttpxAsyncTransport:
    """Async HTTP transport using httpx.

    HTTP/2 is enabled by default for connection multiplexing.
    The httpx.Response type already matches the HttpResponse
    protocol, so it is returned directly.
    """

    def __init__(self, *, http2: bool = True) -> None:
        """Create a transport."""
        self._client = httpx.AsyncClient(
            http2=http2,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        """Send a GET request."""
        # httpx.Response satisfies the HttpResponse protocol structurally
        # (headers, status_code, content, text, json, raise_for_status are
        # all present); the disagreement is only that httpx's headers slot
        # is its own Headers class rather than a literal Mapping[str, str].
        return await self._client.get(url, headers=headers)  # type: ignore[return-value]

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.aclose()
