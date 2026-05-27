"""HTTP transport abstractions for nab-index.

Defines minimal protocols for async HTTP GET requests.
Implementations can use any async HTTP library (httpx, or urllib3
wrapped in to_thread, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AsyncHttpTransport",
    "HttpError",
    "HttpResponse",
]


class HttpError(Exception):
    """A request failed: a connection/transport error or a 4xx/5xx status.

    Transports raise this from ``get`` and ``raise_for_status`` so callers
    can handle index failures without importing a specific HTTP backend.
    """


class HttpResponse(Protocol):
    """Minimal HTTP response shape, shared by sync and async transports."""

    @property
    def status_code(self) -> int:
        """HTTP status code (e.g. 200, 304, 404)."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Response headers, case-insensitive lookup by lowercased key."""
        ...

    @property
    def content(self) -> bytes:
        """Response body as bytes."""
        ...

    @property
    def text(self) -> str:
        """Response body as text."""
        ...

    def json(self) -> Any:
        """Response body parsed as JSON."""
        ...

    def raise_for_status(self) -> None:
        """Raise :class:`HttpError` for 4xx/5xx responses."""
        ...


class AsyncHttpTransport(Protocol):
    """Minimal async HTTP transport for Simple API access.

    Implementations are responsible for connection pooling and
    HTTP version negotiation. Concurrency limits are managed by
    the caller (e.g. via asyncio.Semaphore).
    """

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        """Send a GET request and return the response.

        Raises :class:`HttpError` on a connection or transport failure.
        """
        ...

    async def aclose(self) -> None:
        """Release resources."""
        ...
