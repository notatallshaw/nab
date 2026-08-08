"""Execute uv semantic cross-checks against the deterministic smoke fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlparse
from urllib.request import url2pathname

import deterministic_smoke as smoke_core
from deterministic_smoke import (
    FIXTURE_PATH,
    SCENARIOS_PATH,
    Distribution,
    PreparedScenario,
    Scenario,
    SmokeContractError,
    file_sha256,
    load_fixture,
    load_scenarios,
    materialize_fixture,
    prepare_scenario,
    validate_materialized_fixture,
)

from nab_python._vendor.packaging.markers import InvalidMarker, Marker
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from nab_python._vendor.packaging.utils import (
    InvalidName,
    canonicalize_name,
    parse_wheel_filename,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ADAPTER_PATH = Path(__file__).resolve()
CORE_PATH = Path(smoke_core.__file__).resolve()
UV_PROJECT = ADAPTER_PATH.parent / "smoke" / "uv-lock" / "pyproject.toml"
UV_SUBPROCESS_TIMEOUT_SECONDS = 120
UV_NO_SOLUTION_EXIT_STATUS = 1
_EXECUTION_ENVIRONMENT_ALLOWLIST = (
    "SYSTEMROOT",
    "WINDIR",
)
_CONTROLLED_TEMP_ENVIRONMENT = ("TEMP", "TMP", "TMPDIR")
_UV_PLATFORM = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("win32", "AMD64"): "x86_64-pc-windows-msvc",
}

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


class UvCrossCheckError(Exception):
    """uv did not reproduce the declared deterministic semantic contract."""


@dataclass(frozen=True, slots=True)
class _UvIdentity:
    """Resolved uv executable used throughout one verification."""

    path: Path
    sha256: str
    version: str


@dataclass(frozen=True, slots=True)
class _LockDocument:
    """Parsed lock data and the directory its relative paths use."""

    data: Mapping[str, Any]
    directory: Path


@dataclass(frozen=True, slots=True)
class _UvLockRun:
    """One uv lock result retained while its project directory exists."""

    document: _LockDocument
    command: tuple[str, ...]
    returncode: int


@dataclass(frozen=True, slots=True)
class _UvLockPackages:
    """Project and external package rows from one uv lock."""

    project: Mapping[str, Any]
    external: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _SubprocessPolicy:
    """Fixed subprocess inputs shared by every uv invocation."""

    timeout_seconds: int
    encoding: str
    errors: str
    cwd_policy: str
    environment_allowlist: tuple[str, ...]
    controlled_environment: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    stripped_uv_variables: tuple[str, ...]

    def environment_dict(self, cwd: Path) -> dict[str, str]:
        """Return the minimal child environment for cwd."""
        environment = dict(self.environment)
        environment.update((name, str(cwd)) for name in self.controlled_environment)
        return environment

    def record(self) -> dict[str, object]:
        """Return the report form of this subprocess policy."""
        environment_sha256 = _canonical_sha256(dict(self.environment))
        return {
            "timeout_seconds": self.timeout_seconds,
            "encoding": self.encoding,
            "errors": self.errors,
            "cwd": self.cwd_policy,
            "environment_allowlist": list(self.environment_allowlist),
            "inherited_environment": dict(self.environment),
            "environment_sha256": environment_sha256,
            "controlled_environment": dict.fromkeys(
                self.controlled_environment, "<cwd>"
            ),
            "stripped_uv_variables": list(self.stripped_uv_variables),
        }

    def digest(self) -> str:
        return _canonical_sha256(self.record())


@dataclass(frozen=True, slots=True)
class _ScenarioInputIdentity:
    """Canonical digests for the selected scenario inputs."""

    selected_sha256: str
    scenarios: tuple[tuple[str, str], ...]

    def record(self) -> dict[str, object]:
        """Return the scenario identity fields used by a report."""
        return {
            "selected_scenarios_sha256": self.selected_sha256,
            "scenario_sha256": dict(self.scenarios),
        }


@dataclass(frozen=True, slots=True)
class _InputIdentity:
    """Direct source and scenario inputs captured for one verification."""

    adapter_sha256: str
    core_sha256: str
    scenario_manifest_sha256: str
    fixture_manifest_sha256: str
    fixture_input_sha256: str
    uv_project_sha256: str
    scenarios: _ScenarioInputIdentity

    def record(self) -> dict[str, object]:
        """Return the input identity fields used by a report."""
        return {
            "adapter_sha256": self.adapter_sha256,
            "core_sha256": self.core_sha256,
            "scenario_manifest_sha256": self.scenario_manifest_sha256,
            "fixture_manifest_sha256": self.fixture_manifest_sha256,
            "fixture_input_sha256": self.fixture_input_sha256,
            "uv_project_sha256": self.uv_project_sha256,
            **self.scenarios.record(),
        }


@dataclass(frozen=True, slots=True)
class _VerificationSnapshot:
    """Inputs rechecked after all scenario commands finish."""

    fixture_root: Path
    fixture_sha256: str
    fixture_mode: str
    fixture_access: Mapping[str, object]
    uv: _UvIdentity
    policy: _SubprocessPolicy
    policy_sha256: str
    inputs: _InputIdentity
    scenarios: tuple[Scenario, ...]
    distributions: tuple[Distribution, ...]


def _fail(message: str) -> NoReturn:
    raise UvCrossCheckError(message)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _subprocess_policy() -> _SubprocessPolicy:
    environment = tuple(
        (name, os.environ[name])
        for name in _EXECUTION_ENVIRONMENT_ALLOWLIST
        if name in os.environ
    )
    stripped_uv_variables = tuple(
        sorted(name for name in os.environ if name == "UV" or name.startswith("UV_"))
    )
    return _SubprocessPolicy(
        timeout_seconds=UV_SUBPROCESS_TIMEOUT_SECONDS,
        encoding="utf-8",
        errors="strict",
        cwd_policy="an explicit isolated temporary directory",
        environment_allowlist=_EXECUTION_ENVIRONMENT_ALLOWLIST,
        controlled_environment=_CONTROLLED_TEMP_ENVIRONMENT,
        environment=environment,
        stripped_uv_variables=stripped_uv_variables,
    )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    policy: _SubprocessPolicy,
) -> subprocess.CompletedProcess[str]:
    try:
        resolved_cwd = cwd.resolve(strict=True)
    except OSError as exc:
        _fail(f"cannot use uv working directory {cwd}: {exc}")
    if not resolved_cwd.is_dir():
        _fail(f"uv working directory {resolved_cwd} is not a directory")
    try:
        return subprocess.run(  # noqa: S603 - argv only; no shell invocation
            command,
            capture_output=True,
            check=False,
            cwd=resolved_cwd,
            env=policy.environment_dict(resolved_cwd),
            timeout=policy.timeout_seconds,
            encoding=policy.encoding,
            errors=policy.errors,
        )
    except subprocess.TimeoutExpired:
        _fail(f"{command[0]!r} timed out after {policy.timeout_seconds} seconds")
    except UnicodeDecodeError as exc:
        _fail(f"{command[0]!r} emitted non-UTF-8 output: {exc}")
    except OSError as exc:
        _fail(f"cannot execute {command[0]!r}: {exc}")


def _run_uv_version(
    path: Path, policy: _SubprocessPolicy
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="nab-smoke-uv-version-") as temporary:
        return _run((str(path), "--version"), cwd=Path(temporary), policy=policy)


def _command_record(
    command: Sequence[str], *, fixture_root: Path, work_root: Path
) -> list[str]:
    replacements = (
        (fixture_root.resolve().as_uri(), "file://<fixture>"),
        (str(fixture_root.resolve()), "<fixture>"),
        (str(work_root.resolve()), "<work>"),
    )
    recorded: list[str] = []
    for index, argument in enumerate(command):
        if index == 0:
            recorded.append("<uv>")
            continue
        recorded_argument = argument
        has_redacted_root = False
        for source, replacement in replacements:
            if source not in recorded_argument:
                continue
            recorded_argument = recorded_argument.replace(source, replacement)
            has_redacted_root = True
        if has_redacted_root:
            recorded_argument = recorded_argument.replace("\\", "/")
        recorded.append(recorded_argument)
    return recorded


def _scenario_record(scenario: Scenario) -> dict[str, object]:
    return {
        "id": scenario.id,
        "provenance": scenario.provenance,
        "purpose": scenario.purpose,
        "lane": scenario.lane,
        "outcome": scenario.outcome,
        "warmups": scenario.warmups,
        "batch_size": scenario.batch_size,
        "mode": scenario.mode,
        "requirements": list(scenario.requirements),
        "constraints": list(scenario.constraints),
        "python": scenario.python,
        "platforms": list(scenario.platforms),
        "resolution": (
            scenario.resolution.value if scenario.resolution is not None else None
        ),
        "align_across_targets": scenario.align_across_targets,
        "uv_mapping": scenario.uv_mapping,
        "uv_fork_strategy": scenario.uv_fork_strategy,
        "expected": [
            {
                "target": expected.target,
                "pins": dict(sorted(expected.pins.items())),
            }
            for expected in scenario.expected
        ],
    }


def _scenario_input_identity(
    scenarios: Sequence[Scenario],
) -> _ScenarioInputIdentity:
    records = [_scenario_record(scenario) for scenario in scenarios]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        _fail("selected uv scenario ids must be unique")
    return _ScenarioInputIdentity(
        selected_sha256=_canonical_sha256(records),
        scenarios=tuple(
            (scenario.id, _canonical_sha256(record))
            for scenario, record in zip(scenarios, records, strict=True)
        ),
    )


def _fixture_input_digest(distributions: Sequence[Distribution]) -> str:
    return _canonical_sha256(
        [
            {
                "name": distribution.name,
                "version": distribution.version,
                "dependencies": list(distribution.dependencies),
                "requires_python": distribution.requires_python,
                "provides_extra": list(distribution.provides_extra),
            }
            for distribution in distributions
        ]
    )


def _input_identity(
    scenarios: Sequence[Scenario], distributions: Sequence[Distribution]
) -> _InputIdentity:
    try:
        return _InputIdentity(
            adapter_sha256=file_sha256(ADAPTER_PATH),
            core_sha256=file_sha256(CORE_PATH),
            scenario_manifest_sha256=file_sha256(SCENARIOS_PATH),
            fixture_manifest_sha256=file_sha256(FIXTURE_PATH),
            fixture_input_sha256=_fixture_input_digest(distributions),
            uv_project_sha256=file_sha256(UV_PROJECT),
            scenarios=_scenario_input_identity(scenarios),
        )
    except OSError as exc:
        _fail(f"cannot identify uv comparison inputs: {exc}")


def _uv_platform(environment: Mapping[str, str]) -> str:
    platform = environment.get("sys_platform")
    machine = environment.get("platform_machine")
    if platform is None or machine is None:
        _fail("uv platform mapping requires sys_platform and platform_machine")
    key = (platform, machine)
    try:
        return _UV_PLATFORM[key]
    except KeyError:
        _fail(f"no exact uv platform mapping for {key}")


def _fixture_inventory(fixture_root: Path) -> dict[tuple[str, str], tuple[Path, str]]:
    inventory: dict[tuple[str, str], tuple[Path, str]] = {}
    for wheel in sorted((fixture_root / "packages").glob("*.whl")):
        try:
            name, version, _build, _tags = parse_wheel_filename(wheel.name)
        except ValueError as exc:
            _fail(f"fixture contains an invalid wheel {wheel.name!r}: {exc}")
        key = (str(canonicalize_name(name, validate=True)), str(version))
        if key in inventory:
            _fail(f"fixture contains duplicate wheel artifacts for {key}")
        inventory[key] = (wheel.resolve(), file_sha256(wheel))
    if not inventory:
        _fail("fixture contains no wheels")
    return inventory


def _artifact_path(value: str, label: str, *, document_dir: Path) -> Path:
    if not document_dir.is_absolute():
        _fail(f"{label} document directory is not absolute")

    try:
        path = Path(value)
        if path.is_absolute():
            return path.resolve()

        parsed = urlparse(value)
        if parsed.scheme:
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                _fail(f"{label} is not a local file URL")
            path = Path(url2pathname(parsed.path))
        if "\0" in os.fspath(path):
            _fail(f"{label} has an invalid local path")
        if not path.is_absolute():
            path = document_dir / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        _fail(f"{label} has an invalid local path")


def _validate_artifact(
    package: Mapping[str, Any],
    *,
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
    document_dir: Path,
    pylock: bool,
) -> tuple[str, str]:
    name = package.get("name")
    version = package.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        _fail("uv output contains an unversioned package")
    key = (str(canonicalize_name(name, validate=True)), version)
    expected = inventory.get(key)
    if expected is None:
        _fail(f"uv selected non-fixture package {key}")
    if package.get("sdist") is not None:
        _fail(f"uv selected an sdist for {key}")
    wheels = package.get("wheels")
    if not isinstance(wheels, list) or len(wheels) != 1:
        _fail(f"uv did not retain exactly one fixture wheel for {key}")
    wheel = wheels[0]
    if not isinstance(wheel, dict):
        _fail(f"uv emitted a malformed wheel for {key}")
    location_key = "url" if pylock else "path"
    location = wheel.get(location_key)
    if not isinstance(location, str):
        _fail(f"uv emitted no {location_key} for {key}")
    path = _artifact_path(
        location,
        f"artifact for {key}",
        document_dir=document_dir,
    )
    expected_path, expected_digest = expected
    if path != expected_path or not path.is_file():
        _fail(f"uv artifact for {key} is outside the frozen fixture")
    if pylock:
        hashes = wheel.get("hashes")
        actual_digest = hashes.get("sha256") if isinstance(hashes, dict) else None
    else:
        raw_hash = wheel.get("hash")
        actual_digest = (
            raw_hash.removeprefix("sha256:")
            if isinstance(raw_hash, str) and raw_hash.startswith("sha256:")
            else None
        )
    if actual_digest != expected_digest or file_sha256(path) != expected_digest:
        _fail(f"uv artifact hash for {key} does not match the frozen wheel")
    return key


def _pylock_pins(
    document: _LockDocument,
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
) -> dict[str, str]:
    data = document.data
    if data.get("lock-version") != "1.0" or data.get("created-by") != "uv":
        _fail("uv emitted an unsupported pylock document")
    packages = data.get("packages")
    if not isinstance(packages, list):
        _fail("uv pylock has no packages array")
    pins: dict[str, str] = {}
    for raw_package in packages:
        if not isinstance(raw_package, dict):
            _fail("uv pylock contains a malformed package")
        name, version = _validate_artifact(
            raw_package,
            inventory=inventory,
            document_dir=document.directory,
            pylock=True,
        )
        if name in pins:
            _fail(f"uv emitted {name!r} more than once")
        pins[name] = version
    return pins


def _lock_projection(
    document: Mapping[str, Any],
    marker_environments: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    packages = document.get("package")
    if not isinstance(packages, list):
        _fail("uv.lock has no package array")
    projected = {target: {} for target in marker_environments}
    for raw_package in packages:
        if not isinstance(raw_package, dict):
            _fail("uv.lock contains a malformed package")
        if raw_package.get("source") == {"virtual": "."}:
            continue
        name = raw_package.get("name")
        version = raw_package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            _fail("uv.lock contains an unversioned non-project package")
        canonical = str(canonicalize_name(name, validate=True))
        raw_markers = raw_package.get("resolution-markers")
        if raw_markers is None:
            markers: tuple[Marker, ...] = ()
        elif isinstance(raw_markers, list) and all(
            isinstance(marker, str) for marker in raw_markers
        ):
            markers = tuple(Marker(marker) for marker in raw_markers)
        else:
            _fail(f"uv.lock has malformed resolution markers for {canonical}")
        for target, environment in marker_environments.items():
            if markers and not any(
                marker.evaluate(environment=environment) for marker in markers
            ):
                continue
            if canonical in projected[target]:
                _fail(f"uv.lock selects {canonical!r} twice for {target}")
            projected[target][canonical] = version
    return projected


def _marker_coverage(
    raw_markers: object,
    environments: Mapping[str, Mapping[str, str]],
    label: str,
) -> list[frozenset[str]]:
    if (
        not isinstance(raw_markers, list)
        or not raw_markers
        or not all(isinstance(marker, str) for marker in raw_markers)
    ):
        _fail(f"{label} must be a nonempty string array")
    coverage: list[frozenset[str]] = []
    for raw_marker in raw_markers:
        marker = Marker(raw_marker)
        selected = frozenset(
            target
            for target, environment in environments.items()
            if marker.evaluate(environment=environment)
        )
        if not selected:
            _fail(f"{label} contains a marker outside the declared target domain")
        coverage.append(selected)
    return coverage


def _dependency_names(
    dependencies: object,
    environment: Mapping[str, str],
    selected: Mapping[str, str],
    *,
    fixture_root: Path,
    document_dir: Path,
    label: str,
) -> set[str]:
    if dependencies is None:
        return set()
    if not isinstance(dependencies, list):
        _fail(f"{label} dependencies must be an array")
    names: set[str] = set()
    for raw_dependency in dependencies:
        if not isinstance(raw_dependency, dict):
            _fail(f"{label} contains a malformed dependency")
        raw_marker = raw_dependency.get("marker")
        if raw_marker is not None:
            if not isinstance(raw_marker, str):
                _fail(f"{label} contains a malformed dependency marker")
            if not Marker(raw_marker).evaluate(environment=environment):
                continue
        name = raw_dependency.get("name")
        if not isinstance(name, str):
            _fail(f"{label} contains an unnamed dependency")
        canonical = str(canonicalize_name(name, validate=True))
        if canonical not in selected:
            _fail(f"{label} references unselected dependency {canonical!r}")
        version = raw_dependency.get("version")
        if version is not None and version != selected[canonical]:
            _fail(f"{label} records the wrong version for {canonical!r}")
        source = raw_dependency.get("source")
        if source is not None:
            if not isinstance(source, dict) or not isinstance(
                source.get("registry"), str
            ):
                _fail(f"{label} contains a non-fixture dependency source")
            if (
                _artifact_path(
                    source["registry"],
                    label,
                    document_dir=document_dir,
                )
                != fixture_root.resolve()
            ):
                _fail(f"{label} contains a non-fixture dependency source")
        names.add(canonical)
    return names


def _validate_uv_lock_header(document: Mapping[str, Any], scenario: Scenario) -> None:
    if document.get("version") != 1:
        _fail(f"{scenario.id}: uv emitted an unsupported lock version")
    requires_python = document.get("requires-python")
    if not isinstance(requires_python, str) or SpecifierSet(
        requires_python
    ) != SpecifierSet(scenario.python):
        _fail(f"{scenario.id}: uv lock changed the Python domain")
    options = document.get("options")
    if options is None:
        effective_fork_strategy = "requires-python"
    elif isinstance(options, dict):
        effective_fork_strategy = options.get("fork-strategy", "requires-python")
    else:
        _fail(f"{scenario.id}: uv lock has malformed options")
    if effective_fork_strategy != scenario.uv_fork_strategy:
        _fail(f"{scenario.id}: uv lock did not retain the fork strategy")


def _validate_uv_lock_marker_domain(
    document: Mapping[str, Any],
    scenario: Scenario,
    prepared: PreparedScenario,
) -> dict[str, Mapping[str, str]]:
    environments = {target.label: target.marker_env for target in prepared.targets}
    singleton_domain = {frozenset((target,)) for target in environments}
    resolution_coverage = _marker_coverage(
        document.get("resolution-markers"), environments, "resolution-markers"
    )
    if (
        len(resolution_coverage) != len(environments)
        or set(resolution_coverage) != singleton_domain
    ):
        _fail(f"{scenario.id}: uv lock does not cover each exact target cell once")
    platform_groups: dict[tuple[str, str], set[str]] = {}
    for target, environment in environments.items():
        platform_groups.setdefault(
            (environment["sys_platform"], environment["platform_machine"]), set()
        ).add(target)
    expected_platform_domain = {
        frozenset(targets) for targets in platform_groups.values()
    }
    for key in ("supported-markers", "required-markers"):
        coverage = _marker_coverage(document.get(key), environments, key)
        if len(coverage) != len(expected_platform_domain) or set(coverage) != (
            expected_platform_domain
        ):
            _fail(f"{scenario.id}: {key} differs from the exact platform domain")
    return environments


def _uv_lock_packages(
    document: Mapping[str, Any], scenario: Scenario
) -> _UvLockPackages:
    packages = document.get("package")
    if not isinstance(packages, list):
        _fail("uv.lock has no package array")
    project_packages = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("source") == {"virtual": "."}
    ]
    if len(project_packages) != 1 or project_packages[0].get("name") != (
        "nab-smoke-uv-lock"
    ):
        _fail(f"{scenario.id}: uv lock has a missing or extra project package")
    external_packages: list[Mapping[str, Any]] = []
    for package in packages:
        if package is project_packages[0]:
            continue
        if not isinstance(package, dict):
            _fail("uv.lock contains a malformed package")
        external_packages.append(package)
    return _UvLockPackages(project_packages[0], tuple(external_packages))


def _validate_uv_lock_package_records(
    document: Mapping[str, Any],
    scenario: Scenario,
    prepared: PreparedScenario,
    packages: _UvLockPackages,
    *,
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
    fixture_root: Path,
    document_dir: Path,
) -> None:
    top_markers = set(document["resolution-markers"])
    actual_records: set[tuple[str, str]] = set()
    for package in packages.external:
        raw_markers = package.get("resolution-markers", [])
        if not isinstance(raw_markers, list) or not all(
            isinstance(marker, str) and marker in top_markers for marker in raw_markers
        ):
            _fail(f"{scenario.id}: package markers escape the resolution domain")
        source = package.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("registry"), str):
            _fail(f"{scenario.id}: package has no fixture registry source")
        if (
            _artifact_path(
                source["registry"],
                "package source",
                document_dir=document_dir,
            )
            != fixture_root.resolve()
        ):
            _fail(f"{scenario.id}: package source is outside the fixture")
        record = _validate_artifact(
            package,
            inventory=inventory,
            document_dir=document_dir,
            pylock=False,
        )
        if record in actual_records:
            _fail(f"{scenario.id}: uv lock emitted duplicate artifact records")
        actual_records.add(record)
    expected_records = {
        (name, version)
        for pins in prepared.expected.values()
        for name, version in pins.items()
    }
    if actual_records != expected_records or len(packages.external) != len(
        expected_records
    ):
        _fail(
            f"{scenario.id}: uv lock package records differ:"
            f" {actual_records} != {expected_records}"
        )


def _active_requirement_names(
    requirements: Sequence[str], environment: Mapping[str, str]
) -> set[str]:
    names: set[str] = set()
    for text in requirements:
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate(
            environment=environment
        ):
            names.add(str(canonicalize_name(requirement.name, validate=True)))
    return names


def _packages_for_target(
    packages: Sequence[Mapping[str, Any]], selected: Mapping[str, str]
) -> dict[str, Mapping[str, Any]]:
    by_name: dict[str, Mapping[str, Any]] = {}
    for package in packages:
        canonical = str(canonicalize_name(str(package.get("name")), validate=True))
        if canonical in selected and str(package.get("version")) == selected[canonical]:
            by_name[canonical] = package
    return by_name


def _validate_uv_lock_target_edges(
    scenario: Scenario,
    target: str,
    environment: Mapping[str, str],
    selected: Mapping[str, str],
    packages: _UvLockPackages,
    distributions: Mapping[tuple[str, str], Distribution],
    *,
    fixture_root: Path,
    document_dir: Path,
) -> None:
    actual_root = _dependency_names(
        packages.project.get("dependencies"),
        environment,
        selected,
        fixture_root=fixture_root,
        document_dir=document_dir,
        label=f"{scenario.id}:{target}:project",
    )
    expected_root = _active_requirement_names(scenario.requirements, environment)
    if actual_root != expected_root:
        _fail(f"{scenario.id}:{target}: root dependency edges differ")

    by_name = _packages_for_target(packages.external, selected)
    for parent, version in selected.items():
        distribution = distributions.get((parent, version))
        package = by_name.get(parent)
        if distribution is None or package is None:
            _fail(f"{scenario.id}:{target}: missing selected package {parent!r}")
        expected_edges = _active_requirement_names(
            distribution.dependencies, environment
        )
        actual_edges = _dependency_names(
            package.get("dependencies"),
            environment,
            selected,
            fixture_root=fixture_root,
            document_dir=document_dir,
            label=f"{scenario.id}:{target}:{parent}",
        )
        if actual_edges != expected_edges:
            _fail(
                f"{scenario.id}:{target}:{parent}: dependency edges differ:"
                f" {actual_edges} != {expected_edges}"
            )


def _validate_uv_lock_edges(
    scenario: Scenario,
    distributions: Sequence[Distribution],
    environments: Mapping[str, Mapping[str, str]],
    packages: _UvLockPackages,
    projected: Mapping[str, Mapping[str, str]],
    *,
    fixture_root: Path,
    document_dir: Path,
) -> None:
    fixture_distributions = {
        (str(canonicalize_name(dist.name, validate=True)), dist.version): dist
        for dist in distributions
    }
    for target, environment in environments.items():
        _validate_uv_lock_target_edges(
            scenario,
            target,
            environment,
            projected[target],
            packages,
            fixture_distributions,
            fixture_root=fixture_root,
            document_dir=document_dir,
        )


def _validate_uv_lock(
    document: _LockDocument,
    scenario: Scenario,
    prepared: PreparedScenario,
    distributions: Sequence[Distribution],
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
    fixture_root: Path,
) -> dict[str, dict[str, str]]:
    data = document.data
    _validate_uv_lock_header(data, scenario)
    environments = _validate_uv_lock_marker_domain(data, scenario, prepared)
    packages = _uv_lock_packages(data, scenario)
    _validate_uv_lock_package_records(
        data,
        scenario,
        prepared,
        packages,
        inventory=inventory,
        fixture_root=fixture_root,
        document_dir=document.directory,
    )

    projected = _lock_projection(data, environments)
    if projected != prepared.expected:
        _fail(f"{scenario.id}: uv lock differs: {projected} != {prepared.expected}")
    _validate_uv_lock_edges(
        scenario,
        distributions,
        environments,
        packages,
        projected,
        fixture_root=fixture_root,
        document_dir=document.directory,
    )
    return projected


def _write_lines(path: Path, values: Sequence[str]) -> None:
    path.write_text("\n".join(values) + "\n", encoding="utf-8", newline="\n")


def _pip_compile_command(
    scenario: Scenario,
    prepared: PreparedScenario,
    fixture_root: Path,
    uv: str,
    root: Path,
) -> tuple[list[str], Path]:
    target = prepared.targets[0]
    requirements = root / "requirements.in"
    _write_lines(requirements, scenario.requirements)
    if not scenario.id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in scenario.id
    ):
        _fail(f"{scenario.id!r} is not safe for an output artifact name")
    output = root / f"pylock.{scenario.id}.toml"
    command = [
        uv,
        "pip",
        "compile",
        str(requirements),
        "--format",
        "pylock.toml",
        "--output-file",
        str(output),
        "--default-index",
        fixture_root.resolve().as_uri(),
        "--offline",
        "--no-cache",
        "--no-build",
        "--no-config",
        "--no-python-downloads",
        "--python-version",
        target.marker_env["python_version"],
        "--python-platform",
        _uv_platform(target.marker_env),
        "--no-header",
        "--no-annotate",
    ]
    if scenario.resolution is not None:
        command.extend(("--resolution", scenario.resolution.value))
    if scenario.constraints:
        constraints = root / "constraints.txt"
        _write_lines(constraints, scenario.constraints)
        command.extend(("--constraints", str(constraints)))
    return command, output


def _read_lock_document(
    path: Path,
    scenario_id: str,
    label: str,
) -> _LockDocument:
    """Read one lock document and bind relative paths to its directory."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        directory = path.parent.resolve(strict=True)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _fail(f"{scenario_id}: cannot read {label}: {exc}")
    return _LockDocument(data, directory)


