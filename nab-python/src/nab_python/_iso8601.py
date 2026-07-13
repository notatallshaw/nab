"""Shared parsing for the ISO 8601 timestamps that indexes and pylock files carry."""

from __future__ import annotations

import re
from datetime import datetime

_FRACTIONAL_SECONDS = re.compile(r"\.(\d+)")


def parse_iso_datetime(raw: str) -> datetime:
    """Parse an ISO 8601 datetime, raising ``ValueError`` when it cannot be read.

    The offset in ``raw`` is preserved, and the result is naive when there is
    none, so each caller decides what a missing offset means.
    """
    return datetime.fromisoformat(_to_isoformat(raw))


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
