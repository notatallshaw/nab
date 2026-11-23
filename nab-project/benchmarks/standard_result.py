"""Validate the standard benchmark results used for resuming and comparing runs."""

from __future__ import annotations

from typing import TypedDict, TypeGuard

from benchmark_json import valid_wall_time
from packaging.utils import is_normalized_name
from packaging.version import Version

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
        and valid_wall_time(elapsed)
        and stats.get("yanked_proxy_rounds", 0) <= stats["rounds"]
    )