def _read_optional_pylock(path: Path, scenario_id: str) -> _LockDocument | None:
    if not path.is_file():
        return None
    return _read_lock_document(path, scenario_id, "uv pylock")


def _validate_pip_compile_result(
    scenario: Scenario,
    prepared: PreparedScenario,
    completed: subprocess.CompletedProcess[str],
    document: _LockDocument | None,
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
) -> tuple[dict[str, dict[str, str]], str]:
    target = prepared.targets[0]
    if scenario.outcome == "unsatisfiable":
        diagnostics = completed.stdout + completed.stderr
        if completed.returncode != UV_NO_SOLUTION_EXIT_STATUS:
            _fail(
                f"{scenario.id}: expected uv no-solution exit status"
                f" {UV_NO_SOLUTION_EXIT_STATUS}, got {completed.returncode}:"
                f" {diagnostics[-1000:]}"
            )
        if "No solution found" not in diagnostics or document is not None:
            _fail(
                f"{scenario.id}: uv did not report a resolution failure:"
                f" {diagnostics[-1000:]}"
            )
        actual = {target.label: {}}
        classification = "no-solution"
    else:
        if completed.returncode != 0:
            _fail(f"{scenario.id}: uv failed: {completed.stderr[-1000:]}")
        if document is None:
            _fail(f"{scenario.id}: uv did not emit the requested pylock")
        actual = {target.label: _pylock_pins(document, inventory)}
        classification = "success"
    if actual != prepared.expected:
        _fail(f"{scenario.id}: uv pins differ: {actual} != {prepared.expected}")
    return actual, classification


