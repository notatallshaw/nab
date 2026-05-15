"""Run resolution scenarios against nab-python and record statistics.

Calls nab-python's Python API directly (no subprocess). Each scenario
is resolved via Provider + Resolver, and the resulting ResolverStats
plus provider I/O metrics are saved as JSON.

Usage:
    python nab-python/benchmarks/scenarios.py [--commit LABEL] [--force]
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
from nab_python.fetch import (
    DEFAULT_INDEX_NAME,
    DEFAULT_INDEX_URL,
    FetchCoordinator,
    IndexOverride,
)
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    Provider,
    ResolutionStrategy,
    VcsConfig,
    VcsPolicy,
    split_extra,
)
from nab_resolver.resolver import Resolver

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
RESULTS_DIR = BENCHMARKS_DIR / "results"
DEFAULT_INDEXES: tuple[IndexConfig, ...] = (
    IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),
)


def get_git_commit() -> str:
    """Return the short hash of HEAD."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def expand_project_extras(
    project_name: str,
    requested_extras: list[str],
    optional_dependencies: dict[str, list[str]],
) -> list[str]:
    """Flatten the requested project extras into PEP 508 requirement strings.

    Self-references like ``eval_framework[all]`` are resolved by recursing
    on the named extras.  Cycles are broken via a normalized visited set.
    Unknown extra names contribute no deps.
    """
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
    """Convert PEP 508 requirement strings to resolver input.

    Root requirements whose marker evaluates False against
    ``marker_environment`` are dropped, matching pip / uv behaviour:
    a top-level dependency with a marker is an opt-in per env.

    Direct-URL requirements (``pkg @ git+https://...``) are screened by
    :func:`nab_python._vcs_admission.admit_vcs_url`; admission failures raise
    :class:`UnsupportedVcsError`.  Admitted VCS requirements raise
    :class:`NotImplementedError`.
    """
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
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = canonicalize_name(req.name)
        reqs[name] = req.specifier.to_range()
        for extra in req.extras:
            reqs[f"{name}[{extra}]"] = VersionRange.full()
    return reqs


def _full_marker_environment(
    overlay: dict[str, str] | None,
) -> dict[str, str]:
    """Return the host marker env overlaid with ``overlay``."""
    env = dict(default_environment())
    if overlay:
        env.update(overlay)
    return env


_PYTHON_VERSION_PARTS = 2


