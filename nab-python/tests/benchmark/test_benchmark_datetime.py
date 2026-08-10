"""Tests for benchmark datetime parsing."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

# The runner scripts loaded here import their siblings by bare name.
pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.usefixtures("benchmark_import_path"),
]

_BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"
_FRACTION_WIDTHS = [
    ("", 0),
    (".1", 100_000),
    (".12", 120_000),
    (".123", 123_000),
    (".1234", 123_400),
    (".12345", 123_450),
    (".123456", 123_456),
    (".1234567", 123_456),
    (".123456789", 123_456),
]
_OFFSET_CASES = [
    ("+05:30", timedelta(hours=5, minutes=30)),
    ("-05:30", -timedelta(hours=5, minutes=30)),
    ("+0000", timedelta()),
    ("+00", timedelta()),
    ("-0530", -timedelta(hours=5, minutes=30)),
    ("-05", -timedelta(hours=5)),
]
_UNSUPPORTED_VALUES = [
    "2025-W22-7T00:00:00Z",
    "20250601T000000Z",
    "2025-06-01T00:00:00,1Z",
    "2025-06-01T00:00Z",
    "2025-06-01t00:00:00Z",
    "2025-06-01T00:00:00.",
    "2025-06-01T00:00:00+000",
    "2025-06-01Z",
    "2025-06-01T00:00:00Z trailing",
]
_TIME_SUFFIXES = ["", "Z", "+12", "+1230", "+12:30", "-12", "-1230", "-12:30"]
_OUT_OF_RANGE_VALUES = [
    "2025-06-01T23:60:00Z",
    "2025-06-01T23:59:60Z",
    "2025-06-01T23:59:59+24",
    "2025-06-01T23:59:59+2400",
    "2025-06-01T23:59:59+24:00",
    "2025-06-01T23:59:59-24",
    "2025-06-01T23:59:59+1260",
    "2025-06-01T23:59:59+12:60",
    "2025-06-01T23:59:59-12:60",
]


def _load_harness(name: str) -> ModuleType:
    """Load one benchmark script by path, with its sibling imports available."""
    spec = importlib.util.spec_from_file_location(
        f"_benchmark_datetime_{name}", _BENCHMARKS / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_rejected(module: ModuleType, value: str) -> None:
    with pytest.raises(ValueError, match="Invalid benchmark datetime"):
        module.parse_datetime(value)
    assert module.is_valid_datetime(value) is False


@pytest.mark.parametrize("harness", ["scenarios", "canary", "universal_scenarios"])
def test_runners_accept_utc_suffix(harness: str) -> None:
    module = _load_harness(harness)

    assert module.parse_datetime("2025-06-01T00:00:00.1Z") == datetime(
        2025, 6, 1, microsecond=100_000, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("suffix", ["", "Z", "+00:00"])
@pytest.mark.parametrize(("fraction", "microsecond"), _FRACTION_WIDTHS)
def test_accepts_fraction_widths(
    suffix: str,
    fraction: str,
    microsecond: int,
) -> None:
    module = _load_harness("benchmark_datetime")

    assert module.parse_datetime(f"2025-06-01T00:00:00{fraction}{suffix}") == datetime(
        2025, 6, 1, microsecond=microsecond, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(("offset_text", "offset"), _OFFSET_CASES)
def test_accepts_colon_and_basic_offsets(
    offset_text: str,
    offset: timedelta,
) -> None:
    module = _load_harness("benchmark_datetime")

    timestamp = f"2025-06-01T00:00:00.123456789{offset_text}"
    assert module.parse_datetime(timestamp) == datetime(
        2025, 6, 1, microsecond=123_456, tzinfo=timezone(offset)
    )


def test_bare_date_is_not_rewritten() -> None:
    module = _load_harness("benchmark_datetime")

    assert module._fromisoformat_value("2025-06-01") == "2025-06-01"
    assert module.parse_datetime("2025-06-01") == datetime(
        2025, 6, 1, tzinfo=timezone.utc
    )


def test_accepts_space_separator() -> None:
    module = _load_harness("benchmark_datetime")

    assert module.parse_datetime("2025-06-01 00:00:00") == datetime(
        2025, 6, 1, tzinfo=timezone.utc
    )


def test_accepts_upper_range_boundaries() -> None:
    module = _load_harness("benchmark_datetime")

    assert module.parse_datetime("2025-06-01T23:59:59.9+23:59") == datetime(
        2025,
        6,
        1,
        23,
        59,
        59,
        900_000,
        tzinfo=timezone(timedelta(hours=23, minutes=59)),
    )


@pytest.mark.parametrize("suffix", _TIME_SUFFIXES)
@pytest.mark.parametrize("fraction", ["", ".1"])
def test_rejects_24_hour_for_each_supported_form(
    suffix: str,
    fraction: str,
) -> None:
    module = _load_harness("benchmark_datetime")

    _assert_rejected(module, f"2025-06-01T24:00:00{fraction}{suffix}")


@pytest.mark.parametrize("value", _OUT_OF_RANGE_VALUES)
def test_rejects_out_of_range_fields(value: str) -> None:
    module = _load_harness("benchmark_datetime")

    _assert_rejected(module, value)


@pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
def test_rejects_unsupported_forms(value: str) -> None:
    module = _load_harness("benchmark_datetime")

    _assert_rejected(module, value)


@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_validator_rejects_invalid_values(value: object) -> None:
    module = _load_harness("benchmark_datetime")

    assert module.is_valid_datetime(value) is False