def _verify_pip_compile(
    scenario: Scenario,
    fixture_root: Path,
    uv: str,
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
    policy: _SubprocessPolicy,
) -> dict[str, object]:
    prepared = prepare_scenario(scenario, fixture_root)
    if scenario.uv_mapping != "pip-compile" or len(prepared.targets) != 1:
        _fail(f"{scenario.id}: pip-compile mapping requires exactly one target")
    with tempfile.TemporaryDirectory(prefix="nab-smoke-uv-pip-") as temporary:
        root = Path(temporary)
        command, output = _pip_compile_command(
            scenario, prepared, fixture_root, uv, root
        )
        completed = _run(command, cwd=root, policy=policy)
        command_record = _command_record(
            command, fixture_root=fixture_root, work_root=root
        )
        document = _read_optional_pylock(output, scenario.id)
        actual, classification = _validate_pip_compile_result(
            scenario, prepared, completed, document, inventory
        )
    target = prepared.targets[0]
    return {
        "id": scenario.id,
        "mapping": "pip-compile",
        "outcome": scenario.outcome,
        "resolution": prepared.config.resolution.value,
        "targets": [target.label],
        "pins_per_target": actual,
        "command": command_record,
        "return_status": {
            "code": completed.returncode,
            "classification": classification,
        },
    }