def _scenario_marker_env(
    python_version: str,
    overlay: dict[str, str],
) -> dict[str, str]:
    """Build the marker environment that root-requirement markers use.

    Folds ``python_version`` into the overlay just like ``Provider``
    does internally so root-level markers see the scenario's Python
    rather than the host's.
    """
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
    """Parse an ISO 8601 datetime string to a timezone-aware datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


SCENARIO_WALL_TIMEOUT_SECONDS = 120


class _ScenarioTimeoutError(Exception):
    """Raised when a scenario exceeds the per-run wall-clock budget."""


def _alarm_handler(_signum: int, _frame: object) -> None:
    msg = f"scenario exceeded {SCENARIO_WALL_TIMEOUT_SECONDS}s wall-clock budget"
    raise _ScenarioTimeoutError(msg)


CACHE_DIR = BENCHMARKS_DIR / "cache"


def resolve_scenario(  # noqa: PLR0913 - one wrapper per scenario knob
    requirements: dict[str, VersionRange],
    python_version: str,
    uploaded_prior_to: datetime | None = None,
    constraints: dict[str, VersionRange] | None = None,
    marker_environment: dict[str, str] | None = None,
    indexes: list[IndexConfig] | None = None,
    index_overrides: list[IndexOverride] | None = None,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    resolution_strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
) -> dict:
    """Resolve requirements and return stats dict."""
    direct_packages = frozenset(
        name for name in requirements if split_extra(name)[1] is None
    )
    with FetchCoordinator(
        HttpxAsyncTransport(),
        indexes=indexes,
        cache_dir=CACHE_DIR,
        index_overrides=index_overrides,
        marker_environment=marker_environment,
    ) as coordinator:
        provider = Provider(
            coordinator,
            python_version=python_version,
            root_requirements=requirements,
            uploaded_prior_to=uploaded_prior_to,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            build_policy_overrides=build_policy_overrides,
            marker_environment=marker_environment,
            resolution_strategy=resolution_strategy,
            direct_packages=direct_packages,
        )
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            max_iterations=50_000,
        )

        previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(SCENARIO_WALL_TIMEOUT_SECONDS)
        start = time.monotonic()
        try:
            raw = resolver.resolve(requirements, constraints=constraints)
            elapsed = time.monotonic() - start
            result = {k: v for k, v in raw.items() if split_extra(k)[1] is None}
            success = True
            error = None
            packages_resolved = len(result)
        except Exception as exc:
            elapsed = time.monotonic() - start
            success = False
            error = f"{type(exc).__name__}: {exc}"
            packages_resolved = 0
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

        rstats = resolver.stats
        pstats = provider.stats
        return {
            "result": {
                "success": success,
                "error": error,
            },
            "stats": {
                "rounds": rstats.rounds,
                "decisions": rstats.decisions,
                "conflicts": rstats.conflicts,
                "derivations": rstats.derivations,
                "backjumps": rstats.backjumps,
                "restarts": rstats.restarts,
                "incompatibilities_learned": rstats.incompatibilities_learned,
                "listings_fetched": pstats.listings_fetched,
                "metadata_fetched": pstats.metadata_fetched,
                "sdist_pkg_info_fetched": pstats.sdist_pkg_info_fetched,
                "distributions_seen": pstats.distributions_seen,
                "wheels_seen": pstats.wheels_seen,
                "sdists_seen": pstats.sdists_seen,
                "excluded_by_python": pstats.excluded_by_python,
                "excluded_by_time": pstats.excluded_by_time,
                "excluded_by_dist_policy": pstats.excluded_by_dist_policy,
                "excluded_by_build_policy": pstats.excluded_by_build_policy,
                "sdist_pyproject_fallbacks": pstats.sdist_pyproject_fallbacks,
                "get_dependencies_calls": pstats.get_dependencies_calls,
                "choose_version_calls": pstats.choose_version_calls,
                "prioritize_calls": pstats.prioritize_calls,
                "look_ahead_rejections": pstats.look_ahead_rejections,
                "packages_resolved": packages_resolved,
                "wall_time_seconds": round(elapsed, 3),
            },
        }


def _expected_input(  # noqa: PLR0913 - assembling the JSON dump key
    commit: str,
    python_version: str,
    requirement_strings: list[str],
    constraint_strings: list[str],
    datetime_str: str | None,
    project_name: str | None,
    project_extras: list[str],
    vcs_config: VcsConfig,
    vcs_policy_str: str,
    marker_environment: dict[str, str],
    indexes: list[IndexConfig],
    index_overrides: list[IndexOverride],
    build_packages: list[str] | None = None,
    resolution_strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
) -> dict:
    """Build the JSON-serialisable ``input`` block describing the scenario."""
    expected_input: dict = {
        "commit": commit,
        "python_version": python_version,
        "requirements": requirement_strings,
    }
    if constraint_strings:
        expected_input["constraints"] = constraint_strings
    if datetime_str:
        expected_input["datetime"] = datetime_str
    if project_name:
        expected_input["project_name"] = project_name
        expected_input["project_extras"] = project_extras
    if vcs_config.policy is not VcsPolicy.BLOCK:
        expected_input["vcs_policy"] = vcs_policy_str
        expected_input["vcs_allowed_schemes"] = sorted(vcs_config.allowed_schemes)
        expected_input["vcs_allowed_repos"] = list(vcs_config.allowed_repos)
        expected_input["vcs_require_pin"] = vcs_config.require_pin
    if marker_environment:
        expected_input["marker_environment"] = dict(sorted(marker_environment.items()))
    if list(indexes) != list(DEFAULT_INDEXES):
        expected_input["indexes"] = [
            {"name": cfg.name, "url": cfg.url} for cfg in indexes
        ]
    if index_overrides:
        expected_input["index_overrides"] = [
            {
                "name": o.name,
                "index": o.index,
                **({"marker": o.marker} if o.marker else {}),
            }
            for o in index_overrides
        ]
    if build_packages:
        expected_input["build_packages"] = sorted(build_packages)
    if resolution_strategy is not ResolutionStrategy.HIGHEST:
        expected_input["resolution"] = resolution_strategy.value
    return expected_input


def parse_index_overrides(
    scenario_name: str,
    scenario: dict,
) -> list[IndexOverride]:
    """Read the ``index_overrides`` array of records from a scenario.

    Each entry is a TOML inline table with keys ``name`` (the package
    name) and ``index`` (the *name* of an entry in ``indexes``), plus
    an optional ``marker``.  Entries are returned in declaration order
    so :func:`nab_python.fetch._resolve_overrides` can apply
    first-match-wins on duplicates.
    """
    raw = scenario.get("index_overrides", [])
    if not isinstance(raw, list):
        msg = (
            f"{scenario_name}: index_overrides must be a TOML array of"
            f" tables, got {type(raw).__name__}"
        )
        raise TypeError(msg)
    out: list[IndexOverride] = []
    for entry in raw:
        if not isinstance(entry, dict):
            msg = (
                f"{scenario_name}: index_overrides entries must be tables,"
                f" got {type(entry).__name__}"
            )
            raise TypeError(msg)
        try:
            name = entry["name"]
            index = entry["index"]
        except KeyError as missing:
            msg = (
                f"{scenario_name}: index_overrides entry missing required"
                f" key {missing!s}"
            )
            raise ValueError(msg) from None
        marker = entry.get("marker")
        out.append(IndexOverride(name=str(name), index=str(index), marker=marker))
    return out


def parse_indexes(scenario_name: str, scenario: dict) -> list[IndexConfig]:
    """Read the ``indexes`` array; default to PyPI when missing.

    Each entry is a TOML inline table with keys ``name`` and ``url``.
    Order is significant.
    """
    raw = scenario.get("indexes")
    if raw is None:
        return list(DEFAULT_INDEXES)
    if not isinstance(raw, list):
        msg = (
            f"{scenario_name}: indexes must be a TOML array of tables,"
            f" got {type(raw).__name__}"
        )
        raise TypeError(msg)
    out: list[IndexConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            msg = (
                f"{scenario_name}: indexes entries must be tables, got"
                f" {type(entry).__name__}"
            )
            raise TypeError(msg)
        try:
            name = entry["name"]
            url = entry["url"]
        except KeyError as missing:
            msg = f"{scenario_name}: indexes entry missing required key {missing!s}"
            raise ValueError(msg) from None
        out.append(IndexConfig(name=str(name), url=str(url)))
    return out


def parse_marker_environment(
    scenario_name: str,
    scenario: dict,
) -> dict[str, str]:
    """Read the ``marker_environment`` table + ``platform_system`` shorthand."""
    raw = scenario.get("marker_environment", {})
    if not isinstance(raw, dict):
        msg = (
            f"{scenario_name}: marker_environment must be a TOML table"
            f" of string -> string, got {type(raw).__name__}"
        )
        raise TypeError(msg)
    overlay: dict[str, str] = {str(k): str(v) for k, v in raw.items()}
    platform_system = scenario.get("platform_system")
    if platform_system and "platform_system" not in overlay:
        overlay["platform_system"] = platform_system
    return overlay


def parse_build_packages(
    scenario_name: str,
    scenario: dict,
) -> Mapping[str, BuildPolicy]:
    """Read ``build_packages`` and lift them to ``BUILD_REMOTE`` overrides.

    ``build_packages`` is a list of canonical package names whose
    sdists may be built (PEP 517 backend invocation against the
    fetched sdist).  The default benchmark policy is
    ``BuildPolicy.NEVER``; entries here override that on a
    per-package basis without affecting any other package in the
    same scenario.

    Use this when the scenario's resolution requires a sdist that
    has no usable wheel and the build is cheap enough to run inside
    the benchmark host.  Native or CUDA-heavy sdists belong in
    ``unsupported.toml`` instead.
    """
    raw = scenario.get("build_packages", [])
    if not isinstance(raw, list):
        msg = (
            f"{scenario_name}: build_packages must be an array of"
            f" package names, got {type(raw).__name__}"
        )
        raise TypeError(msg)
    overrides: dict[str, BuildPolicy] = {}
    for i, name in enumerate(raw):
        if not isinstance(name, str):
            msg = (
                f"{scenario_name}: build_packages[{i}] must be a string,"
                f" got {type(name).__name__}"
            )
            raise TypeError(msg)
        overrides[name] = BuildPolicy.BUILD_REMOTE
    return overrides


def process_scenario(
    scenario_name: str,
    scenario: dict,
    commit: str,
    toml_stem: str,
    *,
    force: bool,
) -> None:
    """Resolve one scenario and save results."""
    if "unsupported_reason" in scenario:
        # The scenario lives in scenarios/ for documentation but
        # depends on a feature nab does not implement yet. Skip it.
        return

    python_version: str = scenario["python_version"]
    requirement_strings: list[str] = scenario["requirements"]
    constraint_strings: list[str] = scenario.get("constraints", [])
    marker_environment = parse_marker_environment(scenario_name, scenario)
    indexes = parse_indexes(scenario_name, scenario)
    index_overrides = parse_index_overrides(scenario_name, scenario)
    build_policy_overrides = parse_build_packages(scenario_name, scenario)
    if marker_environment and build_policy_overrides:
        # BuildPolicy.BUILD_REMOTE + marker_environment is rejected at
        # provider construction (host backend cannot reflect the
        # impersonated target).  Drop the per-package builds so the
        # scenario still runs; if resolution now fails the override
        # was load-bearing and the scenario needs an audit.
        print(
            f"\n  [audit] {scenario_name}: dropping {len(build_policy_overrides)}"
            " build_packages override(s) because the scenario uses a"
            " marker_environment overlay (host-built metadata cannot"
            " reflect the impersonated target). Resolution may now skip"
            " versions previously accepted via silent passthrough.",
            flush=True,
        )
        build_policy_overrides = {}
    datetime_str: str | None = scenario.get("datetime")
    project_name: str | None = scenario.get("project_name")
    project_extras: list[str] = scenario.get("project_extras", [])
    resolution_raw: str = scenario.get("resolution", "highest")
    try:
        resolution_strategy = ResolutionStrategy(resolution_raw)
    except ValueError as exc:
        valid = sorted(s.value for s in ResolutionStrategy)
        msg = (
            f"{scenario_name}: resolution must be one of {valid!r},"
            f" got {resolution_raw!r}"
        )
        raise ValueError(msg) from exc
    optional_dependencies: dict[str, list[str]] = scenario.get(
        "optional_dependencies", {}
    )
    vcs_policy_str: str = scenario.get("vcs_policy", "block")
    vcs_config = VcsConfig(
        policy=VcsPolicy(vcs_policy_str),
        allowed_schemes=frozenset(scenario.get("vcs_allowed_schemes", [])),
        allowed_repos=tuple(scenario.get("vcs_allowed_repos", [])),
        require_pin=scenario.get("vcs_require_pin", True),
    )

    if project_name:
        requirement_strings = [
            *requirement_strings,
            *expand_project_extras(project_name, project_extras, optional_dependencies),
        ]

    uploaded_prior_to = parse_datetime(datetime_str) if datetime_str else None

    output_dir = RESULTS_DIR / commit / toml_stem
    output_path = output_dir / f"{scenario_name}.json"

    expected_input = _expected_input(
        commit,
        python_version,
        requirement_strings,
        constraint_strings,
        datetime_str,
        project_name,
        project_extras,
        vcs_config,
        vcs_policy_str,
        marker_environment,
        indexes,
        index_overrides,
        build_packages=sorted(build_policy_overrides),
        resolution_strategy=resolution_strategy,
    )

    if output_path.exists() and not force:
        existing = json.loads(output_path.read_text())
        if existing.get("input") == expected_input:
            return

    print(f"  {scenario_name} ", end="", flush=True)

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
    data = resolve_scenario(
        requirements,
        python_version,
        uploaded_prior_to,
        constraints,
        marker_environment=marker_environment or None,
        indexes=indexes,
        index_overrides=index_overrides or None,
        build_policy_overrides=build_policy_overrides or None,
        resolution_strategy=resolution_strategy,
    )
    data["input"] = expected_input

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2) + "\n")

    stats = data["stats"]
    if data["result"]["success"]:
        print(
            f"ok ({stats['packages_resolved']} pkgs, "
            f"{stats['decisions']} decisions, "
            f"{stats['conflicts']} conflicts, "
            f"{stats['wall_time_seconds']}s)"
        )
    else:
        print(f"FAILED: {data['result']['error']}")


def process_toml_file(toml_file: Path, commit: str, *, force: bool) -> None:
    """Process all scenarios in a TOML file."""
    with toml_file.open("rb") as f:
        scenarios = tomllib.load(f)

    print(f"\n--- {toml_file.stem} ---")
    for scenario_name, scenario in scenarios.items():
        process_scenario(scenario_name, scenario, commit, toml_file.stem, force=force)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run nab resolution scenarios")
    parser.add_argument(
        "--commit",
        default=None,
        help="Label for this run (default: git short hash of HEAD)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing results",
    )
    args = parser.parse_args()

    commit = args.commit or get_git_commit()

    if not SCENARIOS_DIR.is_dir():
        print(f"Error: {SCENARIOS_DIR} does not exist")
        sys.exit(1)

    toml_files = sorted(
        f for f in SCENARIOS_DIR.glob("*.toml") if not f.stem.startswith("universal")
    )
    if not toml_files:
        print(f"No scenario files found in {SCENARIOS_DIR}")
        sys.exit(1)

    print(f"Running scenarios for commit: {commit}")
    for toml_file in toml_files:
        process_toml_file(toml_file, commit, force=args.force)

    print(f"\nResults saved to {RESULTS_DIR / commit}/")


if __name__ == "__main__":
    main()
