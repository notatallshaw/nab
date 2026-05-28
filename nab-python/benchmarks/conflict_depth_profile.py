"""Print conflict-depth histograms for a fixed set of high-conflict scenarios.

Reports the learned-clause term-count histogram, backjump-distance histogram,
and threshold-crossing counts recorded by ResolverStats.

Usage:
    python nab-python/benchmarks/conflict_depth_profile.py
"""

from __future__ import annotations

import json
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from scenarios import RESULTS_DIR, SCENARIOS_DIR, process_scenario

LABEL = "conflict_depth_probe"

TARGETS: tuple[tuple[str, str], ...] = (
    ("uv", "uv-issue-10344-airflow-py312"),
    ("rip-lowest", "rip-apache-airflow-all-modern"),
    ("rip-lowest", "rip-apache-airflow-all-pinned"),
    ("pip-lowest", "cburroughs-v3"),
    ("pip-lowest", "apache-airflow-311-full"),
)


def run_one(toml_stem: str, scenario_name: str) -> dict:
    toml_file = SCENARIOS_DIR / f"{toml_stem}.toml"
    with toml_file.open("rb") as f:
        scenarios = tomllib.load(f)
    process_scenario(
        scenario_name, scenarios[scenario_name], LABEL, toml_stem, force=True
    )
    result_path = RESULTS_DIR / LABEL / toml_stem / f"{scenario_name}.json"
    return json.loads(result_path.read_text())


def main() -> None:
    aggregate_clauses: dict[int, int] = {}
    aggregate_backjumps: dict[int, int] = {}
    for toml_stem, scenario_name in TARGETS:
        data = run_one(toml_stem, scenario_name)
        stats = data["stats"]
        ok = data["result"]["success"]
        clauses = {int(k): v for k, v in stats["learned_clause_term_counts"].items()}
        distances = {int(k): v for k, v in stats["backjump_distances"].items()}
        for term_count, freq in clauses.items():
            aggregate_clauses[term_count] = aggregate_clauses.get(term_count, 0) + freq
        for distance, freq in distances.items():
            aggregate_backjumps[distance] = aggregate_backjumps.get(distance, 0) + freq

        print(f"\n=== {scenario_name} ({toml_stem}) {'ok' if ok else 'FAIL'} ===")
        print(
            f"  decisions={stats['decisions']} conflicts={stats['conflicts']} "
            f"backjumps={stats['backjumps']} "
            f"learned={stats['incompatibilities_learned']} "
            f"restarts={stats['restarts']} wall={stats['wall_time_seconds']}s"
        )
        print(
            f"  conflict_threshold_crossings={stats['conflict_threshold_crossings']} "
            f"culprit_threshold_crossings={stats['culprit_threshold_crossings']}"
        )
        print(f"  learned_clause_term_counts={dict(sorted(clauses.items()))}")
        print(f"  backjump_distances={dict(sorted(distances.items()))}")

    total_clauses = sum(aggregate_clauses.values())
    deep_clauses = sum(v for k, v in aggregate_clauses.items() if k > 2)
    total_backjumps = sum(aggregate_backjumps.values())
    far_backjumps = sum(v for k, v in aggregate_backjumps.items() if k > 1)
    print("\n=== aggregate ===")
    print(f"  learned_clause_term_counts={dict(sorted(aggregate_clauses.items()))}")
    print(f"  backjump_distances={dict(sorted(aggregate_backjumps.items()))}")
    if total_clauses:
        print(
            f"  clauses with >2 terms: {deep_clauses}/{total_clauses} "
            f"({100 * deep_clauses / total_clauses:.1f}%)"
        )
    if total_backjumps:
        print(
            f"  backjumps of distance >1: {far_backjumps}/{total_backjumps} "
            f"({100 * far_backjumps / total_backjumps:.1f}%)"
        )


if __name__ == "__main__":
    main()
