"""HTTP transport abstractions for nab-index.

Defines minimal protocols for async HTTP GET requests.
Implementations can use any async HTTP library (httpx, or urllib3
wrapped in to_thread, etc.).
"""

from __future__ import annotations

import gzip
import zlib
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_HEADERS",
    "IDENTITY_HEADERS",
    "USER_AGENT",
    "AsyncHttpTransport",
    "ContentDecodingError",
    "HttpError",
    "HttpResponse",
    "accepts_gzip",
    "decode_body",
    "raise_for_error_status",
    "raise_unless_ok",
]


def _user_agent() -> str:
    """Return the ``nab-index/<version>`` name PyPI's API guidelines ask for."""
    try:
        return f"nab-index/{version('nab-index')}"
    except PackageNotFoundError:
        return "nab-index/0.0.0+unknown"


USER_AGENT: Final[str] = _user_agent()

# Sent on every request unless the caller overrides an entry.
DEFAULT_HEADERS: Final[dict[str, str]] = {
    "Accept-Encoding": "gzip",
    "User-Agent": USER_AGENT,
}

# For a caller that needs the body exactly as stored, undecoded.
IDENTITY_HEADERS: Final[dict[str, str]] = {"Accept-Encoding": "identity"}

_HTTP_BAD_REQUEST: Final = 400
_CONTENT_STATUSES: Final[frozenset[int]] = frozenset({200, 203})

# RFC 9110 8.4.1.3: "x-gzip" is the same coding as "gzip".
_GZIP_CODINGS: Final[frozenset[str]] = frozenset({"gzip", "x-gzip"})


class HttpError(Exception):
    """A request failed, or answered with a status the caller cannot use.

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
    if content_encoding.strip().lower() not in _GZIP_CODINGS:
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
        """Raise :class:`HttpError` for 4xx/5xx responses.

        Not a gate on reading the body; see :func:`raise_unless_ok`.
        """
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


def raise_for_error_status(status: int, url: str) -> None:
    """Raise :class:`HttpError` for a 4xx/5xx ``status``."""
    if status >= _HTTP_BAD_REQUEST:
        msg = f"HTTP {status} for {url}"
        raise HttpError(msg)


def raise_unless_ok(response: HttpResponse, url: str) -> None:
    """Raise :class:`HttpError` unless ``response`` carries the requested content.

    :meth:`HttpResponse.raise_for_status` clears everything under 400, but a
    204 has no content (RFC 9110 section 15.3.5) and a 3xx a transport did not
    follow names another resource (section 15.4). A 203 passes with the 200:
    its body is the representation a transforming proxy rewrote (section
    15.3.4).
    """
    response.raise_for_status()
    if response.status_code not in _CONTENT_STATUSES:
        msg = f"HTTP {response.status_code} for {url} is not the requested content"
        raise HttpError(msg)
