"""Tests for the shared ISO 8601 datetime parser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nab_python import _iso8601
from nab_python._iso8601 import _to_isoformat, parse_iso_datetime

# PEP 700 serves upload-time as an ISO 8601 UTC timestamp, so an index may use
# any fractional-seconds width from 0 through 6 digits.
PEP_700_WIDTHS = [
    ("2024-01-15T12:30:45Z", 0),
    ("2024-01-15T12:30:45.1Z", 100000),
    ("2024-01-15T12:30:45.12Z", 120000),
    ("2024-01-15T12:30:45.123Z", 123000),
    ("2024-01-15T12:30:45.1234Z", 123400),
    ("2024-01-15T12:30:45.12345Z", 123450),
    ("2024-01-15T12:30:45.123456Z", 123456),
]


@pytest.mark.parametrize(("raw", "microsecond"), PEP_700_WIDTHS)
def test_parses_every_pep700_fraction_width(raw: str, microsecond: int) -> None:
    assert parse_iso_datetime(raw) == datetime(
        2024, 1, 15, 12, 30, 45, microsecond, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("raw", [raw for raw, _ in PEP_700_WIDTHS])
def test_normal_form_is_what_isoformat_emits(raw: str) -> None:
    """Python 3.10 reads back only what ``isoformat`` writes, so pin that shape."""
    iso = _to_isoformat(raw)
    assert datetime.fromisoformat(iso).isoformat() == iso


def test_offset_is_preserved() -> None:
    parsed = parse_iso_datetime("2024-01-15T12:30:45.5+05:30")
    assert parsed.utcoffset() == timedelta(hours=5, minutes=30)
    assert parsed.astimezone(timezone.utc) == datetime(
        2024, 1, 15, 7, 0, 45, 500000, tzinfo=timezone.utc
    )


def test_missing_offset_parses_naive() -> None:
    """A missing offset is left for the caller to reject or coerce."""
    parsed = parse_iso_datetime("2024-01-15T12:30:45.5")
    assert parsed.tzinfo is None
    assert parsed.isoformat() == "2024-01-15T12:30:45.500000"


def test_sub_microsecond_digits_truncated() -> None:
    assert parse_iso_datetime("2024-01-15T12:30:45.1234567Z") == datetime(
        2024, 1, 15, 12, 30, 45, 123456, tzinfo=timezone.utc
    )


def test_fraction_with_no_digits_raises_value_error() -> None:
    """A trailing dot is not a fraction, so it must not be padded into one."""
    with pytest.raises(ValueError, match="2024-01-15T12:30:45."):
        parse_iso_datetime("2024-01-15T12:30:45.")


def test_offset_fraction_is_widened_too() -> None:
    """A fractional offset also has to reach ``fromisoformat`` in normal form."""
    iso = _to_isoformat("2024-01-15T12:30:45.5+05:30:00.5")
    assert datetime.fromisoformat(iso).isoformat() == iso


def test_non_datetime_raises_value_error() -> None:
    with pytest.raises(ValueError, match="not-a-date"):
        parse_iso_datetime("not-a-date")


# Shapes an index could serve, plus the malformed ones the parser has to refuse.
PARSE_INPUTS = [
    *[raw for raw, _ in PEP_700_WIDTHS],
    "2024-01-15T12:30:45.1234567Z",
    "2024-01-15T12:30:45.123456789Z",
    "2024-01-15T12:30:45,123Z",
    "2024-01-15T12:30:45",
    "2024-01-15T12:30:45.5+05:30",
    "2024-01-15T12:30:45.5+05:30:00.5",
    "2024-01-15T12:30:45-00:00",
    "20240115T123045Z",
    "2024-01-15",
    "2024-01-15T12:30:45.",
    "2024-01-15T25:00:00Z",
    "not-a-date",
    "",
]


def _parse_or_error(raw: str) -> object:
    try:
        return parse_iso_datetime(raw)
    except ValueError:
        return ValueError


@pytest.mark.parametrize("raw", PARSE_INPUTS)
def test_native_path_agrees_with_compatibility_path(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handing ``raw`` straight to ``fromisoformat`` must not change the verdict."""
    monkeypatch.setattr(_iso8601, "_NATIVE_ACCEPTS_PEP_700", False)
    compatibility = _parse_or_error(raw)

    monkeypatch.setattr(_iso8601, "_NATIVE_ACCEPTS_PEP_700", True)
    native = _parse_or_error(raw)

    assert native == compatibility
