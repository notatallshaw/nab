"""Decode JSON from index responses and cache records."""

from __future__ import annotations

__lazy_modules__ = (
    "json",
    "typing",
)

import json
from typing import TypeAlias

__all__ = ["JsonValue", "decode_json"]

JsonValue: TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)


def decode_json(raw: bytes) -> JsonValue:
    """Raise ValueError for invalid JSON or excessive nesting."""
    try:
        # The default decoder has no hooks that can construct other Python types.
        value: JsonValue = json.loads(raw)
    except ValueError as exc:
        msg = "not valid JSON"
        raise ValueError(msg) from exc
    except RecursionError as exc:
        msg = "nested too deeply to decode"
        raise ValueError(msg) from exc
    return value
