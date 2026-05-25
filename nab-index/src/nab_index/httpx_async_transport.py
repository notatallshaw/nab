"""httpx-based async HTTP transport for nab-index."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

import httpx
import truststore

from ._retry import RETRY_STATUSES, get_with_retry

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
        """Send a GET request, retrying transient failures with backoff.

        httpx has no status-aware retry of its own, so the shared helper
        retries connection/read errors and transient server statuses.
        """

        async def _send() -> httpx.Response:
            return await self._client.get(url, headers=headers)

        # httpx.Response satisfies the HttpResponse protocol structurally;
        # the headers slot is httpx's own Headers, not a literal Mapping.
        return await get_with_retry(  # type: ignore[return-value]
            _send,
            transient=httpx.TransportError,
            retry_status=lambda r: r.status_code in RETRY_STATUSES,
        )

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.aclose()
