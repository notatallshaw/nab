"""Contracts for the canonical benchmark corpus and explicit strategy matrix."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from operator import setitem
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Literal
from unittest.mock import MagicMock

import pytest

from nab_index.client import WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.tags import Tag
from nab_python.target import ResolveTarget

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
_STANDARD_FILES = 14
_STANDARD_SCENARIOS = 558
_RUNNABLE_SCENARIOS = 536
_UNSUPPORTED_SCENARIOS = 22
_TOTAL_EXECUTION_IDENTITIES = 1_674
_MARKER_BUILD_SCENARIOS = {
    "ai-stack:llama-index-experimental-gpt5",
    "ai-stack:open-r1",
    "forums:so-gluonts-mxnet-pin-68451898",
    "pip:pip-11760-torchgeo-min",
    "pip:pip-11760-torchgeo-nbconvert-pin",
    "pip:pip-9572-textract-pypdf2",
    "uv:uv-issue-13321-axolotl-stack",
}
_MATCHING_HOST_MARKERS = {
    "uv:uv-tensorflow-macos": {
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": "arm64",
        "platform_python_implementation": "CPython",
        "platform_system": "Darwin",
        "sys_platform": "darwin",
    },
    "uv:pywin32-windows-amd64-real-index": {
        "implementation_name": "cpython",
        "os_name": "nt",
        "platform_machine": "AMD64",
        "platform_python_implementation": "CPython",
        "platform_system": "Windows",
        "sys_platform": "win32",
    },
}
_CLEAN_SOURCE = {"commit": "a" * 40, "dirty": False, "diff_hash": None}
_MANIFEST_FIELDS = {
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


def _host(
    module: ModuleType,
    *,
    system: str,
    sys_platform: str,
    machine: str,
    os_name: str,
    platform_tag: str,
    implementation: str = "cpython",
) -> object:
    platform_implementation = "CPython" if implementation == "cpython" else "PyPy"
    interpreter = "cp312" if implementation == "cpython" else "pp312"
    abi = "cp312" if implementation == "cpython" else "pypy312_pp73"
    environment = {
        "implementation_name": implementation,
        "implementation_version": "3.12.0",
        "os_name": os_name,
        "platform_machine": machine,
        "platform_python_implementation": platform_implementation,
        "platform_release": "test-release",
        "platform_system": system,
        "platform_version": "test-version",
        "python_full_version": "3.12.0",
        "python_version": "3.12",
        "sys_platform": sys_platform,
    }
    target = ResolveTarget.for_host(
        env_source=lambda: environment,
        tags_source=lambda: iter(
            (Tag(interpreter, abi, platform_tag), Tag("py3", "none", "any"))
        ),
    )
    return module.BenchmarkHost(target, "test-runtime", 120)


def _linux_host(module: ModuleType) -> object:
    """Return the common Linux x86-64 benchmark host used by wiring tests."""
    return _host(
        module,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
    )


def _empty_provider_stats() -> SimpleNamespace:
    fields = (
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
    )
    return SimpleNamespace(**dict.fromkeys(fields, 0))


def _empty_resolver_stats() -> SimpleNamespace:
    fields = (
        "rounds",
        "decisions",
        "conflicts",
        "derivations",
        "backjumps",
        "restarts",
        "incompatibilities_learned",
    )
    return SimpleNamespace(**dict.fromkeys(fields, 0))


def _wheel(package: str, version: str) -> WheelFile:
    """Return one universal wheel with sidecar metadata for benchmark tests."""
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.test/{package}-{version}.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=(("sha256", "a" * 64),),
    )


def _wheel_metadata(package: str, version: str, *fields: str) -> str:
    """Return wheel metadata with any additional fields requested by a test."""
    return "\n".join(
        (
            "Metadata-Version: 2.1",
            f"Name: {package}",
            f"Version: {version}",
            *fields,
            "",
            "",
        )
    )


def _sidecar(wheel: WheelFile) -> str:
    """Return the sidecar URL advertised by a benchmark-test wheel."""
    url = wheel.metadata_url
    assert url is not None
    return url


def _dropped_extra_coordinator() -> MagicMock:
    """Return an index where aaa 3.0 no longer provides its x extra."""
    aaa_wheels = [_wheel("aaa", version) for version in ("1.0", "2.0", "3.0")]
    bbb_wheel = _wheel("bbb", "1.0")
    metadata = {
        _sidecar(wheel): _wheel_metadata(
            "aaa",
            wheel.version,
            "Provides-Extra: x",
            'Requires-Dist: bbb; extra == "x"',
        )
        for wheel in aaa_wheels[:2]
    }
    metadata[_sidecar(aaa_wheels[2])] = _wheel_metadata("aaa", "3.0")
    metadata[_sidecar(bbb_wheel)] = _wheel_metadata("bbb", "1.0")

    return make_coordinator(
        listings={"aaa": aaa_wheels, "bbb": [bbb_wheel]},
        metadata_by_url=metadata,
    )


def _runner_parity_scenario() -> dict[str, object]:
    """Return a scenario that exercises shared runner configuration."""
    return {
        "python_version": "3.11",
        "requirements": ["demo[feature]>=1"],
        "constraints": ["demo<2"],
        "datetime": "2025-01-02 03:04:05",
        "indexes": [
            {"name": "private", "url": "https://example.test/simple"},
        ],
        "index_routes": [{"name": "demo", "index": "private"}],
        "build_packages": ["demo"],
        "resolution": "lowest-direct",
        "trust_unverified_sdist_deps": False,
        "vcs_policy": "allow",
        "vcs_allowed_schemes": ["git+https"],
        "vcs_allowed_repos": ["https://example.test/project"],
        "vcs_require_pin": False,
        "project_name": "Demo_Project",
        "project_extras": ["All_Features", "Direct_Use"],
        "optional_dependencies": {
            "all.features": ["Project_Leaf>=2", "Second.Leaf"],
            "DIRECT-use": ["Another_Leaf==1"],
        },
    }


def _prepare_standard_scenario(
    standard: ModuleType,
    scenario: dict[str, object],
    host: object,
) -> object:
    """Prepare one standard scenario without running its resolver."""
    strategy = standard.ResolutionStrategy(
        scenario.get("resolution", standard.ResolutionStrategy.HIGHEST.value)
    )
    row = standard.StandardScenario("quick", "example", scenario)
    plan = standard.standard_run_plan([row], (strategy,), host)
    return standard.prepare_standard_execution(
        plan.executions[0],
        plan.targets_by_logical_key[row.logical_key],
        commit="run",
        source=dict(_CLEAN_SOURCE),
        corpus_hash="f" * 64,
        settings_digest="settings",
    )


def _prepare_runner_parity(
    standard: ModuleType,
    canary: ModuleType,
    profile: ModuleType,
    *,
    scenario: dict[str, object] | None = None,
) -> tuple[object, object, dict[str, object]]:
    """Prepare equivalent inputs through the three benchmark entry points."""
    scenario = _runner_parity_scenario() if scenario is None else scenario
    host = _linux_host(standard)
    standard_execution = _prepare_standard_scenario(standard, scenario, host)
    canary_preparation = canary._prepare_canary_execution(
        scenario,
        scenario_name="example",
        resolution_override=None,
        host=host,
    )
    profile_inputs = profile.build_inputs("example", scenario, host=host)

    canary_execution = canary_preparation.execution
    assert canary_execution is not None
    return standard_execution, canary_execution, profile_inputs


@dataclass(frozen=True, order=True, slots=True)
class _FilesystemEntry:
    name: str
    directory: Path = field(compare=False)
    kind: Literal["directory", "file"] = field(compare=False)

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    @property
    def path(self) -> str:
        return str(self.directory / self.name)

    def resolve(self) -> Path:
        return self.directory.resolve() / self.name

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        del follow_symlinks
        return self.kind == "directory"

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        del follow_symlinks
        return self.kind == "file"


@dataclass(frozen=True, slots=True)
class _ScenarioDirectoryListing:
    directory: Path
    entries: tuple[_FilesystemEntry, ...]

    def glob(self, pattern: str) -> list[_FilesystemEntry]:
        assert pattern == "*.toml"
        return list(self.entries)

    def resolve(self) -> Path:
        return self.directory.resolve()


def _add_scanned_member(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    *,
    name: str,
    kind: Literal["directory", "file"],
) -> None:
    real_scandir = module.os.scandir
    member = _FilesystemEntry(name, directory, kind)

    @contextmanager
    def scandir(path: Path) -> Iterator[list[object]]:
        with real_scandir(path) as entries:
            listed: list[object] = list(entries)
        if Path(path) == directory:
            listed.append(member)
        yield listed

    monkeypatch.setattr(module.os, "scandir", scandir)


def _harness(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_benchmark_matrix_{name}", _BENCHMARKS / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_BENCHMARKS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_BENCHMARKS))
    return module


def _matching_host_rows(module: ModuleType) -> dict[str, object]:
    rows = module.load_standard_corpus(module.standard_scenario_files())
    return {
        row.logical_key: row
        for row in rows
        if row.definition.get("requires_matching_host") is True
    }


def _write_corpus(
    path: Path,
    *,
    requirement: str = "demo",
    include_other: bool = True,
) -> None:
    path.mkdir(exist_ok=True)
    (path / "quick.toml").write_text(
        f"""
[example]
python_version = "3.11"
requirements = ["{requirement}"]

[unsupported]
python_version = "3.11"
requirements = ["native-only"]
unsupported_reason = "requires a native system dependency"

[foreign-host]
python_version = "3.11"
requirements = []
platform_system = "Windows"
requires_matching_host = true
""".lstrip(),
        encoding="utf-8",
    )
    other = path / "other.toml"
    if include_other:
        other.write_text(
            """