def _uv_lock_command(
    scenario: Scenario,
    fixture_root: Path,
    uv: str,
    project: Path,
) -> list[str]:
    command = [
        uv,
        "lock",
        "--project",
        str(project),
        "--default-index",
        fixture_root.resolve().as_uri(),
        "--offline",
        "--no-cache",
        "--no-build",
        "--no-config",
        "--no-python-downloads",
    ]
    if scenario.resolution is not None:
        command.extend(("--resolution", scenario.resolution.value))
    if scenario.uv_fork_strategy not in (None, "requires-python"):
        command.extend(("--fork-strategy", str(scenario.uv_fork_strategy)))
    return command


def _run_uv_lock(
    scenario: Scenario,
    fixture_root: Path,
    uv: str,
    policy: _SubprocessPolicy,
    *,
    project: Path,
) -> _UvLockRun:
    shutil.copyfile(UV_PROJECT, project / "pyproject.toml")
    command = _uv_lock_command(scenario, fixture_root, uv, project)
    completed = _run(command, cwd=project, policy=policy)
    if completed.returncode != 0:
        _fail(f"{scenario.id}: uv lock failed: {completed.stderr[-1000:]}")
    document = _read_lock_document(project / "uv.lock", scenario.id, "uv.lock")
    command_record = _command_record(
        command, fixture_root=fixture_root, work_root=project
    )
    return _UvLockRun(document, tuple(command_record), completed.returncode)


