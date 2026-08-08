"""Run resolution scenarios against nab-python and record statistics.

Calls nab-python's Python API directly (no subprocess). Each scenario
is resolved via Provider + Resolver, and the resulting ResolverStats
plus provider I/O metrics are saved as JSON.

Usage:
    python nab-python/benchmarks/scenarios.py [--commit LABEL] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_python.target import ResolveTarget

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from benchmark_host import (
    HOST_TAG_MISMATCH_REASON,
    BenchmarkHost,
    BenchmarkTimeout,
    parse_requires_matching_host,
    parse_target_marker_environment,
    settings_hash,
)

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.multi_index import IndexConfig
from nab_python._vcs_admission import admit_vcs_url
from nab_python._vendor.packaging.markers import default_environment
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name, is_normalized_name
from nab_python._vendor.packaging.version import Version
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
from nab_resolver.resolver import Resolver

BENCHMARKS_DIR = Path(__file__).parent
SCENARIOS_DIR = BENCHMARKS_DIR / "scenarios"
RESULTS_DIR = BENCHMARKS_DIR / "results"
_LEGACY_STRATEGY_SUFFIXES = ("-lowest", "-lowest-direct")
STANDARD_MANIFEST_FILENAME = "_standard_manifest.json"
STANDARD_MANIFEST_SCHEMA = 4
_INAPPLICABLE_KEY_PREVIEW = 8
_STANDARD_METADATA_FILENAMES = frozenset(
    {STANDARD_MANIFEST_FILENAME, "_provenance.json"}
)
DEFAULT_INDEXES: tuple[IndexConfig, ...] = (
    IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),
)
STANDARD_STRATEGIES = (
    ResolutionStrategy.HIGHEST,
    ResolutionStrategy.LOWEST,
    ResolutionStrategy.LOWEST_DIRECT,
)
_LOWER_HEX = frozenset("0123456789abcdef")
_PORTABLE_COMPONENT_START = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)
_PORTABLE_COMPONENT_CHARS = _PORTABLE_COMPONENT_START | frozenset(".-")
_MAX_PORTABLE_COMPONENT_LENGTH = 128
_MAX_STORAGE_COMPONENT_LENGTH = 255
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
_RESERVED_RESULT_DIRECTORIES = frozenset({"universal", "universal-selected"})
_STANDARD_MANIFEST_FIELDS = frozenset(
    {
        "benchmark_schema",
        "commit",
        "source_start",
        "source_end",
        "mode",
        "strategies",
        "settings",
        "corpus_hash",
        "corpus_files",
        "selected_files",
        "available_logical_keys",
        "selected_logical_keys",
        "completed_logical_keys",
        "unsupported_logical_keys",
        "requires_matching_host_logical_keys",
        "inapplicable_logical_keys",
        "available_execution_keys",
        "selected_execution_keys",
        "completed_execution_keys",
        "unsupported_execution_keys",
        "file_execution_keys",
        "complete",
    }
)


def _is_portable_path_component(
    value: str,
    *,
    max_length: int = _MAX_PORTABLE_COMPONENT_LENGTH,
) -> bool:
    """Return whether *value* is a portable single filename component."""
    if (
        not value
        or len(value) > max_length
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
        msg = f"result label must use a portable ASCII filename, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _is_standard_result_directory_component(value: str) -> bool:
    """Return whether *value* can name a standard result directory."""
    return bool(
        _is_portable_path_component(value)
        and not value.casefold().endswith(".json")
        and value.casefold() not in _RESERVED_RESULT_DIRECTORIES
    )


def _is_standard_result_scenario_name(value: str) -> bool:
    """Return whether *value* can produce a standard JSON result filename."""
    filename = f"{value}.json"
    return bool(
        _is_portable_path_component(value)
        and _is_portable_path_component(
            filename,
            max_length=_MAX_STORAGE_COMPONENT_LENGTH,
        )
        and filename.casefold() not in _STANDARD_METADATA_FILENAMES
    )


def _casefold_collisions(values: list[str]) -> list[str]:
    """Return normalized names that collide on case-insensitive filesystems."""
    seen: set[str] = set()
    collisions: set[str] = set()
    for value in values:
        folded = value.casefold()
        if folded in seen:
            collisions.add(folded)
        seen.add(folded)
    return sorted(collisions)


def _result_directory(label: str) -> Path:
    """Return a direct, non-symlinked child of the results directory."""
    if RESULTS_DIR.is_symlink() or (RESULTS_DIR.exists() and not RESULTS_DIR.is_dir()):
        msg = f"RESULTS_DIR must be a real directory: {RESULTS_DIR}"
        raise ValueError(msg)
    results_dir = RESULTS_DIR.resolve()
    candidate = RESULTS_DIR / label
    if (
        candidate.is_symlink()
        or (candidate.exists() and not candidate.is_dir())
        or candidate.resolve().parent != results_dir
    ):
        msg = f"results directory for {label!r} must be a direct, real directory"
        raise ValueError(msg)
    return candidate


def _safe_tree_members(root: Path) -> tuple[list[Path], list[Path]]:
    """Return regular standard members below *root*, rejecting special members."""
    if not root.exists():
        return [], []
    if root.is_symlink() or not root.is_dir():
        msg = f"result namespace must be a real directory: {root}"
        raise ValueError(msg)
    files: list[Path] = []
    directories: list[Path] = []
    pending = [(root, True)]
    while pending:
        directory, top_level = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    msg = f"result namespace contains a symlink: {path}"
                    raise ValueError(msg)
                windows_name = entry.name.rstrip(" .").casefold()
                if (
                    top_level
                    and (
                        windows_name in _RESERVED_RESULT_DIRECTORIES
                        or windows_name == "_provenance.json"
                    )
                    and entry.name != windows_name
                ):
                    msg = f"reserved result name has non-portable casing: {path}"
                    raise ValueError(msg)
                if top_level and entry.name in _RESERVED_RESULT_DIRECTORIES:
                    if not entry.is_dir(follow_symlinks=False):
                        msg = f"reserved result namespace must be a directory: {path}"
                        raise ValueError(msg)
                    continue
                if not _is_portable_path_component(
                    entry.name,
                    max_length=_MAX_STORAGE_COMPONENT_LENGTH,
                ):
                    msg = f"result namespace contains a non-portable member: {path}"
                    raise ValueError(msg)
                if entry.is_dir(follow_symlinks=False):
                    if path.suffix.casefold() == ".json":
                        msg = f"JSON result path must be a regular file: {path}"
                        raise ValueError(msg)
                    directories.append(path)
                    pending.append((path, False))
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
                else:
                    msg = f"result namespace contains a non-regular member: {path}"
                    raise ValueError(msg)
    return files, directories


def _is_standard_json_path(path: Path, output_dir: Path) -> bool:
    relative = path.relative_to(output_dir)
    return bool(
        path.suffix.casefold() == ".json"
        and relative.as_posix() != "_provenance.json"
        and relative.parts[0] not in _RESERVED_RESULT_DIRECTORIES
    )


def _standard_json_paths(output_dir: Path) -> tuple[list[Path], list[Path]]:
    files, directories = _safe_tree_members(output_dir)
    return (
        sorted(path for path in files if _is_standard_json_path(path, output_dir)),
        directories,
    )


def _preflight_standard_result_parents(
    output_dir: Path,
    expected_result_keys: list[str],
    files: list[Path],
    directories: list[Path],
) -> None:
    """Validate every directory needed by the planned standard results."""
    expected_parents: dict[str, str] = {}
    expected_keys: dict[str, str] = {}
    for key in expected_result_keys:
        relative = Path(key)
        canonical = relative.as_posix()
        parts = relative.parts
        scenario_name = (
            parts[1].removesuffix(".json")
            if len(parts) == 2 and parts[1].endswith(".json")
            else ""
        )
        if (
            relative.is_absolute()
            or canonical != key
            or len(parts) != 2
            or not _is_standard_result_directory_component(parts[0])
            or not scenario_name
            or not _is_standard_result_scenario_name(scenario_name)
        ):
            msg = f"unsafe expected standard result key: {key!r}"
            raise ValueError(msg)
        folded_key = canonical.casefold()
        if folded_key in expected_keys:
            previous_key = expected_keys[folded_key]
            msg = (
                "expected standard result keys collide on case-insensitive "
                "filesystems: "
                f"{previous_key!r}, {canonical!r}"
            )
            raise ValueError(msg)
        expected_keys[folded_key] = canonical

        parent = parts[0]
        folded = parent.casefold()
        previous = expected_parents.setdefault(folded, parent)
        if previous != parent:
            msg = (
                "expected standard result directories collide on "
                f"case-insensitive filesystems: {previous!r}, {parent!r}"
            )
            raise ValueError(msg)

    directory_paths = set(directories)
    top_level: dict[str, bool] = {}
    for path in [*files, *directories]:
        relative = path.relative_to(output_dir)
        if len(relative.parts) == 1:
            top_level[relative.parts[0]] = path in directory_paths

    for folded, expected in expected_parents.items():
        aliases = sorted(name for name in top_level if name.casefold() == folded)
        if len(aliases) > 1:
            msg = (
                "result namespace contains case-insensitive path collisions: "
                + ", ".join(aliases)
            )
            raise ValueError(msg)
        if not aliases:
            continue
        actual = aliases[0]
        if actual != expected:
            msg = (
                f"expected standard result directory {expected!r} has "
                f"non-portable casing: {actual!r}"
            )
            raise ValueError(msg)
        if not top_level[actual]:
            parent = output_dir / actual
            msg = f"expected standard result parent must be a directory: {parent}"
            raise ValueError(msg)


def _strict_json_equal(  # noqa: PLR0911 - recursive fail-closed JSON comparator
    left: object,
    right: object,
) -> bool:
    """Compare JSON values without Python's bool/int or int/float coercions."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        assert isinstance(left, dict)
        assert isinstance(right, dict)
        if (
            any(type(key) is not str for key in left)
            or any(type(key) is not str for key in right)
            or set(left) != set(right)
        ):
            return False
        return all(_strict_json_equal(left[key], right[key]) for key in left)
    if type(left) is list:
        assert isinstance(left, list)
        assert isinstance(right, list)
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if type(left) is float:
        assert isinstance(left, float)
        assert isinstance(right, float)
        return math.isfinite(left) and math.isfinite(right) and left == right
    if left is None or type(left) in {str, int, bool}:
        return left == right
    return False


