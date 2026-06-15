"""Quick canary benchmark for fast iteration.

Runs a curated subset of scenarios (canaries + hard cases) N times each
and reports median decisions, distributions seen (search breadth), and
wall time. The set is small enough to finish in a few minutes so it can
be re-run after each algorithm change.

Usage:
    python nab-python/benchmarks/canary.py [--commit LABEL] [--runs N]
                                           [--scenario NAME] [--scenarios FILE]
"""

from __future__ import annotations

import argparse
import json
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.multi_index import IndexConfig
from nab_python._vcs_admission import admit_vcs_url
from nab_python._vendor.packaging.markers import default_environment
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import PackageOverride
from nab_python.fetch import (
    DEFAULT_INDEX_NAME,
    DEFAULT_INDEX_URL,
    FetchCoordinator,
    IndexRoute,
)
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    Provider,
    VcsConfig,
    VcsPolicy,
    split_extra,
)
from nab_resolver.resolver import Resolver

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
RESULTS_DIR = BENCHMARKS_DIR / "canary_results"
CACHE_DIR = BENCHMARKS_DIR / "cache"
DEFAULT_INDEXES: tuple[IndexConfig, ...] = (
    IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),
)

# Each entry pins its TOML variant as ``stem:name``. Most of these names also
# live in -lowest / -lowest-direct siblings with different counts; the pins
# record the variant the suite already ran so the numbers are reproducible
# instead of depending on directory scan order.
CANARY_SCENARIOS = [
    "uv-lowest:boto3-urllib3-transient",
    "pip-lowest:trustllm",
    "pip-lowest:copick",
    "pip-lowest:promptflow-vectordb",
    "pip-lowest:ultralytics-export",
    "pip-lowest:datacontract-cli",
    "pip-lowest:pandas-aws-boto3-dandi-frenzy",
    "ai-stack:vllm-transformers-floor",
    "pip-lowest:google-bigquery-soda",
    "pip-lowest:langchain-ml-course",
    "airflow-lowest-direct:airflow-3-0-2-awswrangler",
    "airflow-lowest-direct:airflow-3-0-3-pandas-sqlalchemy",
    "airflow-lowest-direct:airflow-portalocker-qdrant",
    "airflow-lowest-direct:airflow-fastapi-121",
    "forums-lowest-direct:so-dbt-core-snowflake-79744735",
    "uv-lowest:uv-issue-16601-xinference",
    "uv-lowest:uv-issue-16601-xinference-fixed",
    "ai-stack:rag-chroma-langchain",
    "ai-stack:streamlit-langchain",
]

WALL_TIMEOUT_S = 60
MAX_ITERATIONS = 50_000


class _ScenarioTimeoutError(BaseException):
    """Raised when a scenario exceeds the per-run wall-clock budget.

    Subclasses BaseException so the resolver's internal ``except Exception``
    handlers cannot swallow the alarm mid-resolve.
    """


def _alarm_handler(_signum: int, _frame: object) -> None:
    msg = f"scenario exceeded {WALL_TIMEOUT_S}s wall-clock budget"
    raise _ScenarioTimeoutError(msg)


def expand_project_extras(
    project_name: str,
    requested_extras: list[str],
    optional_dependencies: dict[str, list[str]],
) -> list[str]:
    canonical_project = canonicalize_name(project_name)
    norm_optional = {canonicalize_name(k): v for k, v in optional_dependencies.items()}
    visited: set[str] = set()
    out: list[str] = []

    def visit(extra: str) -> None:
        norm = canonicalize_name(extra)
        if norm in visited:
            return
        visited.add(norm)
        for dep_str in norm_optional.get(norm, []):
            req = Requirement(dep_str)
            if canonicalize_name(req.name) == canonical_project:
                for sub_extra in sorted(req.extras):
                    visit(sub_extra)
            else:
                out.append(dep_str)

    for extra in requested_extras:
        visit(extra)
    return out


