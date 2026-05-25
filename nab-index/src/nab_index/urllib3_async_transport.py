"""urllib3-based async HTTP transport for nab-index.

urllib3 is sync. To present an async surface we run each request
in a worker thread via ``asyncio.to_thread``. This is useful for
benchmarking against the natively-async backends, and for cases
where users already have urllib3 in their environment.
"""

from __future__ import annotations

import asyncio
import json as _json
import ssl
from typing import TYPE_CHECKING, Any

import truststore
import urllib3

from ._retry import urllib3_retry
from ._tls import forbid_unverified_https

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Urllib3AsyncTransport",
]

# nab never sends an unverified HTTPS request; make the degrade fatal.
forbid_unverified_https()


_HTTP_BAD_REQUEST = 400


class _SSLContext(truststore.SSLContext):
    """truststore SSLContext that answers urllib3-future's cert probe.

    urllib3-future removed the ``ssl_context is None`` guard from
    ``ssl_wrap_socket`` in commit ``23d13d6`` (Jun 2025) and now calls
    ``context.cert_store_stats()`` whenever no ``ca_certs`` is supplied;
    truststore's ``cert_store_stats`` raises ``NotImplementedError`` by
    design (``sethmlarson/truststore`` commit ``63dc9e1``, Feb 2023).
    Returning a non-empty count tells urllib3-future the context already
    has trust roots, which is true: truststore delegates verification to
    the OS framework. Upstream ``urllib3`` 2.x is unaffected because PR
    1566 (Apr 2019) left the guard in place. Drop this subclass once
    urllib3-future restores the guard.
    """

    def cert_store_stats(self) -> dict[str, int]:
        return {"x509_ca": 1, "x509": 1, "crl": 0}


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
        self._pool = urllib3.PoolManager(
            num_pools=num_pools,
            maxsize=maxsize,
            ssl_context=_SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            retries=urllib3_retry(),
        )

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
