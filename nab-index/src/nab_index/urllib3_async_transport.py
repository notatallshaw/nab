"""urllib3-based async HTTP transport for nab-index.

urllib3 is sync. To present an async surface we run each request
in a worker thread via ``asyncio.to_thread``. This is useful for
benchmarking against the natively-async backends, and for cases
where users already have urllib3 in their environment.
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import TYPE_CHECKING, Any

import urllib3

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Urllib3AsyncTransport",
]


_HTTP_BAD_REQUEST = 400


class _Urllib3Response:
    """Adapter that gives a urllib3 response the HttpResponse shape."""

    __slots__ = ("_response",)

    def __init__(self, response: urllib3.BaseHTTPResponse) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def content(self) -> bytes:
        return self._response.data

    @property
    def text(self) -> str:
        return self._response.data.decode("utf-8")

    def json(self) -> Any:
        return _json.loads(self._response.data)

    def raise_for_status(self) -> None:
        status = self._response.status
        if status >= _HTTP_BAD_REQUEST:
            msg = f"HTTP {status} for {self._response.geturl() or '<unknown>'}"
            raise urllib3.exceptions.HTTPError(msg)


class Urllib3AsyncTransport:
    """Async HTTP transport using urllib3 (sync) wrapped in to_thread.

    Each ``get`` runs the underlying sync request on the asyncio
    default executor. The PoolManager is thread-safe, so concurrent
    requests from many tasks share connections cleanly.
    """

    def __init__(self, *, num_pools: int = 10, maxsize: int = 50) -> None:
        """Create a transport."""
        self._pool = urllib3.PoolManager(num_pools=num_pools, maxsize=maxsize)

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _Urllib3Response:
        """Send a GET request, off-loaded to a worker thread.

        Requests gzip; without it urllib3's stdlib base sends
        ``Accept-Encoding: identity``, which disables compression.
        """
        request_headers = {"Accept-Encoding": "gzip"}
        if headers is not None:
            request_headers.update(headers)
        response = await asyncio.to_thread(
            self._pool.request, "GET", url, headers=request_headers
        )
        return _Urllib3Response(response)

    async def aclose(self) -> None:
        """Close the underlying pool."""
        await asyncio.to_thread(self._pool.clear)
