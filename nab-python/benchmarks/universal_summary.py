"""Summarise universal benchmark results into a single table."""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"


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
    print(f"Results from {target_dir.relative_to(RESULTS_DIR.parent)}\n")
    print("| Scenario | Tuples | OK | Pkgs | Diverge | Decisions | Wall (s) | Reason |")
    print("|---|---:|---:|---:|---:|---:|---:|---|")
    for path in sorted(target_dir.glob("*.json")):
        data = json.loads(path.read_text())
        s = data["stats"]
        reason = data.get("reason", "")
        timed_out = data["result"].get("timed_out", False)
        success = data["result"]["success"]
        suffix = " (TIMEOUT)" if timed_out else "" if success else " (FAIL)"
        print(
            f"| {path.stem}{suffix}"
            f" | {s['tuples_total']}"
            f" | {s['tuples_ok']}"
            f" | {s['merged_packages']}"
            f" | {s['diverging_packages']}"
            f" | {s['decisions_total']}"
            f" | {s['wall_time_seconds']:.2f}"
            f" | {reason} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