def standard_scenario_files() -> list[Path]:
    """Return canonical single-environment scenarios and reject strategy clones."""
    all_files = sorted(SCENARIOS_DIR.glob("*.toml"))
    standard_files = [
        path for path in all_files if not path.stem.casefold().startswith("universal")
    ]
    invalid = [
        path.name
        for path in standard_files
        if path.is_symlink()
        or not path.is_file()
        or path.resolve().parent != SCENARIOS_DIR.resolve()
        or not _is_standard_result_directory_component(path.stem)
    ]
    if invalid:
        msg = "scenario files must be real top-level portable paths: " + ", ".join(
            invalid
        )
        raise ValueError(msg)
    clones = [
        path.name
        for path in standard_files
        if path.stem.casefold().endswith(_LEGACY_STRATEGY_SUFFIXES)
    ]
    if clones:
        msg = "legacy strategy-clone scenario files are not supported: " + ", ".join(
            clones
        )
        raise ValueError(msg)
    collisions = _casefold_collisions([path.stem for path in standard_files])
    if collisions:
        msg = (
            "scenario file stems collide on case-insensitive filesystems: "
            + ", ".join(collisions)
        )
        raise ValueError(msg)
    return standard_files


class StandardScenario(NamedTuple):
    """One canonical single-environment scenario definition."""

    toml_stem: str
    name: str
    definition: dict

    @property
    def logical_key(self) -> str:
        """Return the stable corpus key independent of execution strategy."""
        return f"{self.toml_stem}:{self.name}"


