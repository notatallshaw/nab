"""Bound successful marker conversions within a caller-owned operation."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ._packaging import Marker
from .markersets import MarkerSet

if TYPE_CHECKING:
    from ._markersets import MarkerLike

__all__ = ["MarkerSetCache"]

_SHORT_SET_LIMIT = 128
# Short source literals fit the minimum enabled integer conversion limit.
_SHORT_TEXT_LIMIT = 256
_NATIVE_MARKER_STR: object = Marker.__str__


def _native_marker_text(marker: str | MarkerLike) -> str | None:
    """Read an unchanged selected-backend renderer, leaving other inputs fresh."""
    if type(marker) is not Marker or Marker.__str__ is not _NATIVE_MARKER_STR:
        return None
    try:
        return str(marker)
    except RecursionError:
        return None


class MarkerSetCache:
    """Reuse short native-marker conversions within one operation on one thread.

    Strings and non-native renderers are converted afresh. Evaluation always
    belongs to the caller and is never cached here.
    """

    __slots__ = ("_sets",)

    def __init__(self) -> None:
        """Start an empty operation-owned cache."""
        self._sets: dict[tuple[str, int], MarkerSet] = {}

    def from_marker(self, marker: str | MarkerLike) -> MarkerSet:
        """Reuse an eligible successful conversion under its parser context."""
        text = _native_marker_text(marker)
        if text is None:
            return MarkerSet.from_marker(marker)
        key = None
        if (
            len(text) <= _SHORT_TEXT_LIMIT
            and "\\" not in text
            and "(" not in text
            and ")" not in text
        ):
            key = (text, sys.getrecursionlimit())
            cached = self._sets.get(key)
            if cached is not None:
                return cached
        parsed = MarkerSet.from_marker(text)
        if (
            key is not None
            and key[1] == sys.getrecursionlimit()
            and len(self._sets) < _SHORT_SET_LIMIT
        ):
            self._sets[key] = parsed
        return parsed
