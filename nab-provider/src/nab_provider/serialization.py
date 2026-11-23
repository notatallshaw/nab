"""Simple API formats that an index configuration can request."""

from __future__ import annotations

import enum

__all__ = ["SimpleSerialization"]


class SimpleSerialization(enum.Enum):
    """Which Simple-API serialization nab asks an index for."""

    NEGOTIATE = "negotiate"
    JSON = "json"
    HTML = "html"
