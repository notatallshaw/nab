"""Read benchmark reports and validate the timing values they can record."""

from __future__ import annotations

import json
import math
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        msg = f"benchmark report contains a non-finite number: {text}"
        raise ValueError(msg)
    return number


def read_benchmark_json(path: Path) -> object:
    """Read a report with finite JSON numbers, including exponent notation."""
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=_finite_float,
            parse_constant=_finite_float,
        )
    except RecursionError as exc:
        msg = "benchmark report is nested too deeply to read"
        raise ValueError(msg) from exc


def valid_wall_time(value: object) -> bool:
    """Check that an elapsed time fits a finite nonnegative float."""
    return (
        type(value) is int or type(value) is float
    ) and 0 <= value <= sys.float_info.max