[other]
python_version = "3.12"
requirements = ["other"]
""".lstrip(),
            encoding="utf-8",
        )
    else:
        other.unlink(missing_ok=True)


def _result_payload(
    module: ModuleType,
    execution: object,
    commit: str,
    source: dict[str, object],
    corpus_hash: str,
) -> dict[str, object]:
    host = module.BenchmarkHost.current(module.SCENARIO_WALL_TIMEOUT_SECONDS)
    plan = module.standard_run_plan(
        [execution.scenario],
        (execution.strategy,),
        host,
    )
    input_data = module.prepare_standard_execution(
        execution,
        plan.targets_by_logical_key[execution.scenario.logical_key],
        commit=commit,
        source=source,
        corpus_hash=corpus_hash,
        settings_digest="test-settings",
    ).expected_input
    stats = dict.fromkeys(module._STANDARD_COUNTER_FIELDS, 0)
    stats["packages_resolved"] = 1
    stats["wall_time_seconds"] = 0.01
    return {
        "input": input_data,
        "result": {"success": True, "error": None},
        "stats": stats,
    }


def _run_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    matrix: bool,
    result_kind: str = "valid",
    source: dict[str, object] | None = None,
    end_source: dict[str, object] | None = None,
    force: bool = False,
    select_quick: bool = True,
    requirement: str = "demo",
    include_other: bool = True,
) -> tuple[ModuleType, list[object], Path]:
    module = _harness("scenarios")
    scenarios_dir = tmp_path / "scenarios"
    results_dir = tmp_path / "results"
    _write_corpus(
        scenarios_dir,
        requirement=requirement,
        include_other=include_other,
    )
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    effective_source = _CLEAN_SOURCE if source is None else source
    effective_end_source = effective_source if end_source is None else end_source
    source_states = iter((effective_source, effective_end_source))
    monkeypatch.setattr(module, "get_git_source_state", lambda: next(source_states))
    seen: list[object] = []
    captured_hosts: list[object] = []
    captured_host = _host(
        module,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
    )

    def current_host(_cls: type, timeout: int) -> object:
        assert timeout == module.SCENARIO_WALL_TIMEOUT_SECONDS
        captured_hosts.append(captured_host)
        return captured_host

    monkeypatch.setattr(module.BenchmarkHost, "current", classmethod(current_host))

    def process(
        execution: object,
        commit: str,
        *,
        force: bool,
        prepared: object,
        host: object,
    ) -> None:
        del force
        assert host is captured_hosts[0]
        seen.append(execution)
        if result_kind == "missing":
            return
        stats = dict.fromkeys(module._STANDARD_COUNTER_FIELDS, 0)
        stats["packages_resolved"] = 1
        stats["wall_time_seconds"] = 0.01
        payload = {
            "input": json.loads(json.dumps(prepared.expected_input)),
            "result": {"success": True, "error": None},
            "stats": stats,
        }
        if result_kind == "malformed":
            payload["stats"] = {"wall_time_seconds": 0.01}
        if result_kind == "changed-input":
            input_data = payload["input"]
            assert isinstance(input_data, dict)
            input_data["python_version"] = "3.10"
        output_path = results_dir / commit / execution.result_key
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        if result_kind == "extra":
            (results_dir / commit / "extra.json").write_text(
                json.dumps(payload) + "\n", encoding="utf-8"
            )
        if result_kind == "nested-special":
            nested = results_dir / commit / "nested" / module.STANDARD_MANIFEST_FILENAME
            nested.parent.mkdir()
            nested.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    monkeypatch.setattr(module, "process_scenario", process)
    argv = ["--commit", "run"]
    if select_quick:
        argv.extend(("--toml", "quick"))
    if matrix:
        argv.append("--strategy-matrix")
    if force:
        argv.append("--force")
    module.main(argv)
    assert len(captured_hosts) == 1
    return module, seen, results_dir / "run" / module.STANDARD_MANIFEST_FILENAME


def test_standard_corpus_is_one_canonical_definition_per_scenario() -> None:
    module = _harness("scenarios")
    files = module.standard_scenario_files()
    rows = module.load_standard_corpus(files)

    assert len(files) == _STANDARD_FILES
    assert len(rows) == _STANDARD_SCENARIOS
    assert len({row.logical_key for row in rows}) == _STANDARD_SCENARIOS
    runnable = sum("unsupported_reason" not in row.definition for row in rows)
    assert runnable == _RUNNABLE_SCENARIOS
    assert len(rows) - runnable == _UNSUPPORTED_SCENARIOS
    assert all("resolution" not in row.definition for row in rows)


def test_standard_execution_census() -> None:
    module = _harness("scenarios")
    rows = module.load_standard_corpus(module.standard_scenario_files())
    execution_keys = module.standard_execution_keys(rows, module.STANDARD_STRATEGIES)

    assert len(execution_keys) == _TOTAL_EXECUTION_IDENTITIES


@pytest.mark.parametrize(
    ("host_kwargs", "expected"),
    [
        (
            {
                "system": "Linux",
                "sys_platform": "linux",
                "machine": "x86_64",
                "os_name": "posix",
                "platform_tag": "manylinux_2_17_x86_64",
            },
            {"linux", "windows", "neutral", "simulated-windows"},
        ),
        (
            {
                "system": "Windows",
                "sys_platform": "win32",
                "machine": "AMD64",
                "os_name": "nt",
                "platform_tag": "win_amd64",
            },
            {"linux", "windows", "windows-amd64", "neutral", "simulated-windows"},
        ),
        (
            {
                "system": "Windows",
                "sys_platform": "win32",
                "machine": "ARM64",
                "os_name": "nt",
                "platform_tag": "win_arm64",
            },
            {"linux", "windows", "neutral", "simulated-windows"},
        ),
        (
            {
                "system": "Darwin",
                "sys_platform": "darwin",
                "machine": "arm64",
                "os_name": "posix",
                "platform_tag": "macosx_14_0_arm64",
            },
            {"linux", "windows", "mac-arm", "neutral", "simulated-windows"},
        ),
        (
            {
                "system": "Darwin",
                "sys_platform": "darwin",
                "machine": "x86_64",
                "os_name": "posix",
                "platform_tag": "macosx_14_0_x86_64",
            },
            {"linux", "windows", "neutral", "simulated-windows"},
        ),
        (
            {
                "system": "Darwin",
                "sys_platform": "darwin",
                "machine": "arm64",
                "os_name": "posix",
                "platform_tag": "macosx_14_0_arm64",
                "implementation": "pypy",
            },
            {"linux", "windows", "neutral", "simulated-windows"},
        ),
    ],
)
def test_standard_plan_applies_explicit_host_requirements(
    host_kwargs: dict[str, str],
    expected: set[str],
) -> None:
    module = _harness("scenarios")
    rows = [
        module.StandardScenario(
            "test",
            "linux",
            {
                "python_version": "3.11",
                "platform_system": "Linux",
            },
        ),
        module.StandardScenario(
            "test",
            "windows",
            {
                "python_version": "3.11",
                "platform_system": "Windows",
            },
        ),
        module.StandardScenario(
            "test",
            "windows-amd64",
            {
                "python_version": "3.11",
                "marker_environment": {
                    "implementation_name": "cpython",
                    "os_name": "nt",
                    "platform_machine": "AMD64",
                    "platform_python_implementation": "CPython",
                    "platform_system": "Windows",
                    "sys_platform": "win32",
                },
                "requires_matching_host": True,
            },
        ),
        module.StandardScenario(
            "test",
            "mac-arm",
            {
                "python_version": "3.11",
                "marker_environment": {
                    "implementation_name": "cpython",
                    "os_name": "posix",
                    "platform_machine": "arm64",
                    "platform_python_implementation": "CPython",
                    "platform_system": "Darwin",
                    "sys_platform": "darwin",
                },
                "requires_matching_host": True,
            },
        ),
        module.StandardScenario(
            "test",
            "simulated-windows",
            {
                "python_version": "3.11",
                "marker_environment": {"platform_system": "Windows"},
            },
        ),
        module.StandardScenario("test", "neutral", {"python_version": "3.11"}),
    ]

    plan = module.standard_run_plan(
        rows,
        (module.ResolutionStrategy.HIGHEST,),
        _host(module, **host_kwargs),
    )

    assert {execution.scenario.name for execution in plan.executions} == expected
    assert all(
        plan.targets_by_logical_key[execution.scenario.logical_key].tags_faithful
        for execution in plan.executions
        if execution.scenario.definition.get("requires_matching_host")
    )
    simulated_target = plan.targets_by_logical_key["test:simulated-windows"]
    assert simulated_target.tags_faithful is (host_kwargs["system"] == "Windows")
    assert len(plan.inapplicable_logical_keys) == len(rows) - len(expected)


def test_manifest_matching_host_census_is_explicit() -> None:
    module = _harness("scenarios")
    rows = [
        module.StandardScenario(
            "test",
            "shorthand",
            {"python_version": "3.11", "platform_system": "Windows"},
        ),
        module.StandardScenario(
            "test",
            "nested",
            {
                "python_version": "3.11",
                "marker_environment": {"platform_system": "Windows"},
            },
        ),
        module.StandardScenario(
            "test",
            "explicit-false",
            {
                "python_version": "3.11",
                "platform_system": "Windows",
                "requires_matching_host": False,
            },
        ),
        module.StandardScenario(
            "test",
            "explicit-true",
            {
                "python_version": "3.11",
                "platform_system": "Windows",
                "requires_matching_host": True,
            },
        ),
    ]
    host = _host(
        module,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
    )
    strategies = (module.ResolutionStrategy.HIGHEST,)
    plan = module.standard_run_plan(rows, strategies, host)
    manifest = module.standard_manifest_contract(
        commit="run",
        mode="default",
        all_rows=rows,
        selected_rows=rows,
        corpus_files=["test.toml"],
        selected_files=["test.toml"],
        strategies=strategies,
        corpus_hash="a" * 64,
        plan=plan,
        settings=module.standard_benchmark_settings(host),
    )

    assert manifest["requires_matching_host_logical_keys"] == ["test:explicit-true"]
    assert manifest["inapplicable_logical_keys"] == ["test:explicit-true"]


@pytest.mark.parametrize(
    ("implementation", "runs"),
    [("cpython", False), ("pypy", True)],
)
def test_matching_host_requirement_accepts_an_interpreter_only_selector(
    implementation: str,
    runs: bool,
) -> None:
    module = _harness("scenarios")
    row = module.StandardScenario(
        "test",
        "pypy",
        {
            "python_version": "3.11",
            "marker_environment": {"implementation_name": "pypy"},
            "requires_matching_host": True,
        },
    )
    host = _host(
        module,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
        implementation=implementation,
    )

    plan = module.standard_run_plan(
        [row],
        (module.ResolutionStrategy.HIGHEST,),
        host,
    )

    if runs:
        assert len(plan.executions) == 1
        assert plan.inapplicable_logical_keys == []
        assert plan.targets_by_logical_key[row.logical_key].tags_faithful
    else:
        assert plan.executions == []
        assert plan.targets_by_logical_key == {}
        assert plan.inapplicable_logical_keys == [row.logical_key]


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ({"platform_system": "Linuz"}, "no supported platform"),
        (
            {
                "marker_environment": {
                    "platform_system": "Darwin",
                    "sys_platform": "win32",
                }
            },
            "no supported platform",
        ),
        (
            {
                "marker_environment": {
                    "implementation_name": "cpython",
                    "platform_python_implementation": "PyPy",
                }
            },
            "no supported interpreter",
        ),
        (
            {
                "platform_system": "Linux",
                "marker_environment": {"platform_system": "Windows"},
            },
            "platform_system conflicts",
        ),
    ],
)
def test_invalid_target_marker_declarations_are_rejected(
    definition: dict[str, object],
    message: str,
) -> None:
    module = _harness("scenarios")

    with pytest.raises(ValueError, match=message):
        module.parse_marker_environment("invalid", definition)


@pytest.mark.parametrize(
    "definition",
    [
        {"platform_system": 1},
        {"marker_environment": {"platform_system": 1}},
    ],
)
def test_target_marker_declarations_require_string_values(
    definition: dict[str, object],
) -> None:
    module = _harness("scenarios")

    with pytest.raises(TypeError, match="must be .*string"):
        module.parse_marker_environment("invalid", definition)


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({}, False),
        ({"platform_system": "Linux"}, False),
        ({"marker_environment": {"platform_system": "Linux"}}, False),
        (
            {
                "marker_environment": {"platform_system": "Linux"},
                "requires_matching_host": True,
            },
            True,
        ),
        (
            {
                "marker_environment": {"platform_system": "Linux"},
                "requires_matching_host": False,
            },
            False,
        ),
        (
            {
                "platform_system": "Linux",
                "requires_matching_host": False,
            },
            False,
        ),
    ],
)
def test_matching_host_requirement_is_explicit(
    definition: dict[str, object],
    expected: bool,
) -> None:
    module = _harness("scenarios")
    markers = module.parse_marker_environment("example", definition)

    assert (
        module.parse_requires_matching_host("example", definition, markers) is expected
    )


@pytest.mark.parametrize("value", [1, "true", [], {}])
def test_matching_host_requirement_must_be_boolean(value: object) -> None:
    module = _harness("scenarios")
    definition = {
        "marker_environment": {"platform_system": "Linux"},
        "requires_matching_host": value,
    }
    markers = module.parse_marker_environment("invalid", definition)

    with pytest.raises(TypeError, match="requires_matching_host must be a boolean"):
        module.parse_requires_matching_host("invalid", definition, markers)


def test_matching_host_requirement_needs_target_identity() -> None:
    module = _harness("scenarios")
    definition = {"requires_matching_host": True}

    with pytest.raises(ValueError, match="needs a platform or interpreter marker"):
        module.parse_requires_matching_host("invalid", definition, {})


def test_standard_plan_reuses_one_admitted_target_across_strategies() -> None:
    module = _harness("scenarios")
    row = module.StandardScenario(
        "test",
        "neutral",
        {"python_version": "3.11", "requirements": []},
    )
    plan = module.standard_run_plan(
        [row],
        module.STANDARD_STRATEGIES,
        module.BenchmarkHost.current(120),
    )

    assert len(plan.targets_by_logical_key) == 1
    target = plan.targets_by_logical_key[row.logical_key]
    assert all(
        plan.targets_by_logical_key[execution.scenario.logical_key] is target
        for execution in plan.executions
    )


def test_host_identity_distinguishes_wheel_tag_sets() -> None:
    module = _harness("scenarios")
    environment = {
        "implementation_name": "cpython",
        "implementation_version": "3.12.0",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "test-release",
        "platform_system": "Linux",
        "platform_version": "test-version",
        "python_full_version": "3.12.0",
        "python_version": "3.12",
        "sys_platform": "linux",
    }
    tags = (
        Tag("cp312", "cp312", "manylinux_2_28_x86_64"),
        Tag("cp312", "abi3", "manylinux_2_17_x86_64"),
    )

    def identity_for(ordered_tags: tuple[Tag, ...]) -> dict[str, object]:
        target = ResolveTarget.for_host(
            env_source=lambda: environment,
            tags_source=lambda: iter(ordered_tags),
        )
        return module.BenchmarkHost(target, "test-runtime", 120).identity()

    identity = identity_for(tags)
    reversed_identity = identity_for(tuple(reversed(tags)))
    wheel_tags_hash = identity["wheel_tags_hash"]

    assert identity == {
        "python": "test-runtime",
        "marker_environment": dict(sorted(environment.items())),
        "wheel_tags_count": 2,
        "wheel_tags_hash": wheel_tags_hash,
    }
    assert isinstance(wheel_tags_hash, str)
    assert len(wheel_tags_hash) == 64
    assert wheel_tags_hash != reversed_identity["wheel_tags_hash"]


def test_process_scenario_uses_the_planned_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("scenarios")
    host = _host(
        module,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
    )
    row = module.StandardScenario(
        "quick",
        "markers",
        {
            "python_version": "3.11",
            "requirements": [
                "selected; platform_machine == 'x86_64'",
                "excluded; platform_machine == 'arm64'",
            ],
        },
    )
    plan = module.standard_run_plan(
        [row],
        (module.ResolutionStrategy.HIGHEST,),
        host,
    )
    execution = plan.executions[0]
    target = plan.targets_by_logical_key[row.logical_key]
    settings = module.standard_benchmark_settings(host)
    prepared = module.prepare_standard_execution(
        execution,
        target,
        commit="run",
        source=dict(_CLEAN_SOURCE),
        corpus_hash="f" * 64,
        settings_digest=module.settings_hash(settings),
    )
    seen: dict[str, object] = {}

    def resolve(requirements: dict, *_args: object, **kwargs: object) -> dict:
        seen["requirements"] = requirements
        seen.update(kwargs)
        stats = dict.fromkeys(module._STANDARD_COUNTER_FIELDS, 0)
        stats["wall_time_seconds"] = 0.01
        return {
            "result": {"success": True, "error": None},
            "stats": stats,
        }

    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "resolve_scenario", resolve)
    module.process_scenario(
        execution,
        "run",
        force=False,
        prepared=prepared,
        host=host,
    )

    requirements = seen["requirements"]
    assert isinstance(requirements, dict)
    assert set(requirements) == {"selected"}
    assert seen["target"] is target
    assert seen["host"] is host


def test_benchmark_config_combines_routing_and_build_policy() -> None:
    module = _harness("scenarios")
    indexes = [
        module.IndexConfig("pypi", "https://pypi.org/simple/"),
        module.IndexConfig("private", "https://example.test/simple"),
    ]
    cutoff = module.parse_datetime("2025-01-02 03:04:05")
    vcs = module.VcsConfig(
        policy=module.VcsPolicy.ALLOW,
        allowed_schemes=frozenset({"git+https"}),
        allowed_repos=("https://example.test/project",),
        require_pin=False,
    )

    config = module.build_benchmark_config(
        uploaded_prior_to=cutoff,
        indexes=indexes,
        index_routes=[module.IndexRoute("Demo_Pkg", "private")],
        build_policy_overrides={
            "demo-pkg": module.BuildPolicy.BUILD_REMOTE,
            "build-only": module.BuildPolicy.BUILD_REMOTE,
        },
        resolution=module.ResolutionStrategy.LOWEST_DIRECT,
        trust_unverified_sdist_deps=True,
        vcs=vcs,
    )

    assert config.constraints == ()
    assert config.uploaded_prior_to is cutoff
    assert config.dist_policy is module.DistPolicy.WHEEL_OR_SDIST
    assert config.build_policy is module.BuildPolicy.NEVER
    assert config.trust_unverified_sdist_deps is True
    assert config.indexes == tuple(indexes)
    assert config.vcs is vcs
    assert config.resolution is module.ResolutionStrategy.LOWEST_DIRECT

    overrides = {override.name: override for override in config.package_overrides}
    assert set(overrides) == {"build-only", "demo-pkg"}
    assert overrides["demo-pkg"].index == "private"
    assert overrides["demo-pkg"].build_policy is module.BuildPolicy.BUILD_REMOTE
    assert overrides["build-only"].index is None
    assert overrides["build-only"].build_policy is module.BuildPolicy.BUILD_REMOTE
    assert module.index_routes_from_config(config) == [
        module.IndexRoute("demo-pkg", "private")
    ]


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ({}, True),
        ({"trust_unverified_sdist_deps": True}, True),
        ({"trust_unverified_sdist_deps": False}, False),
    ],
    ids=("implicit", "trusted", "strict"),
)
def test_parse_trust_unverified_sdist_deps_accepts_exact_booleans(
    scenario: dict[str, object],
    expected: bool,
) -> None:
    module = _harness("benchmark_config")
    original = dict(scenario)

    assert module.parse_trust_unverified_sdist_deps("example", scenario) is expected
    assert scenario == original


@pytest.mark.parametrize(
    ("invalid", "type_name"),
    [
        (0, "int"),
        (1, "int"),
        (0.0, "float"),
        ("false", "str"),
        ([False], "list"),
        ({"value": False}, "dict"),
        (None, "NoneType"),
    ],
    ids=("zero", "one", "float", "string", "list", "table", "none"),
)
def test_parse_trust_unverified_sdist_deps_rejects_other_types(
    invalid: object,
    type_name: str,
) -> None:
    module = _harness("benchmark_config")
    message = f"example: trust_unverified_sdist_deps must be a boolean, got {type_name}"

    with pytest.raises(TypeError, match=message):
        module.parse_trust_unverified_sdist_deps(
            "example",
            {"trust_unverified_sdist_deps": invalid},
        )


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ({}, True),
        ({"vcs_require_pin": True}, True),
        ({"vcs_require_pin": False}, False),
    ],
    ids=("implicit", "required", "optional"),
)
def test_parse_vcs_require_pin_accepts_exact_booleans(
    scenario: dict[str, object],
    expected: bool,
) -> None:
    module = _harness("benchmark_config")
    original = dict(scenario)

    assert module.parse_vcs_require_pin("example", scenario) is expected
    assert scenario == original


@pytest.mark.parametrize(
    ("invalid", "type_name"),
    [
        (0, "int"),
        (1, "int"),
        (0.0, "float"),
        ("false", "str"),
        ([False], "list"),
        ({"value": False}, "dict"),
        (None, "NoneType"),
    ],
    ids=("zero", "one", "float", "string", "list", "table", "none"),
)
def test_parse_vcs_require_pin_rejects_other_types(
    invalid: object,
    type_name: str,
) -> None:
    module = _harness("benchmark_config")
    message = f"example: vcs_require_pin must be a boolean, got {type_name}"

    with pytest.raises(TypeError, match=message):
        module.parse_vcs_require_pin(
            "example",
            {"vcs_require_pin": invalid},
        )


def test_parse_vcs_policy_normalizes_type_errors() -> None:
    module = _harness("benchmark_config")

    class InvalidPolicy:
        """Make Enum lookup fail while comparing a scenario value."""

        def __hash__(self) -> int:
            return hash("allow")

        def __eq__(self, _other: object) -> bool:
            raise TypeError("policy values cannot be compared")

        def __repr__(self) -> str:
            return "<invalid policy>"

    message = (
        "example: vcs_policy must be one of ['allow', 'block'], got <invalid policy>"
    )
    with pytest.raises(ValueError, match=re.escape(message)) as exc_info:
        module.parse_vcs_policy("example", {"vcs_policy": InvalidPolicy()})

    assert isinstance(exc_info.value.__cause__, TypeError)


@pytest.mark.parametrize(
    ("scenario", "expected_schemes", "expected_repos"),
    [
        ({}, frozenset(), ()),
        (
            {"vcs_allowed_schemes": [], "vcs_allowed_repos": []},
            frozenset(),
            (),
        ),
        (
            {
                "vcs_allowed_schemes": [
                    "git+https",
                    "git+https",
                    " git+ssh ",
                    "",
                ],
                "vcs_allowed_repos": [
                    "not a URL",
                    "not a URL",
                    " https://example.test/repo ",
                    "",
                ],
            },
            frozenset({"git+https", " git+ssh ", ""}),
            (
                "not a URL",
                "not a URL",
                " https://example.test/repo ",
                "",
            ),
        ),
    ],
    ids=("missing", "empty", "declared"),
)
def test_parse_vcs_allowlists_preserves_declared_values(
    scenario: dict[str, object],
    expected_schemes: frozenset[str],
    expected_repos: tuple[str, ...],
) -> None:
    module = _harness("benchmark_config")
    original = deepcopy(scenario)

    schemes = module.parse_vcs_allowed_schemes("example", scenario)
    repos = module.parse_vcs_allowed_repos("example", scenario)

    assert schemes == expected_schemes
    assert repos == expected_repos
    assert scenario == original


@pytest.mark.parametrize(
    ("parser_name", "field", "invalid", "message"),
    [
        (
            "parse_vcs_allowed_schemes",
            "vcs_allowed_schemes",
            {"git+https": False},
            "example: vcs_allowed_schemes must be a list, got dict",
        ),
        (
            "parse_vcs_allowed_repos",
            "vcs_allowed_repos",
            {"https://example.test/repo": False},
            "example: vcs_allowed_repos must be a list, got dict",
        ),
        (
            "parse_vcs_allowed_schemes",
            "vcs_allowed_schemes",
            ("git+https",),
            "example: vcs_allowed_schemes must be a list, got tuple",
        ),
        (
            "parse_vcs_allowed_repos",
            "vcs_allowed_repos",
            ("https://example.test/repo",),
            "example: vcs_allowed_repos must be a list, got tuple",
        ),
        (
            "parse_vcs_allowed_schemes",
            "vcs_allowed_schemes",
            ["git+https", False],
            "example: vcs_allowed_schemes[1] must be a string, got bool",
        ),
        (
            "parse_vcs_allowed_repos",
            "vcs_allowed_repos",
            ["https://example.test/repo", 1],
            "example: vcs_allowed_repos[1] must be a string, got int",
        ),
    ],
    ids=(
        "scheme-table",
        "repo-table",
        "scheme-tuple",
        "repo-tuple",
        "scheme-member",
        "repo-member",
    ),
)
def test_parse_vcs_allowlists_rejects_non_lists_and_non_strings(
    parser_name: str,
    field: str,
    invalid: object,
    message: str,
) -> None:
    module = _harness("benchmark_config")
    parser = getattr(module, parser_name)

    with pytest.raises(TypeError, match=re.escape(message)):
        parser("example", {field: invalid})


def test_parse_scenario_requirement_strings_returns_fresh_unchanged_lists() -> None:
    module = _harness("benchmark_config")
    requirements = ["Z_pkg", " demo >= 1 "]
    constraints = ["demo!=2", "Other===local"]
    scenario = {"requirements": requirements, "constraints": constraints}

    parsed = module.parse_scenario_requirement_strings("quick:example", scenario)

    assert parsed.requirements == requirements
    assert parsed.constraints == constraints
    assert parsed.requirements is not requirements
    assert parsed.constraints is not constraints

    parsed.requirements.append("new-root")
    parsed.constraints.clear()
    assert scenario == {
        "requirements": ["Z_pkg", " demo >= 1 "],
        "constraints": ["demo!=2", "Other===local"],
    }


def test_parse_scenario_requirement_strings_accepts_empty_lists() -> None:
    module = _harness("benchmark_config")
    scenario: dict[str, object] = {"requirements": [], "constraints": []}

    parsed = module.parse_scenario_requirement_strings("quick:empty", scenario)

    assert parsed.requirements == []
    assert parsed.constraints == []
    assert parsed.requirements is not scenario["requirements"]
    assert parsed.constraints is not scenario["constraints"]


def test_parse_scenario_requirement_strings_requires_requirements() -> None:
    module = _harness("benchmark_config")

    with pytest.raises(
        ValueError,
        match="quick:missing: missing required field 'requirements'",
    ):
        module.parse_scenario_requirement_strings("quick:missing", {})


@pytest.mark.parametrize("field", ["requirements", "constraints"])
def test_parse_scenario_requirement_strings_rejects_scalar_fields(field: str) -> None:
    module = _harness("benchmark_config")
    scenario: dict[str, object] = {"requirements": []}
    scenario[field] = "demo"

    with pytest.raises(
        TypeError,
        match=rf"quick:scalar: {field} must be a list, got str",
    ):
        module.parse_scenario_requirement_strings("quick:scalar", scenario)


@pytest.mark.parametrize("field", ["requirements", "constraints"])
def test_parse_scenario_requirement_strings_rejects_mixed_fields(field: str) -> None:
    module = _harness("benchmark_config")
    scenario: dict[str, object] = {"requirements": []}
    scenario[field] = ["demo", 1]

    with pytest.raises(
        TypeError,
        match=rf"quick:mixed: {field}\[1\] must be a non-empty string, got int",
    ):
        module.parse_scenario_requirement_strings("quick:mixed", scenario)


@pytest.mark.parametrize("field", ["requirements", "constraints"])
def test_parse_scenario_requirement_strings_rejects_empty_items(field: str) -> None:
    module = _harness("benchmark_config")
    scenario: dict[str, object] = {"requirements": []}
    scenario[field] = ["demo", ""]

    with pytest.raises(
        ValueError,
        match=rf"quick:empty-item: {field}\[1\] must be a non-empty string$",
    ):
        module.parse_scenario_requirement_strings("quick:empty-item", scenario)


def test_parse_scenario_project_metadata_returns_fresh_nested_values() -> None:
    module = _harness("benchmark_config")
    project_extras = ["All_Features", "OpenAI"]
    optional_dependencies = {
        "all.features": ["Eval_Framework[OpenAI]"],
        "open_ai": ["OpenAI_Client>=1.62,<2"],
    }
    scenario = {
        "project_name": "Eval_Framework",
        "project_extras": project_extras,
        "optional_dependencies": optional_dependencies,
    }
    original = deepcopy(scenario)

    parsed = module.parse_scenario_project_metadata(
        "ecosystem:eval-framework", scenario
    )

    assert parsed.project_name == "Eval_Framework"
    assert parsed.project_extras == ["All_Features", "OpenAI"]
    assert list(parsed.optional_dependencies) == ["all.features", "open_ai"]
    assert parsed.optional_dependencies == optional_dependencies

    assert parsed.project_extras is not project_extras
    assert parsed.optional_dependencies is not optional_dependencies
    for extra, dependencies in optional_dependencies.items():
        assert parsed.optional_dependencies[extra] is not dependencies

    parsed.project_extras.reverse()
    parsed.optional_dependencies["all.features"].clear()
    assert scenario == original


@pytest.mark.parametrize(
    "scenario",
    [
        {"project_name": "Eval_Framework"},
        {"project_extras": ["All_Features", "OpenAI"]},
        {
            "optional_dependencies": {
                "all.features": ["Eval_Framework[OpenAI]"],
                "open_ai": ["OpenAI_Client>=1.62,<2"],
            }
        },
        {
            "project_extras": ["OpenAI", "All_Features"],
            "optional_dependencies": {
                "open_ai": ["OpenAI_Client>=1.62,<2"],
                "all.features": ["Eval_Framework[OpenAI]"],
            },
        },
    ],
    ids=("name", "extras", "optional-dependencies", "extras-and-optional"),
)
def test_parse_scenario_project_metadata_accepts_partial_forms(
    scenario: dict[str, object],
) -> None:
    module = _harness("benchmark_config")

    parsed = module.parse_scenario_project_metadata(
        "ecosystem:eval-framework", scenario
    )

    assert parsed.project_name == scenario.get("project_name")
    assert parsed.project_extras == scenario.get("project_extras", [])
    assert list(parsed.optional_dependencies) == list(
        scenario.get("optional_dependencies", {})
    )
    assert parsed.optional_dependencies == scenario.get("optional_dependencies", {})


@pytest.mark.parametrize(
    ("scenario", "error", "message"),
    [
        (
            {"project_name": False},
            TypeError,
            "quick:project: project_name must be a non-empty string, got bool",
        ),
        (
            {"project_name": ""},
            ValueError,
            "quick:project: project_name must be a non-empty string",
        ),
        (
            {"project_extras": "all"},
            TypeError,
            "quick:project: project_extras must be a list, got str",
        ),
        (
            {"project_extras": ["all", 1]},
            TypeError,
            "quick:project: project_extras[1] must be a non-empty string, got int",
        ),
        (
            {"project_extras": ["all", ""]},
            ValueError,
            "quick:project: project_extras[1] must be a non-empty string",
        ),
        (
            {"optional_dependencies": ["demo"]},
            TypeError,
            "quick:project: optional_dependencies must be a table, got list",
        ),
        (
            {"optional_dependencies": {"all": "demo"}},
            TypeError,
            "quick:project: optional_dependencies['all'] must be a list, got str",
        ),
        (
            {"optional_dependencies": {"all": ["demo", 1]}},
            TypeError,
            (
                "quick:project: optional_dependencies['all'][1] must be a "
                "non-empty string, got int"
            ),
        ),
        (
            {"optional_dependencies": {"all": ["demo", ""]}},
            ValueError,
            (
                "quick:project: optional_dependencies['all'][1] must be a "
                "non-empty string"
            ),
        ),
        (
            {"optional_dependencies": {"": ["demo"]}},
            ValueError,
            "quick:project: optional_dependencies keys must be non-empty strings",
        ),
    ],
    ids=(
        "project-name-type",
        "project-name-empty",
        "project-extras-scalar",
        "project-extras-member",
        "project-extras-empty",
        "optional-dependencies-array",
        "optional-dependency-scalar",
        "optional-dependency-member",
        "optional-dependency-empty",
        "optional-dependency-empty-key",
    ),
)
def test_parse_scenario_project_metadata_rejects_malformed_values(
    scenario: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    module = _harness("benchmark_config")

    with pytest.raises(error, match=re.escape(message)):
        module.parse_scenario_project_metadata("quick:project", scenario)


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        (
            [
                ("Demo_Pkg", "private"),
                ("demo-pkg", "private"),
            ],
            "duplicate index route for 'demo-pkg'",
        ),
        (
            [("demo", "missing")],
            "index route for 'demo' names undeclared index 'missing'",
        ),
    ],
)
def test_benchmark_config_rejects_invalid_index_routes(
    routes: list[tuple[str, str]],
    message: str,
) -> None:
    module = _harness("scenarios")

    with pytest.raises(ValueError, match=re.escape(message)):
        module.build_benchmark_config(
            indexes=[
                module.IndexConfig("pypi", "https://pypi.org/simple/"),
                module.IndexConfig("private", "https://example.test/simple"),
            ],
            index_routes=[module.IndexRoute(*route) for route in routes],
        )


def test_build_benchmark_provider_forwards_project_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenarios = _harness("scenarios")
    config_module = _harness("benchmark_config")
    host = _linux_host(scenarios)
    admission = host.target_for("3.11", {}, requires_matching_host=False)
    assert admission.target is not None
    target = admission.target
    requirements = scenarios.parse_requirements(["demo[feature]", "other"])
    constraints = scenarios.parse_requirements(["demo<2"])
    inputs = config_module.build_benchmark_resolver_inputs(
        requirements,
        constraints,
    )
    config = config_module.build_benchmark_config(
        uploaded_prior_to=scenarios.parse_datetime("2025-01-02 03:04:05"),
        indexes=[scenarios.IndexConfig("private", "https://example.test/simple")],
        index_routes=[scenarios.IndexRoute("demo", "private")],
        build_policy_overrides={"demo": scenarios.BuildPolicy.BUILD_REMOTE},
        resolution=config_module.ResolutionStrategy.LOWEST_DIRECT,
        trust_unverified_sdist_deps=True,
        vcs=config_module.VcsConfig(
            policy=scenarios.VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://example.test/project",),
            require_pin=False,
        ),
    )
    seen: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen["args"] = args
            seen["kwargs"] = kwargs

    monkeypatch.setattr(config_module, "Provider", FakeProvider)
    coordinator = object()
    provider = config_module.build_benchmark_provider(
        coordinator,
        config=config,
        target=target,
        inputs=inputs,
    )

    assert isinstance(provider, FakeProvider)
    assert seen["args"] == (coordinator,)
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["target"] is target
    assert kwargs["root_requirements"] is requirements
    assert kwargs["root_extras"] == {("demo", "feature")}
    assert kwargs["constraints"] is inputs.constraints
    assert set(kwargs["constraints"]) == {"demo", "demo[feature]"}

    assert kwargs["uploaded_prior_to"] is config.uploaded_prior_to
    assert kwargs["dist_policy"] is config.dist_policy
    assert kwargs["build_policy"] is scenarios.BuildPolicy.NEVER
    assert kwargs["package_overrides"] is config.package_overrides
    assert kwargs["index_overrides"] is config.index_overrides
    assert kwargs["trust_unverified_sdist_deps"] is True
    assert kwargs["vcs_config"] is config.vcs

    assert kwargs["local_sources"] is None
    assert kwargs["vcs_sources"] is None
    assert kwargs["archive_sources"] is None

    assert kwargs["build_config"] is config
    assert kwargs["resolution_strategy"] is config.resolution
    assert kwargs["direct_packages"] == frozenset({"demo", "other"})


def test_benchmark_resolver_inputs_copy_constraints_to_extra_proxies() -> None:
    scenarios = _harness("scenarios")
    config_module = _harness("benchmark_config")
    requirements = scenarios.parse_requirements(["demo[feature]"])
    constraints = scenarios.parse_requirements(["demo<2"])

    inputs = config_module.build_benchmark_resolver_inputs(
        requirements,
        constraints,
    )

    assert inputs.requirements is requirements
    assert inputs.root_extras == {("demo", "feature")}
    assert inputs.constraints is not constraints
    assert set(constraints) == {"demo"}
    assert inputs.constraints is not None
    assert set(inputs.constraints) == {"demo", "demo[feature]"}
    assert inputs.constraints["demo[feature]"] is inputs.constraints["demo"]
    with pytest.raises(TypeError):
        setitem(inputs.constraints, "demo", constraints["demo"])


def test_resolve_scenario_coordinates_config_target_and_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("scenarios")
    host = _linux_host(module)
    admission = host.target_for("3.11", {}, requires_matching_host=False)
    assert admission.target is not None
    target = admission.target
    requirements = module.parse_requirements(["demo[feature]"])
    constraints = module.parse_requirements(["demo<2"])
    config = module.build_benchmark_config(
        indexes=[module.IndexConfig("private", "https://example.test/simple")],
        index_routes=[module.IndexRoute("demo", "private")],
    )
    coordinator = object()
    provider = SimpleNamespace(stats=_empty_provider_stats())
    seen: dict[str, object] = {}

    @contextmanager
    def fake_coordinator(*_args: object, **kwargs: object) -> Iterator[object]:
        seen["coordinator"] = kwargs
        yield coordinator

    def fake_build_provider(actual: object, **kwargs: object) -> object:
        seen["provider_coordinator"] = actual
        seen["provider"] = kwargs
        return provider

    class FakeResolver:
        def __init__(self, actual_provider: object, **_kwargs: object) -> None:
            seen["resolver_provider"] = actual_provider
            self.stats = _empty_resolver_stats()

        def resolve(self, roots: object, **kwargs: object) -> dict:
            seen["resolver_roots"] = roots
            seen["resolver_constraints"] = kwargs["constraints"]
            return {"demo": "1.0"}

    @contextmanager
    def wall_timeout(actual_host: object) -> Iterator[None]:
        assert actual_host is host
        seen["timeout"] = actual_host
        yield

    monkeypatch.setattr(module, "FetchCoordinator", fake_coordinator)
    monkeypatch.setattr(module, "HttpxAsyncTransport", object)
    monkeypatch.setattr(module, "build_benchmark_provider", fake_build_provider)
    monkeypatch.setattr(module, "Resolver", FakeResolver)
    monkeypatch.setattr(module.BenchmarkHost, "wall_timeout", wall_timeout)

    result = module.resolve_scenario(
        requirements,
        constraints,
        config=config,
        target=target,
        host=host,
    )

    assert result["result"] == {"success": True, "error": None}
    assert seen["coordinator"] == {
        "indexes": list(config.indexes),
        "cache_dir": module.CACHE_DIR,
        "index_routes": module.index_routes_from_config(config),
    }
    assert seen["provider_coordinator"] is coordinator
    provider_kwargs = seen["provider"]
    assert isinstance(provider_kwargs, dict)
    assert provider_kwargs == {
        "config": config,
        "target": target,
        "inputs": module.build_benchmark_resolver_inputs(
            requirements,
            constraints,
        ),
    }
    assert seen["resolver_provider"] is provider
    assert seen["resolver_roots"] is requirements
    resolver_constraints = seen["resolver_constraints"]
    assert isinstance(resolver_constraints, Mapping)
    provider_inputs = provider_kwargs["inputs"]
    assert resolver_constraints is provider_inputs.constraints
    assert set(resolver_constraints) == {"demo", "demo[feature]"}
    assert seen["timeout"] is host


@pytest.mark.parametrize(
    ("constraint", "success", "error_fragments", "expected_packages"),
    [
        pytest.param(
            "aaa==2.0",
            True,
            (),
            2,
            id="constrained-version-provides-extra",
        ),
        pytest.param(
            "aaa==3.0",
            False,
            ("MissingExtraError", "aaa==3.0 does not provide extra 'x'"),
            0,
            id="constrained-version-dropped-extra",
        ),
        pytest.param(
            "aaa<0.5",
            False,
            ("ResolutionError", "the user constrained aaa[x]", "0.5"),
            0,
            id="constraint-empties-extra-proxy",
        ),
    ],
)
def test_standard_runner_applies_root_extra_constraints(
    constraint: str,
    success: bool,
    error_fragments: tuple[str, ...],
    expected_packages: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("scenarios")
    coordinator = _dropped_extra_coordinator()

    @contextmanager
    def fake_coordinator(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield coordinator

    monkeypatch.setattr(module, "FetchCoordinator", fake_coordinator)
    monkeypatch.setattr(module, "HttpxAsyncTransport", object)

    captured = _linux_host(module)
    host = module.BenchmarkHost(captured.target, captured.python_runtime, None)
    admission = host.target_for("3.11", {}, requires_matching_host=False)
    assert admission.target is not None
    result = module.resolve_scenario(
        module.parse_requirements(["aaa[x]"]),
        module.parse_requirements([constraint]),
        config=module.build_benchmark_config(indexes=module.DEFAULT_INDEXES),
        target=admission.target,
        host=host,
    )

    outcome = result["result"]
    assert outcome["success"] is success
    error = outcome["error"]
    if error_fragments:
        assert isinstance(error, str)
        for fragment in error_fragments:
            assert fragment in error
    else:
        assert error is None
    assert result["stats"]["packages_resolved"] == expected_packages


def test_marker_build_scenarios_are_explicitly_unsupported() -> None:
    module = _harness("scenarios")
    rows = module.load_standard_corpus(module.standard_scenario_files())
    marker_build_rows = {
        row.logical_key: row
        for row in rows
        if module.parse_marker_environment(row.name, row.definition)
        and module.parse_build_packages(row.name, row.definition)
    }

    assert set(marker_build_rows) == _MARKER_BUILD_SCENARIOS
    assert all(
        row.definition.get("unsupported_reason") for row in marker_build_rows.values()
    )


def test_matching_host_scenario_census_has_complete_marker_environments() -> None:
    module = _harness("scenarios")
    rows = _matching_host_rows(module)

    assert {
        key: row.definition["marker_environment"] for key, row in rows.items()
    } == _MATCHING_HOST_MARKERS


def test_matching_host_scenarios_retain_their_reviewed_inputs() -> None:
    module = _harness("scenarios")
    rows = _matching_host_rows(module)

    tensorflow = rows["uv:uv-tensorflow-macos"].definition
    assert tensorflow["python_version"] == "3.10.17"
    assert tensorflow["datetime"] == "2025-05-21 09:12:43"
    assert tensorflow["requirements"] == ["protobuf", "retina-face"]

    pywin32 = rows["uv:pywin32-windows-amd64-real-index"].definition
    assert pywin32["requirements"] == ["pywin32; sys_platform == 'win32'"]
    assert all("unsupported_reason" not in row.definition for row in rows.values())


@pytest.mark.parametrize(
    ("logical_key", "host_kwargs"),
    [
        (
            "uv:uv-tensorflow-macos",
            {
                "system": "Darwin",
                "sys_platform": "darwin",
                "machine": "arm64",
                "os_name": "posix",
                "platform_tag": "macosx_14_0_arm64",
            },
        ),
        (
            "uv:pywin32-windows-amd64-real-index",
            {
                "system": "Windows",
                "sys_platform": "win32",
                "machine": "AMD64",
                "os_name": "nt",
                "platform_tag": "win_amd64",
            },
        ),
    ],
)
def test_matching_host_scenarios_run_on_matching_hosts(
    logical_key: str,
    host_kwargs: dict[str, str],
) -> None:
    module = _harness("scenarios")
    row = _matching_host_rows(module)[logical_key]
    host = _host(module, **host_kwargs)

    plan = module.standard_run_plan(
        [row],
        (module.ResolutionStrategy.HIGHEST,),
        host,
    )

    assert len(plan.executions) == 1
    assert plan.inapplicable_logical_keys == []
    assert plan.targets_by_logical_key[logical_key].tags_faithful


def test_standard_corpus_rejects_supported_marker_build_policy(
    tmp_path: Path,
) -> None:
    module = _harness("scenarios")
    path = tmp_path / "quick.toml"
    path.write_text(
        """
