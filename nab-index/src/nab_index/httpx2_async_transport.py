"""httpx2-based async HTTP transport for nab-index."""

from __future__ import annotations

__lazy_modules__ = (
    "asyncio",
    "httpx2",
    "importlib.util",
    "json",
    "nab_index.retry",
    "nab_index.retry_limits",
    "nab_index.transport",
    "ssl",
    "sys",
    "truststore",
)

import asyncio
import json as _json
import ssl
import sys
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

import httpx2
import truststore

from .retry import is_certificate_error, next_delay
from .retry_limits import MAX_REDIRECTS, MAX_RETRIES, RETRY_STATUSES
from .transport import (
    DEFAULT_HEADERS,
    ContentDecodingError,
    HttpError,
    accepts_gzip,
    decode_body,
    prefetch_settings_for_backend,
    raise_for_error_status,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Httpx2AsyncTransport",
]


class _Httpx2Response:
    """An httpx2 response with nab's decoded body and 4xx/5xx error handling."""

    __slots__ = ("_content", "_response")

    def __init__(self, response: httpx2.Response, content: bytes) -> None:
        self._response = response
        self._content = content

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def url(self) -> str:
        return str(self._response.url)

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
        raise_for_error_status(self._response.status_code, self.url)


class Httpx2AsyncTransport:
    """Async HTTP transport using httpx2.

    HTTP/2 is enabled by default for connection multiplexing.
    """

    def __init__(self, *, http2: bool = True) -> None:
        """Check optional package availability and defer the client until a request."""
        required = ("httpx2", "h2") if http2 else ("httpx2",)
        for module_name in required:
            if sys.modules.get(module_name) is None and find_spec(module_name) is None:
                msg = f"No module named {module_name!r}"
                raise ModuleNotFoundError(msg, name=module_name)

        self.prefetch_settings = prefetch_settings_for_backend("httpx2")
        self._http2 = http2
        self._client: httpx2.AsyncClient | None = None
        self._closed = False

    def _client_for_request(self) -> httpx2.AsyncClient:
        """Return the reusable client, refusing requests after close."""
        if self._closed:
            msg = "HTTP transport is closed"
            raise RuntimeError(msg)

        client = self._client
        if client is None:
            client = httpx2.AsyncClient(
                http2=self._http2,
                # Simple API URLs may redirect to mirrors or a canonical project name.
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            )
            self._client = client

        return client

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _Httpx2Response:
        """Send a GET, retrying transient failures and truncated gzip bodies.

        Pass :data:`~nab_index.transport.IDENTITY_HEADERS` for undecoded bytes.
        """
        request_headers = dict(DEFAULT_HEADERS)
        if headers is not None:
            request_headers.update(headers)

        decode = accepts_gzip(request_headers)
        failures = 0

        while True:
            try:
                client = self._client_for_request()
                async with client.stream(
                    "GET", url, headers=request_headers
                ) as response:
                    raw = b"".join([part async for part in response.aiter_raw()])

                content = (
                    decode_body(raw, response.headers.get("Content-Encoding"))
                    if decode
                    else raw
                )
            except (httpx2.TooManyRedirects, httpx2.UnsupportedProtocol) as exc:
                # Retrying cannot fix a redirect loop or an unsupported scheme.
                msg = f"GET {url} failed: {exc}"
                raise HttpError(msg) from exc
            except (httpx2.HTTPError, ContentDecodingError) as exc:
                failures += 1
                if is_certificate_error(exc) or failures > MAX_RETRIES:
                    msg = f"GET {url} failed: {exc}"
                    raise HttpError(msg) from exc
                delay = next_delay(failures)
            except Exception as exc:
                # InvalidURL and IDNA errors fall outside httpx2.HTTPError.
                msg = f"GET {url} failed: {exc}"
                raise HttpError(msg) from exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    return _Httpx2Response(response, content)

                failures += 1
                # Preserve the final status and body when retries run out.
                if failures > MAX_RETRIES:
                    return _Httpx2Response(response, content)
                delay = next_delay(failures, response.headers.get("Retry-After"))

            await asyncio.sleep(delay)

    async def aclose(self) -> None:
        """Close an existing client without constructing an unused one."""
        self._closed = True
        client = self._client
        if client is not None:
            await client.aclose()

    def close_without_loop(self) -> bool:
        """Close synchronously only when no client has been constructed."""
        if self._client is not None:
            return False

        self._closed = True
        return True