def _verify_lock(
    scenario: Scenario,
    fixture_root: Path,
    uv: str,
    distributions: Sequence[Distribution],
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
    policy: _SubprocessPolicy,
) -> dict[str, object]:
    prepared = prepare_scenario(scenario, fixture_root)
    if scenario.uv_mapping != "lock" or scenario.uv_fork_strategy is None:
        _fail(f"{scenario.id}: lock mapping requires a fork strategy")
    with tempfile.TemporaryDirectory(prefix="nab-smoke-uv-lock-") as temporary:
        run = _run_uv_lock(
            scenario,
            fixture_root,
            uv,
            policy,
            project=Path(temporary),
        )
        actual = _validate_uv_lock(
            run.document,
            scenario,
            prepared,
            distributions,
            inventory,
            fixture_root,
        )
        return {
            "id": scenario.id,
            "mapping": "lock",
            "fork_strategy": scenario.uv_fork_strategy,
            "outcome": scenario.outcome,
            "resolution": prepared.config.resolution.value,
            "targets": sorted(prepared.expected),
            "pins_per_target": actual,
            "command": list(run.command),
            "project_sha256": file_sha256(UV_PROJECT),
            "return_status": {
                "code": run.returncode,
                "classification": "success",
            },
        }


def _uv_binary(uv: str | Path, policy: _SubprocessPolicy) -> _UvIdentity:
    requested = str(uv)
    try:
        path = Path(shutil.which(requested) or requested).resolve(strict=True)
    except OSError as exc:
        _fail(f"cannot identify the uv binary {requested!r}: {exc}")
    if not path.is_file():
        _fail(f"uv binary {path} is not a regular file")
    try:
        digest = file_sha256(path)
    except OSError as exc:
        _fail(f"cannot hash the uv binary {path}: {exc}")
    completed = _run_uv_version(path, policy)
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version.startswith("uv "):
        _fail(f"cannot identify uv version: {completed.stderr.strip()}")
    return _UvIdentity(path, digest, version)