[example]
python_version = "3.11"
platform_system = "Linux"
build_packages = ["demo"]
requirements = ["demo"]
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=(
            "example: build_packages cannot be combined "
            "with a marker environment overlay"
        ),
    ):
        module.load_standard_corpus([path])


def test_standard_corpus_validates_host_requirement_on_unsupported_rows(
    tmp_path: Path,
) -> None:
    module = _harness("scenarios")
    path = tmp_path / "quick.toml"
    path.write_text(
        """
[unsupported]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
requires_matching_host = "yes"
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="requires_matching_host must be a boolean"):
        module.load_standard_corpus([path])


def test_standard_corpus_rejects_a_strategy_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("scenarios")
    (tmp_path / "quick.toml").write_text("[example]\n", encoding="utf-8")
    (tmp_path / "quick-lowest.toml").write_text("[example]\n", encoding="utf-8")
    monkeypatch.setattr(module, "SCENARIOS_DIR", tmp_path)

    with pytest.raises(ValueError, match="legacy strategy-clone"):
        module.standard_scenario_files()


def test_standard_corpus_rejects_a_symlinked_scenario_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("scenarios")
    source = tmp_path / "source.toml"
    source.write_text("[example]\n", encoding="utf-8")
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "quick.toml").symlink_to(source)
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios)

    with pytest.raises(ValueError, match="real top-level portable paths"):
        module.standard_scenario_files()


def test_standard_corpus_rejects_a_directory_named_like_a_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("scenarios")
    (tmp_path / "ordinary.toml").mkdir()
    monkeypatch.setattr(module, "SCENARIOS_DIR", tmp_path)

    with pytest.raises(ValueError, match="real top-level portable paths"):
        module.standard_scenario_files()


@pytest.mark.parametrize(
    "filename",
    ["foo.json.toml", "_Provenance.json.toml", "_standard_manifest.json.toml"],
)
def test_standard_corpus_rejects_a_stem_that_cannot_store_result_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    module = _harness("scenarios")
    (tmp_path / filename).write_text("[example]\n", encoding="utf-8")
    monkeypatch.setattr(module, "SCENARIOS_DIR", tmp_path)

    with pytest.raises(ValueError, match="real top-level portable paths"):
        module.standard_scenario_files()


def test_standard_corpus_rejects_case_insensitive_path_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("scenarios")
    entries = tuple(
        _FilesystemEntry(name, tmp_path, "file")
        for name in ("quick.toml", "Quick.toml")
    )
    monkeypatch.setattr(
        module,
        "SCENARIOS_DIR",
        _ScenarioDirectoryListing(tmp_path, entries),
    )

    with pytest.raises(ValueError, match="case-insensitive filesystems"):
        module.standard_scenario_files()


@pytest.mark.parametrize(
    ("toml_stem", "scenario_name"),
    [
        ("../escape", "case"),
        ("quick", "../escape"),
        ("universal", "case"),
    ],
)
def test_standard_result_path_rejects_unsafe_or_reserved_components(
    tmp_path: Path,
    toml_stem: str,
    scenario_name: str,
) -> None:
    module = _harness("scenarios")
    execution = module.StandardExecution(
        module.StandardScenario(toml_stem, scenario_name, {}),
        module.ResolutionStrategy.HIGHEST,
    )

    with pytest.raises(ValueError, match="unsafe standard result key"):
        module._standard_result_path(tmp_path / "run", execution)


def test_result_paths_reject_symlinked_namespaces(tmp_path: Path) -> None:
    module = _harness("scenarios")
    results = tmp_path / "results"
    outside = tmp_path / "outside"
    results.mkdir()
    outside.mkdir()
    (results / "run").symlink_to(outside, target_is_directory=True)
    module.RESULTS_DIR = results

    with pytest.raises(ValueError, match="direct, real directory"):
        module._result_directory("run")

    (results / "run").unlink()
    run = results / "run"
    run.mkdir()
    (run / "quick").symlink_to(outside, target_is_directory=True)
    execution = module.StandardExecution(
        module.StandardScenario("quick", "example", {}),
        module.ResolutionStrategy.HIGHEST,
    )

    with pytest.raises(ValueError, match="direct, real child"):
        module._standard_result_path(run, execution)


@pytest.mark.parametrize("member", ["quick/example.json", "_provenance.json"])
def test_standard_namespace_rejects_a_directory_at_a_json_path(
    tmp_path: Path,
    member: str,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    (run / member).mkdir(parents=True)

    with pytest.raises(ValueError, match="JSON result path must be a regular file"):
        module.prepare_standard_result_namespace(run, {}, [], force=True)


def test_standard_namespace_rejects_a_file_at_a_reserved_directory(
    tmp_path: Path,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    run.mkdir()
    (run / "universal").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match="reserved result namespace"):
        module.prepare_standard_result_namespace(run, {}, [], force=True)


@pytest.mark.parametrize(
    ("member", "directory"), [("Universal", True), ("_Provenance.json", False)]
)
def test_standard_namespace_rejects_nonportable_reserved_name_casing(
    tmp_path: Path,
    member: str,
    directory: bool,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    run.mkdir()
    path = run / member
    if directory:
        path.mkdir()
    else:
        path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-portable casing"):
        module.prepare_standard_result_namespace(run, {}, [], force=True)


@pytest.mark.parametrize(
    ("name", "kind", "message"),
    [
        pytest.param(
            "universal.",
            "directory",
            "non-portable casing",
            id="reserved-alias",
        ),
        pytest.param(
            "NUL",
            "file",
            "non-portable member",
            id="reserved-device",
        ),
    ],
)
@pytest.mark.parametrize("force", [False, True])
def test_standard_namespace_rejects_nonportable_members_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
    name: str,
    kind: Literal["directory", "file"],
    message: str,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    protected = run / "universal" / "fixture.json"
    protected.parent.mkdir(parents=True)
    protected.write_text('{"protected": true}\n', encoding="utf-8")
    standard = run / "quick" / "example.json"
    standard.parent.mkdir(parents=True)
    standard.write_text("{}\n", encoding="utf-8")
    _add_scanned_member(
        module,
        monkeypatch,
        run,
        name=name,
        kind=kind,
    )

    with pytest.raises(ValueError, match=message):
        module.prepare_standard_result_namespace(run, {}, [], force=force)

    assert protected.read_text() == '{"protected": true}\n'
    assert standard.read_text() == "{}\n"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    ("blocking_name", "blocking_kind", "message"),
    [
        ("quick", "file", "parent must be a directory"),
        ("Quick", "directory", "non-portable casing"),
    ],
)
def test_expected_result_parent_preflight_preserves_existing_evidence(
    tmp_path: Path,
    force: bool,
    blocking_name: str,
    blocking_kind: str,
    message: str,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    evidence = run / "other" / "kept.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"kept": true}\n', encoding="utf-8")
    blocker = run / blocking_name
    if blocking_kind == "directory":
        blocker.mkdir()
    else:
        blocker.write_text("blocking file\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        module.prepare_standard_result_namespace(
            run,
            {},
            ["quick/example.json"],
            force=force,
        )

    assert evidence.read_text() == '{"kept": true}\n'
    if blocking_kind == "directory":
        assert blocker.is_dir()
    else:
        assert blocker.read_text() == "blocking file\n"


def test_expected_result_parents_reject_casefold_collisions_before_deletion(
    tmp_path: Path,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    evidence = run / "other" / "kept.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="collide on case-insensitive filesystems"):
        module.prepare_standard_result_namespace(
            run,
            {},
            ["quick/one.json", "Quick/two.json"],
            force=True,
        )

    assert evidence.read_text() == "{}\n"


@pytest.mark.parametrize(
    "result_key",
    [
        "Universal/example.json",
        "UNIVERSAL-SELECTED/example.json",
        "_provenance.json/example.json",
        "_Provenance.json/example.json",
        "_standard_manifest.json/example.json",
        "_STANDARD_MANIFEST.JSON/example.json",
        "quick/_standard_manifest.json",
        "quick/_STANDARD_MANIFEST.JSON",
        "quick/result.txt",
        "quick/result.JSON",
        "quick//result.json",
    ],
)
def test_unsafe_expected_result_keys_fail_before_force_deletes_evidence(
    tmp_path: Path,
    result_key: str,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    evidence = run / "other" / "kept.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe expected standard result key"):
        module.prepare_standard_result_namespace(
            run,
            {},
            [result_key],
            force=True,
        )

    assert evidence.read_text() == "{}\n"


def test_expected_result_leaf_casefold_collision_fails_before_force_deletion(
    tmp_path: Path,
) -> None:
    module = _harness("scenarios")
    run = tmp_path / "run"
    evidence = run / "other" / "kept.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="result keys collide"):
        module.prepare_standard_result_namespace(
            run,
            {},
            ["quick/Foo.json", "quick/foo.json"],
            force=True,
        )

    assert evidence.read_text() == "{}\n"


def test_canonical_scenario_cannot_override_execution_strategy(tmp_path: Path) -> None:
    module = _harness("scenarios")
    path = tmp_path / "quick.toml"
    path.write_text('[example]\nresolution = "lowest"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="declares resolution policy.*example"):
        module.load_standard_corpus([path])


def test_canonical_scenario_name_must_be_a_portable_filename(tmp_path: Path) -> None:
    module = _harness("scenarios")
    path = tmp_path / "quick.toml"
    path.write_text(
        '["../escape"]\npython_version = "3.11"\nrequirements = []\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-portable scenario name"):
        module.load_standard_corpus([path])


@pytest.mark.parametrize("name", ["_standard_manifest", "_Provenance"])
def test_canonical_scenario_name_cannot_collide_with_result_metadata(
    tmp_path: Path,
    name: str,
) -> None:
    module = _harness("scenarios")
    path = tmp_path / "quick.toml"
    path.write_text(f"[{name}]\nrequirements = []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-portable scenario name"):
        module.load_standard_corpus([path])


def test_canonical_scenario_names_cannot_collide_case_insensitively(
    tmp_path: Path,
) -> None:
    module = _harness("scenarios")
    path = tmp_path / "quick.toml"
    path.write_text(
        '[Example]\npython_version = "3.11"\nrequirements = []\n'
        '[example]\npython_version = "3.11"\nrequirements = []\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="case-insensitive scenario-name"):
        module.load_standard_corpus([path])


def test_matrix_uses_historical_result_paths_and_partitions_unsupported() -> None:
    module = _harness("scenarios")
    supported = module.StandardScenario(
        "quick", "example", {"python_version": "3.11", "requirements": []}
    )
    unsupported = module.StandardScenario(
        "quick", "unsupported", {"unsupported_reason": "reviewed"}
    )

    assert [
        execution.result_key
        for execution in module.standard_run_plan(
            [supported, unsupported],
            module.STANDARD_STRATEGIES,
            module.BenchmarkHost.current(120),
        ).executions
    ] == [
        "quick/example.json",
        "quick-lowest/example.json",
        "quick-lowest-direct/example.json",
    ]
    assert module.standard_execution_keys(
        [supported, unsupported], module.STANDARD_STRATEGIES
    ) == [
        "quick-lowest-direct/example.json",
        "quick-lowest-direct/unsupported.json",
        "quick-lowest/example.json",
        "quick-lowest/unsupported.json",
        "quick/example.json",
        "quick/unsupported.json",
    ]


@pytest.mark.parametrize(
    ("field_path", "forged_value"),
    [
        (("benchmark_schema",), 1.0),
        (("trust_unverified_sdist_deps",), 1),
        (("source", "dirty"), 0),
    ],
)
def test_result_validation_rejects_json_type_coercions(
    field_path: tuple[str, ...],
    forged_value: object,
) -> None:
    module = _harness("scenarios")
    execution = module.StandardExecution(
        module.StandardScenario(
            "quick",
            "example",
            {"python_version": "3.11", "requirements": ["demo"]},
        ),
        module.ResolutionStrategy.HIGHEST,
    )
    payload_source = dict(_CLEAN_SOURCE)
    payload = _result_payload(
        module,
        execution,
        "run",
        payload_source,
        "f" * 64,
    )
    expected_input = json.loads(json.dumps(payload["input"]))
    assert isinstance(expected_input, dict)
    target = expected_input
    for key in field_path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = forged_value

    assert not module._standard_result_data_valid(
        payload,
        expected_input,
    )


def test_result_cache_identity_includes_the_host_settings_hash() -> None:
    module = _harness("scenarios")
    row = module.StandardScenario(
        "quick",
        "example",
        {"python_version": "3.11", "requirements": ["demo"]},
    )
    host = module.BenchmarkHost.current(module.SCENARIO_WALL_TIMEOUT_SECONDS)
    plan = module.standard_run_plan(
        [row],
        (module.ResolutionStrategy.HIGHEST,),
        host,
    )
    execution = plan.executions[0]
    target = plan.targets_by_logical_key[row.logical_key]
    first = module.prepare_standard_execution(
        execution,
        target,
        commit="run",
        source=dict(_CLEAN_SOURCE),
        corpus_hash="f" * 64,
        settings_digest="first-host",
    )
    second = module.prepare_standard_execution(
        execution,
        target,
        commit="run",
        source=dict(_CLEAN_SOURCE),
        corpus_hash="f" * 64,
        settings_digest="second-host",
    )
    stats = dict.fromkeys(module._STANDARD_COUNTER_FIELDS, 0)
    stats["wall_time_seconds"] = 0.01
    payload = {
        "input": first.expected_input,
        "result": {"success": True, "error": None},
        "stats": stats,
    }

    assert not module._standard_result_data_valid(payload, second.expected_input)


@pytest.mark.parametrize(
    ("matrix", "expected_strategies", "expected_completed"),
    [
        (False, ["highest"], ["quick/example.json"]),
        (
            True,
            ["highest", "lowest", "lowest-direct"],
            [
                "quick-lowest-direct/example.json",
                "quick-lowest/example.json",
                "quick/example.json",
            ],
        ),
    ],
)
def test_main_writes_exact_complete_default_and_matrix_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matrix: bool,
    expected_strategies: list[str],
    expected_completed: list[str],
) -> None:
    module, seen, manifest_path = _run_fake(tmp_path, monkeypatch, matrix=matrix)
    manifest = json.loads(manifest_path.read_text())

    assert [execution.strategy.value for execution in seen] == expected_strategies

    assert set(manifest) == _MANIFEST_FIELDS
    assert manifest["benchmark_schema"] == module.STANDARD_MANIFEST_SCHEMA
    assert manifest["source_start"] == _CLEAN_SOURCE
    assert manifest["source_end"] == _CLEAN_SOURCE

    assert manifest["mode"] == ("strategy-matrix" if matrix else "default")
    assert manifest["strategies"] == expected_strategies
    assert manifest["settings"]["host"]["wheel_tags_count"] > 0
    assert len(manifest["settings"]["host"]["wheel_tags_hash"]) == 64

    assert manifest["corpus_files"] == ["other", "quick"]
    assert manifest["selected_files"] == ["quick"]
    assert manifest["available_logical_keys"] == [
        "other:other",
        "quick:example",
        "quick:foreign-host",
        "quick:unsupported",
    ]
    assert manifest["selected_logical_keys"] == [
        "quick:example",
        "quick:foreign-host",
        "quick:unsupported",
    ]

    assert manifest["completed_logical_keys"] == ["quick:example"]
    assert manifest["unsupported_logical_keys"] == ["quick:unsupported"]
    assert manifest["requires_matching_host_logical_keys"] == ["quick:foreign-host"]
    assert manifest["inapplicable_logical_keys"] == ["quick:foreign-host"]

    assert manifest["completed_execution_keys"] == expected_completed
    assert manifest["file_execution_keys"] == expected_completed
    inapplicable_execution_keys = [
        key
        for key in manifest["selected_execution_keys"]
        if key.endswith("/foreign-host.json")
    ]
    assert (
        sorted(
            manifest["completed_execution_keys"]
            + manifest["unsupported_execution_keys"]
            + inapplicable_execution_keys
        )
        == manifest["selected_execution_keys"]
    )

    assert manifest["complete"] is True


def test_main_reports_host_inapplicable_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_fake(tmp_path, monkeypatch, matrix=False)

    output = capsys.readouterr().out
    assert (
        "Host-inapplicable: 1 scenario; marker environment requires wheel tags "
        "from a different host."
    ) in output
    assert "  quick:foreign-host" in output


def test_host_inapplicable_report_is_bounded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _harness("scenarios")
    logical_keys = [
        f"cases:case-{index}" for index in range(module._INAPPLICABLE_KEY_PREVIEW + 2)
    ]

    module._report_host_inapplicable(logical_keys)

    assert capsys.readouterr().out.splitlines() == [
        (
            "Host-inapplicable: 10 scenarios; marker environment requires wheel "
            "tags from a different host."
        ),
        "  " + ", ".join(logical_keys[:8]),
        "  ... 2 more; exact keys are in _standard_manifest.json.",
    ]


def test_standard_runner_output_is_accepted_by_the_comparator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenarios, _, manifest_path = _run_fake(
        tmp_path,
        monkeypatch,
        matrix=True,
    )
    comparator = _harness("compare")

    run = comparator.load_run(manifest_path.parent)

    assert comparator._STANDARD_COUNTER_FIELDS == scenarios._STANDARD_COUNTER_FIELDS
    assert run.manifest == json.loads(manifest_path.read_text())
    assert all(
        result["input"]["settings_hash"]
        == comparator._settings_hash(run.manifest["settings"])
        for result in run.results.values()
    )


def test_reusing_a_label_rejects_default_after_strategy_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_fake(tmp_path, monkeypatch, matrix=True)

    with pytest.raises(SystemExit) as exc_info:
        _run_fake(tmp_path, monkeypatch, matrix=False)

    assert exc_info.value.code == 2


def test_force_replaces_only_the_standard_result_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_fake(tmp_path, monkeypatch, matrix=True)
    run_dir = tmp_path / "results" / "run"
    universal = run_dir / "universal"
    universal.mkdir()
    (universal / "fixture.json").write_text("{}\n", encoding="utf-8")
    provenance = run_dir / "_provenance.json"
    provenance.write_text("{}\n", encoding="utf-8")

    _, _, manifest_path = _run_fake(
        tmp_path,
        monkeypatch,
        matrix=False,
        force=True,
    )

    assert manifest_path.is_file()
    assert (run_dir / "quick" / "example.json").is_file()
    assert not (run_dir / "quick-lowest").exists()
    assert not (run_dir / "quick-lowest-direct").exists()
    assert (universal / "fixture.json").read_text() == "{}\n"
    assert provenance.read_text() == "{}\n"


def test_reusing_a_label_rejects_full_to_subset_and_force_prunes_stale_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_fake(tmp_path, monkeypatch, matrix=False, select_quick=False)

    with pytest.raises(SystemExit) as exc_info:
        _run_fake(tmp_path, monkeypatch, matrix=False)
    assert exc_info.value.code == 2

    _run_fake(tmp_path, monkeypatch, matrix=False, force=True)
    run_dir = tmp_path / "results" / "run"
    assert not (run_dir / "other").exists()
    assert (run_dir / "quick" / "example.json").is_file()


def test_reusing_a_label_rejects_changed_corpus_and_force_refreshes_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_fake(tmp_path, monkeypatch, matrix=False)

    with pytest.raises(SystemExit) as exc_info:
        _run_fake(tmp_path, monkeypatch, matrix=False, requirement="demo-two")
    assert exc_info.value.code == 2

    _run_fake(
        tmp_path,
        monkeypatch,
        matrix=False,
        requirement="demo-two",
        force=True,
    )
    payload = json.loads(
        (tmp_path / "results" / "run" / "quick" / "example.json").read_text()
    )
    assert payload["input"]["requirements"] == ["demo-two"]


def test_force_removes_results_for_a_scenario_deleted_from_the_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_fake(tmp_path, monkeypatch, matrix=False, select_quick=False)

    with pytest.raises(SystemExit) as exc_info:
        _run_fake(
            tmp_path,
            monkeypatch,
            matrix=False,
            select_quick=False,
            include_other=False,
        )
    assert exc_info.value.code == 2

    _run_fake(
        tmp_path,
        monkeypatch,
        matrix=False,
        select_quick=False,
        include_other=False,
        force=True,
    )
    run_dir = tmp_path / "results" / "run"
    assert not (run_dir / "other").exists()
    assert (run_dir / "quick" / "example.json").is_file()


def test_stale_result_file_requires_force_and_is_then_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_fake(tmp_path, monkeypatch, matrix=False)
    stale = tmp_path / "results" / "run" / "stale.json"
    stale.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_fake(tmp_path, monkeypatch, matrix=False)
    assert exc_info.value.code == 2

    _run_fake(tmp_path, monkeypatch, matrix=False, force=True)
    assert not stale.exists()


@pytest.mark.parametrize(
    "result_kind", ["missing", "malformed", "changed-input", "extra", "nested-special"]
)
def test_manifest_stays_incomplete_for_invalid_result_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_kind: str,
) -> None:
    with pytest.raises(SystemExit, match="1"):
        _run_fake(
            tmp_path,
            monkeypatch,
            matrix=False,
            result_kind=result_kind,
        )

    manifest = json.loads(
        (tmp_path / "results" / "run" / "_standard_manifest.json").read_text()
    )
    assert manifest["complete"] is False
    if result_kind in {"missing", "malformed", "changed-input"}:
        assert manifest["completed_execution_keys"] == []
    if result_kind == "extra":
        assert "extra.json" in manifest["file_execution_keys"]
    if result_kind == "nested-special":
        assert "nested/_standard_manifest.json" in manifest["file_execution_keys"]


@pytest.mark.parametrize(
    "end_source",
    [
        {"commit": "a" * 40, "dirty": True, "diff_hash": "b" * 64},
        {"commit": "c" * 40, "dirty": False, "diff_hash": None},
    ],
)
def test_manifest_stays_incomplete_when_source_changes_during_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    end_source: dict[str, object],
) -> None:
    with pytest.raises(SystemExit, match="1"):
        _run_fake(
            tmp_path,
            monkeypatch,
            matrix=False,
            end_source=end_source,
        )

    manifest = json.loads(
        (tmp_path / "results" / "run" / "_standard_manifest.json").read_text()
    )
    assert manifest["completed_execution_keys"] == ["quick/example.json"]
    assert manifest["source_start"] == _CLEAN_SOURCE
    assert manifest["source_end"] == end_source
    assert manifest["complete"] is False


@pytest.mark.parametrize("explicit", [False, True])
def test_main_rejects_a_traversing_result_label_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    module = _harness("scenarios")
    results = tmp_path / "results"
    monkeypatch.setattr(module, "RESULTS_DIR", results)
    monkeypatch.setattr(module, "get_git_commit", lambda: "../escape")
    argv = ["--commit", "../escape"] if explicit else []

    with pytest.raises(SystemExit) as exc_info:
        module.main(argv)

    assert exc_info.value.code == 2
    assert not results.exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--toml", "quick-lowest"], "was retired.*--toml quick --strategy-matrix"),
        (["--toml", "absent"], "unknown TOML stem.*absent"),
        (["--toml", "quick", "--toml", "quick"], "duplicate TOML stem.*quick"),
    ],
)
def test_selector_errors_are_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    message: str,
) -> None:
    module = _harness("scenarios")
    scenarios_dir = tmp_path / "scenarios"
    _write_corpus(scenarios_dir)
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(module, "get_git_source_state", lambda: _CLEAN_SOURCE)

    with pytest.raises(SystemExit) as exc_info:
        module.main(["--commit", "run", *arguments])

    assert exc_info.value.code == 2
    assert re.search(message, capsys.readouterr().err)
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(
    ("invalid_toml", "message"),
    [
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
trust_unverified_sdist_deps = "false"
""",
            "other:other: trust_unverified_sdist_deps must be a boolean, got str",
        ),
        (
            """
[other]
python_version = "3.11"
requirements = "demo"
unsupported_reason = "not runnable"
""",
            "other:other: requirements must be a list, got str",
        ),
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
project_name = "demo-project"
project_extras = ["all"]
[other.optional_dependencies]
all = "demo"
""",
            "other:other: optional_dependencies['all'] must be a list, got str",
        ),
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
vcs_require_pin = "false"
project_name = "demo-project"
project_extras = ["all"]
[other.optional_dependencies]
all = "demo"
""",
            "other:other: vcs_require_pin must be a boolean, got str",
        ),
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
vcs_require_pin = "false"
""",
            "other:other: vcs_require_pin must be a boolean, got str",
        ),
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
vcs_policy = "permit"
""",
            "other:other: vcs_policy must be one of ['allow', 'block'], got 'permit'",
        ),
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
vcs_policy = { allow = false }
""",
            (
                "other:other: vcs_policy must be one of ['allow', 'block'], "
                "got {'allow': False}"
            ),
        ),
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
vcs_allowed_schemes = { "git+https" = false }
""",
            "other:other: vcs_allowed_schemes must be a list, got dict",
        ),
        (
            """
[other]
python_version = "3.11"
requirements = []
unsupported_reason = "not runnable"
vcs_allowed_repos = { "https://example.test/repo" = false }
""",
            "other:other: vcs_allowed_repos must be a list, got dict",
        ),
    ],
    ids=(
        "sdist-trust",
        "requirements",
        "project-metadata",
        "vcs-before-project-metadata",
        "vcs-require-pin",
        "vcs-policy",
        "vcs-policy-table",
        "vcs-scheme-table",
        "vcs-repo-table",
    ),
)
def test_unselected_unsupported_definition_fails_before_host_capture_and_result_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid_toml: str,
    message: str,
) -> None:
    module = _harness("scenarios")
    scenarios_dir = tmp_path / "scenarios"
    _write_corpus(scenarios_dir)
    (scenarios_dir / "other.toml").write_text(
        invalid_toml.lstrip(),
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(module, "get_git_source_state", lambda: _CLEAN_SOURCE)

    def fail_host(*_args: object, **_kwargs: object) -> object:
        pytest.fail("scenario validation reached host capture")

    def fail_result_creation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("result namespace creation was reached")

    monkeypatch.setattr(module.BenchmarkHost, "current", classmethod(fail_host))
    monkeypatch.setattr(
        module,
        "prepare_standard_result_namespace",
        fail_result_creation,
    )

    with pytest.raises(SystemExit) as exc_info:
        module.main(["--commit", "run", "--toml", "quick"])

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err
    assert not results_dir.exists()


def test_strategy_sweep_is_only_a_matrix_forwarder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness("strategy_sweep")
    seen: list[list[str]] = []

    def capture(argv: list[str]) -> None:
        seen.append(argv)

    monkeypatch.setattr(module.scenarios, "main", capture)

    module.main(["--toml", "quick", "--force"])

    assert seen == [["--strategy-matrix", "--toml", "quick", "--force"]]


def test_profile_runner_accepts_an_explicit_strategy() -> None:
    module = _harness("_profile_runner")
    scenario = {"python_version": "3.11", "requirements": []}

    default = module.build_inputs("example", scenario)
    lowest = module.build_inputs(
        "example", scenario, module.sc.ResolutionStrategy.LOWEST
    )

    assert default["config"].resolution is module.sc.ResolutionStrategy.HIGHEST
    assert lowest["config"].resolution is module.sc.ResolutionStrategy.LOWEST


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "vcs_require_pin",
            "false",
            "example: vcs_require_pin must be a boolean, got str",
        ),
        (
            "vcs_policy",
            "permit",
            "example: vcs_policy must be one of ['allow', 'block'], got 'permit'",
        ),
        (
            "vcs_allowed_schemes",
            {"git+https": False},
            "example: vcs_allowed_schemes must be a list, got dict",
        ),
        (
            "vcs_allowed_repos",
            {"https://example.test/repo": False},
            "example: vcs_allowed_repos must be a list, got dict",
        ),
    ],
    ids=("pin", "policy", "scheme-table", "repo-table"),
)
def test_profile_main_validates_vcs_settings_before_host_capture(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    module = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        field: value,
    }
    monkeypatch.setattr(module, "find_scenario", lambda _spec: ("example", scenario))
    monkeypatch.setattr(module.sys, "argv", ["_profile_runner.py", "example"])

    def fail_host(*_args: object, **_kwargs: object) -> object:
        pytest.fail("VCS validation reached host capture")

    monkeypatch.setattr(module.sc.BenchmarkHost, "current", classmethod(fail_host))

    with pytest.raises((TypeError, ValueError), match=re.escape(message)):
        module.main()


