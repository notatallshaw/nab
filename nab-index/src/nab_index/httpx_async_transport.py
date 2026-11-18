"""httpx-based async HTTP transport for nab-index."""

from __future__ import annotations

__lazy_modules__ = (
    "asyncio",
    "httpx",
    "nab_index.response",
    "nab_index.retry",
    "nab_index.retry_limits",
    "nab_index.transport",
    "ssl",
    "truststore",
)

import asyncio
import ssl

import httpx
import truststore

from .response import HttpxResponse
from .retry import is_certificate_error, next_delay
from .retry_limits import MAX_REDIRECTS, MAX_RETRIES, RETRY_STATUSES
from .transport import (
    ContentDecodingError,
    HttpError,
    accepts_gzip,
    decode_body,
    merge_request_headers,
    prefetch_settings_for_backend,
)

__all__ = [
    "HttpxAsyncTransport",
]


class HttpxAsyncTransport:
    """Async HTTP transport using httpx.

    HTTP/2 is enabled by default for connection multiplexing.
    """

    def __init__(self, *, http2: bool = True) -> None:
        """Create a transport."""
        self.prefetch_settings = prefetch_settings_for_backend("httpx")
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
    ) -> HttpxResponse:
        """Send a GET request, retrying a blip.

        httpx has no retry machinery beyond reconnecting, so the shared policy
        runs in this loop rather than in the client. The body is read raw and
        decoded with ``decode_body``, so a truncated gzip stream is retried
        instead of returned as if complete. ``headers`` overrides entries of
        :data:`~nab_index.transport.DEFAULT_HEADERS`, so a caller can pass
        :data:`~nab_index.transport.IDENTITY_HEADERS` to get the body
        undecoded.
        """
        request_headers = merge_request_headers(headers)

        decode = accepts_gzip(request_headers)
        failures = 0

        while True:
            try:
                async with self._client.stream(
                    "GET", url, headers=request_headers
                ) as response:
                    raw = b"".join([part async for part in response.aiter_raw()])
                content = (
                    decode_body(raw, response.headers.get("Content-Encoding"))
                    if decode
                    else raw
                )
            except (httpx.TooManyRedirects, httpx.UnsupportedProtocol) as exc:
                # A redirect loop and an unsupported scheme are persistent, so
                # they are raised rather than retried.
                msg = f"GET {url} failed: {exc}"
                raise HttpError(msg) from exc
            except (httpx.HTTPError, ContentDecodingError) as exc:
                failures += 1
                if is_certificate_error(exc) or failures > MAX_RETRIES:
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
                    return HttpxResponse(response, content)
                failures += 1
                # Out of budget: hand back the status the index served, which is
                # what the urllib3 backend does with raise_on_status=False.
                if failures > MAX_RETRIES:
                    return HttpxResponse(response, content)
                delay = next_delay(failures, response.headers.get("Retry-After"))

            await asyncio.sleep(delay)

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.aclose()
