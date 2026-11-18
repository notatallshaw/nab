"""Buffered response bodies and the metadata adapter shared by the HTTPX clients."""

from __future__ import annotations

__lazy_modules__ = ("json", "nab_index.transport")

import json
from typing import TYPE_CHECKING

from . import transport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Protocol

    class _ResponseMetadata(Protocol):
        @property
        def status_code(self) -> int: ...

        @property
        def headers(self) -> Mapping[str, str]: ...

        @property
        def url(self) -> object: ...


__all__ = ["HttpxResponse", "ResponseBody"]


class ResponseBody:
    """The bytes returned by a transport after applying its content-coding policy."""

    __slots__ = ("_content",)

    def __init__(self, content: bytes) -> None:
        """Store the bytes supplied by the transport."""
        self._content = content

    @property
    def content(self) -> bytes:
        """Return the buffered bytes."""
        return self._content

    @property
    def text(self) -> str:
        """Read the body as UTF-8 text."""
        return self._content.decode("utf-8")

    def json(self) -> transport.JsonValue:
        """Parse the body with the standard JSON decoder."""
        value: transport.JsonValue = json.loads(self._content)
        return value


class HttpxResponse(ResponseBody):
    """Keep the native status, headers and final URL alongside the buffered body."""

    __slots__ = ("_response",)

    def __init__(self, response: _ResponseMetadata, content: bytes) -> None:
        """Retain the response metadata after its stream closes."""
        super().__init__(content)
        self._response = response

    @property
    def status_code(self) -> int:
        """Return the server's HTTP status code."""
        return self._response.status_code

    @property
    def url(self) -> str:
        """Return the final URL after redirects."""
        return str(self._response.url)

    @property
    def headers(self) -> Mapping[str, str]:
        """Expose the client's case-insensitive response headers."""
        return self._response.headers

    def raise_for_status(self) -> None:
        """Raise HttpError for a client or server error status."""
        transport.raise_for_error_status(self.status_code, self.url)
