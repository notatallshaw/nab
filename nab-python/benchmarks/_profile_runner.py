r"""Single-scenario profiling runner.

Usage:
    # sampling profiler (requires .venv-3.15)
    .venv-3.15/bin/python -m profiling.sampling run -r 5khz --flamegraph \
        -o profile.html nab-python/benchmarks/_profile_runner.py <scenario>

    # cProfile (any venv)
    python nab-python/benchmarks/_profile_runner.py <scenario> --cprofile

<scenario> is a bare name (first TOML match wins) or ``toml_stem:name``,
e.g. ``pip-lowest:cburroughs-v3``.
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

sys.path.insert(0, str(Path(__file__).parent))

import scenarios as sc


def find_scenario(spec: str) -> tuple[str, dict]:
    if ":" in spec:
        toml_stem, name = spec.split(":", 1)
        toml_path = sc.SCENARIOS_DIR / f"{toml_stem}.toml"
        try:
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            sys.exit(f"no TOML file for stem {toml_stem!r}")
        if name not in data:
            sys.exit(f"scenario {name!r} not found in {toml_stem}.toml")
        return name, data[name]
    for path in sorted(sc.SCENARIOS_DIR.glob("*.toml")):
        with path.open("rb") as f:
            data = tomllib.load(f)
        if spec in data:
            return spec, data[spec]
    sys.exit(f"scenario {spec!r} not found")


def build_inputs(name: str, scenario: dict) -> dict:
    python_version = scenario["python_version"]
    requirement_strings = list(scenario["requirements"])
    constraint_strings = scenario.get("constraints", [])
    marker_environment = sc.parse_marker_environment(name, scenario)
    build_policy_overrides = sc.parse_build_packages(name, scenario)

    # BUILD_REMOTE + marker_environment overlay is unsupported; drop overrides.
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

    marker_env = sc.scenario_marker_env(python_version, marker_environment)
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
        "indexes": sc.parse_indexes(name, scenario),
        "index_routes": sc.parse_index_routes(name, scenario) or None,
        "build_policy_overrides": build_policy_overrides or None,
        "resolution_strategy": resolution_strategy,
    }


def report(name: str, data: dict) -> None:
    stats = data["stats"]
    state = "OK" if data["result"]["success"] else f"FAIL {data['result']['error']}"
    print(
        f"{name}: {state} in {stats['wall_time_seconds']}s, "
        f"{stats['decisions']} decisions, {stats['conflicts']} conflicts"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", help="bare name or toml_stem:name")
    parser.add_argument(
        "--cprofile",
        action="store_true",
        help="profile in-process and print the top functions by self time",
    )
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    name, scenario = find_scenario(args.scenario)
    inputs = build_inputs(name, scenario)

    if not args.cprofile:
        report(name, sc.resolve_scenario(**inputs))
        return

    profiler = cProfile.Profile()
    data = profiler.runcall(sc.resolve_scenario, **inputs)
    report(name, data)
    pstats.Stats(profiler).sort_stats("tottime").print_stats(args.limit)


if __name__ == "__main__":
    main()
