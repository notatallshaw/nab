"""Tests for the deterministic smoke suite's external uv semantic oracle."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import pytest
import tomli_w

from nab_python._vendor.packaging.requirements import Requirement

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"


class _FixtureDistribution(Protocol):
    """Distribution fields used to construct a synthetic uv lock."""

    name: str
    version: str
    dependencies: Sequence[str]


class _PreparedTarget(Protocol):
    """Target fields used to construct uv resolution markers."""

    label: str
    marker_env: Mapping[str, str]


class _PreparedSmokeScenario(Protocol):
    """Prepared fields used to construct a synthetic uv lock."""

    targets: Sequence[_PreparedTarget]
    expected: Mapping[str, Mapping[str, str]]


class _ExpectedTarget(Protocol):
    """Expected target fields used by input-mutation tests."""

    pins: Mapping[str, str]


class _SmokeScenario(Protocol):
    """Scenario fields used by the uv adapter tests."""

    id: str
    python: str
    uv_fork_strategy: str | None
    expected: Sequence[_ExpectedTarget]


def _module(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _core() -> ModuleType:
    return _module("deterministic_smoke", _BENCHMARKS / "deterministic_smoke.py")


def _adapter() -> ModuleType:
    _core()
    return _module(
        "_nab_deterministic_smoke_uv",
        _BENCHMARKS / "deterministic_smoke_uv.py",
    )


@dataclass(frozen=True, slots=True)
class _FixtureCase:
    """Materialized smoke fixture shared by the uv adapter tests."""

    core: ModuleType
    adapter: ModuleType
    root: Path
    distributions: tuple[_FixtureDistribution, ...]
    digest: str
    inventory: dict[tuple[str, str], tuple[Path, str]]

    def scenario(self, scenario_id: str) -> _SmokeScenario:
        """Return one declared scenario by identifier."""
        return next(
            scenario
            for scenario in self.core.load_scenarios()
            if scenario.id == scenario_id
        )


def _materialized_case(case: _FixtureCase, root: Path) -> _FixtureCase:
    """Materialize the same fixture at root and return its bound test data."""
    case.core.materialize_fixture(root, case.distributions, case.digest)
    return _FixtureCase(
        core=case.core,
        adapter=case.adapter,
        root=root,
        distributions=case.distributions,
        digest=case.digest,
        inventory=case.adapter._fixture_inventory(root),
    )


@pytest.fixture(scope="module")
def fixture_case(tmp_path_factory: pytest.TempPathFactory) -> _FixtureCase:
    """Build the frozen wheel fixture once for this test module."""
    core = _core()
    adapter = _adapter()
    distributions, digest = core.load_fixture()
    root = tmp_path_factory.mktemp("deterministic-smoke-uv") / "index"
    core.materialize_fixture(root, distributions, digest)
    return _FixtureCase(
        core=core,
        adapter=adapter,
        root=root,
        distributions=tuple(distributions),
        digest=digest,
        inventory=adapter._fixture_inventory(root),
    )


def _pylock_document(case: _FixtureCase, pins: dict[str, str]) -> dict[str, object]:
    """Build a pylock document for fixture-backed pins."""
    packages: list[dict[str, object]] = []
    for name, version in pins.items():
        wheel, digest = case.inventory[(name, version)]
        packages.append(
            {
                "name": name,
                "version": version,
                "wheels": [
                    {
                        "url": wheel.as_uri(),
                        "hashes": {"sha256": digest},
                    }
                ],
            }
        )
    return {"lock-version": "1.0", "created-by": "uv", "packages": packages}


def _lock_document(
    case: _FixtureCase,
    data: Mapping[str, Any],
    directory: Path,
) -> Any:
    """Bind parsed test data to the directory used by its relative paths."""
    return case.adapter._LockDocument(data, directory.resolve())


def _resolved_relative_path(path: Path, *, start: Path) -> str:
    """Return a POSIX path relative to the resolved start directory."""
    return Path(os.path.relpath(path.resolve(), start.resolve())).as_posix()


def _write_pylock(
    path: Path,
    case: _FixtureCase,
    pins: Mapping[str, str],
    *,
    artifact_location: str | None = None,
    relative_artifacts: bool = False,
) -> None:
    """Write a minimal uv pylock, optionally overriding its artifact location."""
    lines = ['lock-version = "1.0"', 'created-by = "uv"']
    for name, version in pins.items():
        wheel, digest = case.inventory[(name, version)]
        location = artifact_location
        if location is None:
            location = (
                _resolved_relative_path(wheel, start=path.parent)
                if relative_artifacts
                else wheel.as_uri()
            )
        lines.extend(
            (
                "",
                "[[packages]]",
                f"name = {json.dumps(name)}",
                f"version = {json.dumps(version)}",
                (
                    "wheels = [{ url = "
                    f"{json.dumps(location)}, hashes = {{ sha256 = {json.dumps(digest)} }} }}]"
                ),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _target_markers(prepared: _PreparedSmokeScenario) -> dict[str, str]:
    """Return uv resolution markers keyed by prepared target label."""
    return {
        target.label: (
            f"python_version == '{target.marker_env['python_version']}' and "
            f"sys_platform == '{target.marker_env['sys_platform']}'"
        )
        for target in prepared.targets
    }


def _translated_dependencies(
    distribution: _FixtureDistribution,
) -> list[dict[str, str]]:
    """Translate fixture requirements into uv lock dependency records."""
    dependencies: list[dict[str, str]] = []
    for text in distribution.dependencies:
        requirement = Requirement(text)
        dependency = {"name": requirement.name}
        if requirement.marker is not None:
            dependency["marker"] = str(requirement.marker)
        dependencies.append(dependency)
    return dependencies


def _fixture_package_record(
    case: _FixtureCase,
    distribution: _FixtureDistribution,
    selected_targets: set[str],
    target_markers: Mapping[str, str],
) -> dict[str, object]:
    """Build one uv lock package record from a fixture artifact."""
    name = case.core.canonicalize_name(distribution.name)
    wheel, digest = case.inventory[(name, distribution.version)]
    package: dict[str, object] = {
        "name": name,
        "version": distribution.version,
        "source": {"registry": case.root.resolve().as_uri()},
        "wheels": [{"path": str(wheel), "hash": f"sha256:{digest}"}],
    }
    if selected_targets != set(target_markers):
        package["resolution-markers"] = [
            target_markers[target] for target in sorted(selected_targets)
        ]

    dependencies = _translated_dependencies(distribution)
    if dependencies:
        package["dependencies"] = dependencies
    return package


def _selected_fixture_packages(
    case: _FixtureCase,
    prepared: _PreparedSmokeScenario,
    target_markers: Mapping[str, str],
) -> list[dict[str, object]]:
    """Build uv lock package records for every selected fixture version."""
    distribution_by_key = {
        (case.core.canonicalize_name(item.name), item.version): item
        for item in case.distributions
    }
    records = {
        (name, version)
        for pins in prepared.expected.values()
        for name, version in pins.items()
    }
    packages: list[dict[str, object]] = []
    for name, version in sorted(records):
        selected_targets = {
            target
            for target, pins in prepared.expected.items()
            if pins.get(name) == version
        }
        packages.append(
            _fixture_package_record(
                case,
                distribution_by_key[(name, version)],
                selected_targets,
                target_markers,
            )
        )
    return packages


def _uv_lock_document(
    case: _FixtureCase,
    scenario: _SmokeScenario,
    prepared: _PreparedSmokeScenario,
) -> dict[str, object]:
    """Assemble a uv lock document for one prepared smoke scenario."""
    target_markers = _target_markers(prepared)
    packages = [
        {
            "name": "nab-smoke-uv-lock",
            "version": "0.0.0",
            "source": {"virtual": "."},
            "dependencies": [{"name": "nab-smoke-universal"}],
        }
    ]
    packages.extend(_selected_fixture_packages(case, prepared, target_markers))

    platform_markers = [
        "sys_platform == 'linux'",
        "sys_platform == 'win32'",
    ]
    return {
        "version": 1,
        "requires-python": scenario.python,
        "options": {"fork-strategy": scenario.uv_fork_strategy},
        "resolution-markers": list(target_markers.values()),
        "supported-markers": platform_markers,
        "required-markers": platform_markers,
        "package": packages,
    }


def _make_uv_lock_paths_relative(
    document: dict[str, object],
    case: _FixtureCase,
    document_dir: Path,
) -> None:
    """Make fixture paths relative to the synthetic uv.lock directory."""
    relative_registry = _resolved_relative_path(case.root, start=document_dir)
    packages = document["package"]
    assert isinstance(packages, list)
    for package in packages:
        assert isinstance(package, dict)
        if package.get("source") != {"virtual": "."}:
            package["source"] = {"registry": relative_registry}
            wheels = package["wheels"]
            assert isinstance(wheels, list)
            wheel = wheels[0]
            assert isinstance(wheel, dict)
            wheel["path"] = _resolved_relative_path(
                Path(str(wheel["path"])), start=document_dir
            )
        dependencies = package.get("dependencies", [])
        assert isinstance(dependencies, list)
        for dependency in dependencies:
            assert isinstance(dependency, dict)
            dependency["source"] = {"registry": relative_registry}


def test_resolved_relative_path_uses_both_resolved_operands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical_root = tmp_path / "var"
    resolved_root = tmp_path / "private" / "var"
    lexical_path = lexical_root / "fixture" / "wheel.whl"
    lexical_start = lexical_root / "document"
    resolutions = {
        lexical_path: resolved_root / "fixture" / "wheel.whl",
        lexical_start: resolved_root / "document",
    }

    def resolve_alias(path: Path, strict: bool = False) -> Path:
        """Map a lexical path into the shared resolved test root."""
        del strict
        return resolutions[path]

    monkeypatch.setattr(Path, "resolve", resolve_alias)

    assert _resolved_relative_path(lexical_path, start=lexical_start) == (
        "../fixture/wheel.whl"
    )


def test_manifest_declares_one_exact_uv_mapping_per_scenario() -> None:
    scenarios = _core().load_scenarios()

    assert [scenario.uv_mapping for scenario in scenarios].count("pip-compile") == 9
    assert [scenario.uv_mapping for scenario in scenarios].count("lock") == 2
    assert {
        scenario.id: scenario.uv_fork_strategy
        for scenario in scenarios
        if scenario.uv_mapping == "lock"
    } == {
        "universal-aligned": "fewest",
        "universal-independent": "requires-python",
    }
    assert all(
        scenario.uv_fork_strategy is None
        for scenario in scenarios
        if scenario.uv_mapping == "pip-compile"
    )


@pytest.mark.parametrize(
    ("raw", "mode", "message"),
    [
        ({"uv-mapping": "other"}, "specific", "uv-mapping is invalid"),
        ({"uv-mapping": "lock"}, "universal", "fork-strategy"),
        (
            {"uv-mapping": "pip-compile", "uv-fork-strategy": "fewest"},
            "specific",
            "only valid for a lock",
        ),
        ({"uv-mapping": "pip-compile"}, "universal", "requires specific"),
        (
            {"uv-mapping": "lock", "uv-fork-strategy": "fewest"},
            "specific",
            "requires universal",
        ),
    ],
)
def test_uv_mapping_contract_rejects_inexact_adapters(
    raw: dict[str, object], mode: str, message: str
) -> None:
    core = _core()

    with pytest.raises(core.SmokeContractError, match=message):
        core._parse_uv_mapping(raw, "scenario[0]", mode)


def test_uv_project_declares_the_exact_universal_domain() -> None:
    adapter = _adapter()
    document = adapter.tomllib.loads(adapter.UV_PROJECT.read_text(encoding="utf-8"))

    assert document["project"]["requires-python"] == ">=3.11,<3.13"
    assert document["project"]["dependencies"] == ["nab-smoke-universal==1.0.0"]
    assert document["tool"]["uv"]["environments"] == [
        (
            "python_version >= '3.11' and python_version < '3.13'"
            " and implementation_name == 'cpython' and sys_platform == 'linux'"
            " and platform_machine == 'x86_64'"
        ),
        (
            "python_version >= '3.11' and python_version < '3.13'"
            " and implementation_name == 'cpython' and sys_platform == 'win32'"
            " and platform_machine == 'AMD64'"
        ),
    ]
    assert document["tool"]["uv"]["required-environments"] == [
        "sys_platform == 'linux' and platform_machine == 'x86_64'",
        "sys_platform == 'win32' and platform_machine == 'AMD64'",
    ]


def test_artifact_paths_use_the_document_directory(tmp_path: Path) -> None:
    adapter = _adapter()
    fixture = (tmp_path / "fixture").resolve()
    work = (tmp_path / "work").resolve()
    artifact = (fixture / "wheel.whl").resolve()
    fixture.mkdir()
    work.mkdir()
    artifact.write_bytes(b"wheel")

    relative_artifact = Path(os.path.relpath(artifact, work)).as_posix()

    assert adapter._artifact_path(str(artifact), "wheel", document_dir=work) == artifact
    assert (
        adapter._artifact_path(artifact.as_uri(), "wheel", document_dir=work)
        == artifact
    )
    assert (
        adapter._artifact_path(relative_artifact, "wheel", document_dir=work)
        == artifact
    )
    assert (
        adapter._artifact_path(
            f"file:{relative_artifact}",
            "wheel",
            document_dir=work,
        )
        == artifact
    )
    with pytest.raises(adapter.UvCrossCheckError, match="local file URL"):
        adapter._artifact_path(
            "https://example.invalid/wheel.whl",
            "wheel",
            document_dir=work,
        )
    with pytest.raises(adapter.UvCrossCheckError, match="local file URL"):
        adapter._artifact_path(
            "file://remotehost/wheel.whl",
            "wheel",
            document_dir=work,
        )
    with pytest.raises(adapter.UvCrossCheckError, match="directory is not absolute"):
        adapter._artifact_path(
            "wheel.whl",
            "wheel",
            document_dir=Path("relative"),
        )


def test_artifact_path_normalizes_platform_resolution_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()

    def fail_resolution(_path: Path, strict: bool = False) -> Path:
        del strict
        raise OSError("platform path failure")

    monkeypatch.setattr(Path, "resolve", fail_resolution)

    with pytest.raises(adapter.UvCrossCheckError, match="invalid local path"):
        adapter._artifact_path(
            "wheel.whl",
            "wheel",
            document_dir=tmp_path,
        )


def test_artifact_path_rejects_decoded_nul_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()

    def accept_path(path: Path, strict: bool = False) -> Path:
        """Model a Path.resolve implementation that accepts embedded NULs."""
        del strict
        return path

    monkeypatch.setattr(Path, "resolve", accept_path)

    with pytest.raises(adapter.UvCrossCheckError, match="invalid local path"):
        adapter._artifact_path(
            "file:%00wheel.whl",
            "wheel",
            document_dir=tmp_path,
        )


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_command_records_do_not_leak_temporary_roots(
    tmp_path: Path, separator: str
) -> None:
    adapter = _adapter()
    fixture = (tmp_path / "fixture").resolve()
    work = (tmp_path / "work").resolve()

    command = [
        "/real/uv",
        "--index",
        fixture.as_uri(),
        "--project",
        f"{work}{separator}project",
    ]
    assert adapter._command_record(command, fixture_root=fixture, work_root=work) == [
        "<uv>",
        "--index",
        "file://<fixture>",
        "--project",
        "<work>/project",
    ]


def test_pip_compile_command_carries_the_exact_offline_policy(
    fixture_case: _FixtureCase, tmp_path: Path
) -> None:
    case = fixture_case
    scenario = case.scenario("constraint-ceiling")
    prepared = case.core.prepare_scenario(scenario, case.root)

    command, output = case.adapter._pip_compile_command(
        scenario, prepared, case.root, "/tool/uv", tmp_path
    )

    assert command[:3] == ["/tool/uv", "pip", "compile"]
    assert output == tmp_path / "pylock.constraint-ceiling.toml"
    assert (tmp_path / "requirements.in").read_text(encoding="utf-8") == (
        "nab-smoke-constrained>=1.0.0\n"
    )
    assert (tmp_path / "constraints.txt").read_text(encoding="utf-8") == (
        "nab-smoke-constrained<3.0.0\n"
    )
    for flag in (
        "--offline",
        "--no-cache",
        "--no-build",
        "--no-config",
        "--no-python-downloads",
    ):
        assert flag in command
    assert "--resolution" not in command
    assert command[command.index("--python-version") + 1] == "3.11"
    assert command[command.index("--python-platform") + 1] == (
        "x86_64-unknown-linux-gnu"
    )


@pytest.mark.parametrize(
    ("scenario_id", "resolution"),
    [
        ("strategy-lowest", "lowest"),
        ("strategy-lowest-direct", "lowest-direct"),
    ],
)
def test_pip_compile_command_retains_non_default_resolution(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    scenario_id: str,
    resolution: str,
) -> None:
    case = fixture_case
    scenario = case.scenario(scenario_id)
    prepared = case.core.prepare_scenario(scenario, case.root)

    command, _ = case.adapter._pip_compile_command(
        scenario, prepared, case.root, "/tool/uv", tmp_path
    )

    assert command.count("--resolution") == 1
    assert command[command.index("--resolution") + 1] == resolution


@pytest.mark.parametrize(
    ("scenario_id", "expected_fork_strategy_argument"),
    [
        ("universal-aligned", "fewest"),
        ("universal-independent", None),
    ],
)
def test_uv_lock_command_only_passes_non_default_policy(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    scenario_id: str,
    expected_fork_strategy_argument: str | None,
) -> None:
    case = fixture_case
    scenario = case.scenario(scenario_id)
    prepared = case.core.prepare_scenario(scenario, case.root)

    command = case.adapter._uv_lock_command(scenario, case.root, "/tool/uv", tmp_path)

    assert len(prepared.targets) == 4
    assert command[:2] == ["/tool/uv", "lock"]
    assert command.count("--project") == 1
    assert "--resolution" not in command
    if expected_fork_strategy_argument is None:
        assert "--fork-strategy" not in command
    else:
        assert command.count("--fork-strategy") == 1
        assert (
            command[command.index("--fork-strategy") + 1]
            == expected_fork_strategy_argument
        )
    assert "--python-version" not in command
    assert "--python-platform" not in command


def test_uv_lock_command_retains_non_default_resolution(
    fixture_case: _FixtureCase,
    tmp_path: Path,
) -> None:
    case = fixture_case
    scenario = replace(
        case.scenario("universal-independent"),
        resolution=case.core.ResolutionStrategy.LOWEST,
    )

    command = case.adapter._uv_lock_command(scenario, case.root, "/tool/uv", tmp_path)

    assert command.count("--resolution") == 1
    assert command[command.index("--resolution") + 1] == "lowest"
    assert "--fork-strategy" not in command


def test_pip_compile_result_accepts_fixture_pins(
    fixture_case: _FixtureCase,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    prepared = case.core.prepare_scenario(scenario, case.root)
    expected = dict(next(iter(prepared.expected.values())))

    assert case.adapter._validate_pip_compile_result(
        scenario,
        prepared,
        subprocess.CompletedProcess(["uv"], 0, "", ""),
        _lock_document(case, _pylock_document(case, expected), case.root),
        case.inventory,
    ) == (prepared.expected, "success")


def test_pip_compile_validation_anchors_relative_artifacts_to_the_pylock(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    prepared = case.core.prepare_scenario(scenario, case.root)
    expected = dict(next(iter(prepared.expected.values())))
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.chdir(ambient)

    commands: list[tuple[str, ...]] = []

    def run(
        command: Sequence[str], *, cwd: Path, policy: object
    ) -> subprocess.CompletedProcess[str]:
        del cwd, policy
        commands.append(tuple(command))
        if list(command[1:]) == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "uv 1.2.3\n", "")
        output = Path(command[command.index("--output-file") + 1])
        _write_pylock(output, case, expected, relative_artifacts=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(case.adapter, "_run", run)

    report = case.adapter.verify_scenarios(
        [scenario],
        case.root,
        case.digest,
        binary,
        case.distributions,
    )

    assert report["scenarios"][0]["pins_per_target"] == prepared.expected
    assert len([command for command in commands if "compile" in command]) == 1


def test_public_verification_rejects_invalid_artifact_paths(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    prepared = case.core.prepare_scenario(scenario, case.root)
    expected = dict(next(iter(prepared.expected.values())))
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    scenario_commands: list[tuple[str, ...]] = []

    def run(
        command: Sequence[str], *, cwd: Path, policy: object
    ) -> subprocess.CompletedProcess[str]:
        del cwd, policy
        if list(command[1:]) == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "uv 1.2.3\n", "")

        scenario_commands.append(tuple(command))
        output = Path(command[command.index("--output-file") + 1])
        _write_pylock(
            output,
            case,
            expected,
            artifact_location="file:%00wheel.whl",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(case.adapter, "_run", run)

    with pytest.raises(
        case.adapter.UvCrossCheckError,
        match=r"artifact for .* has an invalid local path",
    ):
        case.adapter.verify_scenarios(
            [scenario],
            case.root,
            case.digest,
            binary,
            case.distributions,
        )

    assert len(scenario_commands) == 1


def test_pip_compile_result_rejects_mismatched_artifact_hash(
    fixture_case: _FixtureCase,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    prepared = case.core.prepare_scenario(scenario, case.root)
    expected = dict(next(iter(prepared.expected.values())))
    document = _pylock_document(case, expected)
    packages = document["packages"]
    assert isinstance(packages, list)
    package = packages[0]
    assert isinstance(package, dict)
    wheels = package["wheels"]
    assert isinstance(wheels, list)
    wheel = wheels[0]
    assert isinstance(wheel, dict)
    hashes = wheel["hashes"]
    assert isinstance(hashes, dict)
    hashes["sha256"] = "0" * 64

    with pytest.raises(case.adapter.UvCrossCheckError, match="artifact hash"):
        case.adapter._validate_pip_compile_result(
            scenario,
            prepared,
            subprocess.CompletedProcess(["uv"], 0, "", ""),
            _lock_document(case, document, case.root),
            case.inventory,
        )


def test_pip_compile_result_accepts_expected_no_solution(
    fixture_case: _FixtureCase,
) -> None:
    case = fixture_case
    scenario = case.scenario("pip-deep-backtracking-unsatisfiable")
    prepared = case.core.prepare_scenario(scenario, case.root)

    assert case.adapter._validate_pip_compile_result(
        scenario,
        prepared,
        subprocess.CompletedProcess(["uv"], 1, "", "No solution found"),
        None,
        case.inventory,
    ) == (prepared.expected, "no-solution")


def test_pip_compile_result_rejects_unrelated_failure_diagnostic(
    fixture_case: _FixtureCase,
) -> None:
    case = fixture_case
    scenario = case.scenario("pip-deep-backtracking-unsatisfiable")
    prepared = case.core.prepare_scenario(scenario, case.root)

    with pytest.raises(case.adapter.UvCrossCheckError, match="resolution failure"):
        case.adapter._validate_pip_compile_result(
            scenario,
            prepared,
            subprocess.CompletedProcess(["uv"], 1, "", "unrelated failure"),
            None,
            case.inventory,
        )


@pytest.mark.parametrize("returncode", [2, -9])
def test_no_solution_requires_uv_exit_status_one(
    fixture_case: _FixtureCase, returncode: int
) -> None:
    case = fixture_case
    scenario = case.scenario("pip-deep-backtracking-unsatisfiable")
    prepared = case.core.prepare_scenario(scenario, case.root)
    completed = subprocess.CompletedProcess(["uv"], returncode, "", "No solution found")

    with pytest.raises(
        case.adapter.UvCrossCheckError,
        match="expected uv no-solution exit status 1",
    ):
        case.adapter._validate_pip_compile_result(
            scenario, prepared, completed, None, case.inventory
        )


def test_subprocess_runner_fixes_decoding_timeout_cwd_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter()
    for name in adapter._EXECUTION_ENVIRONMENT_ALLOWLIST:
        monkeypatch.delenv(name, raising=False)
    for name in tuple(os.environ):
        if name == "UV" or name.startswith("UV_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", "/ambient/bin")
    monkeypatch.setenv("TMPDIR", "/ambient/tmp")
    monkeypatch.setenv("UV_INDEX", "https://ambient.invalid/simple")
    monkeypatch.setenv("UV_RESOLUTION", "lowest")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-inherit")
    policy = adapter._subprocess_policy()
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "uv test\n", "")

    monkeypatch.setattr(adapter.subprocess, "run", run)

    completed = adapter._run(["/tool/uv", "--version"], cwd=tmp_path, policy=policy)

    assert completed.stdout == "uv test\n"
    assert captured == {
        "capture_output": True,
        "check": False,
        "cwd": tmp_path.resolve(),
        "env": {
            "TEMP": str(tmp_path.resolve()),
            "TMP": str(tmp_path.resolve()),
            "TMPDIR": str(tmp_path.resolve()),
        },
        "timeout": adapter.UV_SUBPROCESS_TIMEOUT_SECONDS,
        "encoding": "utf-8",
        "errors": "strict",
    }
    assert policy.stripped_uv_variables == ("UV_INDEX", "UV_RESOLUTION")
    effective_environment = policy.environment_dict(tmp_path.resolve())
    assert "PATH" not in effective_environment
    assert "UNRELATED_SECRET" not in effective_environment


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            subprocess.TimeoutExpired(["uv"], 120),
            "timed out after 120 seconds",
        ),
        (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            "emitted non-UTF-8 output",
        ),
        (OSError("execution failed"), "cannot execute"),
    ],
    ids=("timeout", "decode", "os-error"),
)
def test_subprocess_failures_use_the_cross_check_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    message: str,
) -> None:
    adapter = _adapter()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(adapter.subprocess, "run", fail)

    with pytest.raises(adapter.UvCrossCheckError, match=message):
        adapter._run(["/tool/uv"], cwd=tmp_path, policy=adapter._subprocess_policy())


def test_uv_lock_validator_checks_domain_artifacts_projection_and_edges(
    fixture_case: _FixtureCase,
) -> None:
    case = fixture_case
    scenario = case.scenario("universal-aligned")
    prepared = case.core.prepare_scenario(scenario, case.root)
    document = _uv_lock_document(case, scenario, prepared)

    assert (
        case.adapter._validate_uv_lock(
            _lock_document(case, document, case.root),
            scenario,
            prepared,
            case.distributions,
            case.inventory,
            case.root,
        )
        == prepared.expected
    )

    bad_domain = copy.deepcopy(document)
    resolution_markers = bad_domain["resolution-markers"]
    assert isinstance(resolution_markers, list)
    resolution_markers[0] = "sys_platform == 'darwin'"
    with pytest.raises(case.adapter.UvCrossCheckError, match="declared target domain"):
        case.adapter._validate_uv_lock(
            _lock_document(case, bad_domain, case.root),
            scenario,
            prepared,
            case.distributions,
            case.inventory,
            case.root,
        )

    bad_edges = copy.deepcopy(document)
    packages = bad_edges["package"]
    assert isinstance(packages, list)
    project = packages[0]
    assert isinstance(project, dict)
    project["dependencies"] = []
    with pytest.raises(case.adapter.UvCrossCheckError, match="root dependency edges"):
        case.adapter._validate_uv_lock(
            _lock_document(case, bad_edges, case.root),
            scenario,
            prepared,
            case.distributions,
            case.inventory,
            case.root,
        )


def test_uv_lock_validation_anchors_relative_paths_to_the_lock(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    scenario = case.scenario("universal-aligned")
    prepared = case.core.prepare_scenario(scenario, case.root)
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    commands: list[tuple[str, ...]] = []

    def run(
        command: Sequence[str], *, cwd: Path, policy: object
    ) -> subprocess.CompletedProcess[str]:
        del policy
        commands.append(tuple(command))
        if list(command[1:]) == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "uv 1.2.3\n", "")
        document = _uv_lock_document(case, scenario, prepared)
        _make_uv_lock_paths_relative(document, case, cwd)
        (cwd / "uv.lock").write_text(
            tomli_w.dumps(document),
            encoding="utf-8",
            newline="\n",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(case.adapter, "_run", run)
    monkeypatch.chdir(ambient)

    report = case.adapter.verify_scenarios(
        [scenario],
        case.root,
        case.digest,
        binary,
        case.distributions,
    )

    assert report["scenarios"][0]["pins_per_target"] == prepared.expected
    assert len([command for command in commands if command[1:2] == ("lock",)]) == 1


def test_uv_lock_runner_uses_a_temporary_project_and_redacts_it(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    scenario = case.scenario("universal-independent")
    invocations: list[list[str]] = []

    def run(
        command: list[str], *, cwd: Path, policy: object
    ) -> subprocess.CompletedProcess[str]:
        del policy
        invocations.append(command)
        project = Path(command[command.index("--project") + 1])
        assert cwd == project.resolve()
        (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(case.adapter, "_run", run)
    lock_run = case.adapter._run_uv_lock(
        scenario,
        case.root,
        "/tool/uv",
        case.adapter._subprocess_policy(),
        project=tmp_path,
    )

    assert lock_run.document.data == {"version": 1}
    assert lock_run.document.directory == tmp_path.resolve()
    assert len(invocations) == 1
    command = invocations[0]
    assert command[:2] == ["/tool/uv", "lock"]
    assert "--fork-strategy" not in command
    assert "--resolution" not in command
    assert lock_run.command[0] == "<uv>"
    assert lock_run.command[lock_run.command.index("--project") + 1] == "<work>"
    assert lock_run.command[lock_run.command.index("--default-index") + 1] == (
        "file://<fixture>"
    )
    assert lock_run.returncode == 0


@pytest.mark.parametrize(
    ("scenario_id", "message"),
    [
        ("basic-highest", "cannot read uv pylock"),
        ("universal-independent", "cannot read uv.lock"),
    ],
)
def test_public_verification_rejects_malformed_toml_output(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_id: str,
    message: str,
) -> None:
    case = fixture_case
    scenario = case.scenario(scenario_id)
    binary = tmp_path / f"uv-{scenario_id}"
    binary.write_bytes(b"fake uv")
    scenario_commands: list[tuple[str, ...]] = []

    def run(
        command: Sequence[str], *, cwd: Path, policy: object
    ) -> subprocess.CompletedProcess[str]:
        del policy
        if list(command[1:]) == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "uv 1.2.3\n", "")
        scenario_commands.append(tuple(command))
        if "--output-file" in command:
            output = Path(command[command.index("--output-file") + 1])
        else:
            output = cwd / "uv.lock"
        output.write_text("[[malformed]\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(case.adapter, "_run", run)

    with pytest.raises(case.adapter.UvCrossCheckError, match=message):
        case.adapter.verify_scenarios(
            [scenario],
            case.root,
            case.digest,
            binary,
            case.distributions,
        )

    assert len(scenario_commands) == 1


def test_uv_binary_identity_binds_resolved_path_hash_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter()
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "uv 1.2.3\n", ""
        ),
    )

    identity = adapter._uv_binary(binary, adapter._subprocess_policy())

    assert identity.path == binary.resolve()
    assert identity.sha256 == adapter.file_sha256(binary)
    assert identity.version == "uv 1.2.3"


def _assert_report_inputs(
    case: _FixtureCase,
    report: dict[str, Any],
    scenario: _SmokeScenario,
) -> None:
    """Check the adapter, core, manifest, and scenario identities."""
    scenario_record = case.adapter._scenario_record(scenario)

    assert report["schema"] == 2
    assert report["adapter_sha256"] == case.adapter.file_sha256(
        case.adapter.ADAPTER_PATH
    )
    assert report["core_sha256"] == case.adapter.file_sha256(case.adapter.CORE_PATH)
    assert report["scenario_manifest_sha256"] == case.adapter.file_sha256(
        case.adapter.SCENARIOS_PATH
    )
    assert report["selected_scenarios_sha256"] == case.adapter._canonical_sha256(
        [scenario_record]
    )
    assert report["scenario_sha256"] == {
        scenario.id: case.adapter._canonical_sha256(scenario_record)
    }


def _assert_report_uv(
    case: _FixtureCase,
    report: dict[str, Any],
    binary: Path,
) -> None:
    """Check the uv executable and project identities."""
    assert report["uv"] == "uv 1.2.3"
    assert report["uv_binary_path"] == str(binary.resolve())
    assert report["uv_binary_sha256"] == case.adapter.file_sha256(binary)
    assert report["uv_project_sha256"] == case.adapter.file_sha256(
        case.adapter.UV_PROJECT
    )


def _assert_report_fixture(
    case: _FixtureCase,
    report: dict[str, Any],
) -> None:
    """Check the fixture inputs and before-and-after access identity."""
    assert report["fixture_manifest_sha256"] == case.adapter.file_sha256(
        case.adapter.FIXTURE_PATH
    )
    assert report["fixture_input_sha256"] == case.adapter._fixture_input_digest(
        case.distributions
    )
    assert report["fixture_access_before"] == report["fixture_access_after"]
    assert report["fixture_access_before"]["digest"] == case.digest


def _assert_report_subprocess_policy(
    case: _FixtureCase,
    report: dict[str, Any],
) -> None:
    """Check the recorded subprocess policy and its digest."""
    assert report["subprocess_policy_sha256"] == case.adapter._canonical_sha256(
        report["subprocess_policy"]
    )
    assert report["subprocess_policy"]["timeout_seconds"] == 120


def _assert_report_excludes_timing(report: dict[str, Any]) -> None:
    """Check that semantic reports contain no timing sample."""
    assert "elapsed" not in json.dumps(report)


def test_report_binds_declared_inputs_and_execution_policy(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")

    monkeypatch.setattr(
        case.adapter,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "uv 1.2.3\n", ""
        ),
    )
    monkeypatch.setattr(
        case.adapter,
        "_verify_scenario",
        lambda *_args: {
            "id": scenario.id,
            "return_status": {"code": 0, "classification": "success"},
        },
    )

    report = case.adapter.verify_scenarios(
        [scenario],
        case.root,
        case.digest,
        binary,
        case.distributions,
    )

    _assert_report_inputs(case, report, scenario)
    _assert_report_uv(case, report, binary)
    _assert_report_fixture(case, report)
    _assert_report_subprocess_policy(case, report)
    _assert_report_excludes_timing(report)


def test_verification_freezes_caller_scenario_and_fixture_sequences(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    original = case.scenario("basic-highest")
    replacement = case.scenario("constraint-ceiling")
    selected = [original]
    distributions = list(case.distributions)
    expected_distribution_count = len(distributions)
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    identity = case.adapter._UvIdentity(
        binary.resolve(), case.adapter.file_sha256(binary), "uv test"
    )
    observed: list[str] = []

    def identify(_uv: object, _policy: object) -> object:
        selected[0] = replacement
        distributions.clear()
        return identity

    def verify(
        scenario: Any,
        _fixture_root: object,
        _uv: object,
        frozen_distributions: tuple[Any, ...],
        _inventory: object,
        _policy: object,
    ) -> dict[str, object]:
        scenario_id = scenario.id
        assert isinstance(scenario_id, str)
        observed.append(scenario_id)
        assert len(frozen_distributions) == expected_distribution_count
        return {"id": scenario_id}

    monkeypatch.setattr(case.adapter, "_uv_binary", identify)
    monkeypatch.setattr(case.adapter, "_verify_scenario", verify)
    monkeypatch.setattr(
        case.adapter,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "uv test\n", ""
        ),
    )

    report = case.adapter.verify_scenarios(
        selected,
        case.root,
        case.digest,
        binary,
        distributions,
    )

    assert observed == [original.id]
    assert report["scenario_sha256"] == {
        original.id: case.adapter._canonical_sha256(
            case.adapter._scenario_record(original)
        )
    }


_MalformedBoundaryCase = Callable[
    [_FixtureCase, _SmokeScenario, Mapping[str, str], Path], None
]


def _raise_invalid_name(
    case: _FixtureCase,
    _scenario: _SmokeScenario,
    _environment: Mapping[str, str],
    _malformed: Path,
) -> None:
    """Exercise invalid package-name parsing at the public boundary."""
    case.adapter._validate_artifact(
        {"name": "!!!", "version": "1"},
        inventory=case.inventory,
        document_dir=case.root.resolve(),
        pylock=True,
    )


def _raise_invalid_marker(
    case: _FixtureCase,
    _scenario: _SmokeScenario,
    environment: Mapping[str, str],
    _malformed: Path,
) -> None:
    """Exercise invalid marker parsing at the public boundary."""
    case.adapter._marker_coverage(
        ["not a valid marker"],
        {"target": environment},
        "markers",
    )


def _raise_invalid_specifier(
    case: _FixtureCase,
    scenario: _SmokeScenario,
    _environment: Mapping[str, str],
    _malformed: Path,
) -> None:
    """Exercise invalid Python specifier parsing at the public boundary."""
    case.adapter._validate_uv_lock_header(
        {"version": 1, "requires-python": "not a specifier"},
        scenario,
    )


def _raise_invalid_utf8(
    case: _FixtureCase,
    scenario: _SmokeScenario,
    _environment: Mapping[str, str],
    malformed: Path,
) -> None:
    """Exercise invalid pylock decoding at the public boundary."""
    case.adapter._read_optional_pylock(malformed, scenario.id)


@pytest.mark.parametrize(
    ("malformed_case", "error_name"),
    [
        pytest.param(_raise_invalid_name, "InvalidName", id="name"),
        pytest.param(_raise_invalid_marker, "InvalidMarker", id="marker"),
        pytest.param(_raise_invalid_specifier, "InvalidSpecifier", id="specifier"),
        pytest.param(_raise_invalid_utf8, "UnicodeDecodeError", id="utf8"),
    ],
)
def test_public_boundary_wraps_malformed_external_data(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_case: _MalformedBoundaryCase,
    error_name: str,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    identity = case.adapter._UvIdentity(
        binary.resolve(), case.adapter.file_sha256(binary), "uv test"
    )
    malformed = tmp_path / "malformed.toml"
    malformed.write_bytes(b"\xff")
    environment = case.core.prepare_scenario(scenario, case.root).targets[0].marker_env
    monkeypatch.setattr(case.adapter, "_uv_binary", lambda _uv, _policy: identity)

    def reject(*_args: object) -> dict[str, object]:
        malformed_case(case, scenario, environment, malformed)
        raise AssertionError("malformed input unexpectedly passed")

    monkeypatch.setattr(case.adapter, "_verify_scenario", reject)

    with pytest.raises(
        case.adapter.UvCrossCheckError,
        match=rf"invalid uv comparison data \({error_name}\)",
    ):
        case.adapter.verify_scenarios(
            [scenario],
            case.root,
            case.digest,
            binary,
            case.distributions,
        )


def test_verification_rejects_a_uv_binary_that_changes_mid_run(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    binary = tmp_path / "uv"
    binary.write_bytes(b"before")
    identity = case.adapter._UvIdentity(
        binary.resolve(), case.adapter.file_sha256(binary), "uv test"
    )
    monkeypatch.setattr(case.adapter, "_uv_binary", lambda _uv, _policy: identity)

    def replace_binary(*_args: object) -> dict[str, object]:
        binary.write_bytes(b"after")
        return {"id": scenario.id}

    monkeypatch.setattr(case.adapter, "_verify_scenario", replace_binary)

    with pytest.raises(case.adapter.UvCrossCheckError, match="binary changed"):
        case.adapter.verify_scenarios(
            [scenario],
            case.root,
            case.digest,
            binary,
            case.distributions,
        )


def test_verification_rejects_post_run_fixture_replacement(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = fixture_case
    case = _materialized_case(shared, tmp_path / "fixture")
    scenario = case.scenario("basic-highest")
    prepared = case.core.prepare_scenario(scenario, case.root)
    expected = dict(next(iter(prepared.expected.values())))
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    replacement = tmp_path / "replacement"
    original = tmp_path / "original"
    verify_scenario = case.adapter._verify_scenario

    def run(
        command: Sequence[str], *, cwd: Path, policy: object
    ) -> subprocess.CompletedProcess[str]:
        del cwd, policy
        if list(command[1:]) == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "uv 1.2.3\n", "")
        output = Path(command[command.index("--output-file") + 1])
        _write_pylock(output, case, expected)
        return subprocess.CompletedProcess(command, 0, "", "")

    def replace_after_verification(*args: object) -> dict[str, object]:
        result = verify_scenario(*args)
        case.core.materialize_fixture(
            replacement,
            case.distributions,
            case.digest,
        )
        case.root.rename(original)
        replacement.rename(case.root)
        return result

    monkeypatch.setattr(case.adapter, "_run", run)
    monkeypatch.setattr(case.adapter, "_verify_scenario", replace_after_verification)

    with pytest.raises(
        case.adapter.UvCrossCheckError,
        match="fixture storage changed during uv verification",
    ):
        case.adapter.verify_scenarios(
            [scenario],
            case.root,
            case.digest,
            binary,
            case.distributions,
        )

    assert case.core.fixture_digest(case.root) == case.digest


def test_verification_rejects_selected_scenario_input_changes(
    fixture_case: _FixtureCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fixture_case
    scenario = case.scenario("basic-highest")
    binary = tmp_path / "uv"
    binary.write_bytes(b"fake uv")
    identity = case.adapter._UvIdentity(
        binary.resolve(), case.adapter.file_sha256(binary), "uv test"
    )
    monkeypatch.setattr(case.adapter, "_uv_binary", lambda _uv, _policy: identity)
    monkeypatch.setattr(
        case.adapter,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "uv test\n", ""
        ),
    )

    def mutate(*_args: object) -> dict[str, object]:
        pins = scenario.expected[0].pins
        assert isinstance(pins, dict)
        pins["nab-smoke-basic"] = "9.9.9"
        return {"id": scenario.id}

    monkeypatch.setattr(case.adapter, "_verify_scenario", mutate)

    with pytest.raises(
        case.adapter.UvCrossCheckError,
        match="selected scenario inputs changed",
    ):
        case.adapter.verify_scenarios(
            [scenario],
            case.root,
            case.digest,
            binary,
            case.distributions,
        )
