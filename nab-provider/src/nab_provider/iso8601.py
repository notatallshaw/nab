"""Shared parsing for the ISO 8601 timestamps that indexes and pylock files carry."""

from __future__ import annotations

import re
import sys
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_FRACTIONAL_SECONDS = re.compile(r"\.(\d+)")

# From 3.11 ``fromisoformat`` reads the ``Z`` suffix and every fraction width
# PEP 700 permits, so ``_to_isoformat`` is only the 3.10 fallback.
_NATIVE_ACCEPTS_PEP_700 = sys.version_info >= (3, 11)


def parse_iso_datetime(raw: str) -> datetime:
    """Parse an ISO 8601 datetime, raising ``ValueError`` when it cannot be read.

    The offset in ``raw`` is preserved, and the result is naive when there is
    none, so each caller decides what a missing offset means.
    """
    if _NATIVE_ACCEPTS_PEP_700:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass

    return datetime.fromisoformat(_to_isoformat(raw))


# The cheapest parser for one timestamp per record: on 3.11+ the native one,
# skipping the ``parse_iso_datetime`` wrapper. It rejects the shapes
# ``_to_isoformat`` rewrites, so callers keep ``parse_iso_datetime`` as a fallback.
fast_iso_parser: Callable[[str], datetime] = (
    datetime.fromisoformat if _NATIVE_ACCEPTS_PEP_700 else parse_iso_datetime
)


def _to_isoformat(raw: str) -> str:
    """Rewrite ``raw`` into the shape ``datetime.isoformat`` emits.

    Before Python 3.11, ``datetime.fromisoformat`` parses only that shape: an
    explicit ``+HH:MM`` offset rather than ``Z``, and a fraction of exactly 3 or
    6 digits. PEP 700 serves ``Z`` and permits 0 through 6, so those are the two
    parts normalized here; the rest is left for ``fromisoformat`` to judge.
    """
    iso = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    return _FRACTIONAL_SECONDS.sub(_microseconds, iso)


def _microseconds(match: re.Match[str]) -> str:
    # A datetime holds microseconds, so digits past the sixth are dropped.
    return "." + match.group(1)[:6].ljust(6, "0")