def test_standard_canary_and_profile_build_the_same_project_config() -> None:
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = _runner_parity_scenario()
    original = deepcopy(scenario)
    standard_execution, canary_execution, profile_inputs = _prepare_runner_parity(
        standard,
        canary,
        profile,
        scenario=scenario,
    )

    assert (
        standard_execution.config == canary_execution.config == profile_inputs["config"]
    )
    assert standard_execution.config.constraints == ()
    assert standard_execution.constraint_strings == ["demo<2"]

    assert "trust_unverified_sdist_deps" not in standard_execution.expected_input
    assert standard_execution.expected_input["vcs_policy"] == "allow"
    assert standard_execution.expected_input["vcs_allowed_schemes"] == ["git+https"]
    assert standard_execution.expected_input["vcs_allowed_repos"] == [
        "https://example.test/project"
    ]
    assert standard_execution.expected_input["project_name"] == "Demo_Project"
    assert standard_execution.expected_input["project_extras"] == [
        "All_Features",
        "Direct_Use",
    ]

    assert standard_execution.expected_input["requirements"] == [
        "demo[feature]>=1",
        "Project_Leaf>=2",
        "Second.Leaf",
        "Another_Leaf==1",
    ]
    assert set(canary_execution.requirements) == {
        "another-leaf",
        "demo",
        "demo[feature]",
        "project-leaf",
        "second-leaf",
    }
    assert set(profile_inputs["requirements"]) == {
        "another-leaf",
        "demo",
        "demo[feature]",
        "project-leaf",
        "second-leaf",
    }

    assert scenario == original


