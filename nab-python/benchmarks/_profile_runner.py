"""Single-scenario profiling runner.

Loads one scenario from the benchmark TOML files and runs the resolver
once.  Designed to be wrapped by ``python -m profiling.sampling run``.

Usage:
    .venv-3.15/bin/python -m profiling.sampling run -r 5khz \
        --flamegraph -o profile.html \
        nab-python/benchmarks/_profile_runner.py <scenario>
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.multi_index import IndexConfig
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.fetch import (
    DEFAULT_INDEX_NAME,
    DEFAULT_INDEX_URL,
    FetchCoordinator,
)
from nab_python.provider import DistPolicy, Provider
from nab_resolver.resolver import Resolver

if TYPE_CHECKING:
    from collections.abc import Iterable

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
CACHE_DIR = BENCHMARKS_DIR / "cache"
DEFAULT_INDEXES = (IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),)


def parse_requirements(strs: Iterable[str]) -> dict[str, VersionRange]:
    out: dict[str, VersionRange] = {}
    for r in strs:
        req = Requirement(r)
        name = canonicalize_name(req.name)
        vi = req.specifier.to_range()
        if vi is not None:
            out[name] = vi
        for ex in req.extras:
            out[f"{name}[{ex}]"] = VersionRange.full()
    return out


def find_scenario(name: str) -> dict[str, Any] | None:
    for p in SCENARIOS_DIR.glob("*.toml"):
        with p.open("rb") as f:
            data = tomllib.load(f)
        if name in data:
            return data[name]
    return None


def main() -> None:
    name = sys.argv[1]
    scn = find_scenario(name)
    if scn is None:
        print(f"scenario {name!r} not found", file=sys.stderr)
        sys.exit(2)
    reqs = parse_requirements(scn["requirements"])
    constraints = (
        parse_requirements(scn.get("constraints", []))
        if scn.get("constraints")
        else None
    )
    py = scn["python_version"]
    dt = scn.get("datetime")
    upload = datetime.fromisoformat(dt).replace(tzinfo=timezone.utc) if dt else None
    with FetchCoordinator(
        HttpxAsyncTransport(),
        indexes=list(DEFAULT_INDEXES),
        cache_dir=CACHE_DIR,
    ) as coord:
        provider = Provider(
            coord,
            python_version=py,
            root_requirements=reqs,
            uploaded_prior_to=upload,
            dist_policy=DistPolicy.PREFER_BINARY,
        )
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            max_iterations=200_000,
        )
        t0 = time.monotonic()
        try:
            resolver.resolve(reqs, constraints=constraints)
            elapsed = time.monotonic() - t0
            print(
                f"{name}: OK in {elapsed:.2f}s, "
                f"{resolver.stats.decisions} decisions, "
                f"{resolver.stats.conflicts} conflicts"
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(
                f"{name}: FAILED ({type(exc).__name__}) in {elapsed:.2f}s, "
                f"{resolver.stats.decisions} decisions"
            )


if __name__ == "__main__":
    main()