class StandardExecution(NamedTuple):
    """One scenario and resolution strategy selected for execution."""

    scenario: StandardScenario
    strategy: ResolutionStrategy

    @property
    def result_stem(self) -> str:
        """Return the strategy-specific result-directory stem."""
        if self.strategy is ResolutionStrategy.HIGHEST:
            return self.scenario.toml_stem
        return f"{self.scenario.toml_stem}-{self.strategy.value}"

    @property
    def result_key(self) -> str:
        """Return the result path relative to a labeled run directory."""
        return f"{self.result_stem}/{self.scenario.name}.json"


class StandardRunPlan(NamedTuple):
    """The admitted executions and host-inapplicable scenarios for one run."""

    executions: list[StandardExecution]
    targets_by_logical_key: dict[str, ResolveTarget]
    inapplicable_logical_keys: list[str]


def _standard_result_path(
    output_dir: Path,
    execution: StandardExecution,
) -> Path:
    """Return one safe regular result path inside *output_dir*."""
    if not _is_standard_result_directory_component(
        execution.result_stem
    ) or not _is_standard_result_scenario_name(execution.scenario.name):
        msg = f"unsafe standard result key: {execution.result_key!r}"
        raise ValueError(msg)
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        msg = f"result namespace must be a real directory: {output_dir}"
        raise ValueError(msg)
    resolved_output_dir = output_dir.resolve()
    result_dir = output_dir / execution.result_stem
    if (
        result_dir.is_symlink()
        or (result_dir.exists() and not result_dir.is_dir())
        or result_dir.resolve().parent != resolved_output_dir
    ):
        msg = f"result directory must be a direct, real child: {result_dir}"
        raise ValueError(msg)
    output_path = result_dir / f"{execution.scenario.name}.json"
    if (
        output_path.is_symlink()
        or (output_path.exists() and not output_path.is_file())
        or output_path.resolve().parent != result_dir.resolve()
    ):
        msg = (
            "result path must be a regular file inside its scenario directory: "
            f"{output_path}"
        )
        raise ValueError(msg)
    return output_path


class PreparedStandardExecution(NamedTuple):
    """Validated inputs shared by cache validation and actual resolution."""

    requirement_strings: list[str]
    constraint_strings: list[str]
    indexes: list[IndexConfig]
    index_routes: list[IndexRoute]
    build_policy_overrides: dict[str, BuildPolicy]
    uploaded_prior_to: datetime | None
    vcs_config: VcsConfig
    trust_unverified_sdist_deps: bool
    target: ResolveTarget
    expected_input: dict[str, object]


def load_standard_corpus(files: list[Path]) -> list[StandardScenario]:
    """Load canonical scenarios from *files* in stable declaration order."""
    stem_collisions = _casefold_collisions([path.stem for path in files])
    if stem_collisions:
        msg = (
            "scenario file stems collide on case-insensitive filesystems: "
            + ", ".join(stem_collisions)
        )
        raise ValueError(msg)
    rows: list[StandardScenario] = []
    for path in files:
        generated_stems = [
            path.stem,
            f"{path.stem}-{ResolutionStrategy.LOWEST.value}",
            f"{path.stem}-{ResolutionStrategy.LOWEST_DIRECT.value}",
        ]
        if any(
            not _is_standard_result_directory_component(stem)
            for stem in generated_stems
        ):
            msg = f"scenario file stem must be a portable ASCII filename: {path.stem!r}"
            raise ValueError(msg)
        with path.open("rb") as f:
            scenarios = tomllib.load(f)
        invalid_names = sorted(
            name
            for name in scenarios
            if not isinstance(name, str) or not _is_standard_result_scenario_name(name)
        )
        if invalid_names:
            msg = f"{path.name} has non-portable scenario name(s): " + ", ".join(
                repr(name) for name in invalid_names
            )
            raise ValueError(msg)
        name_collisions = _casefold_collisions(list(scenarios))
        if name_collisions:
            msg = (
                f"{path.name} has case-insensitive scenario-name collisions: "
                + ", ".join(name_collisions)
            )
            raise ValueError(msg)
        declared = sorted(
            name for name, definition in scenarios.items() if "resolution" in definition
        )
        if declared:
            msg = (
                f"{path.name} declares resolution policy for canonical scenario(s): "
                + ", ".join(declared)
            )
            raise ValueError(msg)
        rows.extend(
            StandardScenario(path.stem, name, definition)
            for name, definition in scenarios.items()
        )
    for row in rows:
        marker_environment = parse_marker_environment(row.name, row.definition)
        parse_requires_matching_host(row.name, row.definition, marker_environment)
        build_policy_overrides = parse_build_packages(row.name, row.definition)
        if "unsupported_reason" in row.definition:
            continue
        validate_scenario_build_policy(
            row.name,
            marker_environment,
            build_policy_overrides,
        )
    return rows


