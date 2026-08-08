"""Compatibility entry point for ``scenarios.py --strategy-matrix``."""

from __future__ import annotations

import sys
from pathlib import Path

_BENCHMARKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BENCHMARKS_DIR))

import scenarios  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    """Forward arguments to the standard strategy-matrix runner."""
    forwarded = sys.argv[1:] if argv is None else argv
    scenarios.main(["--strategy-matrix", *forwarded])


if __name__ == "__main__":
    main()
