"""Datetime parsing shared by the benchmark runners and result validator."""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = ["is_valid_datetime", "parse_datetime"]

_HOUR = r"(?:[01][0-9]|2[0-3])"
_MINUTE_OR_SECOND = r"[0-5][0-9]"
_BENCHMARK_DATETIME = re.compile(
    rf"""
    (?P<date>[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})
    (?:
        (?P<time>[T ]{_HOUR}:{_MINUTE_OR_SECOND}:{_MINUTE_OR_SECOND})
        (?:\.(?P<fraction>[0-9]+))?
        (?P<offset>Z|[+-]{_HOUR}(?::?{_MINUTE_OR_SECOND})?)?
    )?
    """,
    re.VERBOSE,
)


def _normalize_offset(value: str | None) -> str:
    """Return an offset in the colon form Python 3.10 accepts."""
    if value is None:
        return ""
    if value == "Z":
        return "+00:00"

    digits = value[1:].replace(":", "")
    return f"{value[0]}{digits[:2]}:{digits[2:] or '00'}"


def _fromisoformat_value(value: str) -> str:
    """Validate and normalize a benchmark cutoff for supported Python versions."""
    match = _BENCHMARK_DATETIME.fullmatch(value)
    if match is None:
        message = f"Invalid benchmark datetime: {value!r}"
        raise ValueError(message)

    date = match.group("date")
    time = match.group("time")
    if time is None:
        return date

    fraction = match.group("fraction")
    normalized_fraction = "" if fraction is None else "." + fraction[:6].ljust(6, "0")
    offset = _normalize_offset(match.group("offset"))
    return f"{date}{time}{normalized_fraction}{offset}"


def parse_datetime(value: str) -> datetime:
    """Parse a benchmark cutoff, treating a missing offset as UTC."""
    parsed = datetime.fromisoformat(_fromisoformat_value(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_valid_datetime(value: object) -> bool:
    """Return whether ``value`` is a supported benchmark cutoff."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parse_datetime(value)
    except ValueError:
        return False
    return True
