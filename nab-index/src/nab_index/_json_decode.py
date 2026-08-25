"""One JSON decode for the cache and Simple-API read paths.

``json.loads`` reports most bad input as a :class:`ValueError`, but a document
nested past the decoder's recursion limit raises :class:`RecursionError`, which
a handler written for the first kind does not catch.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["decode_json"]


def decode_json(raw: bytes) -> Any:
    """Decode ``raw``, or raise :class:`ValueError` naming the fault."""
    try:
        return json.loads(raw)
    except ValueError as exc:
        msg = "not valid JSON"
        raise ValueError(msg) from exc
    except RecursionError as exc:
        msg = "nested too deeply to decode"
        raise ValueError(msg) from exc
