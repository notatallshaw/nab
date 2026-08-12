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
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from nab_python.target import ResolveTarget

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from benchmark_config import (
    benchmark_index_settings,
    build_benchmark_config,
    build_benchmark_provider,
    build_benchmark_resolver_inputs,
    direct_packages_from_requirements,
    parse_scenario_build_packages,
    parse_scenario_index_routes,
    parse_scenario_indexes,
    parse_scenario_project_metadata,
    parse_scenario_requirement_strings,
    parse_scenario_vcs_config,
    parse_trust_unverified_sdist_deps,
    parse_vcs_allowed_repos,
    parse_vcs_allowed_schemes,
    parse_vcs_policy,
    parse_vcs_require_pin,
    validate_scenario_build_policy,
    validate_scenario_settings,
)
from benchmark_datetime import parse_datetime
from benchmark_host import (
    BenchmarkHost,
    BenchmarkTimeout,
    parse_requires_matching_host,
    parse_target_marker_environment,
)

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_python._vcs_admission import admit_vcs_url
from nab_python._vendor.packaging.markers import default_environment
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import NabProjectConfig, index_routes_from_config
from nab_python.fetch import FetchCoordinator
from nab_python.provider import BuildPolicy, ResolutionStrategy, VcsConfig, split_extra
from nab_resolver.resolver import DEFAULT_MAX_ITERATIONS, Resolver

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
RESULTS_DIR = BENCHMARKS_DIR / "canary_results"
CACHE_DIR = BENCHMARKS_DIR / "cache"
CANARY_MANIFEST = BENCHMARKS_DIR / "canary.toml"
CANARY_MANIFEST_SCHEMA = 1
_LEGACY_STRATEGY_SUFFIXES = ("-lowest", "-lowest-direct")


class CanaryCase(NamedTuple):
    """One canonical scenario plus an optional execution-policy override."""

    scenario: str
    resolution: ResolutionStrategy | None


class CanaryV2Identity(NamedTuple):
    """The selector and definition stored under canary contract v2."""

    scenario: str
    definition: dict


class PreparedCanaryExecution(NamedTuple):
    """Validated inputs for one or more runs of a canary scenario."""

    requirements: dict[str, VersionRange]
    constraints: dict[str, VersionRange] | None
    config: NabProjectConfig
    target: ResolveTarget
    host: BenchmarkHost


class CanaryPreparation(NamedTuple):
    """A prepared canary execution, or its host-inapplicable reason."""

    execution: PreparedCanaryExecution | None
    inapplicable_reason: str | None


WALL_TIMEOUT_S = 60
# Preserve contract v2 for comparisons with existing canary results.
CANARY_CONTRACT_VERSION = 2


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
    """Return the contract-v2 identity of an effective canary target."""
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


def _runtime_manifest(host: BenchmarkHost) -> dict[str, str]:
    """Return the contract-v2 runtime fields from the captured host."""
    marker_environment = host.target.marker_env
    return {
        "python": host.python_runtime,
        "implementation": marker_environment["implementation_name"],
        "system": marker_environment["platform_system"],
        "release": marker_environment["platform_release"],
        "machine": marker_environment["platform_machine"],
    }


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


def get_git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=BENCHMARKS_DIR,
    )
    return result.stdout.strip()