def parse_requirements(
    requirement_strings: list[str],
    *,
    vcs_config: VcsConfig | None = None,
    marker_environment: dict[str, str] | None = None,
) -> dict[str, VersionRange]:
    config = vcs_config or VcsConfig()
    env = _full_marker_environment(marker_environment)
    reqs: dict[str, VersionRange] = {}
    for req_str in requirement_strings:
        req = Requirement(req_str)
        if req.marker is not None and not req.marker.evaluate(env):
            continue
        if req.url is not None:
            admit_vcs_url(req.url, config)
            msg = (
                f"VCS requirement admitted by policy but resolver path is not"
                f" yet implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = canonicalize_name(req.name)
        reqs[name] = reqs.get(name, VersionRange.full()) & req.specifier.to_range()
        for extra in req.extras:
            reqs[f"{name}[{extra}]"] = VersionRange.full()
    return reqs


def _full_marker_environment(
    overlay: dict[str, str] | None,
) -> dict[str, str]:
    env = dict(default_environment())
    if overlay:
        env.update(overlay)
    return env


_PYTHON_VERSION_PARTS = 2


def _scenario_marker_env(
    python_version: str,
    overlay: dict[str, str],
) -> dict[str, str]:
    env = dict(overlay)
    env.setdefault("python_full_version", python_version)
    if "python_version" not in env:
        parts = python_version.split(".")
        env["python_version"] = (
            ".".join(parts[:_PYTHON_VERSION_PARTS])
            if len(parts) >= _PYTHON_VERSION_PARTS
            else python_version
        )
    return env


def parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def run_one(  # noqa: PLR0913 - one wrapper per scenario knob
    requirements: dict[str, VersionRange],
    python_version: str,
    uploaded_prior_to: datetime | None,
    constraints: dict[str, VersionRange] | None,
    marker_environment: dict[str, str] | None = None,
    indexes: list[IndexConfig] | None = None,
    index_routes: list[IndexRoute] | None = None,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    *,
    trust_unverified_sdist_deps: bool = False,
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
            python_version=python_version,
            root_requirements=requirements,
            uploaded_prior_to=uploaded_prior_to,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            package_overrides=package_overrides,
            trust_unverified_sdist_deps=trust_unverified_sdist_deps,
            marker_environment=marker_environment,
        )
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            max_iterations=MAX_ITERATIONS,
        )

        previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(WALL_TIMEOUT_S)
        start = time.monotonic()
        try:
            raw = resolver.resolve(requirements, constraints=constraints)
            elapsed = time.monotonic() - start
            result = {k: v for k, v in raw.items() if split_extra(k)[1] is None}
            success = True
            error = None
            packages = len(result)
        except (_ScenarioTimeoutError, Exception) as exc:
            elapsed = time.monotonic() - start
            success = False
            error = f"{type(exc).__name__}: {exc}"
            packages = 0
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

        rs = resolver.stats
        ps = provider.stats
        return {
            "success": success,
            "error": error,
            "decisions": rs.decisions,
            "conflicts": rs.conflicts,
            "backjumps": rs.backjumps,
            "restarts": rs.restarts,
            "incompatibilities_learned": rs.incompatibilities_learned,
            "metadata_fetched": ps.metadata_fetched,
            "distributions_seen": ps.distributions_seen,
            "look_ahead_rejections": ps.look_ahead_rejections,
            "packages": packages,
            "wall_time_seconds": round(elapsed, 3),
        }


