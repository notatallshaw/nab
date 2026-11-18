"""Statistics recorded by the standard benchmark runner and read by its comparator."""

from __future__ import annotations

import math

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


def valid_statistics(stats: object, packages: int) -> bool:
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
        and math.isfinite(elapsed)
        and elapsed >= 0
    )
