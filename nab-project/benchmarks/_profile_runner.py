r"""Single-scenario profiling runner.

Usage:
    # sampling profiler (requires .venv-3.15)
    .venv-3.15/bin/python -m profiling.sampling run -r 5khz --flamegraph \
        -o profile.html nab-project/benchmarks/_profile_runner.py <scenario>

    # cProfile (any venv)
    python nab-project/benchmarks/_profile_runner.py <scenario> --cprofile

<scenario> is a bare name (first TOML match wins) or ``toml_stem:name``,
e.g. ``pip:cburroughs-v3 --resolution lowest``.
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
from benchmark_config import (
    build_benchmark_config,
    parse_scenario_build_packages,
    parse_scenario_index_routes,
    parse_scenario_indexes,
    parse_scenario_project_metadata,
    parse_scenario_requirement_strings,
    parse_scenario_vcs_config,
    parse_trust_unverified_sdist_deps,
    validate_scenario_build_policy,
    validate_scenario_settings,
)


def find_scenario(spec: str) -> tuple[str, dict]:
    if ":" in spec:
        toml_stem, name = spec.split(":", 1)
        for suffix, resolution in (
            ("-lowest-direct", "lowest-direct"),
            ("-lowest", "lowest"),
        ):
            if toml_stem.endswith(suffix):
                canonical = toml_stem.removesuffix(suffix)
                sys.exit(
                    f"strategy-clone selector {spec!r} was retired; use "
                    f"{canonical}:{name} --resolution {resolution}"
                )
        toml_path = sc.SCENARIOS_DIR / f"{toml_stem}.toml"
        try:
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            sys.exit(f"no TOML file for stem {toml_stem!r}")
        if name not in data:
            sys.exit(f"scenario {name!r} not found in {toml_stem}.toml")
        return name, data[name]
    try:
        paths = sc.standard_scenario_files()
    except ValueError as exc:
        sys.exit(str(exc))
    for path in paths:
        with path.open("rb") as f:
            data = tomllib.load(f)
        if spec in data:
            return spec, data[spec]
    sys.exit(f"scenario {spec!r} not found")


def build_inputs(
    name: str,
    scenario: dict,
    resolution_override: sc.ResolutionStrategy | None = None,
    host: sc.BenchmarkHost | None = None,
) -> dict:
    """Validate a scenario and build resolver inputs for one applicable host.

    Returns requirements, constraints, config, target, and the effective host.
    """
    validate_scenario_settings(name, scenario)
    trust_unverified_sdist_deps = parse_trust_unverified_sdist_deps(name, scenario)
    requirement_inputs = parse_scenario_requirement_strings(name, scenario)
    vcs_config = parse_scenario_vcs_config(name, scenario)
    project_metadata = parse_scenario_project_metadata(name, scenario)
    indexes = parse_scenario_indexes(name, scenario)
    index_routes = parse_scenario_index_routes(name, scenario, indexes)
    python_version = scenario["python_version"]
    requirement_strings = requirement_inputs.requirements
    constraint_strings = requirement_inputs.constraints
    marker_environment = sc.parse_marker_environment(name, scenario)
    build_policy_overrides = parse_scenario_build_packages(name, scenario)
    validate_scenario_build_policy(
        name,
        marker_environment,
        build_policy_overrides,
    )
    declared_resolution = sc.ResolutionStrategy(
        scenario.get("resolution", sc.ResolutionStrategy.HIGHEST.value)
    )
    resolution_strategy = resolution_override or declared_resolution
    requires_matching_host = sc.parse_requires_matching_host(
        name,
        scenario,
        marker_environment,
    )
    effective_host = host or sc.BenchmarkHost.current(sc.SCENARIO_WALL_TIMEOUT_SECONDS)
    admission = effective_host.target_for(
        python_version,
        marker_environment,
        requires_matching_host=requires_matching_host,
    )
    if admission.target is None:
        msg = f"{name}: benchmark is inapplicable: {admission.inapplicable_reason}"
        raise SystemExit(msg)
    target = admission.target

    if project_metadata.project_name:
        requirement_strings += sc.expand_project_extras(
            project_metadata.project_name,
            project_metadata.project_extras,
            project_metadata.optional_dependencies,
        )

    marker_env = dict(target.marker_env)
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
    config = build_benchmark_config(
        uploaded_prior_to=sc.parse_datetime(datetime_str) if datetime_str else None,
        indexes=indexes,
        index_routes=index_routes,
        build_policy_overrides=build_policy_overrides,
        resolution=resolution_strategy,
        trust_unverified_sdist_deps=trust_unverified_sdist_deps,
        vcs=vcs_config,
    )
    return {
        "requirements": requirements,
        "constraints": constraints,
        "config": config,
        "target": target,
        "host": effective_host,
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
    parser.add_argument(
        "--resolution",
        choices=[strategy.value for strategy in sc.ResolutionStrategy],
        default=None,
        help="Explicit resolution strategy (default: scenario setting or highest)",
    )
    args = parser.parse_args()

    name, scenario = find_scenario(args.scenario)
    resolution_override = (
        sc.ResolutionStrategy(args.resolution) if args.resolution is not None else None
    )
    inputs = build_inputs(name, scenario, resolution_override, host=None)

    if not args.cprofile:
        report(name, sc.resolve_scenario(**inputs))
        return

    profiler = cProfile.Profile()
    data = profiler.runcall(sc.resolve_scenario, **inputs)
    report(name, data)
    pstats.Stats(profiler).sort_stats("tottime").print_stats(args.limit)


if __name__ == "__main__":
    main()