def find_scenario(spec: str) -> dict | None:
    """Locate a scenario by name, or by ``stem:name`` to pin a specific TOML file.

    Many names live in several variant TOMLs (a ``highest`` file plus
    ``-lowest`` / ``-lowest-direct`` siblings) with different decision counts.
    A bare name scans the files in sorted order so the match is reproducible
    across machines, and warns when the name is ambiguous so an exact-count run
    can pin the variant with ``stem:name``.
    """
    if ":" in spec:
        toml_stem, name = spec.split(":", 1)
        toml_path = SCENARIOS_DIR / f"{toml_stem}.toml"
        if not toml_path.is_file():
            return None
        with toml_path.open("rb") as f:
            return tomllib.load(f).get(name)

    matches: list[tuple[str, dict]] = []
    for toml_file in sorted(SCENARIOS_DIR.glob("*.toml")):
        with toml_file.open("rb") as f:
            data = tomllib.load(f)
        if spec in data:
            matches.append((toml_file.stem, data[spec]))
    if not matches:
        return None
    if len(matches) > 1:
        stems = ", ".join(stem for stem, _ in matches)
        print(
            f"  [ambiguous] {spec!r} is in {len(matches)} files ({stems}); "
            f"using {matches[0][0]}. Pin one with '{matches[0][0]}:{spec}'.",
            flush=True,
        )
    return matches[0][1]


