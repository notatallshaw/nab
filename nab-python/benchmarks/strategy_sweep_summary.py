"""Post-process the strategy sweep results into a focused summary.

Usage:
    python nab-python/benchmarks/strategy_sweep_summary.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

_BENCH_DIR = Path(__file__).resolve().parent
SUMMARY = _BENCH_DIR / "strategy_sweep_results" / "summary.json"


def main() -> None:
    if not SUMMARY.exists():
        sys.stderr.write(f"Error: {SUMMARY} not found.  Run strategy_sweep.py first.\n")
        sys.exit(1)
    data = json.loads(SUMMARY.read_text())

    by_strat_outcome: dict[str, Counter[str]] = defaultdict(Counter)
    diverge: list[dict] = []
    explosions: list[dict] = []
    slowdowns: list[dict] = []
    new_errors: list[dict] = []
    timeouts: list[dict] = []

    for entry in data:
        scen = f"{entry['toml']}/{entry['scenario']}"
        results = {r["strategy"]: r for r in entry["results"]}

        for strat, r in results.items():
            if r["success"]:
                by_strat_outcome[strat]["ok"] += 1
            elif r["error"] and r["error"].startswith("Timeout"):
                by_strat_outcome[strat]["timeout"] += 1
                timeouts.append({"scenario": scen, "strategy": strat})
            else:
                by_strat_outcome[strat]["fail"] += 1

        successes = {s: r["success"] for s, r in results.items()}
        if len(set(successes.values())) > 1:
            diverge.append(
                {
                    "scenario": scen,
                    "outcomes": {
                        s: ("ok" if r["success"] else r["error"])
                        for s, r in results.items()
                    },
                }
            )

        base = results.get("highest", {})
        if base.get("success") and base.get("decisions", 0) > 0:
            for strat in ("lowest", "lowest-direct"):
                other = results.get(strat, {})
                if not other.get("success"):
                    continue
                if (
                    other["decisions"] >= 10 * base["decisions"]
                    and other["decisions"] > 50
                ):
                    explosions.append(
                        {
                            "scenario": scen,
                            "strategy": strat,
                            "decisions": other["decisions"],
                            "highest_decisions": base["decisions"],
                            "wall_time": other["wall_time"],
                        }
                    )
                if other["wall_time"] >= 10 * max(base["wall_time"], 0.5):
                    slowdowns.append(
                        {
                            "scenario": scen,
                            "strategy": strat,
                            "wall_time": other["wall_time"],
                            "highest_wall_time": base["wall_time"],
                        }
                    )

        # Surface unfamiliar exception types; the existing test surface
        # produces ``ResolutionError`` and ``MetadataError`` (and their
        # subclasses).  Anything else may be a real bug.
        for strat, r in results.items():
            if r["success"]:
                continue
            err = r["error"] or ""
            if err.startswith(
                (
                    "Timeout",
                    "ResolutionError",
                    "MetadataError",
                    "UnsupportedSdistError",
                    "UnsupportedVcsError",
                    "MissingExtraError",
                    "InvalidVersion",
                    "InvalidSpecifier",
                )
            ):
                continue
            new_errors.append({"scenario": scen, "strategy": strat, "error": err})

    print("=== Outcome counts (per strategy) ===")
    for strat in ("highest", "lowest", "lowest-direct"):
        c = by_strat_outcome[strat]
        total = c["ok"] + c["fail"] + c["timeout"]
        print(
            f"  {strat:<14} ok={c['ok']:<4} fail={c['fail']:<4}"
            f" timeout={c['timeout']:<3} total={total}"
        )

    print(f"\n=== DIVERGE ({len(diverge)}): different success between strategies ===")
    for d in diverge:
        print(f"  {d['scenario']}")
        for s, o in d["outcomes"].items():
            print(f"    {s:<14} {o}")

    print(f"\n=== EXPLOSION ({len(explosions)}): >=10x more decisions vs highest ===")
    for e in explosions:
        print(
            f"  {e['scenario']} [{e['strategy']}]:"
            f" {e['decisions']} decisions vs {e['highest_decisions']}"
            f" (wall {e['wall_time']}s)"
        )

    print(f"\n=== SLOW ({len(slowdowns)}): >=10x slower vs highest (>=0.5s base) ===")
    for s in slowdowns:
        print(
            f"  {s['scenario']} [{s['strategy']}]:"
            f" {s['wall_time']}s vs {s['highest_wall_time']}s"
        )

    print(f"\n=== TIMEOUTS ({len(timeouts)}) ===")
    for t in timeouts:
        print(f"  {t['scenario']} [{t['strategy']}]")

    print(f"\n=== UNEXPECTED ERRORS ({len(new_errors)}) ===")
    for e in new_errors:
        print(f"  {e['scenario']} [{e['strategy']}]: {e['error']}")


if __name__ == "__main__":
    main()