def run_one(
    requirements: dict[str, VersionRange],
    constraints: dict[str, VersionRange] | None,
    *,
    config: NabProjectConfig,
    target: ResolveTarget,
    host: BenchmarkHost,
) -> dict:
    direct_packages = direct_packages_from_requirements(requirements)
    inputs = build_benchmark_resolver_inputs(requirements, constraints)
    with FetchCoordinator(
        HttpxAsyncTransport(),
        indexes=list(config.indexes),
        cache_dir=CACHE_DIR,
        index_routes=index_routes_from_config(config),
    ) as coordinator:
        provider = build_benchmark_provider(
            coordinator,
            config=config,
            target=target,
            inputs=inputs,
        )
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
        )

        start = time.monotonic()
        try:
            with host.wall_timeout():
                raw = resolver.resolve(
                    inputs.requirements,
                    constraints=inputs.constraints,
                )
            elapsed = time.monotonic() - start
            result = {k: v for k, v in raw.items() if split_extra(k)[1] is None}
            success = True
            error = None
            packages = len(result)
        except (BenchmarkTimeout, Exception) as exc:
            elapsed = time.monotonic() - start
            success = False
            error = f"{type(exc).__name__}: {exc}"
            packages = 0

        rs = resolver.stats
        ps = provider.stats
        return {
            "settings": {
                "resolution": config.resolution.value,
                "dist_policy": config.dist_policy.value,
                "build_policy": config.build_policy.value,
                "trust_unverified_sdist_deps": config.trust_unverified_sdist_deps,
                "max_iterations": DEFAULT_MAX_ITERATIONS,
                "wall_timeout_seconds": host.wall_timeout_seconds,
                "runtime": _runtime_manifest(host),
                "direct_packages": sorted(direct_packages),
                "target": _target_manifest(target),
                "indexes": benchmark_index_settings(config.indexes),
                "index_routes": [
                    {"name": route.name, "index": route.index}
                    for route in index_routes_from_config(config)
                ],
                "build_policy_overrides": {
                    override.name: override.build_policy.value
                    for override in config.package_overrides
                    if override.build_policy is not None
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


def _validate_selected_execution_fields(
    cases: list[CanaryCase],
    scenarios: list[tuple[str, dict]],
) -> None:
    """Validate marker, build, resolution, and host fields by phase."""
    marker_environments = [
        parse_target_marker_environment(scenario_name, scenario)
        for scenario_name, scenario in scenarios
    ]
    build_policy_overrides: list[dict[str, BuildPolicy]] = [
        parse_scenario_build_packages(scenario_name, scenario)
        for scenario_name, scenario in scenarios
    ]

    for (scenario_name, scenario), marker_environment, overrides in zip(
        scenarios,
        marker_environments,
        build_policy_overrides,
        strict=True,
    ):
        if "unsupported_reason" not in scenario:
            validate_scenario_build_policy(
                scenario_name,
                marker_environment,
                overrides,
            )

    for case, (scenario_name, scenario), marker_environment in zip(
        cases,
        scenarios,
        marker_environments,
        strict=True,
    ):
        scenario_resolution(
            scenario,
            scenario_name=scenario_name,
            override=case.resolution,
        )
        parse_requires_matching_host(
            scenario_name,
            scenario,
            marker_environment,
        )


def select_scenarios(cases: list[CanaryCase]) -> list[CanaryCase]:
    """Validate every selection before a benchmark run or result write."""
    if not cases:
        msg = "at least one scenario must be selected"
        raise _SelectionError(msg)
    missing: list[str] = []
    found_scenarios: list[tuple[str, dict]] = []
    for case in cases:
        scenario = find_scenario(case.scenario)
        if scenario is None:
            missing.append(case.scenario)
            continue
        found_scenarios.append((case.scenario, scenario))
    if missing:
        label = "scenario" if len(missing) == 1 else "scenarios"
        msg = f"{label} not found: {', '.join(repr(spec) for spec in missing)}"
        raise _SelectionError(msg)

    labels = [case.scenario.split(":", 1)[-1] for case in cases]
    duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
    if duplicate_labels:
        msg = (
            "scenario names must be unique across selected files; duplicate(s): "
            + ", ".join(duplicate_labels)
        )
        raise _SelectionError(msg)

    for scenario_name, scenario in found_scenarios:
        validate_scenario_settings(scenario_name, scenario)

    field_validators = (
        parse_trust_unverified_sdist_deps,
        parse_scenario_requirement_strings,
        parse_vcs_require_pin,
        parse_vcs_policy,
        parse_vcs_allowed_schemes,
        parse_vcs_allowed_repos,
        parse_scenario_project_metadata,
    )
    for validate_field in field_validators:
        for scenario_name, scenario in found_scenarios:
            validate_field(scenario_name, scenario)

    parsed_indexes = [
        parse_scenario_indexes(scenario_name, scenario)
        for scenario_name, scenario in found_scenarios
    ]
    for (scenario_name, scenario), indexes in zip(
        found_scenarios,
        parsed_indexes,
        strict=True,
    ):
        parse_scenario_index_routes(scenario_name, scenario, indexes)
    _validate_selected_execution_fields(cases, found_scenarios)
    return cases


def _parse_scenario_selection(
    parser: argparse.ArgumentParser,
    specs: list[str],
) -> list[CanaryCase]:
    try:
        return select_scenarios([parse_canary_case(spec) for spec in specs])
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))


def _parse_default_selection(
    parser: argparse.ArgumentParser,
) -> list[CanaryCase]:
    try:
        return select_scenarios(load_canary_manifest())
    except (TypeError, ValueError) as exc:
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


def _prepare_canary_execution(
    scenario: dict,
    *,
    scenario_name: str,
    resolution_override: ResolutionStrategy | None,
    host: BenchmarkHost,
) -> CanaryPreparation:
    """Validate and prepare a supported canary scenario for this host."""
    validate_scenario_settings(scenario_name, scenario)
    trust_unverified_sdist_deps = parse_trust_unverified_sdist_deps(
        scenario_name,
        scenario,
    )
    requirement_inputs = parse_scenario_requirement_strings(scenario_name, scenario)
    vcs_config = parse_scenario_vcs_config(scenario_name, scenario)
    project_metadata = parse_scenario_project_metadata(scenario_name, scenario)
    indexes = parse_scenario_indexes(scenario_name, scenario)
    index_routes = parse_scenario_index_routes(scenario_name, scenario, indexes)
    python_version = scenario["python_version"]
    requirement_strings = requirement_inputs.requirements
    constraint_strings = requirement_inputs.constraints
    marker_environment = parse_target_marker_environment(scenario_name, scenario)
    build_policy_overrides = parse_scenario_build_packages(scenario_name, scenario)
    validate_scenario_build_policy(
        scenario_name,
        marker_environment,
        build_policy_overrides,
    )

    resolution_strategy = scenario_resolution(
        scenario,
        scenario_name=scenario_name,
        override=resolution_override,
    )

    requires_matching_host = parse_requires_matching_host(
        scenario_name,
        scenario,
        marker_environment,
    )
    admission = host.target_for(
        python_version,
        marker_environment,
        requires_matching_host=requires_matching_host,
    )
    if admission.target is None:
        return CanaryPreparation(None, admission.inapplicable_reason)
    target = admission.target

    project_name = project_metadata.project_name
    if project_name:
        requirement_strings.extend(
            expand_project_extras(
                project_name,
                project_metadata.project_extras,
                project_metadata.optional_dependencies,
            )
        )

    requirement_marker_env = dict(target.marker_env)
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
    datetime_str = scenario.get("datetime")
    config = build_benchmark_config(
        uploaded_prior_to=parse_datetime(datetime_str) if datetime_str else None,
        indexes=indexes,
        index_routes=index_routes,
        build_policy_overrides=build_policy_overrides,
        resolution=resolution_strategy,
        trust_unverified_sdist_deps=trust_unverified_sdist_deps,
        vcs=vcs_config,
    )
    return CanaryPreparation(
        PreparedCanaryExecution(
            requirements=requirements,
            constraints=constraints,
            config=config,
            target=target,
            host=host,
        ),
        None,
    )


def _run_prepared_canary(execution: PreparedCanaryExecution) -> dict:
    return run_one(
        execution.requirements,
        execution.constraints,
        config=execution.config,
        target=execution.target,
        host=execution.host,
    )


def _summarize_runs(runs_data: list[dict]) -> dict[str, object]:
    def median(key: str) -> float:
        values = [
            run[key] for run in runs_data if isinstance(run.get(key), (int, float))
        ]
        return statistics.median(values) if values else 0

    successes = sum(1 for run in runs_data if run["success"])
    return {
        "success_runs": f"{successes}/{len(runs_data)}",
        "median_decisions": int(median("decisions")),
        "median_distributions_seen": int(median("distributions_seen")),
        "median_metadata_fetched": int(median("metadata_fetched")),
        "median_packages": int(median("packages")),
        "median_conflicts": int(median("conflicts")),
        "median_backjumps": int(median("backjumps")),
        "median_wall": round(median("wall_time_seconds"), 2),
        "min_decisions": min(run["decisions"] for run in runs_data),
        "max_decisions": max(run["decisions"] for run in runs_data),
        "min_wall": round(min(run["wall_time_seconds"] for run in runs_data), 2),
        "max_wall": round(max(run["wall_time_seconds"] for run in runs_data), 2),
    }


def median_run(
    scenario: dict,
    runs: int,
    *,
    scenario_name: str = "canary",
    resolution_override: ResolutionStrategy | None = None,
    host: BenchmarkHost | None = None,
) -> tuple[list[dict], dict]:
    if "unsupported_reason" in scenario:
        return [], {"skipped": scenario["unsupported_reason"]}

    effective_host = host or BenchmarkHost.current(WALL_TIMEOUT_S)
    preparation = _prepare_canary_execution(
        scenario,
        scenario_name=scenario_name,
        resolution_override=resolution_override,
        host=effective_host,
    )
    if preparation.execution is None:
        assert preparation.inapplicable_reason is not None
        return [], {"skipped": preparation.inapplicable_reason}

    runs_data = [_run_prepared_canary(preparation.execution) for _ in range(runs)]
    return runs_data, _summarize_runs(runs_data)


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

    host = BenchmarkHost.current(WALL_TIMEOUT_S)
    labels = [case.scenario.split(":", 1)[-1] for case in cases_to_run]

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
            host=host,
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
