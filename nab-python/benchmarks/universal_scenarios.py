"""Run universal-resolution benchmark scenarios against nab_python.

Reads scenarios from ``scenarios/universal.toml`` and resolves each
via ``nab_python.resolve.resolve_with_coordinator``, sharing a single
``FetchCoordinator`` per scenario for metadata reuse across tuples.

Output mirrors ``scenarios.py`` but with a ``per_tuple`` array so the
divergence between tuples is visible.

Usage:
    python nab-python/benchmarks/universal_scenarios.py [--commit LABEL] [--force]
        [--scenario NAME]

The runner is opt-in: it lives outside the standard scenario flow so
existing benchmarks are not affected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

if sys.version_info >= (3, 11):
    import tomllib
else:
    # nab pins py>=3.10 but the import fallback matches scenarios.py
    import tomli as tomllib  # type: ignore[no-redef] # pragma: no cover

from universal_result import (
    MANIFEST_FILENAME,
    RESULT_SCHEMA_VERSION,
    result_is_accepted,
    result_is_well_formed,
)

from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import NabProjectConfig, enforce_build_policy_for_targets
from nab_python.fetch import FetchCoordinator
from nab_python.lockfile import build_pylock, render_lock
from nab_python.provider import ResolutionStrategy
from nab_python.resolve import (
    ResolveResult,
    build_lock_input,
    resolve_with_coordinator,
)
from nab_python.tags import PlatformSpec
from nab_python.target import Matrix

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
RESULTS_DIR = BENCHMARKS_DIR / "results"
CACHE_DIR = BENCHMARKS_DIR / "cache"

# Per-scenario wall-time cap.  Universal resolves are bounded by
# matrix size * single-tuple cost, so we give them a generous budget.
SCENARIO_WALL_TIMEOUT_SECONDS = 300
UNIVERSAL_SCENARIO_KEYS = frozenset(
    {
        "align_across_tuples",
        "constraints",
        "datetime",
        "platforms",
        "python",
        "python_order",
        "reason",
        "requirements",
        "resolution_strategy",
        "skip_on_fail",
    }
)


class _ScenarioTimeoutError(BaseException):
    """Raised when a scenario exceeds the per-run wall-clock budget.

    Subclasses BaseException so the resolver's internal ``except Exception``
    handlers cannot swallow the alarm mid-resolve.
    """


def _alarm_handler(_signum: int, _frame: object) -> None:
    msg = f"scenario exceeded {SCENARIO_WALL_TIMEOUT_SECONDS}s wall-clock budget"
    raise _ScenarioTimeoutError(msg)


@contextmanager
def _scenario_wall_timeout() -> Iterator[None]:
    """Install the POSIX wall timer when the platform provides one."""
    sigalrm = getattr(signal, "SIGALRM", None)
    alarm = getattr(signal, "alarm", None)
    if sigalrm is None or alarm is None:
        yield
        return

    previous_handler = signal.signal(sigalrm, _alarm_handler)
    alarm(SCENARIO_WALL_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        alarm(0)
        signal.signal(sigalrm, previous_handler)


def get_git_commit() -> str:
    """Return the Git SHA for the working tree, or 'no-git'."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=BENCHMARKS_DIR,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "no-git"
    return out.stdout.strip()


def get_git_source_state() -> dict[str, str | bool | None]:
    """Record source identity separately from the result-directory label."""
    commit = get_git_commit()
    if commit == "no-git":
        return {"commit": None, "dirty": True, "diff_hash": None}
    try:
        root = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                check=True,
                text=True,
                cwd=BENCHMARKS_DIR,
            ).stdout.strip()
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            check=True,
            cwd=root,
        ).stdout
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            capture_output=True,
            check=True,
            cwd=root,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            check=True,
            cwd=root,
        ).stdout
        untracked_hashes = bytearray()
        for raw_path in untracked.split(b"\0"):
            if not raw_path:
                continue
            object_hash = subprocess.run(  # noqa: S603 - path is one arg after --
                ["git", "hash-object", "--", os.fsdecode(raw_path)],
                capture_output=True,
                check=True,
                cwd=root,
            ).stdout
            untracked_hashes.extend(raw_path + b"\0" + object_hash)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"commit": commit, "dirty": True, "diff_hash": None}
    dirty = bool(status)
    diff_hash = (
        hashlib.sha256(status + b"\0" + diff + b"\0" + untracked_hashes).hexdigest()
        if dirty
        else None
    )
    return {"commit": commit, "dirty": dirty, "diff_hash": diff_hash}


def parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime; default to UTC if naive."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_lock_consistency(result: ResolveResult) -> tuple[bool, list[str]]:
    """Verify the marker-gated lock reproduces each tuple's solution.

    Builds the real PEP 751 lock and, for each tuple, projects every
    package marker onto that tuple's environment. The packages whose
    markers admit the environment must be exactly that tuple's pins; a
    missing one is an under-firing marker (a silently dropped dep), an
    extra one an over-firing marker. The corpus declares no conflict
    forks, so the markers are pure environment markers and the
    projection is a plain ``marker.evaluate``.

    Call only when ``result.success`` so the lock covers every tuple.
    """
    lock_input = build_lock_input(result)
    try:
        render_lock(lock_input)
        pylock = build_pylock(lock_input)
    except Exception as exc:
        return False, [f"lock emission raised {type(exc).__name__}: {exc}"[:200]]

    problems: list[str] = []
    for tr in result.target_results:
        expected = {canonicalize_name(n): str(v) for n, v in tr.pins.items()}
        selected: dict[str, str | None] = {}
        duplicates: set[str] = set()
        for pkg in pylock.packages:
            marker = pkg.marker
            if marker is not None and not marker.evaluate(tr.target.marker_env):
                continue
            name = canonicalize_name(str(pkg.name))
            if name in selected:
                duplicates.add(name)
            selected[name] = str(pkg.version) if pkg.version is not None else None
        if selected != expected or duplicates:
            missing = sorted(expected.keys() - selected.keys())
            extra = sorted(selected.keys() - expected.keys())
            mismatch = sorted(
                k
                for k in expected.keys() & selected.keys()
                if expected[k] != selected[k]
            )
            problems.append(
                f"{tr.target.label}: missing={missing} extra={extra} "
                f"mismatch={mismatch} duplicate={sorted(duplicates)}"
            )
    return not problems, problems


def validate_scenario(scenario_name: str, scenario: dict) -> None:
    """Reject invalid settings before running a universal scenario."""
    unknown = sorted(set(scenario) - UNIVERSAL_SCENARIO_KEYS)
    if unknown:
        msg = f"{scenario_name}: unknown scenario setting(s): {', '.join(unknown)}"
        raise ValueError(msg)
    for key in ("python", "platforms", "requirements"):
        if key not in scenario:
            msg = f"{scenario_name}: missing required setting {key!r}"
            raise ValueError(msg)
    if not isinstance(scenario["python"], str) or not scenario["python"]:
        msg = f"{scenario_name}: python must be a non-empty string"
        raise TypeError(msg)
    for key in ("platforms", "requirements", "constraints"):
        value = scenario.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            msg = f"{scenario_name}: {key} must be a list of strings"
            raise TypeError(msg)
    if not scenario["platforms"] or not scenario["requirements"]:
        msg = f"{scenario_name}: platforms and requirements cannot be empty"
        raise ValueError(msg)
    seen_platforms: set[str] = set()
    for platform in scenario["platforms"]:
        if platform in seen_platforms:
            msg = f"{scenario_name}: platforms has duplicate entry: {platform!r}"
            raise ValueError(msg)
        seen_platforms.add(platform)
    for key in ("align_across_tuples", "skip_on_fail"):
        if key in scenario and type(scenario[key]) is not bool:
            msg = f"{scenario_name}: {key} must be a boolean"
            raise TypeError(msg)
    for key in ("datetime", "reason"):
        if key in scenario and not isinstance(scenario[key], str):
            msg = f"{scenario_name}: {key} must be a string"
            raise TypeError(msg)
    python_order = scenario.get("python_order", "asc")
    if python_order not in {"asc", "desc"}:
        msg = f"{scenario_name}: python_order must be 'asc' or 'desc'"
        raise ValueError(msg)
    resolution = scenario.get("resolution_strategy", "highest")
    try:
        ResolutionStrategy(resolution)
    except (TypeError, ValueError) as exc:
        msg = f"{scenario_name}: unknown resolution_strategy {resolution!r}"
        raise ValueError(msg) from exc


