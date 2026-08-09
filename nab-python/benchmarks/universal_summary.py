"""Summarise universal benchmark results into a single table."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

from universal_result import (
    MANIFEST_FILENAME,
    RESULT_SCHEMA_VERSION,
    is_portable_scenario_name,
    result_is_accepted,
    result_is_well_formed,
)

RESULTS_DIR = Path(__file__).parent / "results"


def _valid_scenario_names(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if not all(is_portable_scenario_name(name) for name in value):
        return False
    names: list[str] = value
    return len({name.casefold() for name in names}) == len(names)


def _valid_clean_source(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    commit = value.get("commit")
    return (
        isinstance(commit, str)
        and len(commit) in {40, 64}
        and all(character in "0123456789abcdef" for character in commit)
        and value.get("dirty") is False
        and value.get("diff_hash") is None
    )


def authoritative_result_paths(
    target_dir: Path,
) -> tuple[list[Path], str, dict] | None:
    """Return the complete full-run scenario set declared by its manifest."""
    manifest_path = target_dir / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("benchmark_schema") != RESULT_SCHEMA_VERSION
        or manifest.get("run_kind") != "full"
        or manifest.get("complete") is not True
    ):
        return None
    commit = manifest.get("commit")
    source = manifest.get("source")
    available = manifest.get("available_scenarios")
    selected = manifest.get("selected_scenarios")
    completed = manifest.get("completed_scenarios")
    if (
        not isinstance(commit, str)
        or not commit
        or commit != target_dir.parent.name
        or not _valid_clean_source(source)
        or not all(
            _valid_scenario_names(names) for names in (available, selected, completed)
        )
    ):
        return None
    available_names = cast("list[str]", available)
    selected_names = cast("list[str]", selected)
    completed_names = cast("list[str]", completed)
    if sorted(available_names) != sorted(selected_names) or sorted(
        available_names
    ) != sorted(completed_names):
        return None
    paths = [target_dir / f"{name}.json" for name in sorted(available_names)]
    return (
        (paths, commit, cast("dict", source))
        if all(path.is_file() for path in paths)
        else None
    )


def main() -> int:
    """Print a markdown table with one row per scenario."""
    if len(sys.argv) > 1:
        target_dir = RESULTS_DIR / sys.argv[1] / "universal"
    else:
        # Pick the most recently modified universal/ dir.
        candidates = sorted(
            RESULTS_DIR.glob("*/universal"), key=lambda p: p.stat().st_mtime
        )
        if not candidates:
            print("No universal/ result directory found.", file=sys.stderr)
            return 1
        target_dir = candidates[-1]
    authoritative = authoritative_result_paths(target_dir)
    if authoritative is None:
        print(f"No complete universal run found in {target_dir}.", file=sys.stderr)
        return 1
    paths, manifest_commit, manifest_source = authoritative
    records: list[tuple[Path, dict]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            print(f"Invalid universal result: {path}", file=sys.stderr)
            return 1
        if not isinstance(data, dict) or not result_is_well_formed(data):
            print(f"Invalid universal result: {path}", file=sys.stderr)
            return 1
        input_data = data["input"]
        if (
            input_data.get("scenario") != path.stem
            or input_data.get("commit") != manifest_commit
            or input_data.get("source") != manifest_source
        ):
            print(f"Universal result provenance mismatch: {path}", file=sys.stderr)
            return 1
        records.append((path, data))

    print(f"Results from {target_dir.relative_to(RESULTS_DIR.parent)}\n")
    print(
        "| Scenario | Tuples | OK | Pkgs | Diverge | Lock"
        " | Decisions | Wall (s) | Reason |"
    )
    print("|---|---:|---:|---:|---:|:---:|---:|---:|---|")
    checked = 0
    inconsistent = 0
    unexpected_failures = 0
    for path, data in records:
        s = data["stats"]
        reason = data.get("reason", "")
        result = data.get("result") or {}
        timed_out = result.get("timed_out", False)
        success = result.get("success") is True
        accepted = result_is_accepted(data)
        expected_failure = accepted and result.get("expected_failure") is True
        if timed_out:
            suffix = " (TIMEOUT)"
        elif expected_failure:
            suffix = " (EXPECTED FAIL)"
        elif accepted and success:
            suffix = ""
        else:
            suffix = " (FAIL)"
        if not accepted:
            unexpected_failures += 1
        lock_consistent = result.get("lock_consistent")
        if lock_consistent is None:
            lock_cell = "-"
        elif lock_consistent:
            lock_cell = "ok"
            checked += 1
        else:
            lock_cell = "BAD"
            checked += 1
            inconsistent += 1
        print(
            f"| {path.stem}{suffix}"
            f" | {s['tuples_total']}"
            f" | {s['tuples_ok']}"
            f" | {s['merged_packages']}"
            f" | {s['diverging_packages']}"
            f" | {lock_cell}"
            f" | {s['decisions_total']}"
            f" | {s['wall_time_seconds']:.2f}"
            f" | {reason} |"
        )
    print(
        f"\nlock consistency: {checked - inconsistent}/{checked} successful locks "
        f"reproduce every tuple's solution"
    )
    return 1 if inconsistent or unexpected_failures else 0


if __name__ == "__main__":
    sys.exit(main())
