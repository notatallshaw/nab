"""Simple-API serialization vocabulary for nab-index.

The enum and the ``Accept`` header each of its members asks for.  Kept free
of nab-index imports so the client, the cache, and the config layer can all
reach it.
"""

from __future__ import annotations

import enum

__all__ = [
    "SimpleSerialization",
    "simple_accept_header",
]


class SimpleSerialization(enum.Enum):
    """Which Simple-API serialization nab asks an index for."""

    NEGOTIATE = "negotiate"
    JSON = "json"
    HTML = "html"


# PEP 691: negotiation advertises every serialization we can read, because an
# index that cannot honour the header may answer with a type we did not ask
# for.  The HTML pin names text/html too, which PEP 691 treats as an alias for
# the pre-691 spelling rather than a second format.
_ACCEPT: dict[SimpleSerialization, str] = {
    SimpleSerialization.NEGOTIATE: (
        "application/vnd.pypi.simple.v1+json, "
        "application/vnd.pypi.simple.v1+html;q=0.2, "
        "text/html;q=0.01"
    ),
    SimpleSerialization.JSON: "application/vnd.pypi.simple.v1+json",
    SimpleSerialization.HTML: "application/vnd.pypi.simple.v1+html, text/html;q=0.01",
}


def simple_accept_header(serialization: SimpleSerialization) -> str:
    """Return the ``Accept`` header for a listing request under ``serialization``."""
    return _ACCEPT[serialization]
