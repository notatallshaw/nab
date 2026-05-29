"""Targeted A/B for the materialize-before-order spike.

Runs named scenarios from named TOML files (so the per-scenario
``resolution`` strategy is honoured, unlike canary.py) under the
NAB_MATERIALIZE_BEFORE_ORDER flag off vs on.

Usage:
    python nab-python/benchmarks/materialize_ab.py \
        --target rip-lowest:rip-apache-airflow-all-pinned \
        --target uv-lowest:uv-issue-3078-airflow-2_8_4 \
        --runs 2
"""

from __future__ import annotations

import argparse
import os
import statistics

import scenarios as sc
import tomllib

FLAG = "NAB_MATERIALIZE_BEFORE_ORDER"


def load_scenario(toml_stem: str, name: str) -> dict:
    path = sc.SCENARIOS_DIR / f"{toml_stem}.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)
    return data[name]


def build_inputs(name: str, scenario: dict) -> dict:
    python_version = scenario["python_version"]
    requirement_strings = list(scenario["requirements"])
    constraint_strings = scenario.get("constraints", [])
    marker_environment = sc.parse_marker_environment(name, scenario)
    indexes = sc.parse_indexes(name, scenario)
    index_overrides = sc.parse_index_overrides(name, scenario)
    build_policy_overrides = sc.parse_build_packages(name, scenario)
    if marker_environment and build_policy_overrides:
        build_policy_overrides = {}
    resolution_strategy = sc.ResolutionStrategy(scenario.get("resolution", "highest"))
    vcs_config = sc.VcsConfig(
        policy=sc.VcsPolicy(scenario.get("vcs_policy", "block")),
        allowed_schemes=frozenset(scenario.get("vcs_allowed_schemes", [])),
        allowed_repos=tuple(scenario.get("vcs_allowed_repos", [])),
        require_pin=scenario.get("vcs_require_pin", True),
    )

    if scenario.get("project_name"):
        requirement_strings += sc.expand_project_extras(
            scenario["project_name"],
            scenario.get("project_extras", []),
            scenario.get("optional_dependencies", {}),
        )

    marker_env = sc._scenario_marker_env(python_version, marker_environment)
    requirements = sc.parse_requirements(
        requirement_strings, vcs_config=vcs_config, marker_environment=marker_env
    )
    constraints = (
        sc.parse_requirements(
            constraint_strings, vcs_config=vcs_config, marker_environment=marker_env
        )
        if constraint_strings
        else None
    )
    datetime_str = scenario.get("datetime")
    return {
        "requirements": requirements,
        "python_version": python_version,
        "uploaded_prior_to": sc.parse_datetime(datetime_str) if datetime_str else None,
        "constraints": constraints,
        "marker_environment": marker_environment or None,
        "indexes": indexes,
        "index_overrides": index_overrides or None,
        "build_policy_overrides": build_policy_overrides or None,
        "resolution_strategy": resolution_strategy,
    }


def run_target(toml_stem: str, name: str, runs: int) -> None:
    inputs = build_inputs(name, load_scenario(toml_stem, name))
    runs_data = [sc.resolve_scenario(**inputs) for _ in range(runs)]
    decisions = [r["stats"]["decisions"] for r in runs_data]
    walls = [r["stats"]["wall_time_seconds"] for r in runs_data]
    successes = sum(1 for r in runs_data if r["result"]["success"])
    print(
        f"  {toml_stem}:{name}  success {successes}/{runs}  "
        f"decisions med {int(statistics.median(decisions))} "
        f"min {min(decisions)} max {max(decisions)}  "
        f"wall med {round(statistics.median(walls), 1)}s"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target", action="append", required=True, help="toml:scenario"
    )
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--materialize", action="store_true")
    args = parser.parse_args()

    if args.materialize:
        os.environ[FLAG] = "1"
    else:
        os.environ.pop(FLAG, None)

    flag_state = "on" if args.materialize else "off"
    print(f"=== materialize_ab, flag={flag_state}, runs={args.runs} ===")
    for target in args.target:
        toml_stem, name = target.split(":", 1)
        run_target(toml_stem, name, args.runs)


if __name__ == "__main__":
    main()
