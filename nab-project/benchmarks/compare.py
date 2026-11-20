"""Compare two complete standard benchmark runs.

Usage:
    python nab-project/benchmarks/compare.py <baseline> <comparison>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple, NoReturn

from benchmark_json import read_benchmark_json
from standard_result import (
    STAT_LABELS,
    StandardResult,
    valid_outcome,
    valid_statistics,
)

RESULTS_DIR = Path(__file__).parent / "results"
MANIFEST_FILENAME = "_standard_manifest.json"
MANIFEST_SCHEMA = 4
_RESERVED_DIRECTORIES = frozenset({"universal", "universal-selected"})
_MANIFEST_FIELDS = frozenset(
    {
        "benchmark_schema",
        "commit",
        "source_start",
        "source_end",
        "mode",
        "strategies",
        "settings",
        "corpus_hash",
        "corpus_files",
        "selected_files",
        "available_logical_keys",
        "selected_logical_keys",
        "completed_logical_keys",
        "unsupported_logical_keys",
        "requires_matching_host_logical_keys",
        "inapplicable_logical_keys",
        "available_execution_keys",
        "selected_execution_keys",
        "completed_execution_keys",
        "unsupported_execution_keys",
        "file_execution_keys",
        "complete",
    }
)
_LIST_FIELDS = tuple(
    sorted(
        field
        for field in _MANIFEST_FIELDS
        if field.endswith("_keys")
        or field in {"strategies", "corpus_files", "selected_files"}
    )
)

_STANDARD_SETTINGS_FIELDS = frozenset(
    {
        "dist_policy",
        "build_policy",
        "trust_unverified_sdist_deps_default",
        "max_iterations",
        "wall_timeout_seconds",
        "host",
    }
)
_HOST_IDENTITY_FIELDS = frozenset(
    {"python", "marker_environment", "wheel_tags_count", "wheel_tags_hash"}
)
_MODE_STRATEGIES = {
    "default": ["highest"],
    "strategy-matrix": ["highest", "lowest", "lowest-direct"],
}


class ComparisonError(ValueError):
    """Raised when stored benchmark runs are not comparable."""


class BenchmarkRun(NamedTuple):
    """A validated manifest and its declared result payloads."""

    manifest: dict[str, object]
    results: dict[str, StandardResult]


def _fail(message: str, cause: BaseException | None = None) -> NoReturn:
    """Raise a comparison error, optionally chaining an underlying failure."""
    raise ComparisonError(message) from cause


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_json(path: Path) -> object:
    try:
        return read_benchmark_json(path)
    except (OSError, ValueError) as exc:
        _fail(f"unreadable benchmark JSON: {path}: {exc}", exc)


def _manifest_lists(data: dict[str, object]) -> dict[str, list[str]]:
    lists: dict[str, list[str]] = {}
    for field in _LIST_FIELDS:
        value = data.get(field)
        if (
            not isinstance(value, list)
            or any(type(item) is not str for item in value)
            or value != sorted(set(value))
        ):
            _fail(f"manifest field {field!r} must be sorted and unique")
        lists[field] = value
    return lists


def _clean_source(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"commit", "dirty", "diff_hash"}:
        return False
    commit = value.get("commit")
    return (
        isinstance(commit, str)
        and len(commit) in {40, 64}
        and all(character in "0123456789abcdef" for character in commit)
        and value == {"commit": commit, "dirty": False, "diff_hash": None}
    )


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_settings(value: object, run_dir: Path) -> None:
    if not isinstance(value, dict) or set(value) != _STANDARD_SETTINGS_FIELDS:
        _fail(f"invalid standard benchmark settings: {run_dir}")
    timeout = value["wall_timeout_seconds"]
    if (
        value["dist_policy"] != "wheel-or-sdist"
        or value["build_policy"] != "never"
        or value["trust_unverified_sdist_deps_default"] is not False
        or type(value["max_iterations"]) is not int
        or value["max_iterations"] <= 0
        or (timeout is not None and (type(timeout) is not int or timeout <= 0))
    ):
        _fail(f"invalid standard benchmark settings: {run_dir}")

    host = value["host"]
    if not isinstance(host, dict) or set(host) != _HOST_IDENTITY_FIELDS:
        _fail(f"invalid standard benchmark host identity: {run_dir}")
    marker_environment = host["marker_environment"]
    if (
        not isinstance(host["python"], str)
        or not host["python"]
        or not isinstance(marker_environment, dict)
        or not marker_environment
        or any(
            type(key) is not str or type(marker) is not str
            for key, marker in marker_environment.items()
        )
        or type(host["wheel_tags_count"]) is not int
        or host["wheel_tags_count"] <= 0
        or not _is_sha256(host["wheel_tags_hash"])
    ):
        _fail(f"invalid standard benchmark host identity: {run_dir}")


def _execution_map(logical_keys: list[str], strategies: list[str]) -> dict[str, str]:
    executions: dict[str, str] = {}
    for logical_key in logical_keys:
        stem, separator, name = logical_key.partition(":")
        if (
            not separator
            or not stem
            or not name
            or ":" in name
            or any(character in stem + name for character in "/\\")
        ):
            _fail(f"invalid logical benchmark key: {logical_key!r}")
        for strategy in strategies:
            directory = stem if strategy == "highest" else f"{stem}-{strategy}"
            execution_key = f"{directory}/{name}.json"
            if execution_key in executions:
                _fail(f"colliding benchmark key: {execution_key!r}")
            executions[execution_key] = logical_key
    return executions


def _exact_partition(whole: list[str], *parts: list[str]) -> bool:
    combined = [item for part in parts for item in part]
    return len(combined) == len(set(combined)) and sorted(combined) == whole


def _actual_result_keys(run_dir: Path) -> list[str]:
    """List regular standard result JSON files below a run directory."""
    keys: list[str] = []
    try:
        paths = run_dir.rglob("*")
        for path in paths:
            relative = path.relative_to(run_dir)
            if relative.parts[0] in _RESERVED_DIRECTORIES:
                if len(relative.parts) == 1 and (
                    path.is_symlink() or not path.is_dir()
                ):
                    _fail(f"reserved result path is not a directory: {path}")
                continue
            if path.is_symlink():
                _fail(f"benchmark result path is a symlink: {path}")
            if path.suffix.casefold() != ".json":
                continue
            if not path.is_file():
                _fail(f"benchmark result path is not regular: {path}")
            key = relative.as_posix()
            if key not in {MANIFEST_FILENAME, "_provenance.json"}:
                keys.append(key)
    except OSError as exc:
        _fail(f"unreadable benchmark directory: {run_dir}", exc)
    return sorted(keys)


def _validate_manifest_identity(
    data: dict[str, object],
    run_dir: Path,
) -> dict[str, list[str]]:
    if data["benchmark_schema"] != MANIFEST_SCHEMA or data["complete"] is not True:
        _fail(f"standard benchmark run is not complete: {run_dir}")
    if data["commit"] != run_dir.name:
        _fail(f"standard benchmark manifest does not own {run_dir}")
    source = data["source_start"]
    if not _clean_source(source) or source != data["source_end"]:
        _fail(f"standard benchmark source is not clean and stable: {run_dir}")
    if not _is_sha256(data["corpus_hash"]):
        _fail(f"invalid standard benchmark identity: {run_dir}")
    _validate_settings(data["settings"], run_dir)

    lists = _manifest_lists(data)
    strategies = lists["strategies"]
    mode = data["mode"]
    if (
        not isinstance(mode, str)
        or strategies != _MODE_STRATEGIES.get(mode)
        or not set(lists["selected_files"]) <= set(lists["corpus_files"])
    ):
        _fail(f"invalid standard benchmark selection: {run_dir}")
    return lists


def _validate_manifest_partitions(
    lists: dict[str, list[str]],
    run_dir: Path,
) -> dict[str, str]:
    strategies = lists["strategies"]
    available = lists["available_logical_keys"]
    selected = lists["selected_logical_keys"]
    completed = lists["completed_logical_keys"]
    unsupported = lists["unsupported_logical_keys"]
    requires_matching_host = lists["requires_matching_host_logical_keys"]
    inapplicable = lists["inapplicable_logical_keys"]
    available_keys = lists["available_execution_keys"]
    selected_keys = lists["selected_execution_keys"]
    if (
        not set(selected) <= set(available)
        or not set(requires_matching_host) <= set(selected)
        or not set(inapplicable) <= set(requires_matching_host)
        or not _exact_partition(selected, completed, unsupported, inapplicable)
    ):
        _fail(f"invalid logical benchmark partition: {run_dir}")
    available_map = _execution_map(available, strategies)
    selected_map = _execution_map(selected, strategies)
    completed_map = _execution_map(completed, strategies)
    unsupported_map = _execution_map(unsupported, strategies)
    inapplicable_map = _execution_map(inapplicable, strategies)
    if available_keys != sorted(available_map) or selected_keys != sorted(selected_map):
        _fail(f"invalid declared benchmark executions: {run_dir}")

    completed_keys = lists["completed_execution_keys"]
    unsupported_keys = lists["unsupported_execution_keys"]
    if (
        completed_keys != sorted(completed_map)
        or unsupported_keys != sorted(unsupported_map)
        or not _exact_partition(
            selected_keys,
            completed_keys,
            unsupported_keys,
            sorted(inapplicable_map),
        )
    ):
        _fail(f"invalid execution benchmark partition: {run_dir}")
    if (
        lists["file_execution_keys"] != completed_keys
        or _actual_result_keys(run_dir) != completed_keys
    ):
        _fail(f"result files do not match the benchmark manifest: {run_dir}")
    return completed_map


def _validate_manifest(run_dir: Path) -> tuple[dict[str, object], dict[str, str]]:
    data = _read_json(run_dir / MANIFEST_FILENAME)
    if not isinstance(data, dict) or set(data) != _MANIFEST_FIELDS:
        _fail(f"invalid standard benchmark manifest: {run_dir}")
    lists = _validate_manifest_identity(data, run_dir)
    return data, _validate_manifest_partitions(lists, run_dir)


def _settings_hash(settings: object) -> str:
    return hashlib.sha256(_canonical_json(settings).encode()).hexdigest()


def _validate_result(
    path: Path,
    execution_key: str,
    logical_key: str,
    manifest: dict[str, object],
    settings_hash: str,
) -> StandardResult:
    data = _read_json(path)
    if not isinstance(data, dict) or set(data) != {"input", "result", "stats"}:
        _fail(f"invalid benchmark result: {path}")
    input_data = data.get("input")
    if not isinstance(input_data, dict):
        _fail(f"invalid benchmark input: {path}")
    provenance = {
        "benchmark_schema": manifest["benchmark_schema"],
        "commit": manifest["commit"],
        "source": manifest["source_start"],
        "corpus_hash": manifest["corpus_hash"],
        "logical_key": logical_key,
        "execution_key": execution_key,
        "settings_hash": settings_hash,
    }
    if any(
        key not in input_data
        or _canonical_json(input_data[key]) != _canonical_json(value)
        for key, value in provenance.items()
    ):
        _fail(f"benchmark input does not match its manifest: {path}")

    outcome = data["result"]
    if not valid_outcome(outcome):
        _fail(f"invalid benchmark result payload: {path}")
    stats = data["stats"]
    if not valid_statistics(stats, len(outcome["pins"])):
        _fail(f"invalid benchmark statistics: {path}")
    return {"input": input_data, "result": outcome, "stats": stats}


def load_run(run_dir: Path) -> BenchmarkRun:
    """Load a complete run and validate every declared standard result."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        _fail(f"no standard benchmark results in {run_dir}")
    manifest, logical_by_execution = _validate_manifest(run_dir)
    settings_hash = _settings_hash(manifest["settings"])
    results = {
        key: _validate_result(run_dir / key, key, logical_key, manifest, settings_hash)
        for key, logical_key in sorted(logical_by_execution.items())
    }
    return BenchmarkRun(manifest, results)


