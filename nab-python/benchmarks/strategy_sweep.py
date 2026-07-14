"""Sweep all benchmark scenarios under each ResolutionStrategy.

For every scenario in ``benchmarks/scenarios/*.toml`` (excluding
``universal.toml``), resolve under HIGHEST, LOWEST, and LOWEST_DIRECT.
Reports any divergence: a strategy that fails where another succeeds,
a backtracking explosion (>=10x more decisions than HIGHEST), or a
wall-time blow-up (>=10x slower than HIGHEST).

Usage:
    python nab-python/benchmarks/strategy_sweep.py [--toml NAME]
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import PackageOverride
from nab_python.fetch import FetchCoordinator
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    ExtrasMode,
    Provider,
    ResolutionStrategy,
    split_extra,
)
from nab_resolver.resolver import Resolver

# Reuse the existing scenarios.py helpers so this stays in lockstep
# with the canonical runner.  ``scenarios.py`` lives next to this
# file, so an in-package import works under both ``python -m`` and
# direct invocation.
_BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BENCH_DIR))

import scenarios as _scenarios  # noqa: E402

SCENARIOS_DIR = _BENCH_DIR / "scenarios"
CACHE_DIR = _BENCH_DIR / "cache"
RESULTS_DIR = _BENCH_DIR / "strategy_sweep_results"

WALL_TIMEOUT_S = 60
MAX_ITERATIONS = 50_000


class _Timeout(BaseException):
    """Raised when a scenario exceeds the per-run wall-clock budget.

    Subclasses BaseException so the resolver's internal ``except Exception``
    handlers cannot swallow the alarm mid-resolve.
    """


def _on_alarm(_signum: int, _frame: object) -> None:
    raise _Timeout


def _resolve_one(  # noqa: PLR0913 - one kwarg per knob; bundling hides the surface
    requirements: dict[str, VersionRange],
    constraints: dict[str, VersionRange] | None,
    *,
    python_version: str,
    marker_environment: dict[str, str] | None,
    indexes: list,
    index_routes: list | None,
    uploaded_prior_to: datetime | None,
    strategy: ResolutionStrategy,
    direct_packages: frozenset[str],
    extras_mode: ExtrasMode,
    build_policy_overrides: Mapping[str, BuildPolicy] | None,
) -> dict:
    package_overrides = tuple(
        PackageOverride(
            requirement=Requirement(name),
            name=canonicalize_name(name),
            version_range=VersionRange.full(),
            build_policy=policy,
        )
        for name, policy in (build_policy_overrides or {}).items()
    )
    with FetchCoordinator(
        HttpxAsyncTransport(),
        indexes=indexes,
        cache_dir=CACHE_DIR,
        index_routes=index_routes,
    ) as coordinator:
        provider = Provider(
            coordinator,
            target=_scenarios.resolve_target(python_version, marker_environment),
            root_requirements=requirements,
            uploaded_prior_to=uploaded_prior_to,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            package_overrides=package_overrides,
            resolution_strategy=strategy,
            direct_packages=direct_packages,
            extras_mode=extras_mode,
        )
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            max_iterations=MAX_ITERATIONS,
        )
        prior = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(WALL_TIMEOUT_S)
        start = time.monotonic()
        success = False
        error: str | None = None
        packages = 0
        try:
            raw = resolver.resolve(requirements, constraints=constraints)
            elapsed = time.monotonic() - start
            packages = sum(1 for k in raw if split_extra(k)[1] is None)
            success = True
        except _Timeout:
            elapsed = time.monotonic() - start
            error = f"Timeout: exceeded {WALL_TIMEOUT_S}s"
        except Exception as exc:
            elapsed = time.monotonic() - start
            error = f"{type(exc).__name__}: {exc}"
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prior)
        rstats = resolver.stats
        return {
            "strategy": strategy.value,
            "success": success,
            "error": error,
            "packages": packages,
            "decisions": rstats.decisions,
            "conflicts": rstats.conflicts,
            "rounds": rstats.rounds,
            "backjumps": rstats.backjumps,
            "wall_time": round(elapsed, 3),
        }


def _direct_set(requirements: dict[str, VersionRange]) -> frozenset[str]:
    """Strategy decision is keyed off base canonical names only."""
    return frozenset(name for name in requirements if split_extra(name)[1] is None)


def _scenario_inputs(scenario: dict) -> dict | None:
    """Convert one TOML scenario into the kwargs ``_resolve_one`` wants.

    Returns ``None`` when the scenario is marked unsupported (the
    canonical runner skips those too).
    """
    if "unsupported_reason" in scenario:
        return None

    python_version: str = scenario["python_version"]
    requirement_strings: list[str] = list(scenario["requirements"])
    constraint_strings: list[str] = scenario.get("constraints", [])
    marker_env = _scenarios.parse_marker_environment("sweep", scenario)
    indexes = _scenarios.parse_indexes("sweep", scenario)
    index_routes = _scenarios.parse_index_routes("sweep", scenario)
    datetime_str = scenario.get("datetime")
    project_name = scenario.get("project_name")
    project_extras = scenario.get("project_extras", [])
    optional_dependencies = scenario.get("optional_dependencies", {})
    vcs_policy_str = scenario.get("vcs_policy", "block")
    from nab_python.provider import VcsConfig, VcsPolicy  # noqa: PLC0415

    vcs_config = VcsConfig(
        policy=VcsPolicy(vcs_policy_str),
        allowed_schemes=frozenset(scenario.get("vcs_allowed_schemes", [])),
        allowed_repos=tuple(scenario.get("vcs_allowed_repos", [])),
        require_pin=scenario.get("vcs_require_pin", True),
    )
    if project_name:
        requirement_strings = [
            *requirement_strings,
            *_scenarios.expand_project_extras(
                project_name, project_extras, optional_dependencies
            ),
        ]
    uploaded_prior_to = (
        _scenarios.parse_datetime(datetime_str) if datetime_str else None
    )
    requirement_marker_env = _scenarios.scenario_marker_env(python_version, marker_env)
    requirements = _scenarios.parse_requirements(
        requirement_strings,
        vcs_config=vcs_config,
        marker_environment=requirement_marker_env,
    )
    constraints = (
        _scenarios.parse_requirements(
            constraint_strings,
            vcs_config=vcs_config,
            marker_environment=requirement_marker_env,
        )
        if constraint_strings
        else None
    )
    build_policy_overrides = _scenarios.parse_build_packages("sweep", scenario)
    if marker_env and build_policy_overrides:
        # BUILD_REMOTE + marker_environment is rejected at provider construction.
        build_policy_overrides = []
    return {
        "requirements": requirements,
        "constraints": constraints,
        "python_version": python_version,
        "marker_environment": marker_env or None,
        "indexes": indexes,
        "index_routes": index_routes or None,
        "uploaded_prior_to": uploaded_prior_to,
        "direct_packages": _direct_set(requirements),
        "build_policy_overrides": build_policy_overrides or None,
    }


def _classify(scenario_results: list[dict]) -> list[str]:
    """Flag divergences/explosions across the three strategy results."""
    by_strat = {r["strategy"]: r for r in scenario_results}
    flags: list[str] = []
    successes = {s: r["success"] for s, r in by_strat.items()}
    if len(set(successes.values())) > 1:
        ok = sorted(s for s, v in successes.items() if v)
        bad = sorted(s for s, v in successes.items() if not v)
        flags.append(f"DIVERGE: ok={ok}, fail={bad}")

    base = by_strat.get("highest", {})
    if base.get("success") and base.get("decisions", 0) > 0:
        for strat in ("lowest", "lowest-direct"):
            other = by_strat.get(strat, {})
            if not other.get("success"):
                continue
            if other["decisions"] >= 10 * base["decisions"] and other["decisions"] > 50:
                flags.append(
                    f"EXPLOSION[{strat}]:"
                    f" {other['decisions']} decisions vs {base['decisions']} (highest)"
                )
            if other["wall_time"] >= 10 * max(base["wall_time"], 0.5):
                flags.append(
                    f"SLOW[{strat}]:"
                    f" {other['wall_time']}s vs {base['wall_time']}s (highest)"
                )
    return flags


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--toml",
        action="append",
        help="Restrict to specific toml file(s) (without .toml extension)."
        " May be repeated.",
    )
    parser.add_argument(
        "--extras-mode",
        choices=[m.value for m in ExtrasMode],
        default=ExtrasMode.ERROR_USER.value,
        help="ExtrasMode for the provider (default: error_user)."
        " warn drops missing extras with a warning (uv-style); error_user"
        " errors for user-provided extras; backtrack searches for a"
        " version that provides the extra.",
    )
    parser.add_argument(
        "--results-dir",
        default=None,
        help="Override the output directory (default: strategy_sweep_results).",
    )
    args = parser.parse_args()
    extras_mode = ExtrasMode(args.extras_mode)
    results_dir = Path(args.results_dir) if args.results_dir else RESULTS_DIR

    if not SCENARIOS_DIR.is_dir():
        sys.stderr.write(f"Error: {SCENARIOS_DIR} does not exist\n")
        sys.exit(1)

    toml_files = sorted(
        f for f in SCENARIOS_DIR.glob("*.toml") if f.stem != "universal"
    )
    if args.toml:
        wanted = set(args.toml)
        toml_files = [f for f in toml_files if f.stem in wanted]
    if not toml_files:
        sys.stderr.write("No matching scenario files\n")
        sys.exit(1)

    results_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    flagged: list[tuple[str, str, list[str]]] = []
    counts = {
        "highest": {"ok": 0, "fail": 0, "timeout": 0},
        "lowest": {"ok": 0, "fail": 0, "timeout": 0},
        "lowest-direct": {"ok": 0, "fail": 0, "timeout": 0},
    }

    for toml_file in toml_files:
        with toml_file.open("rb") as f:
            scenarios = tomllib.load(f)
        print(f"\n--- {toml_file.stem} ({len(scenarios)} scenarios) ---")
        for scenario_name, scenario in scenarios.items():
            inputs = _scenario_inputs(scenario)
            if inputs is None:
                continue

            print(f"  {scenario_name} ", end="", flush=True)
            results: list[dict] = []
            for strategy in (
                ResolutionStrategy.HIGHEST,
                ResolutionStrategy.LOWEST,
                ResolutionStrategy.LOWEST_DIRECT,
            ):
                r = _resolve_one(strategy=strategy, extras_mode=extras_mode, **inputs)
                results.append(r)
                bucket = counts[strategy.value]
                if r["success"]:
                    bucket["ok"] += 1
                elif r["error"] and r["error"].startswith("Timeout"):
                    bucket["timeout"] += 1
                else:
                    bucket["fail"] += 1

            flags = _classify(results)
            tag = " | ".join(flags) if flags else "consistent"
            print(tag)
            if flags:
                flagged.append((toml_file.stem, scenario_name, flags))

            summary.append(
                {
                    "toml": toml_file.stem,
                    "scenario": scenario_name,
                    "results": results,
                    "flags": flags,
                }
            )

    out_path = results_dir / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2) + "\n")

    print("\n=== Outcome counts ===")
    for strat, bucket in counts.items():
        total = bucket["ok"] + bucket["fail"] + bucket["timeout"]
        print(
            f"  {strat:<14} ok={bucket['ok']} fail={bucket['fail']}"
            f" timeout={bucket['timeout']} total={total}"
        )

    print(f"\n=== Flagged scenarios ({len(flagged)}) ===")
    for toml_stem, name, flags in flagged:
        print(f"  {toml_stem}/{name}: {' | '.join(flags)}")

    print(f"\nFull results: {out_path}")


if __name__ == "__main__":
    main()