@pytest.mark.parametrize(
    ("vcs_settings", "message"),
    [
        (
            {},
            "example: optional_dependencies['all'] must be a list, got str",
        ),
        (
            {"vcs_require_pin": "false"},
            "example: vcs_require_pin must be a boolean, got str",
        ),
    ],
    ids=("project-metadata", "vcs-precedence"),
)
def test_all_runners_validate_project_metadata_in_schema_order(
    vcs_settings: dict[str, object],
    message: str,
) -> None:
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        "project_name": "demo-project",
        "project_extras": ["all"],
        "optional_dependencies": {"all": "demo"},
        **vcs_settings,
    }
    host = _linux_host(standard)

    with pytest.raises(TypeError, match=re.escape(message)):
        _prepare_standard_scenario(standard, scenario, host)

    with pytest.raises(TypeError, match=re.escape(message)):
        canary._prepare_canary_execution(
            scenario,
            scenario_name="example",
            resolution_override=None,
            host=host,
        )

    with pytest.raises(TypeError, match=re.escape(message)):
        profile.build_inputs("example", scenario, host=host)


def test_profile_project_metadata_validation_precedes_host_admission() -> None:
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        "project_name": "demo-project",
        "project_extras": ["all"],
        "optional_dependencies": {"all": "demo"},
    }

    class UnusedHost:
        """Fail if project-metadata validation reaches target admission."""

        def target_for(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("project-metadata validation reached host admission")

    with pytest.raises(
        TypeError,
        match=re.escape(
            "example: optional_dependencies['all'] must be a list, got str"
        ),
    ):
        profile.build_inputs("example", scenario, host=UnusedHost())


@pytest.mark.parametrize(
    ("vcs_setting", "expected"),
    [
        ({}, True),
        ({"vcs_require_pin": True}, True),
        ({"vcs_require_pin": False}, False),
    ],
    ids=("implicit", "required", "optional"),
)
def test_standard_canary_and_profile_use_valid_vcs_pin_settings(
    vcs_setting: dict[str, object],
    expected: bool,
) -> None:
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        **vcs_setting,
    }
    original = dict(scenario)

    standard_execution, canary_execution, profile_inputs = _prepare_runner_parity(
        standard,
        canary,
        profile,
        scenario=scenario,
    )

    assert standard_execution.config.vcs.require_pin is expected
    assert canary_execution.config.vcs.require_pin is expected
    assert profile_inputs["config"].vcs.require_pin is expected
    assert scenario == original