def _validate_fixture_inventory(
    distributions: Sequence[Distribution],
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
) -> None:
    expected_records = {
        (str(canonicalize_name(dist.name, validate=True)), dist.version)
        for dist in distributions
    }
    if set(inventory) != expected_records:
        _fail("materialized fixture artifacts differ from the fixture manifest")


def _verify_scenario(
    scenario: Scenario,
    fixture_root: Path,
    uv: _UvIdentity,
    distributions: Sequence[Distribution],
    inventory: Mapping[tuple[str, str], tuple[Path, str]],
    policy: _SubprocessPolicy,
) -> dict[str, object]:
    if scenario.uv_mapping == "pip-compile":
        return _verify_pip_compile(
            scenario, fixture_root, str(uv.path), inventory, policy
        )
    if scenario.uv_mapping == "lock":
        return _verify_lock(
            scenario,
            fixture_root,
            str(uv.path),
            distributions,
            inventory,
            policy,
        )
    _fail(f"{scenario.id}: no executable uv mapping")


def _validate_uv_identity(uv: _UvIdentity, policy: _SubprocessPolicy) -> None:
    try:
        digest = file_sha256(uv.path)
    except OSError as exc:
        _fail(f"cannot revalidate the uv binary {uv.path}: {exc}")
    if digest != uv.sha256:
        _fail("uv binary changed during semantic verification")
    completed = _run_uv_version(uv.path, policy)
    if completed.returncode != 0 or completed.stdout.strip() != uv.version:
        _fail("uv version identity changed during semantic verification")