def write_manifest(
    path: Path,
    *,
    commit: str,
    source: dict[str, str | bool | None],
    run_kind: str,
    available_scenarios: list[str],
    selected_scenarios: list[str],
    completed_scenarios: list[str],
    complete: bool,
) -> None:
    """Write the scenario-set contract consumed by benchmark summaries."""
    data = {
        "benchmark_schema": RESULT_SCHEMA_VERSION,
        "commit": commit,
        "source": source,
        "run_kind": run_kind,
        "available_scenarios": sorted(available_scenarios),
        "selected_scenarios": sorted(selected_scenarios),
        "completed_scenarios": sorted(completed_scenarios),
        "complete": complete,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def process_scenario(
    scenario_name: str,
    scenario: dict,
    commit: str,
    *,
    force: bool,
    output_dir: Path | None = None,
    source: dict[str, str | bool | None] | None = None,
) -> bool:
    """Resolve one universal scenario and save results."""
    validate_scenario(scenario_name, scenario)
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
    matrix = Matrix(
        python=python_spec,
        platforms=tuple(PlatformSpec(p) for p in platforms),
        python_order=python_order,
    )
    targets = matrix.expand()
    config = NabProjectConfig(
        constraints=tuple(constraint_strings),
        uploaded_prior_to=uploaded_prior_to,
    )
    build_policy = enforce_build_policy_for_targets(
        targets=targets,
        build_policy=config.build_policy,
        build_policy_set=False,
        package_overrides=config.package_overrides,
        index_overrides=config.index_overrides,
    )
    config = replace(config, build_policy=build_policy)

    output_dir = output_dir or RESULTS_DIR / commit / "universal"
    output_path = output_dir / f"{scenario_name}.json"

    expected_input = {
        "benchmark_schema": RESULT_SCHEMA_VERSION,
        "scenario": scenario_name,
        "commit": commit,
        "source": source if source is not None else get_git_source_state(),
        "python": python_spec,
        "platforms": list(platforms),
        "python_order": python_order,
        "requirements": requirement_strings,
        "align_across_tuples": align,
        "resolution_strategy": resolution_strategy,
        "dist_policy": config.dist_policy.value,
        "build_policy": config.build_policy.value,
        "trust_unverified_sdist_deps": config.trust_unverified_sdist_deps,
        "skip_on_fail": skip_on_fail,
        "reason": reason,
    }
    if constraint_strings:
        expected_input["constraints"] = constraint_strings
    if datetime_str:
        expected_input["datetime"] = datetime_str

    source_state = expected_input["source"]
    source_is_cacheable = isinstance(source_state, dict) and (
        source_state.get("dirty") is False
        or isinstance(source_state.get("diff_hash"), str)
    )
    if output_path.exists() and not force and source_is_cacheable:
        try:
            existing = json.loads(output_path.read_text())
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and existing.get("input") == expected_input:
            if result_is_well_formed(existing):
                accepted = result_is_accepted(existing)
                status = "cached, accepted" if accepted else "CACHED FAILURE"
                print(f"  {scenario_name} {status}")
                return accepted
            print(f"  {scenario_name} invalid cache; rerunning")
        elif existing is None:
            print(f"  {scenario_name} unreadable cache; rerunning")
    elif output_path.exists() and not force:
        print(f"  {scenario_name} source identity unavailable; rerunning")

    print(f"  {scenario_name} ", end="", flush=True)
    start = time.monotonic()
    timed_out = False
    completed = False
    harness_error: str | None = None
    resolution_success: bool | None = None
    per_tuple: list[dict] = []
    merged: dict = {}
    diverging_packages = 0
    lock_consistent: bool | None = None
    lock_inconsistencies: list[str] = []
    try:
        with (
            _scenario_wall_timeout(),
            FetchCoordinator(
                Urllib3AsyncTransport(),
                indexes=list(config.indexes),
                cache_dir=CACHE_DIR,
            ) as coordinator,
        ):
            result = resolve_with_coordinator(
                coordinator,
                targets,
                [Requirement(text) for text in requirement_strings],
                config=config,
                cache_dir=CACHE_DIR,
                resolution_strategy=ResolutionStrategy(resolution_strategy),
                align_across_targets=align,
            )
        resolution_success = result.success
        elapsed = time.monotonic() - start
        per_tuple = [
            {
                "label": tr.target.label,
                "python_version": tr.target.python_version,
                "platform_id": tr.target.platform_id,
                "success": tr.success,
                "error": str(tr.error) if tr.error is not None else None,
                "decisions": tr.decisions,
                "rounds": tr.rounds,
                "conflicts": tr.conflicts,
                "backjumps": tr.backjumps,
                "metadata_fetched": tr.metadata_fetched,
                "distributions_seen": tr.distributions_seen,
                "wall_time_seconds": round(tr.wall_time, 3),
                "package_count": len(tr.pins),
                "pins": {
                    canonicalize_name(name): str(version)
                    for name, version in sorted(tr.pins.items())
                },
            }
            for tr in result.target_results
        ]
        merged = result.merged_pins()
        diverging_packages = sum(
            1 for pins in merged.values() if len({version for version, _ in pins}) > 1
        )
        if resolution_success:
            lock_consistent, lock_inconsistencies = check_lock_consistency(result)
        else:
            lock_consistent, lock_inconsistencies = None, []
        completed = True
    except _ScenarioTimeoutError as exc:
        elapsed = time.monotonic() - start
        timed_out = True
        harness_error = str(exc)
    except Exception as exc:
        elapsed = time.monotonic() - start
        harness_error = f"{type(exc).__name__}: {exc}"[:200]

    scenario_success = resolution_success is True and lock_consistent is True
    expected_failure = skip_on_fail and completed and resolution_success is False
    data = {
        "input": expected_input,
        "reason": reason,
        "result": {
            "success": scenario_success,
            "resolution_success": resolution_success,
            "expected_failure": expected_failure,
            "timed_out": timed_out,
            "skip_on_fail": skip_on_fail,
            "lock_consistent": lock_consistent,
            "lock_inconsistencies": lock_inconsistencies,
            "error": harness_error,
        },
        "merged_pins": {
            canonicalize_name(name): [
                {"version": str(version), "target": target}
                for version, target in target_pins
            ]
            for name, target_pins in sorted(merged.items())
        },
        "stats": {
            "wall_time_seconds": round(elapsed, 3),
            "tuples_total": len(targets),
            "tuples_recorded": len(per_tuple),
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
    elif scenario_success:
        print(
            f"ok ({stats['tuples_total']} tuples, "
            f"{stats['merged_packages']} pkgs, "
            f"{stats['diverging_packages']} diverging, "
            f"{stats['decisions_total']} decisions, "
            f"{stats['wall_time_seconds']}s)"
        )
    elif harness_error:
        print(f"FAILED ({harness_error})")
    elif resolution_success is True:
        print(
            "FAILED (resolved, but the emitted lock does not reproduce every tuple; "
            f"{len(lock_inconsistencies)} inconsistency report(s))"
        )
    else:
        failures = [t for t in per_tuple if not t["success"]]
        label = "skipped (expected to fail)" if expected_failure else "FAILED"
        first_fail = failures[0]["error"] if failures else harness_error or "?"
        print(
            f"{label} ({stats['tuples_fail']}/{stats['tuples_total']} tuples; "
            f"first error: {first_fail[:80]})"
        )
    return result_is_accepted(data)


def main() -> None:
    """Run the selected universal scenarios."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        default=None,
        help="Override the commit label used for the results directory",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        help="Run only the named scenario; may be repeated",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run scenarios even when cached results match",
    )
    args = parser.parse_args()

    source = get_git_source_state()
    commit = args.commit or str(source["commit"] or "no-git")
    toml_path = SCENARIOS_DIR / "universal.toml"
    print(f"Running universal scenarios from {toml_path}")
    scenarios = tomllib.loads(toml_path.read_text())
    if args.scenario:
        duplicates = sorted(
            {name for name in args.scenario if args.scenario.count(name) > 1}
        )
        if duplicates:
            parser.error(f"duplicate scenario(s): {', '.join(duplicates)}")
        missing = sorted(set(args.scenario) - scenarios.keys())
        if missing:
            parser.error(f"unknown scenario(s): {', '.join(missing)}")
        selected = [(name, scenarios[name]) for name in args.scenario]
    else:
        selected = list(scenarios.items())

    for name, scenario in selected:
        validate_scenario(name, scenario)

    result_kind = "universal-selected" if args.scenario else "universal"
    output_dir = RESULTS_DIR / commit / result_kind
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Results -> {output_dir}\n")
    manifest_path = output_dir / MANIFEST_FILENAME
    available_names = list(scenarios)
    selected_names = [name for name, _scenario in selected]
    run_kind = "selected" if args.scenario else "full"
    completed_names: list[str] = []
    accepted: list[bool] = []
    write_manifest(
        manifest_path,
        commit=commit,
        source=source,
        run_kind=run_kind,
        available_scenarios=available_names,
        selected_scenarios=selected_names,
        completed_scenarios=completed_names,
        complete=False,
    )
    for name, scenario in selected:
        accepted.append(
            process_scenario(
                name,
                scenario,
                commit,
                force=args.force,
                output_dir=output_dir,
                source=source,
            )
        )
        if (output_dir / f"{name}.json").is_file():
            completed_names.append(name)
        write_manifest(
            manifest_path,
            commit=commit,
            source=source,
            run_kind=run_kind,
            available_scenarios=available_names,
            selected_scenarios=selected_names,
            completed_scenarios=completed_names,
            complete=False,
        )
    write_manifest(
        manifest_path,
        commit=commit,
        source=source,
        run_kind=run_kind,
        available_scenarios=available_names,
        selected_scenarios=selected_names,
        completed_scenarios=completed_names,
        complete=len(completed_names) == len(selected_names),
    )
    if not all(accepted):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