@pytest.mark.parametrize(
    ("vcs_setting", "expected"),
    [
        ({}, "block"),
        ({"vcs_policy": "block"}, "block"),
        ({"vcs_policy": "allow"}, "allow"),
    ],
    ids=("implicit", "block", "allow"),
)
def test_vcs_policy_matches_across_parser_and_runners(
    vcs_setting: dict[str, object],
    expected: str,
) -> None:
    config_parser = _harness("benchmark_config")
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        **vcs_setting,
    }
    original = dict(scenario)
    expected_policy = standard.VcsPolicy(expected)

    parsed_config = config_parser.parse_scenario_vcs_config("example", scenario)
    standard_execution, canary_execution, profile_inputs = _prepare_runner_parity(
        standard,
        canary,
        profile,
        scenario=scenario,
    )

    assert parsed_config.policy is expected_policy
    assert standard_execution.config.vcs.policy is expected_policy
    assert canary_execution.config.vcs.policy is expected_policy
    assert profile_inputs["config"].vcs.policy is expected_policy
    assert scenario == original


def test_sdist_trust_validation_precedes_requirement_and_vcs_validation() -> None:
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": "demo",
        "trust_unverified_sdist_deps": "false",
        "vcs_require_pin": "false",
        "vcs_policy": "permit",
        "vcs_allowed_schemes": {"git+https": False},
        "vcs_allowed_repos": {"https://example.test/repo": False},
    }
    host = _linux_host(standard)
    message = "example: trust_unverified_sdist_deps must be a boolean"

    with pytest.raises(TypeError, match=message):
        _prepare_standard_scenario(standard, scenario, host)

    with pytest.raises(TypeError, match=message):
        canary._prepare_canary_execution(
            scenario,
            scenario_name="example",
            resolution_override=None,
            host=host,
        )

    with pytest.raises(TypeError, match=message):
        profile.build_inputs("example", scenario, host=host)


