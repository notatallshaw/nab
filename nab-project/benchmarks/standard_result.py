"""Validate the standard benchmark results used for resuming and comparing runs."""

from __future__ import annotations

import json
import math
import sys
from typing import TYPE_CHECKING, TypedDict, TypeGuard

from nab_provider._vendor.packaging.utils import is_normalized_name
from nab_provider._vendor.packaging.version import Version

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


STAT_LABELS = {
    "rounds": "Rounds",
    "yanked_proxy_rounds": "Yanked proxy rounds",
    "decisions": "Decisions",
    "conflicts": "Conflicts",
    "derivations": "Derivations",
    "backjumps": "Backjumps",
    "restarts": "Restarts",
    "incompatibilities_learned": "Incompatibilities learned",
    "listings_fetched": "Listings fetched",
    "metadata_fetched": "Metadata fetched",
    "sdist_pkg_info_fetched": "Sdist PKG-INFO fetched",
    "distributions_seen": "Distributions seen",
    "wheels_seen": "Wheels seen",
    "sdists_seen": "Sdists seen",
    "excluded_by_python": "Excluded by Python",
    "excluded_by_time": "Excluded by upload time",
    "excluded_by_dist_policy": "Excluded by dist policy",
    "excluded_by_build_policy": "Excluded by build policy",
    "sdist_pyproject_fallbacks": "Sdist pyproject.toml fallbacks",
    "get_dependencies_calls": "get_dependencies calls",
    "choose_version_calls": "choose_version calls",
    "prioritize_calls": "prioritize calls",
    "look_ahead_rejections": "Look-ahead rejections",
    "packages_resolved": "Packages resolved",
    "wall_time_seconds": "Wall time (s)",
}

# Schema 4 runs produced before yank admission recorded only the total rounds.
_REQUIRED_FIELDS = frozenset(STAT_LABELS) - {"yanked_proxy_rounds"}
_ALL_FIELDS = frozenset(STAT_LABELS)


class ResolutionOutcome(TypedDict):
    """The verdict, error message and package versions from one solve."""

    success: bool
    error: str | None
    pins: dict[str, str]


class StandardResult(TypedDict):
    """One validated execution as stored in the results directory."""

    input: dict[str, object]
    result: ResolutionOutcome
    stats: dict[str, int | float]


def _valid_pins(value: object) -> TypeGuard[dict[str, str]]:
    if not isinstance(value, dict):
        return False
    try:
        return all(
            type(name) is str
            and is_normalized_name(name)
            and type(version) is str
            and str(Version(version)) == version
            for name, version in value.items()
        )
    except ValueError:
        return False


def valid_outcome(value: object) -> TypeGuard[ResolutionOutcome]:
    """Check that the verdict, error message and selected versions agree."""
    if not isinstance(value, dict) or set(value) != {"success", "error", "pins"}:
        return False
    success, error, pins = value["success"], value["error"], value["pins"]
    if type(success) is not bool or not _valid_pins(pins):
        return False
    if success:
        return error is None
    return isinstance(error, str) and bool(error) and not pins


def valid_statistics(stats: object, packages: int) -> TypeGuard[dict[str, int | float]]:
    """Validate counters and timing against the number of returned pins."""
    if not isinstance(stats, dict) or stats.keys() not in (
        _REQUIRED_FIELDS,
        _ALL_FIELDS,
    ):
        return False

    for name, value in stats.items():
        if name != "wall_time_seconds" and (type(value) is not int or value < 0):
            return False

    elapsed = stats["wall_time_seconds"]
    return (
        stats["packages_resolved"] == packages
        and type(elapsed) in (int, float)
        and 0 <= elapsed <= sys.float_info.max
        and stats.get("yanked_proxy_rounds", 0) <= stats["rounds"]
    )