def standard_corpus_hash(rows: list[StandardScenario]) -> str:
    """Hash every parsed canonical definition and its stable logical key."""
    encoded = json.dumps(
        {row.logical_key: row.definition for row in rows},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def standard_run_plan(
    rows: list[StandardScenario],
    strategies: tuple[ResolutionStrategy, ...],
    host: BenchmarkHost,
) -> StandardRunPlan:
    """Plan each scenario once, then expand admitted targets by strategy."""
    targets: dict[str, ResolveTarget] = {}
    inapplicable: list[str] = []
    for row in rows:
        if "unsupported_reason" in row.definition:
            continue
        marker_environment = parse_marker_environment(row.name, row.definition)
        requires_matching_host = parse_requires_matching_host(
            row.name,
            row.definition,
            marker_environment,
        )
        admission = host.target_for(
            row.definition["python_version"],
            marker_environment,
            requires_matching_host=requires_matching_host,
        )
        if admission.target is None:
            inapplicable.append(row.logical_key)
            continue
        targets[row.logical_key] = admission.target

    executions = [
        StandardExecution(row, strategy)
        for strategy in strategies
        for row in rows
        if row.logical_key in targets
    ]
    return StandardRunPlan(executions, targets, sorted(inapplicable))


def _report_host_inapplicable(logical_keys: list[str]) -> None:
    """Print a bounded summary of scenarios this host cannot run faithfully."""
    if not logical_keys:
        return
    count = len(logical_keys)
    noun = "scenario" if count == 1 else "scenarios"
    print(f"Host-inapplicable: {count} {noun}; {HOST_TAG_MISMATCH_REASON}.")
    print("  " + ", ".join(logical_keys[:_INAPPLICABLE_KEY_PREVIEW]))
    remaining = count - _INAPPLICABLE_KEY_PREVIEW
    if remaining > 0:
        print(
            f"  ... {remaining} more; exact keys are in {STANDARD_MANIFEST_FILENAME}."
        )


def standard_execution_keys(
    rows: list[StandardScenario],
    strategies: tuple[ResolutionStrategy, ...],
) -> list[str]:
    """Return every logical execution key, including declared unsupported rows."""
    return sorted(
        StandardExecution(row, strategy).result_key
        for strategy in strategies
        for row in rows
    )


def _requires_matching_host_logical_keys(rows: list[StandardScenario]) -> list[str]:
    """Return scenarios whose target must match the physical host."""
    required: list[str] = []
    for row in rows:
        marker_environment = parse_marker_environment(row.name, row.definition)
        if parse_requires_matching_host(
            row.name,
            row.definition,
            marker_environment,
        ):
            required.append(row.logical_key)
    return sorted(required)


def get_git_commit() -> str:
    """Return the short hash of HEAD."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_git_source_state() -> dict[str, str | bool | None]:
    """Return the full Git source identity for a standard benchmark run."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=BENCHMARKS_DIR,
        ).stdout.strip()
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
            object_hash = subprocess.run(  # noqa: S603 - path follows --
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
    """Return the host marker env overlaid with ``overlay``."""
    env = dict(default_environment())
    if overlay:
        env.update(overlay)
    return env


def parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime string to a timezone-aware datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


SCENARIO_WALL_TIMEOUT_SECONDS = 120
MAX_ITERATIONS = 50_000


CACHE_DIR = BENCHMARKS_DIR / "cache"

_STANDARD_COUNTER_FIELDS = frozenset(
    {
        "rounds",
        "decisions",
        "conflicts",
        "derivations",
        "backjumps",
        "restarts",
        "incompatibilities_learned",
        "listings_fetched",
        "metadata_fetched",
        "sdist_pkg_info_fetched",
        "distributions_seen",
        "wheels_seen",
        "sdists_seen",
        "excluded_by_python",
        "excluded_by_time",
        "excluded_by_dist_policy",
        "excluded_by_build_policy",
        "sdist_pyproject_fallbacks",
        "get_dependencies_calls",
        "choose_version_calls",
        "prioritize_calls",
        "look_ahead_rejections",
        "packages_resolved",
    }
)


def standard_benchmark_settings(host: BenchmarkHost) -> dict[str, object]:
    """Return the global settings shared by every standard result."""
    return {
        "dist_policy": DistPolicy.WHEEL_OR_SDIST.value,
        "build_policy": BuildPolicy.NEVER.value,
        "trust_unverified_sdist_deps_default": True,
        "max_iterations": MAX_ITERATIONS,
        "wall_timeout_seconds": host.wall_timeout_seconds,
        "host": host.identity(),
    }


def _clean_source_identity(source: object) -> bool:
    if not isinstance(source, dict) or set(source) != {
        "commit",
        "dirty",
        "diff_hash",
    }:
        return False
    commit = source.get("commit")
    return (
        isinstance(commit, str)
        and len(commit) in {40, 64}
        and all(character in _LOWER_HEX for character in commit)
        and source.get("dirty") is False
        and source.get("diff_hash") is None
    )


def _valid_standard_pins(value: object) -> bool:
    """Return whether *value* is a normalized name-to-version mapping."""
    if not isinstance(value, dict):
        return False
    try:
        return all(
            type(name) is str
            and is_normalized_name(name)
            and type(version) is str
            and str(Version(version)) == version
            for name, version in value.items()
        )
    except ValueError:
        return False


def _standard_result_data_valid(  # noqa: PLR0911 - fail-closed schema validator
    data: object,
    expected_input: dict,
) -> bool:
    if not isinstance(data, dict) or set(data) != {"input", "result", "stats"}:
        return False
    if not _strict_json_equal(data.get("input"), expected_input):
        return False
    result = data.get("result")
    if not isinstance(result, dict) or set(result) != {"success", "error", "pins"}:
        return False
    success = result.get("success")
    error = result.get("error")
    pins = result.get("pins")
    if type(success) is not bool or (success and error is not None):
        return False
    if not success and (not isinstance(error, str) or not error):
        return False
    if not _valid_standard_pins(pins) or (not success and pins):
        return False
    stats = data.get("stats")
    if not isinstance(stats, dict) or set(stats) != _STANDARD_COUNTER_FIELDS | {
        "wall_time_seconds"
    }:
        return False
    if any(type(stats.get(field)) is not int for field in _STANDARD_COUNTER_FIELDS):
        return False
    if any(stats[field] < 0 for field in _STANDARD_COUNTER_FIELDS):
        return False
    if stats["packages_resolved"] != len(pins):
        return False
    wall_time = stats.get("wall_time_seconds")
    return (
        isinstance(wall_time, (int, float))
        and not isinstance(wall_time, bool)
        and math.isfinite(wall_time)
        and wall_time >= 0
    )


def _result_input(
    execution: StandardExecution,
    *,
    commit: str,
    source: dict[str, str | bool | None],
    corpus_hash: str,
    settings_digest: str,
) -> dict[str, object]:
    return {
        "benchmark_schema": STANDARD_MANIFEST_SCHEMA,
        "commit": commit,
        "source": source,
        "corpus_hash": corpus_hash,
        "logical_key": execution.scenario.logical_key,
        "execution_key": execution.result_key,
        "settings_hash": settings_digest,
    }


def _standard_result_files(output_dir: Path) -> list[str]:
    if not output_dir.is_dir():
        return []
    paths, _directories = _standard_json_paths(output_dir)
    files: list[str] = []
    for path in paths:
        key = path.relative_to(output_dir).as_posix()
        if key == STANDARD_MANIFEST_FILENAME:
            continue
        files.append(key)
    return sorted(files)


def _completed_standard_executions(
    output_dir: Path,
    executions: list[StandardExecution],
    prepared_executions: Mapping[str, PreparedStandardExecution],
) -> list[str]:
    completed: list[str] = []
    for execution in executions:
        path = output_dir / execution.result_key
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        prepared = prepared_executions[execution.result_key]
        if _standard_result_data_valid(data, prepared.expected_input):
            completed.append(execution.result_key)
    return sorted(completed)


def standard_manifest_contract(  # noqa: PLR0913 - explicit contract fields
    *,
    commit: str,
    mode: str,
    all_rows: list[StandardScenario],
    selected_rows: list[StandardScenario],
    corpus_files: list[str],
    selected_files: list[str],
    strategies: tuple[ResolutionStrategy, ...],
    corpus_hash: str,
    plan: StandardRunPlan,
    settings: dict[str, object],
) -> dict[str, object]:
    """Return the immutable identity of one standard benchmark run."""
    unsupported_rows = [
        row for row in selected_rows if "unsupported_reason" in row.definition
    ]
    return {
        "benchmark_schema": STANDARD_MANIFEST_SCHEMA,
        "commit": commit,
        "mode": mode,
        "strategies": [strategy.value for strategy in strategies],
        "settings": settings,
        "corpus_hash": corpus_hash,
        "corpus_files": sorted(corpus_files),
        "selected_files": sorted(selected_files),
        "available_logical_keys": sorted(row.logical_key for row in all_rows),
        "selected_logical_keys": sorted(row.logical_key for row in selected_rows),
        "unsupported_logical_keys": sorted(row.logical_key for row in unsupported_rows),
        "requires_matching_host_logical_keys": (
            _requires_matching_host_logical_keys(selected_rows)
        ),
        "inapplicable_logical_keys": plan.inapplicable_logical_keys,
        "available_execution_keys": standard_execution_keys(all_rows, strategies),
        "selected_execution_keys": standard_execution_keys(selected_rows, strategies),
        "unsupported_execution_keys": sorted(
            StandardExecution(row, strategy).result_key
            for row in unsupported_rows
            for strategy in strategies
        ),
    }


def _standard_manifest_matches_contract(
    data: object,
    contract: dict[str, object],
) -> bool:
    """Return whether an existing manifest owns this exact run namespace."""
    return bool(
        isinstance(data, dict)
        and set(data) == _STANDARD_MANIFEST_FIELDS
        and all(
            key in data and _strict_json_equal(data[key], value)
            for key, value in contract.items()
        )
    )


def _clear_standard_result_namespace(
    paths: list[Path],
    directories: list[Path],
) -> None:
    """Remove standard JSON results and now-empty standard directories."""
    for path in paths:
        path.unlink()
    for directory in sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if any(directory.iterdir()):
            continue
        directory.rmdir()


def prepare_standard_result_namespace(
    output_dir: Path,
    contract: dict[str, object],
    expected_result_keys: list[str],
    *,
    force: bool,
) -> None:
    """Validate or safely clear the standard result namespace for one run."""
    files, directories = _safe_tree_members(output_dir)
    paths = sorted(path for path in files if _is_standard_json_path(path, output_dir))
    _preflight_standard_result_parents(
        output_dir,
        expected_result_keys,
        files,
        directories,
    )
    if force:
        _clear_standard_result_namespace(paths, directories)
        return
    if not paths:
        return

    manifest_path = output_dir / STANDARD_MANIFEST_FILENAME
    if manifest_path not in paths:
        msg = (
            f"existing standard results in {output_dir} have no owning manifest; "
            "use --force or a new --commit label"
        )
        raise ValueError(msg)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        msg = (
            f"existing standard manifest is unreadable: {manifest_path}; "
            "use --force or a new --commit label"
        )
        raise ValueError(msg) from exc
    if not _standard_manifest_matches_contract(manifest, contract):
        msg = (
            f"existing standard manifest does not match this run: {manifest_path}; "
            "use --force or a new --commit label"
        )
        raise ValueError(msg)

    expected = set(expected_result_keys)
    unexpected = sorted(
        path.relative_to(output_dir).as_posix()
        for path in paths
        if path != manifest_path
        and path.relative_to(output_dir).as_posix() not in expected
    )
    if unexpected:
        msg = (
            f"existing standard result namespace contains stale files: "
            f"{', '.join(unexpected)}; use --force or a new --commit label"
        )
        raise ValueError(msg)


def _is_exact_partition(whole: list[str], *parts: list[str]) -> bool:
    """Return whether disjoint parts contain every member of whole."""
    combined = [item for part in parts for item in part]
    return len(combined) == len(set(combined)) and sorted(combined) == whole


class StandardCompletionState(NamedTuple):
    """Terminal logical keys, execution keys, and files for one standard run."""

    completed_logical_keys: list[str]
    unsupported_logical_keys: list[str]
    completed_execution_keys: list[str]
    unsupported_execution_keys: list[str]
    inapplicable_execution_keys: list[str]
    file_execution_keys: list[str]


def _standard_completion_state(
    output_dir: Path,
    selected_rows: list[StandardScenario],
    strategies: tuple[ResolutionStrategy, ...],
    plan: StandardRunPlan,
    prepared_executions: Mapping[str, PreparedStandardExecution],
) -> StandardCompletionState:
    """Derive each terminal partition from the selected rows and result files."""
    completed_execution_keys = _completed_standard_executions(
        output_dir,
        plan.executions,
        prepared_executions,
    )
    completed_set = set(completed_execution_keys)
    supported_rows = [
        row
        for row in selected_rows
        if "unsupported_reason" not in row.definition
        and row.logical_key not in plan.inapplicable_logical_keys
    ]
    completed_logical_keys = sorted(
        row.logical_key
        for row in supported_rows
        if all(
            StandardExecution(row, strategy).result_key in completed_set
            for strategy in strategies
        )
    )

    unsupported_rows = [
        row for row in selected_rows if "unsupported_reason" in row.definition
    ]
    unsupported_logical_keys = sorted(row.logical_key for row in unsupported_rows)
    unsupported_execution_keys = sorted(
        StandardExecution(row, strategy).result_key
        for row in unsupported_rows
        for strategy in strategies
    )

    inapplicable_rows = [
        row
        for row in selected_rows
        if row.logical_key in plan.inapplicable_logical_keys
    ]
    inapplicable_execution_keys = sorted(
        StandardExecution(row, strategy).result_key
        for row in inapplicable_rows
        for strategy in strategies
    )
    return StandardCompletionState(
        completed_logical_keys=completed_logical_keys,
        unsupported_logical_keys=unsupported_logical_keys,
        completed_execution_keys=completed_execution_keys,
        unsupported_execution_keys=unsupported_execution_keys,
        inapplicable_execution_keys=inapplicable_execution_keys,
        file_execution_keys=_standard_result_files(output_dir),
    )


def _standard_run_is_complete(
    state: StandardCompletionState,
    selected_rows: list[StandardScenario],
    strategies: tuple[ResolutionStrategy, ...],
    plan: StandardRunPlan,
    source_start: dict[str, str | bool | None],
    source_end: dict[str, str | bool | None] | None,
    *,
    finalize: bool,
) -> bool:
    """Return whether the terminal state satisfies the standard-run contract."""
    selected_logical_keys = sorted(row.logical_key for row in selected_rows)
    selected_execution_keys = standard_execution_keys(selected_rows, strategies)
    expected_execution_keys = sorted(item.result_key for item in plan.executions)
    return bool(
        finalize
        and _clean_source_identity(source_start)
        and _clean_source_identity(source_end)
        and source_end == source_start
        and _is_exact_partition(
            selected_logical_keys,
            state.completed_logical_keys,
            state.unsupported_logical_keys,
            plan.inapplicable_logical_keys,
        )
        and _is_exact_partition(
            selected_execution_keys,
            state.completed_execution_keys,
            state.unsupported_execution_keys,
            state.inapplicable_execution_keys,
        )
        and state.completed_execution_keys == expected_execution_keys
        and state.file_execution_keys == expected_execution_keys
    )


def write_standard_manifest(  # noqa: PLR0913 - explicit contract fields
    path: Path,
    *,
    commit: str,
    source_start: dict[str, str | bool | None],
    source_end: dict[str, str | bool | None] | None,
    mode: str,
    all_rows: list[StandardScenario],
    selected_rows: list[StandardScenario],
    corpus_files: list[str],
    selected_files: list[str],
    strategies: tuple[ResolutionStrategy, ...],
    corpus_hash: str,
    plan: StandardRunPlan,
    settings: dict[str, object],
    prepared_executions: Mapping[str, PreparedStandardExecution],
    finalize: bool,
) -> bool:
    """Write the strict standard-suite contract and return its completeness."""
    output_dir = path.parent
    contract = standard_manifest_contract(
        commit=commit,
        mode=mode,
        all_rows=all_rows,
        selected_rows=selected_rows,
        corpus_files=corpus_files,
        selected_files=selected_files,
        strategies=strategies,
        corpus_hash=corpus_hash,
        plan=plan,
        settings=settings,
    )
    state = _standard_completion_state(
        output_dir,
        selected_rows,
        strategies,
        plan,
        prepared_executions,
    )
    complete = _standard_run_is_complete(
        state,
        selected_rows,
        strategies,
        plan,
        source_start,
        source_end,
        finalize=finalize,
    )
    data = {
        **contract,
        "source_start": source_start,
        "source_end": source_end,
        "completed_logical_keys": state.completed_logical_keys,
        "completed_execution_keys": state.completed_execution_keys,
        "file_execution_keys": state.file_execution_keys,
        "complete": complete,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return complete


def resolve_scenario(  # noqa: PLR0913 - one wrapper per scenario knob
    requirements: dict[str, VersionRange],
    uploaded_prior_to: datetime | None = None,
    constraints: dict[str, VersionRange] | None = None,
    indexes: list[IndexConfig] | None = None,
    index_routes: list[IndexRoute] | None = None,
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    resolution_strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
    *,
    target: ResolveTarget,
    host: BenchmarkHost,
    trust_unverified_sdist_deps: bool = False,
) -> dict:
    """Resolve requirements and return stats dict."""
    direct_packages = frozenset(
        name for name in requirements if split_extra(name)[1] is None
    )
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

        pins: dict[str, str] = {}
        start = time.monotonic()
        try:
            with host.wall_timeout():
                raw = resolver.resolve(requirements, constraints=constraints)
            elapsed = time.monotonic() - start
            pins = dict(
                sorted(
                    (canonicalize_name(name), str(version))
                    for name, version in raw.items()
                    if split_extra(name)[1] is None
                )
            )
            success = True
            error = None
            packages_resolved = len(pins)
        except (BenchmarkTimeout, Exception) as exc:
            elapsed = time.monotonic() - start
            success = False
            error = f"{type(exc).__name__}: {exc}"
            packages_resolved = 0

        rstats = resolver.stats
        pstats = provider.stats
        return {
            "result": {
                "success": success,
                "error": error,
                "pins": pins,
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


def _expected_input(  # noqa: PLR0913, PLR0917 - assembling the JSON dump key
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
    index_routes: list[IndexRoute],
    build_packages: list[str] | None = None,
    resolution_strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
    *,
    trust_unverified_sdist_deps: bool = False,
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
    if index_routes:
        expected_input["index_routes"] = [
            {"name": o.name, "index": o.index} for o in index_routes
        ]
    if build_packages:
        expected_input["build_packages"] = sorted(build_packages)
    if resolution_strategy is not ResolutionStrategy.HIGHEST:
        expected_input["resolution"] = resolution_strategy.value
    if trust_unverified_sdist_deps:
        expected_input["trust_unverified_sdist_deps"] = True
    return expected_input


def parse_index_routes(
    scenario_name: str,
    scenario: dict,
) -> list[IndexRoute]:
    """Read the ``index_routes`` array of records from a scenario.

    Each entry is a TOML inline table with keys ``name`` (the package
    name) and ``index`` (the *name* of an entry in ``indexes``).  A route
    carries no version scope and no marker.  Entries are returned in
    declaration order so :func:`nab_python.fetch._resolve_routes` can
    apply last-match-wins on duplicates.
    """
    raw = scenario.get("index_routes", [])
    if not isinstance(raw, list):
        msg = (
            f"{scenario_name}: index_routes must be a TOML array of"
            f" tables, got {type(raw).__name__}"
        )
        raise TypeError(msg)
    out: list[IndexRoute] = []
    for entry in raw:
        if not isinstance(entry, dict):
            msg = (
                f"{scenario_name}: index_routes entries must be tables,"
                f" got {type(entry).__name__}"
            )
            raise TypeError(msg)
        try:
            name = entry["name"]
            index = entry["index"]
        except KeyError as missing:
            msg = (
                f"{scenario_name}: index_routes entry missing required key {missing!s}"
            )
            raise ValueError(msg) from None
        out.append(IndexRoute(name=str(name), index=str(index)))
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
    return parse_target_marker_environment(scenario_name, scenario)


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


def validate_scenario_build_policy(
    scenario_name: str,
    marker_environment: Mapping[str, str],
    build_policy_overrides: Mapping[str, BuildPolicy],
) -> None:
    """Reject build policy paired with a marker environment overlay."""
    if marker_environment and build_policy_overrides:
        msg = (
            f"{scenario_name}: build_packages cannot be combined "
            "with a marker environment overlay"
        )
        raise ValueError(msg)


def prepare_standard_execution(
    execution: StandardExecution,
    target: ResolveTarget,
    *,
    commit: str,
    source: dict[str, str | bool | None],
    corpus_hash: str,
    settings_digest: str,
) -> PreparedStandardExecution:
    """Prepare and identify one execution without performing network work."""
    scenario_name = execution.scenario.name
    scenario = execution.scenario.definition
    python_version: str = scenario["python_version"]
    requirement_strings: list[str] = list(scenario["requirements"])
    constraint_strings: list[str] = scenario.get("constraints", [])
    marker_environment = parse_marker_environment(scenario_name, scenario)
    indexes = parse_indexes(scenario_name, scenario)
    index_routes = parse_index_routes(scenario_name, scenario)
    build_policy_overrides = dict(parse_build_packages(scenario_name, scenario))
    if "unsupported_reason" not in scenario:
        validate_scenario_build_policy(
            scenario_name,
            marker_environment,
            build_policy_overrides,
        )
    datetime_str: str | None = scenario.get("datetime")
    project_name: str | None = scenario.get("project_name")
    project_extras: list[str] = scenario.get("project_extras", [])
    optional_dependencies: dict[str, list[str]] = scenario.get(
        "optional_dependencies", {}
    )
    if project_name:
        requirement_strings.extend(
            expand_project_extras(project_name, project_extras, optional_dependencies)
        )
    vcs_policy_str: str = scenario.get("vcs_policy", "block")
    vcs_config = VcsConfig(
        policy=VcsPolicy(vcs_policy_str),
        allowed_schemes=frozenset(scenario.get("vcs_allowed_schemes", [])),
        allowed_repos=tuple(scenario.get("vcs_allowed_repos", [])),
        require_pin=scenario.get("vcs_require_pin", True),
    )
    # Search benchmarks accept pre-2.2 PKG-INFO dependency metadata by default;
    # individual strict-policy scenarios opt out explicitly.
    trust_unverified_sdist_deps: bool = scenario.get(
        "trust_unverified_sdist_deps", True
    )
    expected_input = {
        **_expected_input(
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
            index_routes,
            build_packages=sorted(build_policy_overrides),
            resolution_strategy=execution.strategy,
            trust_unverified_sdist_deps=trust_unverified_sdist_deps,
        ),
        **_result_input(
            execution,
            commit=commit,
            source=source,
            corpus_hash=corpus_hash,
            settings_digest=settings_digest,
        ),
    }
    return PreparedStandardExecution(
        requirement_strings=requirement_strings,
        constraint_strings=constraint_strings,
        indexes=indexes,
        index_routes=index_routes,
        build_policy_overrides=build_policy_overrides,
        uploaded_prior_to=parse_datetime(datetime_str) if datetime_str else None,
        vcs_config=vcs_config,
        trust_unverified_sdist_deps=trust_unverified_sdist_deps,
        target=target,
        expected_input=expected_input,
    )


def process_scenario(
    execution: StandardExecution,
    commit: str,
    *,
    force: bool,
    prepared: PreparedStandardExecution,
    host: BenchmarkHost,
) -> None:
    """Resolve one scenario and save results."""
    scenario_name = execution.scenario.name

    run_dir = _result_directory(commit)
    output_path = _standard_result_path(run_dir, execution)
    output_dir = output_path.parent

    if output_path.exists() and not force:
        try:
            existing = json.loads(output_path.read_text())
        except (OSError, ValueError):
            existing = None
        if _standard_result_data_valid(existing, prepared.expected_input):
            return

    print(f"  {scenario_name} ", end="", flush=True)

    requirement_marker_env = dict(prepared.target.marker_env)
    requirements = parse_requirements(
        prepared.requirement_strings,
        vcs_config=prepared.vcs_config,
        marker_environment=requirement_marker_env,
    )
    constraints = (
        parse_requirements(
            prepared.constraint_strings,
            vcs_config=prepared.vcs_config,
            marker_environment=requirement_marker_env,
        )
        if prepared.constraint_strings
        else None
    )
    data = resolve_scenario(
        requirements,
        prepared.uploaded_prior_to,
        constraints,
        indexes=prepared.indexes,
        index_routes=prepared.index_routes or None,
        build_policy_overrides=prepared.build_policy_overrides or None,
        resolution_strategy=execution.strategy,
        target=prepared.target,
        host=host,
        trust_unverified_sdist_deps=prepared.trust_unverified_sdist_deps,
    )
    data["input"] = prepared.expected_input

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run nab resolution scenarios")
    parser.add_argument(
        "--commit",
        type=_result_directory_label,
        default=None,
        help="Label for this run (default: git short hash of HEAD)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear and replace existing standard results for this label",
    )
    parser.add_argument(
        "--strategy-matrix",
        action="store_true",
        help="Run every selected scenario under highest, lowest, and lowest-direct",
    )
    parser.add_argument(
        "--toml",
        action="append",
        help="Restrict the canonical corpus to a TOML stem; may be repeated",
    )
    args = parser.parse_args(argv)

    commit = args.commit
    if commit is None:
        try:
            commit = _result_directory_label(get_git_commit())
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    source_start = get_git_source_state()
    host = BenchmarkHost.current(SCENARIO_WALL_TIMEOUT_SECONDS)

    if not SCENARIOS_DIR.is_dir():
        print(f"Error: {SCENARIOS_DIR} does not exist")
        sys.exit(1)

    try:
        all_files = standard_scenario_files()
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    if not all_files:
        print(f"No scenario files found in {SCENARIOS_DIR}")
        sys.exit(1)

    if args.toml:
        duplicates = sorted({stem for stem in args.toml if args.toml.count(stem) > 1})
        if duplicates:
            parser.error("duplicate TOML stem(s): " + ", ".join(duplicates))
        for stem in args.toml:
            for suffix in ("-lowest-direct", "-lowest"):
                if stem.endswith(suffix):
                    canonical = stem.removesuffix(suffix)
                    parser.error(
                        f"strategy-clone stem {stem!r} was retired; use --toml "
                        f"{canonical} --strategy-matrix"
                    )
        by_stem = {path.stem: path for path in all_files}
        missing = sorted(set(args.toml) - by_stem.keys())
        if missing:
            parser.error("unknown TOML stem(s): " + ", ".join(missing))
        selected_files = [by_stem[stem] for stem in args.toml]
    else:
        selected_files = all_files

    try:
        all_rows = load_standard_corpus(all_files)
    except ValueError as exc:
        parser.error(str(exc))
    selected_stems = {path.stem for path in selected_files}
    selected_rows = [row for row in all_rows if row.toml_stem in selected_stems]
    strategies = (
        STANDARD_STRATEGIES if args.strategy_matrix else (ResolutionStrategy.HIGHEST,)
    )
    mode = "strategy-matrix" if args.strategy_matrix else "default"
    corpus_hash = standard_corpus_hash(all_rows)
    plan = standard_run_plan(selected_rows, strategies, host)
    execution_plan = plan.executions
    settings = standard_benchmark_settings(host)
    settings_digest = settings_hash(settings)
    prepared_executions = {
        execution.result_key: prepare_standard_execution(
            execution,
            plan.targets_by_logical_key[execution.scenario.logical_key],
            commit=commit,
            source=source_start,
            corpus_hash=corpus_hash,
            settings_digest=settings_digest,
        )
        for execution in execution_plan
    }
    contract = standard_manifest_contract(
        commit=commit,
        mode=mode,
        all_rows=all_rows,
        selected_rows=selected_rows,
        corpus_files=[path.stem for path in all_files],
        selected_files=[path.stem for path in selected_files],
        strategies=strategies,
        corpus_hash=corpus_hash,
        plan=plan,
        settings=settings,
    )
    try:
        output_dir = _result_directory(commit)
        prepare_standard_result_namespace(
            output_dir,
            contract,
            sorted(execution.result_key for execution in execution_plan),
            force=args.force,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    manifest_path = output_dir / STANDARD_MANIFEST_FILENAME
    manifest_kwargs = {
        "commit": commit,
        "source_start": source_start,
        "mode": mode,
        "all_rows": all_rows,
        "selected_rows": selected_rows,
        "corpus_files": [path.stem for path in all_files],
        "selected_files": [path.stem for path in selected_files],
        "strategies": strategies,
        "corpus_hash": corpus_hash,
        "plan": plan,
        "settings": settings,
        "prepared_executions": prepared_executions,
    }
    write_standard_manifest(
        manifest_path,
        **manifest_kwargs,
        source_end=None,
        finalize=False,
    )

    print(f"Running scenarios for commit: {commit} ({mode})")
    _report_host_inapplicable(plan.inapplicable_logical_keys)
    previous_stem: str | None = None
    for execution in execution_plan:
        if execution.result_stem != previous_stem:
            print(f"\n--- {execution.result_stem} ---")
            previous_stem = execution.result_stem
        process_scenario(
            execution,
            commit,
            force=args.force,
            prepared=prepared_executions[execution.result_key],
            host=host,
        )

    complete = write_standard_manifest(
        manifest_path,
        **manifest_kwargs,
        source_end=get_git_source_state(),
        finalize=True,
    )
    if not complete:
        print(f"Error: standard benchmark manifest is incomplete: {manifest_path}")
        raise SystemExit(1)

    print(f"\nResults saved to {output_dir}/")


if __name__ == "__main__":
    main()