def test_standard_canary_and_profile_reject_non_boolean_vcs_require_pin() -> None:
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        "vcs_require_pin": "false",
        "vcs_policy": "permit",
        "vcs_allowed_schemes": {"git+https": False},
        "vcs_allowed_repos": {"https://example.test/repo": False},
    }
    standard_host = _linux_host(standard)
    message = "example: vcs_require_pin must be a boolean, got str"

    class UnusedHost:
        """Fail if schema validation reaches target admission."""

        def target_for(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("VCS pin validation reached host admission")

    with pytest.raises(TypeError, match=message):
        _prepare_standard_scenario(standard, scenario, standard_host)

    with pytest.raises(TypeError, match=message):
        canary._prepare_canary_execution(
            scenario,
            scenario_name="example",
            resolution_override=None,
            host=UnusedHost(),
        )

    with pytest.raises(TypeError, match=message):
        profile.build_inputs("example", scenario, host=UnusedHost())


@pytest.mark.parametrize(
    ("vcs_settings", "message"),
    [
        (
            {
                "vcs_policy": "permit",
                "vcs_allowed_schemes": {"git+https": False},
                "vcs_allowed_repos": {"https://example.test/repo": False},
            },
            "example: vcs_policy must be one of ['allow', 'block'], got 'permit'",
        ),
        (
            {
                "vcs_allowed_schemes": {"git+https": False},
                "vcs_allowed_repos": {"https://example.test/repo": False},
            },
            "example: vcs_allowed_schemes must be a list, got dict",
        ),
        (
            {"vcs_allowed_repos": {"https://example.test/repo": False}},
            "example: vcs_allowed_repos must be a list, got dict",
        ),
    ],
    ids=("policy", "schemes", "repos"),
)
def test_standard_canary_and_profile_validate_vcs_settings_in_order(
    vcs_settings: dict[str, object],
    message: str,
) -> None:
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        **vcs_settings,
    }
    standard_host = _linux_host(standard)

    class UnusedHost:
        """Fail if VCS validation reaches target admission."""

        def target_for(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("VCS validation reached host admission")

    with pytest.raises((TypeError, ValueError), match=re.escape(message)):
        _prepare_standard_scenario(standard, scenario, standard_host)

    with pytest.raises((TypeError, ValueError), match=re.escape(message)):
        canary._prepare_canary_execution(
            scenario,
            scenario_name="example",
            resolution_override=None,
            host=UnusedHost(),
        )

    with pytest.raises((TypeError, ValueError), match=re.escape(message)):
        profile.build_inputs("example", scenario, host=UnusedHost())


@pytest.mark.parametrize("field", ["requirements", "constraints"])
def test_standard_requirement_list_validation_precedes_vcs_pin_validation(
    field: str,
) -> None:
    standard = _harness("scenarios")
    scenario: dict[str, object] = {
        "python_version": "3.11",
        "requirements": [],
        "vcs_require_pin": "false",
        "vcs_policy": "permit",
        "vcs_allowed_schemes": {"git+https": False},
        "vcs_allowed_repos": {"https://example.test/repo": False},
    }
    scenario[field] = "demo"
    host = _linux_host(standard)

    with pytest.raises(
        TypeError,
        match=rf"example: {field} must be a list, got str",
    ):
        _prepare_standard_scenario(standard, scenario, host)


@pytest.mark.parametrize("field", ["requirements", "constraints"])
def test_canary_requirement_list_validation_precedes_vcs_pin_validation(
    field: str,
) -> None:
    canary = _harness("canary")
    scenario: dict[str, object] = {
        "python_version": "3.11",
        "requirements": [],
        "vcs_require_pin": "false",
        "vcs_policy": "permit",
        "vcs_allowed_schemes": {"git+https": False},
        "vcs_allowed_repos": {"https://example.test/repo": False},
    }
    scenario[field] = "demo"
    host = _linux_host(canary)

    with pytest.raises(
        TypeError,
        match=rf"example: {field} must be a list, got str",
    ):
        canary._prepare_canary_execution(
            scenario,
            scenario_name="example",
            resolution_override=None,
            host=host,
        )


@pytest.mark.parametrize("field", ["requirements", "constraints"])
def test_profile_requirement_list_validation_precedes_vcs_pin_validation_and_host_admission(
    field: str,
) -> None:
    profile = _harness("_profile_runner")
    scenario: dict[str, object] = {
        "python_version": "3.11",
        "requirements": [],
        "vcs_require_pin": "false",
        "vcs_policy": "permit",
        "vcs_allowed_schemes": {"git+https": False},
        "vcs_allowed_repos": {"https://example.test/repo": False},
    }
    scenario[field] = "demo"
    host = MagicMock()

    with pytest.raises(
        TypeError,
        match=rf"example: {field} must be a list, got str",
    ):
        profile.build_inputs("example", scenario, host=host)

    host.target_for.assert_not_called()


def test_sdist_trust_validation_precedes_host_admission() -> None:
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        "platform_system": "Windows",
        "requires_matching_host": True,
        "trust_unverified_sdist_deps": "false",
    }
    host = _linux_host(canary)
    message = "example: trust_unverified_sdist_deps must be a boolean"

    with pytest.raises(TypeError, match=message):
        canary._prepare_canary_execution(
            scenario,
            scenario_name="example",
            resolution_override=None,
            host=host,
        )

    with pytest.raises(TypeError, match=message):
        profile.build_inputs("example", scenario, host=host)