def median_run(scenario: dict, runs: int) -> tuple[list[dict], dict]:
    if "unsupported_reason" in scenario:
        return [], {"skipped": scenario["unsupported_reason"]}

    python_version = scenario["python_version"]
    requirement_strings = scenario["requirements"]
    constraint_strings = scenario.get("constraints", [])
    platform_system = scenario.get("platform_system")
    marker_environment_raw = scenario.get("marker_environment", {})
    if not isinstance(marker_environment_raw, dict):
        msg = (
            "marker_environment must be a TOML table of string -> string,"
            f" got {type(marker_environment_raw).__name__}"
        )
        raise TypeError(msg)
    marker_environment: dict[str, str] = {
        str(k): str(v) for k, v in marker_environment_raw.items()
    }
    if platform_system and "platform_system" not in marker_environment:
        marker_environment["platform_system"] = platform_system
    datetime_str = scenario.get("datetime")
    project_name = scenario.get("project_name")
    project_extras = scenario.get("project_extras", [])
    optional_dependencies = scenario.get("optional_dependencies", {})
    vcs_config = VcsConfig(
        policy=VcsPolicy(scenario.get("vcs_policy", "block")),
        allowed_schemes=frozenset(scenario.get("vcs_allowed_schemes", [])),
        allowed_repos=tuple(scenario.get("vcs_allowed_repos", [])),
        require_pin=scenario.get("vcs_require_pin", True),
    )

    if project_name:
        requirement_strings = [
            *requirement_strings,
            *expand_project_extras(project_name, project_extras, optional_dependencies),
        ]

    raw_indexes = scenario.get("indexes")
    if raw_indexes is None:
        indexes = list(DEFAULT_INDEXES)
    else:
        indexes = [
            IndexConfig(name=str(entry["name"]), url=str(entry["url"]))
            for entry in raw_indexes
        ]
    raw_overrides = scenario.get("index_overrides", [])
    index_routes = [
        IndexRoute(
            name=str(entry["name"]),
            index=str(entry["index"]),
            marker=entry.get("marker"),
        )
        for entry in raw_overrides
    ]
    raw_build_packages = scenario.get("build_packages", []) or []
    build_policy_overrides = {
        str(name): BuildPolicy.BUILD_REMOTE for name in raw_build_packages
    }
    if marker_environment and build_policy_overrides:
        # See ``scenarios.py``: marker_environment + BUILD_REMOTE override
        # is rejected to preserve metadata soundness.  Drop the overrides;
        # a resolution that now fails was relying on the previous silent
        # passthrough and needs an audit.
        print(
            f"  [audit] dropping {len(build_policy_overrides)} build_packages"
            " override(s) because of marker_environment overlay.",
            flush=True,
        )
        build_policy_overrides = {}
    requirement_marker_env = _scenario_marker_env(python_version, marker_environment)
    requirements = parse_requirements(
        requirement_strings,
        vcs_config=vcs_config,
        marker_environment=requirement_marker_env,
    )
    constraints = (
        parse_requirements(
            constraint_strings,
            vcs_config=vcs_config,
            marker_environment=requirement_marker_env,
        )
        if constraint_strings
        else None
    )
    uploaded_prior_to = parse_datetime(datetime_str) if datetime_str else None
    # See scenarios.py: trust pre-2.2 sdist PKG-INFO deps by default so the
    # benchmark measures search, not strict PEP 643 sdist rejection.
    trust_unverified_sdist_deps = bool(
        scenario.get("trust_unverified_sdist_deps", True)
    )

    runs_data: list[dict] = [
        run_one(
            requirements,
            python_version,
            uploaded_prior_to,
            constraints,
            marker_environment=marker_environment or None,
            indexes=indexes,
            index_routes=index_routes or None,
            build_policy_overrides=build_policy_overrides or None,
            trust_unverified_sdist_deps=trust_unverified_sdist_deps,
        )
        for _ in range(runs)
    ]

    def med(key: str) -> float:
        vals = [r[key] for r in runs_data if isinstance(r.get(key), (int, float))]
        return statistics.median(vals) if vals else 0

    successes = sum(1 for r in runs_data if r["success"])
    summary = {
        "success_runs": f"{successes}/{len(runs_data)}",
        "median_decisions": int(med("decisions")),
        "median_distributions_seen": int(med("distributions_seen")),
        "median_metadata_fetched": int(med("metadata_fetched")),
        "median_packages": int(med("packages")),
        "median_conflicts": int(med("conflicts")),
        "median_backjumps": int(med("backjumps")),
        "median_wall": round(med("wall_time_seconds"), 2),
        "min_decisions": min(r["decisions"] for r in runs_data),
        "max_decisions": max(r["decisions"] for r in runs_data),
        "min_wall": round(min(r["wall_time_seconds"] for r in runs_data), 2),
        "max_wall": round(max(r["wall_time_seconds"] for r in runs_data), 2),
    }
    return runs_data, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run canary benchmark")
    parser.add_argument("--commit", default=None)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenario", action="append", help="Run only named scenarios")
    parser.add_argument("--scenarios-list", help="File with one scenario per line")
    args = parser.parse_args()

    commit = args.commit or get_git_commit()

    scenarios_to_run: list[str]
    if args.scenarios_list:
        with Path(args.scenarios_list).open(encoding="utf-8") as f:
            scenarios_to_run = [line.strip() for line in f if line.strip()]
    elif args.scenario:
        scenarios_to_run = args.scenario
    else:
        scenarios_to_run = list(CANARY_SCENARIOS)

    out_dir = RESULTS_DIR / commit
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Canary benchmark, commit={commit}, runs={args.runs} ===")
    print(
        f"{'scenario':<45} "
        f"{'success':>9} "
        f"{'med_dec':>8} "
        f"{'med_dist':>10} "
        f"{'med_wall':>10} "
        f"{'min_dec':>8} "
        f"{'max_dec':>8}"
    )
    print("-" * 110)

    summary_all: dict[str, dict] = {}
    for name in scenarios_to_run:
        scenario = find_scenario(name)
        label = name.split(":", 1)[-1]
        if scenario is None:
            print(f"{label:<45} NOT FOUND")
            continue
        runs_data, summary = median_run(scenario, args.runs)
        summary_all[label] = {"runs": runs_data, "summary": summary}

        if "skipped" in summary:
            print(f"{label:<45} SKIPPED: {summary['skipped']}")
            continue

        print(
            f"{label:<45} "
            f"{summary['success_runs']:>9} "
            f"{summary['median_decisions']:>8} "
            f"{summary['median_distributions_seen']:>10} "
            f"{summary['median_wall']:>10} "
            f"{summary['min_decisions']:>8} "
            f"{summary['max_decisions']:>8}"
        )

    out_file = out_dir / f"canary_{int(time.time())}.json"
    out_file.write_text(json.dumps(summary_all, indent=2) + "\n")
    print(f"\nResults: {out_file}")


if __name__ == "__main__":
    main()
