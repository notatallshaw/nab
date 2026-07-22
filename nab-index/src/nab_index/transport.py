"""HTTP transport abstractions for nab-index.

Defines minimal protocols for async HTTP GET requests.
Implementations can use any async HTTP library (httpx, or urllib3
wrapped in to_thread, etc.).
"""

from __future__ import annotations

import gzip
import zlib
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "IDENTITY_HEADERS",
    "AsyncHttpTransport",
    "ContentDecodingError",
    "HttpError",
    "HttpResponse",
    "accepts_gzip",
    "decode_body",
]

# For a caller that needs the body exactly as stored, undecoded.
IDENTITY_HEADERS: Final[dict[str, str]] = {"Accept-Encoding": "identity"}


class HttpError(Exception):
    """A request failed: a connection/transport error or a 4xx/5xx status.

    Transports raise this from ``get`` and ``raise_for_status`` so callers
    can handle index failures without importing a specific HTTP backend.
    """


class ContentDecodingError(Exception):
    """A response body did not decode as its Content-Encoding promised."""


def _quality(params: str) -> float:
    """Return an Accept-Encoding entry's ``q`` value, 1.0 when it carries none.

    A ``q`` that does not parse reads as a refusal.
    """
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "q":
            try:
                return float(value)
            except ValueError:
                return 0.0
    return 1.0


def accepts_gzip(request_headers: Mapping[str, str]) -> bool:
    """Whether ``request_headers`` asked the server for gzip.

    A static file server derives Content-Encoding from the filename, so it
    serves a ``.tar.gz`` as its own untouched bytes under
    ``Content-Encoding: gzip``. Decoding that yields a bare tar, which no
    published digest covers, so only a coding the request asked for may be
    undone.
    """
    folded = {name.lower(): value for name, value in request_headers.items()}
    for entry in folded.get("accept-encoding", "").split(","):
        coding, _, params = entry.partition(";")
        if coding.strip().lower() == "gzip" and _quality(params) > 0:
            return True
    return False


def decode_body(body: bytes, content_encoding: str | None) -> bytes:
    """Return ``body`` decoded per ``content_encoding``.

    Transports fetch bodies undecoded and decode them here: the HTTP
    libraries' own gzip decoders accept a stream cut before its trailer
    and hand back a silent prefix under a 200, while :func:`gzip.decompress`
    checks the trailer (CRC and length) and turns the truncation into
    :class:`ContentDecodingError`.

    Only gzip is handled, the one coding the transports advertise; any
    other coding passes through untouched. An empty body also passes
    through: a bodiless response (a 304) may still carry the
    representation's Content-Encoding.
    """
    if not body or content_encoding is None:
        return body
    if content_encoding.strip().lower() != "gzip":
        return body
    try:
        return gzip.decompress(body)
    except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
        msg = f"gzip response body is truncated or corrupt: {exc}"
        raise ContentDecodingError(msg) from exc


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

        An implementation must not decode a coding ``headers`` did not ask
        for; see :func:`accepts_gzip`.

        Raises :class:`HttpError` on a connection or transport failure.
        """
        ...

    async def aclose(self) -> None:
        """Release resources."""
        ...