def test_standard_canary_and_profile_prepare_the_same_resolver_inputs() -> None:
    standard = _harness("scenarios")
    canary = _harness("canary")
    profile = _harness("_profile_runner")
    standard_execution, canary_execution, profile_inputs = _prepare_runner_parity(
        standard,
        canary,
        profile,
    )

    marker_environment = dict(standard_execution.target.marker_env)
    standard_requirements = standard.parse_requirements(
        standard_execution.requirement_strings,
        vcs_config=standard_execution.config.vcs,
        marker_environment=marker_environment,
    )
    standard_constraints = standard.parse_requirements(
        standard_execution.constraint_strings,
        vcs_config=standard_execution.config.vcs,
        marker_environment=marker_environment,
    )
    standard_resolver_inputs = standard.build_benchmark_resolver_inputs(
        standard_requirements,
        standard_constraints,
    )
    canary_resolver_inputs = canary.build_benchmark_resolver_inputs(
        canary_execution.requirements,
        canary_execution.constraints,
    )
    profile_resolver_inputs = profile.sc.build_benchmark_resolver_inputs(
        profile_inputs["requirements"],
        profile_inputs["constraints"],
    )

    assert standard_resolver_inputs == canary_resolver_inputs == profile_resolver_inputs
    assert standard_resolver_inputs.root_extras == {("demo", "feature")}
    assert set(standard_resolver_inputs.constraints or {}) == {
        "demo",
        "demo[feature]",
    }


def test_profile_runner_uses_scenario_sdist_trust_policy() -> None:
    module = _harness("_profile_runner")
    base = {"python_version": "3.11", "requirements": []}

    implicit = module.build_inputs("implicit", base)
    trusted = module.build_inputs(
        "trusted", {**base, "trust_unverified_sdist_deps": True}
    )
    strict = module.build_inputs(
        "strict", {**base, "trust_unverified_sdist_deps": False}
    )

    assert implicit["config"].trust_unverified_sdist_deps is True
    assert trusted["config"].trust_unverified_sdist_deps is True
    assert strict["config"].trust_unverified_sdist_deps is False


def test_profile_runner_uses_the_admitted_target_for_roots_and_resolution() -> None:
    module = _harness("_profile_runner")
    physical_host = _host(
        module.sc,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
    )
    admission = physical_host.target_for(
        "3.11",
        {},
        requires_matching_host=False,
    )
    assert admission.target is not None

    class PlannedHost:
        target = physical_host.target

        def target_for(
            self,
            python_version: str,
            marker_environment: dict[str, str],
            *,
            requires_matching_host: bool,
        ) -> object:
            assert python_version == "3.11"
            assert marker_environment == {}
            assert requires_matching_host is False
            return admission

    host = PlannedHost()
    inputs = module.build_inputs(
        "example",
        {
            "python_version": "3.11",
            "requirements": [
                "selected; python_version == '3.11'",
                "excluded; python_version != '3.11'",
            ],
        },
        host=host,
    )

    assert set(inputs["requirements"]) == {"selected"}
    assert inputs["target"] is admission.target
    assert inputs["host"] is host


def test_profile_runner_rejects_supported_marker_build_policy() -> None:
    module = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": ["demo"],
        "platform_system": "Linux",
        "build_packages": ["demo"],
    }

    with pytest.raises(
        ValueError,
        match=(
            "example: build_packages cannot be combined "
            "with a marker environment overlay"
        ),
    ):
        module.build_inputs("example", scenario)


def test_profile_runner_rejects_an_inapplicable_host_before_resolution() -> None:
    module = _harness("_profile_runner")
    scenario = {
        "python_version": "3.11",
        "requirements": [],
        "marker_environment": {"platform_system": "Windows"},
        "requires_matching_host": True,
    }
    host = _host(
        module.sc,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
    )

    with pytest.raises(SystemExit, match="benchmark is inapplicable"):
        module.build_inputs("example", scenario, host=host)


def test_profile_runner_allows_target_only_foreign_overlay() -> None:
    module = _harness("_profile_runner")
    host = _host(
        module.sc,
        system="Linux",
        sys_platform="linux",
        machine="x86_64",
        os_name="posix",
        platform_tag="manylinux_2_17_x86_64",
    )
    inputs = module.build_inputs(
        "example",
        {
            "python_version": "3.11",
            "requirements": [],
            "marker_environment": {"platform_system": "Windows"},
        },
        host=host,
    )

    target = inputs["target"]
    assert target.marker_env["platform_system"] == "Windows"
    assert target.tags_faithful is False
