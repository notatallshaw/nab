"""Simple-API serialization vocabulary for nab-index.

A leaf module: the enum and the ``Accept`` header each of its members asks
for, importable from the client, the cache and the config layer alike.
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


# PEP 691: advertise every serialization we can read, because an index that
# cannot honour the header may answer with a type we did not ask for.  The
# HTML entry names text/html as well because PEP 691 makes it an alias for
# the pre-691 spelling, not a second format.
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