def _without_provenance(data: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in data.items()
        if key not in {"commit", "source", "source_start", "source_end"}
    }


def require_comparable(first: BenchmarkRun, second: BenchmarkRun) -> None:
    """Reject runs whose identities or normalized scenario inputs differ."""
    if _canonical_json(_without_provenance(first.manifest)) != _canonical_json(
        _without_provenance(second.manifest)
    ):
        _fail("standard benchmark manifests have different identities")
    if not first.results:
        _fail("standard benchmark runs have no completed executions")
    for key in first.results:
        first_input = first.results[key]["input"]
        second_input = second.results[key]["input"]
        if _canonical_json(_without_provenance(first_input)) != _canonical_json(
            _without_provenance(second_input)
        ):
            _fail(f"benchmark inputs differ for {key}")


def percent_change(old: float, new: float) -> str:
    if old == 0:
        return "+inf%" if new != 0 else "0.0%"
    # Preserve integer counters and round even ratios too large for a float.
    baseline = Fraction(old)
    change = (Fraction(new) - baseline) / baseline
    whole, tenth = divmod(abs(round(change * 1000)), 10)
    sign = "-" if change < 0 else "+"
    return f"{sign}{Decimal(whole)}.{tenth}%"


def _pin_changes(first: dict[str, str], second: dict[str, str]) -> list[str]:
    """Describe each package whose selected version changed."""
    messages: list[str] = []
    for name in sorted(first.keys() | second.keys()):
        if name not in first:
            messages.append(f"Pin added: {name}=={second[name]}")
        elif name not in second:
            messages.append(f"Pin removed: {name}=={first[name]}")
        elif first[name] != second[name]:
            messages.append(f"Pin changed: {name} {first[name]} -> {second[name]}")
    return messages


