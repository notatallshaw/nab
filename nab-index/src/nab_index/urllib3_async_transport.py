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
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import truststore
import urllib3
from typing_extensions import override

from .retry import GET_RETRY, MAX_RETRIES, next_delay
from .transport import (
    DEFAULT_HEADERS,
    ContentDecodingError,
    HttpError,
    accepts_gzip,
    decode_body,
    raise_for_error_status,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Urllib3AsyncTransport",
]


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

    @override
    def cert_store_stats(self) -> dict[str, int]:
        return {"x509_ca": 1, "x509": 1, "crl": 0}


def _final_url(response: urllib3.BaseHTTPResponse, requested_url: str) -> str:
    """Return the absolute URL ``response`` was retrieved from.

    urllib3 records status, connect, and read retries in the same history as
    redirects, and records them with the request path alone, so only a hop
    carrying a ``Location`` moves the URL. A ``Location`` is reported verbatim
    and may be relative, so each is resolved against the URL that served it
    (RFC 3986 section 5.1.3).
    """
    history = response.retries.history if response.retries is not None else ()

    url = requested_url
    for hop in history:
        if hop.redirect_location:
            url = urljoin(hop.url or url, hop.redirect_location)
    return url


class _Urllib3Response:
    """Adapter that gives a urllib3 response the HttpResponse shape.

    The body is fetched undecoded, and decoded by the transport when the
    request asked for gzip (see :func:`~nab_index.transport.decode_body`),
    so it is carried here rather than read from the urllib3 response.
    """

    __slots__ = ("_content", "_requested_url", "_response")

    def __init__(
        self, response: urllib3.BaseHTTPResponse, content: bytes, requested_url: str
    ) -> None:
        self._response = response
        self._content = content
        self._requested_url = requested_url

    @property
    def status_code(self) -> int:
        return self._response.status

    @property
    def url(self) -> str:
        return _final_url(self._response, self._requested_url)

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8")

    def json(self) -> Any:
        return _json.loads(self._content)

    def raise_for_status(self) -> None:
        raise_for_error_status(self._response.status, self.url)


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

    def _request(self, url: str, headers: dict[str, str]) -> _Urllib3Response:
        """Issue the GET on this worker thread, retrying a truncated body.

        The body is fetched raw, and decoded with ``decode_body`` when the
        request asked for gzip. urllib3's retries cover connection errors and
        statuses, not a body that decodes short, so that case is retried here
        on the same schedule.
        """
        pool = self._pool()
        decode = accepts_gzip(headers)
        failures = 0

        while True:
            response = pool.request(
                "GET",
                url,
                headers=headers,
                timeout=self._timeout,
                retries=GET_RETRY,
                decode_content=False,
            )

            if not decode:
                return _Urllib3Response(response, response.data, url)

            try:
                content = decode_body(
                    response.data, response.headers.get("Content-Encoding")
                )
            except ContentDecodingError:
                failures += 1
                if failures > MAX_RETRIES:
                    raise
                time.sleep(next_delay(failures))
                continue

            return _Urllib3Response(response, content, url)

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _Urllib3Response:
        """Send a GET request, off-loaded to a worker thread.

        ``headers`` overrides entries of
        :data:`~nab_index.transport.DEFAULT_HEADERS`, so a caller can pass
        :data:`~nab_index.transport.IDENTITY_HEADERS` to get the body
        undecoded. Requesting gzip matters here: without it urllib3's stdlib
        base sends ``Accept-Encoding: identity``, which disables compression.
        The defaults go on the request rather than on the pool because urllib3
        replaces the pool's headers with a request's own instead of merging.
        """
        request_headers = dict(DEFAULT_HEADERS)
        if headers is not None:
            request_headers.update(headers)
        try:
            return await asyncio.to_thread(self._request, url, request_headers)
        except Exception as exc:
            # A malformed IPv6 host in a redirect's Location makes urllib3's
            # urljoin re-parse raise a bare ValueError, outside its HTTPError
            # hierarchy.
            msg = f"GET {url} failed: {exc}"
            raise HttpError(msg) from exc

    async def aclose(self) -> None:
        """Close every per-thread pool."""
        with self._pools_lock:
            pools = list(self._pools)
        for pool in pools:
            await asyncio.to_thread(pool.clear)
