"""Quick canary benchmark for fast iteration.

Runs a curated subset of scenarios (canaries + hard cases) N times each
and reports median decisions, distributions seen (search breadth), and
wall time. The set is small enough to finish in a few minutes so it can
be re-run after each algorithm change.

Usage:
    python nab-python/benchmarks/canary.py [--commit LABEL] [--runs N]
        [--scenario STEM:NAME[@STRATEGY]] [--scenarios-list FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

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
    ResolutionStrategy,
    VcsConfig,
    VcsPolicy,
    split_extra,
)
from nab_python.target import ResolveTarget
from nab_resolver.resolver import Resolver

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
RESULTS_DIR = BENCHMARKS_DIR / "canary_results"
CACHE_DIR = BENCHMARKS_DIR / "cache"
CANARY_MANIFEST = BENCHMARKS_DIR / "canary.toml"
CANARY_MANIFEST_SCHEMA = 1
_LEGACY_STRATEGY_SUFFIXES = ("-lowest", "-lowest-direct")
DEFAULT_INDEXES: tuple[IndexConfig, ...] = (
    IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),
)


class CanaryCase(NamedTuple):
    """One canonical scenario plus an optional execution-policy override."""

    scenario: str
    resolution: ResolutionStrategy | None


class CanaryV2Identity(NamedTuple):
    """The selector and definition stored under canary contract v2."""

    scenario: str
    definition: dict


WALL_TIMEOUT_S = 60
MAX_ITERATIONS = 50_000
# Preserve contract v2 for comparisons with existing canary results.
CANARY_CONTRACT_VERSION = 2


class _ScenarioTimeoutError(BaseException):
    """Raised when a scenario exceeds the per-run wall-clock budget.

    Subclasses BaseException so the resolver's internal ``except Exception``
    handlers cannot swallow the alarm mid-resolve.
    """


def scenario_input_hash(scenario_name: str, scenario: dict) -> str:
    """Hash the selected source and its complete executable TOML definition."""
    encoded = json.dumps(
        {"scenario": scenario_name, "definition": scenario},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def scenario_execution_hash(
    input_hash: str,
    effective_settings: dict | None,
) -> str:
    """Hash the source input together with the effective host-dependent policy."""
    encoded = json.dumps(
        {"input_hash": input_hash, "effective_settings": effective_settings},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def get_git_source_state() -> dict[str, str | bool | None]:
    """Describe the checked-out source without treating a label as identity."""
    try:
        commit = get_git_commit()
    except (FileNotFoundError, subprocess.CalledProcessError):
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


def _target_manifest(target: ResolveTarget) -> dict[str, object]:
    tags = [str(tag) for tag in target.tags.ordered]
    return {
        "label": target.label,
        "platform_id": target.platform_id,
        "host_faithful": target.host_faithful,
        "tags_faithful": target.tags_faithful,
        "marker_environment": dict(sorted(target.marker_env.items())),
        "wheel_tags_count": len(tags),
        "wheel_tags_hash": hashlib.sha256("\n".join(tags).encode()).hexdigest(),
    }


def _runtime_manifest() -> dict[str, str]:
    return {
        "python": sys.version,
        "implementation": sys.implementation.name,
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }


def _alarm_handler(_signum: int, _frame: object) -> None:
    msg = f"scenario exceeded {WALL_TIMEOUT_S}s wall-clock budget"
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
    alarm(WALL_TIMEOUT_S)
    try:
        yield
    finally:
        alarm(0)
        signal.signal(sigalrm, previous_handler)


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
        term = (
            req.specifier.to_range()
            if req.specifier
            else VersionRange.full(admit_arbitrary=False)
        )
        reqs[name] = reqs.get(name, VersionRange.full()) & term
        for extra in sorted(req.extras):
            reqs[f"{name}[{extra}]"] = VersionRange.full(admit_arbitrary=False)
    return reqs


def _full_marker_environment(
    overlay: dict[str, str] | None,
) -> dict[str, str]:
    env = dict(default_environment())
    if overlay:
        env.update(overlay)
    return env


_PYTHON_VERSION_PARTS = 2


def _resolve_target(
    python_version: str,
    marker_environment: dict[str, str] | None,
) -> ResolveTarget:
    target = ResolveTarget.for_host_python(python_version)
    return target.with_marker_overrides(marker_environment or {})


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
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=BENCHMARKS_DIR,
    )
    return result.stdout.strip()


def run_one(  # noqa: PLR0913 - one wrapper per scenario knob
    requirements: dict[str, VersionRange],
    python_version: str,
    uploaded_prior_to: datetime | None,
    constraints: dict[str, VersionRange] | None,
    *,
    marker_environment: dict[str, str] | None = None,
    indexes: list[IndexConfig] | None = None,
    index_routes: list[IndexRoute] | None = None,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    resolution_strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
    trust_unverified_sdist_deps: bool = False,
) -> dict:
    direct_packages = frozenset(
        name for name in requirements if split_extra(name)[1] is None
    )
    effective_indexes = list(indexes) if indexes is not None else list(DEFAULT_INDEXES)
    package_overrides = tuple(
        PackageOverride(
            requirement=Requirement(name),
            name=canonicalize_name(name),
            version_range=VersionRange.full(),
            build_policy=policy,
        )
        for name, policy in (build_policy_overrides or {}).items()
    )
    target = _resolve_target(python_version, marker_environment)
    with FetchCoordinator(
        HttpxAsyncTransport(),
        indexes=effective_indexes,
        cache_dir=CACHE_DIR,
        index_routes=index_routes,
    ) as coordinator:
        provider = Provider(
            coordinator,
            target=target,
            root_requirements=requirements,
            uploaded_prior_to=uploaded_prior_to,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            package_overrides=package_overrides,
            trust_unverified_sdist_deps=trust_unverified_sdist_deps,
            resolution_strategy=resolution_strategy,
            direct_packages=direct_packages,
        )
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            max_iterations=MAX_ITERATIONS,
        )

        start = time.monotonic()
        try:
            with _scenario_wall_timeout():
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

        rs = resolver.stats
        ps = provider.stats
        return {
            "settings": {
                "resolution": resolution_strategy.value,
                "dist_policy": DistPolicy.WHEEL_OR_SDIST.value,
                "build_policy": BuildPolicy.NEVER.value,
                "trust_unverified_sdist_deps": trust_unverified_sdist_deps,
                "max_iterations": MAX_ITERATIONS,
                "wall_timeout_seconds": WALL_TIMEOUT_S,
                "runtime": _runtime_manifest(),
                "direct_packages": sorted(direct_packages),
                "target": _target_manifest(target),
                "indexes": [
                    {"name": index.name, "url": index.url}
                    for index in effective_indexes
                ],
                "index_routes": [
                    {"name": route.name, "index": route.index}
                    for route in (index_routes or [])
                ],
                "build_policy_overrides": {
                    name: policy.value
                    for name, policy in sorted((build_policy_overrides or {}).items())
                },
            },
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


_PORTABLE_COMPONENT_START = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)
_PORTABLE_COMPONENT_CHARS = _PORTABLE_COMPONENT_START | frozenset(".-")
_MAX_PORTABLE_COMPONENT_LENGTH = 128
_WINDOWS_RESERVED_COMPONENTS = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


class _SelectionError(ValueError):
    """Raised when benchmark inputs select an unsafe or missing path."""


def load_canary_manifest(path: Path | None = None) -> list[CanaryCase]:
    """Load the strict, ordered default-canary execution manifest."""
    manifest_path = path or CANARY_MANIFEST
    try:
        with manifest_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"cannot read canary manifest {manifest_path}: {exc}"
        raise _SelectionError(msg) from exc

    if set(data) != {"schema", "case"}:
        msg = "canary manifest must contain exactly 'schema' and 'case'"
        raise _SelectionError(msg)
    if type(data["schema"]) is not int or data["schema"] != CANARY_MANIFEST_SCHEMA:
        msg = (
            "canary manifest schema must be "
            f"{CANARY_MANIFEST_SCHEMA}, got {data['schema']!r}"
        )
        raise _SelectionError(msg)
    raw_cases = data["case"]
    if not isinstance(raw_cases, list) or not raw_cases:
        msg = "canary manifest 'case' must be a nonempty array of tables"
        raise _SelectionError(msg)

    cases: list[CanaryCase] = []
    seen_scenarios: set[str] = set()
    duplicate_scenarios: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict) or set(raw_case) != {
            "scenario",
            "resolution",
        }:
            msg = (
                f"canary manifest case {index} must contain exactly"
                " 'scenario' and 'resolution'"
            )
            raise _SelectionError(msg)
        scenario = raw_case["scenario"]
        resolution_raw = raw_case["resolution"]
        if not isinstance(scenario, str) or not scenario:
            msg = f"canary manifest case {index} has an invalid scenario"
            raise _SelectionError(msg)
        try:
            resolution = ResolutionStrategy(resolution_raw)
        except (TypeError, ValueError) as exc:
            valid = sorted(strategy.value for strategy in ResolutionStrategy)
            msg = (
                f"canary manifest case {index} resolution must be one of"
                f" {valid!r}, got {resolution_raw!r}"
            )
            raise _SelectionError(msg) from exc
        cases.append(CanaryCase(scenario, resolution))
        if scenario in seen_scenarios:
            duplicate_scenarios.add(scenario)
        seen_scenarios.add(scenario)
    if duplicate_scenarios:
        msg = "canary manifest contains duplicate scenarios: " + ", ".join(
            sorted(duplicate_scenarios)
        )
        raise _SelectionError(msg)
    return cases


def parse_canary_case(spec: str) -> CanaryCase:
    """Parse ``stem:name`` with an optional explicit ``@strategy`` suffix."""
    scenario, separator, resolution_raw = spec.rpartition("@")
    if not separator:
        return CanaryCase(spec, None)
    if not scenario or not resolution_raw:
        msg = f"invalid canary scenario selection {spec!r}"
        raise _SelectionError(msg)
    try:
        resolution = ResolutionStrategy(resolution_raw)
    except ValueError as exc:
        valid = sorted(strategy.value for strategy in ResolutionStrategy)
        msg = f"canary resolution must be one of {valid!r}, got {resolution_raw!r}"
        raise _SelectionError(msg) from exc
    return CanaryCase(scenario, resolution)


def _retired_selector_replacement(spec: str) -> str | None:
    """Translate a deleted clone selector into the explicit modern spelling."""
    if ":" not in spec:
        return None
    stem, name = spec.split(":", 1)
    for suffix, resolution in (
        ("-lowest-direct", ResolutionStrategy.LOWEST_DIRECT),
        ("-lowest", ResolutionStrategy.LOWEST),
    ):
        if stem.endswith(suffix):
            canonical_stem = stem.removesuffix(suffix)
            return f"{canonical_stem}:{name}@{resolution.value}"
    return None


def _is_portable_path_component(value: str) -> bool:
    """Return whether *value* fits the portable ASCII filename grammar."""
    if (
        not value
        or len(value) > _MAX_PORTABLE_COMPONENT_LENGTH
        or value[0] not in _PORTABLE_COMPONENT_START
        or value.endswith(".")
        or any(char not in _PORTABLE_COMPONENT_CHARS for char in value)
    ):
        return False
    windows_basename = value.partition(".")[0].casefold()
    return windows_basename not in _WINDOWS_RESERVED_COMPONENTS


def _result_directory_label(value: str) -> str:
    """Validate a user-provided results-directory label."""
    if not _is_portable_path_component(value):
        msg = f"commit label must use a portable ASCII filename, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _result_directory(label: str) -> Path:
    """Return a direct, non-symlinked child of the configured results directory."""
    results_dir = RESULTS_DIR.resolve()
    candidate = RESULTS_DIR / label
    if candidate.is_symlink() or candidate.resolve().parent != results_dir:
        msg = f"results directory for {label!r} must be a direct child of RESULTS_DIR"
        raise _SelectionError(msg)
    return candidate


def _scenario_file(toml_stem: str) -> Path:
    """Return a top-level TOML path contained by the scenarios directory."""
    if not _is_portable_path_component(toml_stem):
        msg = (
            f"scenario file stem must use a portable ASCII filename, got {toml_stem!r}"
        )
        raise _SelectionError(msg)
    scenarios_dir = SCENARIOS_DIR.resolve()
    toml_path = (scenarios_dir / f"{toml_stem}.toml").resolve()
    if not toml_path.is_relative_to(scenarios_dir):
        msg = f"scenario file {toml_stem!r} resolves outside SCENARIOS_DIR"
        raise _SelectionError(msg)
    return toml_path


def select_scenarios(cases: list[CanaryCase]) -> list[CanaryCase]:
    """Validate every selection before a benchmark run or result write."""
    if not cases:
        msg = "at least one scenario must be selected"
        raise _SelectionError(msg)
    missing = [case.scenario for case in cases if find_scenario(case.scenario) is None]
    if missing:
        label = "scenario" if len(missing) == 1 else "scenarios"
        msg = f"{label} not found: {', '.join(repr(spec) for spec in missing)}"
        raise _SelectionError(msg)
    return cases


def _parse_scenario_selection(
    parser: argparse.ArgumentParser,
    specs: list[str],
) -> list[CanaryCase]:
    try:
        return select_scenarios([parse_canary_case(spec) for spec in specs])
    except _SelectionError as exc:
        parser.error(str(exc))


def _parse_default_selection(
    parser: argparse.ArgumentParser,
) -> list[CanaryCase]:
    try:
        return select_scenarios(load_canary_manifest())
    except _SelectionError as exc:
        parser.error(str(exc))


def _check_result_directory(
    parser: argparse.ArgumentParser,
    label: str,
) -> None:
    try:
        _result_directory(label)
    except _SelectionError as exc:
        parser.error(str(exc))


def _summary_path(parser: argparse.ArgumentParser, label: str) -> Path:
    try:
        out_dir = _result_directory(label)
    except _SelectionError as exc:
        parser.error(str(exc))
    return out_dir / f"canary_{int(time.time())}.json"


def find_scenario(spec: str) -> dict | None:
    """Locate a canonical scenario by name or qualified ``stem:name``."""
    replacement = _retired_selector_replacement(spec)
    if replacement is not None:
        msg = f"retired strategy-clone selector {spec!r}; use {replacement!r}"
        raise _SelectionError(msg)
    if ":" in spec:
        if (
            len(spec) >= 3
            and spec[0].isalpha()
            and spec[1] == ":"
            and spec[2] in {"/", "\\"}
        ):
            msg = f"scenario file stem must use a portable ASCII filename, got {spec!r}"
            raise _SelectionError(msg)
        toml_stem, name = spec.split(":", 1)
        toml_path = _scenario_file(toml_stem)
        if not toml_path.is_file():
            return None
        with toml_path.open("rb") as f:
            return tomllib.load(f).get(name)

    matches: list[tuple[str, dict]] = []
    entries = sorted(SCENARIOS_DIR.glob("*.toml"))
    clones = [
        entry.name
        for entry in entries
        if entry.stem.endswith(_LEGACY_STRATEGY_SUFFIXES)
    ]
    if clones:
        msg = "legacy strategy-clone scenario files are not supported: " + ", ".join(
            clones
        )
        raise _SelectionError(msg)
    for entry in entries:
        if entry.stem.startswith("universal"):
            continue
        toml_file = _scenario_file(entry.stem)
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


def scenario_resolution(
    scenario: dict,
    *,
    scenario_name: str,
    override: ResolutionStrategy | None = None,
) -> ResolutionStrategy:
    """Return a validated scenario strategy, optionally overridden explicitly."""
    resolution_raw = scenario.get("resolution", ResolutionStrategy.HIGHEST.value)
    try:
        declared = ResolutionStrategy(resolution_raw)
    except (TypeError, ValueError) as exc:
        valid = sorted(strategy.value for strategy in ResolutionStrategy)
        msg = (
            f"{scenario_name}: resolution must be one of {valid!r},"
            f" got {resolution_raw!r}"
        )
        raise ValueError(msg) from exc
    return override or declared


def canary_v2_identity(
    case: CanaryCase,
    scenario: dict,
    resolution: ResolutionStrategy,
) -> CanaryV2Identity:
    """Build the contract-v2 identity for a canonical scenario."""
    if case.resolution is None:
        return CanaryV2Identity(case.scenario, scenario)

    if resolution is ResolutionStrategy.HIGHEST:
        definition = dict(scenario)
        definition.pop("resolution", None)
        return CanaryV2Identity(case.scenario, definition)

    # Preserve the clone TOMLs' field order in the serialized v2 input.
    definition = {"resolution": resolution.value, **scenario}
    if ":" not in case.scenario:
        return CanaryV2Identity(case.scenario, definition)
    stem, name = case.scenario.split(":", 1)
    return CanaryV2Identity(f"{stem}-{resolution.value}:{name}", definition)


def median_run(
    scenario: dict,
    runs: int,
    *,
    scenario_name: str = "canary",
    resolution_override: ResolutionStrategy | None = None,
) -> tuple[list[dict], dict]:
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
    raw_routes = scenario.get("index_routes", [])
    index_routes = [
        IndexRoute(name=str(entry["name"]), index=str(entry["index"]))
        for entry in raw_routes
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
    resolution_strategy = scenario_resolution(
        scenario,
        scenario_name=scenario_name,
        override=resolution_override,
    )
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
            resolution_strategy=resolution_strategy,
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
    parser.add_argument("--commit", type=_result_directory_label, default=None)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--scenario",
        action="append",
        help="Run only STEM:NAME, optionally with an explicit @STRATEGY",
    )
    parser.add_argument("--scenarios-list", help="File with one scenario per line")
    args = parser.parse_args()

    source = get_git_source_state()
    commit = args.commit or str(source["commit"] or "no-git")

    cases_to_run: list[CanaryCase]
    if args.scenarios_list:
        with Path(args.scenarios_list).open(encoding="utf-8") as f:
            cases_to_run = _parse_scenario_selection(
                parser,
                [line.strip() for line in f if line.strip()],
            )
    elif args.scenario:
        cases_to_run = _parse_scenario_selection(parser, args.scenario)
    else:
        cases_to_run = _parse_default_selection(parser)

    labels = [case.scenario.split(":", 1)[-1] for case in cases_to_run]
    duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
    if duplicate_labels:
        parser.error(
            "scenario names must be unique across selected files; duplicate(s): "
            + ", ".join(duplicate_labels)
        )

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
    _check_result_directory(parser, commit)

    summary_all: dict[str, dict] = {}
    for case, label in zip(cases_to_run, labels, strict=True):
        scenario = find_scenario(case.scenario)
        if scenario is None:
            print(f"{label:<45} NOT FOUND")
            continue
        resolution = scenario_resolution(
            scenario,
            scenario_name=label,
            override=case.resolution,
        )
        runs_data, summary = median_run(
            scenario,
            args.runs,
            scenario_name=label,
            resolution_override=resolution,
        )
        identity = canary_v2_identity(case, scenario, resolution)
        input_hash = scenario_input_hash(identity.scenario, identity.definition)
        effective_settings = runs_data[0]["settings"] if runs_data else None
        summary_all[label] = {
            "contract_version": CANARY_CONTRACT_VERSION,
            "scenario": identity.scenario,
            "source": source,
            "input_hash": input_hash,
            "execution_hash": scenario_execution_hash(
                input_hash,
                effective_settings,
            ),
            "input": identity.definition,
            "effective_settings": effective_settings,
            "runs": runs_data,
            "summary": summary,
        }

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

    out_file = _summary_path(parser, commit)
    with out_file.open("x", encoding="utf-8") as f:
        f.write(json.dumps(summary_all, indent=2) + "\n")
    print(f"\nResults: {out_file}")


if __name__ == "__main__":
    main()
