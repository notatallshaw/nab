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
import threading
from typing import TYPE_CHECKING, Any

import truststore
import urllib3

from .retry import GET_RETRY
from .transport import HttpError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Urllib3AsyncTransport",
]


_HTTP_BAD_REQUEST = 400

# urllib3's default read timeout is None, so bound it against a stalled index.
_DEFAULT_TIMEOUT_SECONDS = 5.0


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
            raise HttpError(msg)


class Urllib3AsyncTransport:
    """Async HTTP transport using urllib3 (sync) wrapped in to_thread.

    Each ``get`` runs the underlying sync request on the asyncio default
    executor.  A separate :class:`~urllib3.PoolManager` (and truststore
    SSLContext) is kept per worker thread: truststore toggles the
    context's ``verify_mode`` to ``CERT_NONE`` for the duration of each
    ``wrap_socket`` (truststore#209), so sharing one context across the
    executor threads races that toggle and trips spurious
    ``InsecureRequestWarning``s.  One context per thread means no two
    threads ever touch the same context; connections are still reused
    within each thread.
    """

    def __init__(
        self,
        *,
        num_pools: int = 10,
        maxsize: int = 50,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Create a transport."""
        self._num_pools = num_pools
        self._maxsize = maxsize
        self._timeout = timeout
        self._local = threading.local()
        self._pools: list[urllib3.PoolManager] = []
        self._pools_lock = threading.Lock()

    def _pool(self) -> urllib3.PoolManager:
        """Return this worker thread's pool, creating it on first use."""
        pool: urllib3.PoolManager | None = getattr(self._local, "pool", None)
        if pool is None:
            pool = urllib3.PoolManager(
                num_pools=self._num_pools,
                maxsize=self._maxsize,
                ssl_context=_SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            )
            self._local.pool = pool
            with self._pools_lock:
                self._pools.append(pool)
        return pool

    def _request(self, url: str, headers: dict[str, str]) -> urllib3.BaseHTTPResponse:
        return self._pool().request(
            "GET",
            url,
            headers=headers,
            timeout=self._timeout,
            retries=GET_RETRY,
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
        try:
            response = await asyncio.to_thread(self._request, url, request_headers)
        except urllib3.exceptions.HTTPError as exc:
            msg = f"GET {url} failed: {exc}"
            raise HttpError(msg) from exc
        return _Urllib3Response(response)

    async def aclose(self) -> None:
        """Close every per-thread pool."""
        with self._pools_lock:
            pools = list(self._pools)
        for pool in pools:
            await asyncio.to_thread(pool.clear)
