"""Check the public imports and typing markers of installed distributions."""

from __future__ import annotations

import sys
from importlib import import_module
from importlib.resources import files


def main(packages: list[str]) -> None:
    """Import the built packages and verify their installed PEP 561 markers."""
    for package in packages:
        module = package.replace("-", "_")
        if not files(module).joinpath("py.typed").is_file():
            msg = f"{package}: installed package is missing {module}/py.typed"
            raise SystemExit(msg)

        # The marker package root alone does not load the selected packaging backend.
        if package == "nab-markersets":
            import_module("nab_markersets.markersets")


if __name__ == "__main__":
    main(sys.argv[1:])