def _validate_input_identity(
    before: _InputIdentity,
    scenarios: Sequence[Scenario],
    distributions: Sequence[Distribution],
) -> None:
    after = _input_identity(scenarios, distributions)
    checks = (
        (before.adapter_sha256, after.adapter_sha256, "uv adapter"),
        (before.core_sha256, after.core_sha256, "deterministic smoke core"),
        (
            before.scenario_manifest_sha256,
            after.scenario_manifest_sha256,
            "scenario manifest",
        ),
        (
            before.fixture_manifest_sha256,
            after.fixture_manifest_sha256,
            "fixture manifest",
        ),
        (
            before.fixture_input_sha256,
            after.fixture_input_sha256,
            "fixture inputs",
        ),
        (before.uv_project_sha256, after.uv_project_sha256, "uv project"),
        (
            before.scenarios.selected_sha256,
            after.scenarios.selected_sha256,
            "selected scenario inputs",
        ),
        (before.scenarios.scenarios, after.scenarios.scenarios, "scenario inputs"),
    )
    for expected, actual, label in checks:
        if actual != expected:
            _fail(f"{label} changed during semantic verification")


def _validate_end_identities(
    snapshot: _VerificationSnapshot,
) -> tuple[str, Mapping[str, object]]:
    _resolved_after, fixture_sha256_after, fixture_access_after = (
        validate_materialized_fixture(
            snapshot.fixture_root,
            snapshot.fixture_sha256,
            mode=snapshot.fixture_mode,
        )
    )
    if fixture_access_after != snapshot.fixture_access:
        _fail("fixture storage changed during uv verification")
    _validate_uv_identity(snapshot.uv, snapshot.policy)
    _validate_input_identity(
        snapshot.inputs, snapshot.scenarios, snapshot.distributions
    )
    if snapshot.policy.digest() != snapshot.policy_sha256:
        _fail("subprocess policy changed during semantic verification")
    return fixture_sha256_after, fixture_access_after


