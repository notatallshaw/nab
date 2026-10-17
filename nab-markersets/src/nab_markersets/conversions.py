"""Bound successful marker conversions within a caller-owned operation."""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

from ._packaging import Marker
from .markersets import MarkerSet

if TYPE_CHECKING:
    from ._markersets import MarkerLike

__all__ = ["MarkerSetCache", "PreparedMarkerSets"]

_SHORT_SET_LIMIT = 128
# Short source literals fit the minimum enabled integer conversion limit.
_SHORT_TEXT_LIMIT = 256
_PREPARED_SET_LIMIT = 32
_PREPARED_TEXT_LIMIT = 65_536
_PREPARED_LITERAL_LIMIT = 64
_QUOTED_LITERAL = re.compile(r"""(?:"[^"]*"|'[^']*')""")
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
    """Reuse short native marker conversions within one operation on one thread.

    Strings and non-native renderers stay fresh. Evaluate the returned set separately.
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


class PreparedMarkerSets:
    """Hand successful fresh conversions to a consumer within one operation."""

    __slots__ = ("_characters", "_sets")

    def __init__(self) -> None:
        """Start an empty operation-owned conversion handoff."""
        self._sets: dict[tuple[str, int], MarkerSet] = {}
        self._characters = 0

    def prepare(self, marker: str | MarkerLike) -> MarkerSet:
        """Convert afresh, retaining eligible successful results for the consumer."""
        text = _native_marker_text(marker)
        limit = sys.getrecursionlimit()
        converted = MarkerSet.from_marker(marker if text is None else text)
        if text is not None and limit == sys.getrecursionlimit():
            self._remember(text, converted, limit)
        return converted

    def _remember(self, text: str, converted: MarkerSet, limit: int) -> None:
        """Retain a bounded source and its conversion for the consumer."""
        if "\\" in text:
            return
        key = (text, limit)
        if (
            key in self._sets
            or len(self._sets) >= _PREPARED_SET_LIMIT
            or self._characters + len(text) > _PREPARED_TEXT_LIMIT
        ):
            return
        # Check original literals, including any the converted formula folded away.
        if any(
            match.end() - match.start() - 2 > _PREPARED_LITERAL_LIMIT
            for match in _QUOTED_LITERAL.finditer(text)
        ):
            return
        self._sets[key] = converted
        self._characters += len(text)

    def convert(self, marker: str | MarkerLike) -> MarkerSet:
        """Use a matching prepared conversion, or convert afresh without retention."""
        text = _native_marker_text(marker)
        if text is None:
            return MarkerSet.from_marker(marker)
        converted = self._sets.get((text, sys.getrecursionlimit()))
        return MarkerSet.from_marker(text) if converted is None else converted
