"""Run universal-resolution benchmark scenarios against nab_python.universal.

Reads scenarios from ``scenarios/universal.toml`` and resolves each
via ``nab_python.universal.resolve_universal``, sharing a single
``FetchCoordinator`` per scenario for metadata reuse across tuples.

Output mirrors ``scenarios.py`` but with a ``per_tuple`` array so the
divergence between tuples is visible.

Usage:
    python nab-python/benchmarks/universal_scenarios.py [--commit LABEL] [--force]

The runner is opt-in: it lives outside the standard scenario flow so
existing benchmarks are not affected.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    # nab pins py>=3.10 but the import fallback matches scenarios.py
    import tomli as tomllib  # type: ignore[no-redef] # pragma: no cover

from nab_python.universal.matrix import Matrix
from nab_python.universal.resolve import resolve_universal

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
RESULTS_DIR = BENCHMARKS_DIR / "results"
CACHE_DIR = BENCHMARKS_DIR / "cache"

# Per-scenario wall-time cap.  Universal resolves are bounded by
# matrix size * single-tuple cost, so we give them a generous budget.
SCENARIO_WALL_TIMEOUT_SECONDS = 300


class _ScenarioTimeoutError(BaseException):
    """Raised when a scenario exceeds the per-run wall-clock budget.

    Subclasses BaseException so the resolver's internal ``except Exception``
    handlers cannot swallow the alarm mid-resolve.
    """


def _alarm_handler(_signum: int, _frame: object) -> None:
    msg = f"scenario exceeded {SCENARIO_WALL_TIMEOUT_SECONDS}s wall-clock budget"
    raise _ScenarioTimeoutError(msg)


def get_git_commit() -> str:
    """Return a short git SHA for the working tree, or 'no-git'."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "no-git"
    return out.stdout.strip()


def parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime; default to UTC if naive."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def process_scenario(
    scenario_name: str,
    scenario: dict,
    commit: str,
    *,
    force: bool,
) -> None:
    """Resolve one universal scenario and save results."""
    python_spec: str = scenario["python"]
    platforms: tuple[str, ...] = tuple(scenario["platforms"])
    python_order: str = scenario.get("python_order", "asc")
    requirement_strings: list[str] = scenario["requirements"]
    constraint_strings: list[str] = scenario.get("constraints", [])
    datetime_str: str | None = scenario.get("datetime")
    align: bool = scenario.get("align_across_tuples", True)
    resolution_strategy: str = scenario.get("resolution_strategy", "highest")
    reason: str = scenario.get("reason", "")
    skip_on_fail: bool = scenario.get("skip_on_fail", False)

    uploaded_prior_to = parse_datetime(datetime_str) if datetime_str else None

    output_dir = RESULTS_DIR / commit / "universal"
    output_path = output_dir / f"{scenario_name}.json"

    expected_input = {
        "commit": commit,
        "python": python_spec,
        "platforms": list(platforms),
        "python_order": python_order,
        "requirements": requirement_strings,
        "align_across_tuples": align,
        "resolution_strategy": resolution_strategy,
    }
    if constraint_strings:
        expected_input["constraints"] = constraint_strings
    if datetime_str:
        expected_input["datetime"] = datetime_str

    if output_path.exists() and not force:
        existing = json.loads(output_path.read_text())
        if existing.get("input") == expected_input:
            return

    print(f"  {scenario_name} ", end="", flush=True)
    matrix = Matrix(python=python_spec, platforms=platforms, python_order=python_order)

    previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(SCENARIO_WALL_TIMEOUT_SECONDS)
    start = time.monotonic()
    timed_out = False
    try:
        result = resolve_universal(
            matrix=matrix,
            requirements=requirement_strings,
            constraints=constraint_strings or None,
            cache_dir=CACHE_DIR,
            uploaded_prior_to=uploaded_prior_to,
            resolution_strategy=resolution_strategy,
            align_across_tuples=align,
        )
        elapsed = time.monotonic() - start
        per_tuple = [
            {
                "label": tr.tuple_.label,
                "python_version": tr.tuple_.python_version,
                "platform_id": tr.tuple_.platform_id,
                "success": tr.success,
                "error": tr.error,
                "decisions": tr.decisions,
                "rounds": tr.rounds,
                "conflicts": tr.conflicts,
                "backjumps": tr.backjumps,
                "metadata_fetched": tr.metadata_fetched,
                "distributions_seen": tr.distributions_seen,
                "wall_time_seconds": round(tr.wall_time, 3),
                "package_count": len(tr.pins),
            }
            for tr in result.tuple_results
        ]
        merged = result.merged_lock()
        diverging_packages = sum(
            1 for pins in merged.values() if len({version for version, _ in pins}) > 1
        )
        success = result.success
    except _ScenarioTimeoutError:
        elapsed = time.monotonic() - start
        timed_out = True
        per_tuple = []
        merged = {}
        diverging_packages = 0
        success = False
    except Exception as exc:
        elapsed = time.monotonic() - start
        per_tuple = [
            {
                "label": "n/a",
                "python_version": "n/a",
                "platform_id": "n/a",
                "success": False,
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "decisions": 0,
                "rounds": 0,
                "conflicts": 0,
                "backjumps": 0,
                "metadata_fetched": 0,
                "distributions_seen": 0,
                "wall_time_seconds": 0.0,
                "package_count": 0,
            }
        ]
        merged = {}
        diverging_packages = 0
        success = False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)

    data = {
        "input": expected_input,
        "reason": reason,
        "result": {
            "success": success,
            "timed_out": timed_out,
            "skip_on_fail": skip_on_fail,
        },
        "stats": {
            "wall_time_seconds": round(elapsed, 3),
            "tuples_total": len(per_tuple),
            "tuples_ok": sum(1 for t in per_tuple if t["success"]),
            "tuples_fail": sum(1 for t in per_tuple if not t["success"]),
            "merged_packages": len(merged),
            "diverging_packages": diverging_packages,
            "decisions_total": sum(t["decisions"] for t in per_tuple),
            "rounds_total": sum(t["rounds"] for t in per_tuple),
            "conflicts_total": sum(t["conflicts"] for t in per_tuple),
            "backjumps_total": sum(t["backjumps"] for t in per_tuple),
            "metadata_fetched_total": sum(t["metadata_fetched"] for t in per_tuple),
            "distributions_seen_total": sum(t["distributions_seen"] for t in per_tuple),
        },
        "per_tuple": per_tuple,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2) + "\n")

    stats = data["stats"]
    if timed_out:
        print(f"TIMEOUT after {elapsed:.1f}s ({stats['tuples_total']} tuples)")
    elif success:
        print(
            f"ok ({stats['tuples_total']} tuples, "
            f"{stats['merged_packages']} pkgs, "
            f"{stats['diverging_packages']} diverging, "
            f"{stats['decisions_total']} decisions, "
            f"{stats['wall_time_seconds']}s)"
        )
    else:
        failures = [t for t in per_tuple if not t["success"]]
        label = "skipped (expected to fail)" if skip_on_fail else "FAILED"
        first_fail = failures[0]["error"] if failures else "?"
        print(
            f"{label} ({stats['tuples_fail']}/{stats['tuples_total']} tuples; "
            f"first error: {first_fail[:80]})"
        )


def main() -> None:
    """Run all universal scenarios."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        default=None,
        help="Override the commit label used for the results directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run scenarios even when cached results match",
    )
    args = parser.parse_args()

    commit = args.commit or get_git_commit()
    toml_path = SCENARIOS_DIR / "universal.toml"
    print(f"Running universal scenarios from {toml_path}")
    print(f"Results -> {RESULTS_DIR / commit / 'universal'}\n")
    scenarios = tomllib.loads(toml_path.read_text())
    for scenario_name, scenario in scenarios.items():
        process_scenario(scenario_name, scenario, commit, force=args.force)


if __name__ == "__main__":
    main()