def _verify_scenarios(
    scenarios: Sequence[Scenario],
    fixture_root: Path,
    fixture_sha256: str,
    uv: str | Path,
    distributions: Sequence[Distribution],
    *,
    fixture_mode: str = "caller-materialized",
) -> dict[str, Any]:
    selected_scenarios = tuple(scenarios)
    fixture_distributions = tuple(distributions)
    if not selected_scenarios:
        _fail("at least one uv scenario is required")
    missing_mappings = [
        scenario.id for scenario in selected_scenarios if scenario.uv_mapping is None
    ]
    if missing_mappings:
        _fail(f"scenarios have no executable uv mapping: {missing_mappings}")
    policy = _subprocess_policy()
    policy_sha256 = policy.digest()
    inputs = _input_identity(selected_scenarios, fixture_distributions)
    fixture_root, fixture_sha256, fixture_access = validate_materialized_fixture(
        fixture_root,
        fixture_sha256,
        mode=fixture_mode,
    )
    uv_identity = _uv_binary(uv, policy)
    snapshot = _VerificationSnapshot(
        fixture_root=fixture_root,
        fixture_sha256=fixture_sha256,
        fixture_mode=fixture_mode,
        fixture_access=fixture_access,
        uv=uv_identity,
        policy=policy,
        policy_sha256=policy_sha256,
        inputs=inputs,
        scenarios=selected_scenarios,
        distributions=fixture_distributions,
    )
    inventory = _fixture_inventory(fixture_root)
    _validate_fixture_inventory(fixture_distributions, inventory)
    results = [
        _verify_scenario(
            scenario,
            fixture_root,
            uv_identity,
            fixture_distributions,
            inventory,
            policy,
        )
        for scenario in selected_scenarios
    ]
    fixture_sha256_after, fixture_access_after = _validate_end_identities(snapshot)
    return {
        "schema": 2,
        **inputs.record(),
        "uv": uv_identity.version,
        "uv_binary_path": str(uv_identity.path),
        "uv_binary_sha256": uv_identity.sha256,
        "fixture_sha256": fixture_sha256,
        "fixture_sha256_after": fixture_sha256_after,
        "fixture_mode": fixture_mode,
        "fixture_access_before": fixture_access,
        "fixture_access_after": fixture_access_after,
        "subprocess_policy": policy.record(),
        "subprocess_policy_sha256": policy_sha256,
        "scenarios": results,
    }


def verify_scenarios(
    scenarios: Sequence[Scenario],
    fixture_root: Path,
    fixture_sha256: str,
    uv: str | Path,
    distributions: Sequence[Distribution],
    *,
    fixture_mode: str = "caller-materialized",
) -> dict[str, Any]:
    """Run uv and translate malformed external data into the public error type."""
    try:
        return _verify_scenarios(
            scenarios,
            fixture_root,
            fixture_sha256,
            uv,
            distributions,
            fixture_mode=fixture_mode,
        )
    except (InvalidMarker, InvalidName, InvalidSpecifier, UnicodeDecodeError) as exc:
        _fail(f"invalid uv comparison data ({type(exc).__name__}): {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    try:
        scenarios = load_scenarios()
        requested = set(args.scenario)
        known = {scenario.id for scenario in scenarios}
        if requested - known:
            parser.error(f"unknown --scenario values: {sorted(requested - known)}")
        selected = [
            scenario
            for scenario in scenarios
            if not requested or scenario.id in requested
        ]
        distributions, expected_digest = load_fixture()
        if args.fixture_dir is not None:
            digest = materialize_fixture(
                args.fixture_dir, distributions, expected_digest
            )
            report = verify_scenarios(
                selected,
                args.fixture_dir,
                digest,
                args.uv,
                distributions,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="nab-smoke-uv-index-") as temporary:
                fixture_root = Path(temporary)
                digest = materialize_fixture(
                    fixture_root, distributions, expected_digest
                )
                report = verify_scenarios(
                    selected,
                    fixture_root,
                    digest,
                    args.uv,
                    distributions,
                    fixture_mode="ephemeral-generated",
                )
    except (
        InvalidMarker,
        InvalidName,
        InvalidSpecifier,
        SmokeContractError,
        UnicodeDecodeError,
        UvCrossCheckError,
    ) as exc:
        parser.error(str(exc))
    for result in report["scenarios"]:
        print(f"{result['id']}: {result['outcome']} ({result['mapping']})")
    if args.json is not None:
        args.json.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


if __name__ == "__main__":
    main()