def compare_scenario(data1: StandardResult, data2: StandardResult, label: str) -> None:
    """Print result and counter differences for one scenario execution."""
    result1 = data1["result"]
    result2 = data2["result"]
    stats1 = data1["stats"]
    stats2 = data2["stats"]
    pins1 = result1["pins"]
    pins2 = result2["pins"]
    messages = _pin_changes(pins1, pins2)

    if result1["success"] != result2["success"]:
        messages.append(f"Success: {result1['success']} -> {result2['success']}")
    err1 = result1["error"]
    err2 = result2["error"]
    if err1 != err2:
        if err1 and err2:
            messages.append(f"Error changed: {err1} -> {err2}")
        elif err1:
            messages.append(f"Error resolved: {err1}")
        else:
            messages.append(f"New error: {err2}")
    for key, label_text in STAT_LABELS.items():
        if key not in stats1 or key not in stats2:
            continue
        value1 = stats1[key]
        value2 = stats2[key]
        if value1 != value2:
            messages.append(
                f"{label_text}: {value1} -> {value2} ({percent_change(value1, value2)})"
            )
    if messages:
        print(f"{label}:")
        for message in messages:
            print(f"\t{message}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two complete nab benchmark result sets"
    )
    parser.add_argument("baseline", help="Baseline result label")
    parser.add_argument("comparison", help="Comparison result label")
    args = parser.parse_args()

    try:
        first = load_run(RESULTS_DIR / args.baseline)
        second = load_run(RESULTS_DIR / args.comparison)
        require_comparable(first, second)
    except ComparisonError as exc:
        print(f"Cannot compare benchmark runs: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    for key in first.results:
        compare_scenario(first.results[key], second.results[key], key)


if __name__ == "__main__":
    main()
