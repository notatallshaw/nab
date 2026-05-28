"""Compare benchmark results between two commits.

Usage:
    python nab-python/benchmarks/compare.py <commit1> <commit2>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"

STAT_LABELS = {
    "rounds": "Rounds",
    "decisions": "Decisions",
    "conflicts": "Conflicts",
    "derivations": "Derivations",
    "backjumps": "Backjumps",
    "restarts": "Restarts",
    "incompatibilities_learned": "Incompatibilities learned",
    "conflict_threshold_crossings": "Conflict threshold crossings",
    "culprit_threshold_crossings": "Culprit threshold crossings",
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
    # legacy keys (older results before stats rework):
    "listing_requests": "Listing requests (legacy)",
    "metadata_requests": "Metadata requests (legacy)",
}


def percent_change(old: float, new: float) -> str:
    if old == 0:
        return "inf%" if new != 0 else "0%"
    return f"{(new * 100) / old:.1f}%"


def compare_scenario(path1: Path, path2: Path, label: str) -> None:
    """Compare two result JSONs and print differences."""
    data1 = json.loads(path1.read_text())
    data2 = json.loads(path2.read_text())

    success1 = data1["result"]["success"]
    success2 = data2["result"]["success"]
    stats1 = data1["stats"]
    stats2 = data2["stats"]

    messages: list[str] = []

    if success1 != success2:
        messages.append(f"Success: {success1} -> {success2}")

    err1 = data1["result"]["error"]
    err2 = data2["result"]["error"]
    if err1 != err2:
        if err1 and err2:
            messages.append(f"Error changed: {err1} -> {err2}")
        elif err1:
            messages.append(f"Error resolved: {err1}")
        else:
            messages.append(f"New error: {err2}")

    for key, label_text in STAT_LABELS.items():
        v1 = stats1.get(key, 0)
        v2 = stats2.get(key, 0)
        if v1 != v2:
            messages.append(f"{label_text}: {v1} -> {v2} ({percent_change(v1, v2)})")

    if messages:
        print(f"{label}:")
        for msg in messages:
            print(f"\t{msg}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare nab benchmark results between commits"
    )
    parser.add_argument("commit1", help="First commit label (baseline)")
    parser.add_argument("commit2", help="Second commit label (comparison)")
    args = parser.parse_args()

    dir1 = RESULTS_DIR / args.commit1
    dir2 = RESULTS_DIR / args.commit2

    if not dir1.is_dir():
        print(f"No results for commit {args.commit1}")
        sys.exit(1)
    if not dir2.is_dir():
        print(f"No results for commit {args.commit2}")
        sys.exit(1)

    any_diff = False
    for toml_dir in sorted(dir1.iterdir()):
        if not toml_dir.is_dir():
            continue
        other_toml_dir = dir2 / toml_dir.name
        if not other_toml_dir.is_dir():
            continue

        for result_file in sorted(toml_dir.glob("*.json")):
            other_file = other_toml_dir / result_file.name
            if not other_file.exists():
                continue

            scenario_name = result_file.stem
            label = f"{toml_dir.name} / {scenario_name}"
            compare_scenario(result_file, other_file, label)
            any_diff = True

    if not any_diff:
        print("No matching scenarios found between the two commits.")


if __name__ == "__main__":
    main()
