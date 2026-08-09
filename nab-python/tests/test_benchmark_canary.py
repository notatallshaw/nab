"""Tests for the canary benchmark contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typing_extensions import Self

from nab_python.target import ResolveTarget

pytestmark = pytest.mark.benchmark

_CANARY = Path(__file__).resolve().parents[1] / "benchmarks" / "canary.py"


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_benchmark_canary", _CANARY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    benchmark_dir = str(_CANARY.parent)
    sys.path.insert(0, benchmark_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(benchmark_dir)
    return module


def _run_result(
    *,
    success: bool = True,
    decisions: int = 1,
    distributions_seen: int = 0,
    metadata_fetched: int = 0,
    packages: int = 0,
    conflicts: int = 0,
    backjumps: int = 0,
    wall_time_seconds: float = 0.0,
) -> dict[str, object]:
    return {
        "success": success,
        "error": None if success else "resolution failed",
        "decisions": decisions,
        "conflicts": conflicts,
        "backjumps": backjumps,
        "restarts": 0,
        "incompatibilities_learned": 0,
        "metadata_fetched": metadata_fetched,
        "distributions_seen": distributions_seen,
        "look_ahead_rejections": 0,
        "packages": packages,
        "wall_time_seconds": wall_time_seconds,
    }


def test_wall_timeout_noops_without_posix_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    host_module = sys.modules[module.BenchmarkHost.__module__]
    monkeypatch.delattr(host_module.signal, "SIGALRM", raising=False)
    monkeypatch.delattr(host_module.signal, "alarm", raising=False)
    host = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)

    assert host.wall_timeout_seconds is None
    with host.wall_timeout():
        pass


def test_wall_timeout_installs_and_restores_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    host_module = sys.modules[module.BenchmarkHost.__module__]
    previous_handler = object()
    handlers: list[tuple[int, object]] = []
    alarms: list[int] = []

    def fake_signal(signum: int, handler: object) -> object:
        handlers.append((signum, handler))
        return previous_handler

    def fake_alarm(seconds: int) -> None:
        alarms.append(seconds)

    monkeypatch.setattr(host_module.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(host_module.signal, "signal", fake_signal)
    monkeypatch.setattr(host_module.signal, "alarm", fake_alarm, raising=False)
    host = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)

    def trigger_timeout() -> None:
        with host.wall_timeout():
            assert alarms == [module.WALL_TIMEOUT_S]
            timeout_handler = handlers[0][1]
            assert callable(timeout_handler)
            timeout_handler(14, None)

    assert issubclass(module.BenchmarkTimeout, BaseException)
    assert not issubclass(module.BenchmarkTimeout, Exception)
    with pytest.raises(module.BenchmarkTimeout, match="wall-clock budget"):
        trigger_timeout()

    assert alarms == [module.WALL_TIMEOUT_S, 0]
    assert len(handlers) == 2
    assert handlers[0][0] == 14
    assert handlers[1] == (14, previous_handler)


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        (None, "highest"),
        ("highest", "highest"),
        ("lowest", "lowest"),
        ("lowest-direct", "lowest-direct"),
    ],
)
def test_canary_uses_scenario_resolution(
    monkeypatch: pytest.MonkeyPatch,
    resolution: str | None,
    expected: str,
) -> None:
    module = _harness()
    seen: list[str] = []

    def fake_run_one(*_args: object, **kwargs: object) -> dict:
        seen.append(str(kwargs["resolution_strategy"].value))
        return _run_result()

    monkeypatch.setattr(module, "run_one", fake_run_one)
    scenario = {
        "python_version": "3.11",
        "requirements": [],
    }
    if resolution is not None:
        scenario["resolution"] = resolution
    module.median_run(
        scenario,
        1,
        scenario_name="example",
    )

    assert seen == [expected]


def test_canary_rejects_unknown_resolution() -> None:
    module = _harness()
    with pytest.raises(
        ValueError,
        match="example: resolution must be one of.*got 'middle'",
    ):
        module.median_run(
            {
                "python_version": "3.11",
                "requirements": [],
                "resolution": "middle",
            },
            1,
            scenario_name="example",
        )


def test_canary_rejects_supported_marker_build_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    host = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)
    current_system = host.target.marker_env["platform_system"]
    required_system = "Windows" if current_system != "Windows" else "Linux"

    def unexpected_run(*_args: object, **_kwargs: object) -> dict:
        pytest.fail("scenario reached the resolver")

    monkeypatch.setattr(module, "run_one", unexpected_run)
    scenario = {
        "python_version": "3.11",
        "requirements": ["demo"],
        "platform_system": required_system,
        "build_packages": ["demo"],
    }

    with pytest.raises(
        ValueError,
        match=(
            "example: build_packages cannot be combined "
            "with a marker environment overlay"
        ),
    ):
        module.median_run(scenario, 1, scenario_name="example", host=host)


@pytest.mark.parametrize("nested", [False, True])
def test_canary_runs_a_platform_overlay_by_default(
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    module = _harness()
    host = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)
    current_system = host.target.marker_env["platform_system"]
    required_system = "Windows" if current_system != "Windows" else "Linux"

    targets: list[ResolveTarget] = []

    def fake_run_one(*_args: object, **kwargs: object) -> dict[str, object]:
        target = kwargs["target"]
        assert isinstance(target, ResolveTarget)
        targets.append(target)
        return _run_result()

    monkeypatch.setattr(module, "run_one", fake_run_one)
    scenario: dict[str, object] = {"python_version": "3.11", "requirements": []}
    if nested:
        scenario["marker_environment"] = {"platform_system": required_system}
    else:
        scenario["platform_system"] = required_system
    runs, _summary = module.median_run(scenario, 1, host=host)

    assert runs == [_run_result()]
    assert len(targets) == 1
    target = targets[0]
    assert target.marker_env["platform_system"] == required_system
    assert target.tags_faithful is False


def test_canary_skips_an_explicit_host_requirement_with_unfaithful_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    host = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)
    current_system = host.target.marker_env["platform_system"]
    required_system = "Windows" if current_system != "Windows" else "Linux"

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("host admission must happen before resolution")

    monkeypatch.setattr(module, "run_one", unexpected_run)
    runs, summary = module.median_run(
        {
            "python_version": "3.11",
            "requirements": [],
            "marker_environment": {"platform_system": required_system},
            "requires_matching_host": True,
        },
        1,
        host=host,
    )

    assert runs == []
    assert summary == {
        "skipped": "marker environment requires wheel tags from a different host"
    }


def test_canary_rejects_an_invalid_target_marker_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()

    def unexpected_run(*_args: object, **_kwargs: object) -> dict:
        pytest.fail("invalid scenario reached the resolver")

    monkeypatch.setattr(module, "run_one", unexpected_run)

    with pytest.raises(ValueError, match="no supported platform"):
        module.median_run(
            {
                "python_version": "3.11",
                "requirements": [],
                "platform_system": "Linuz",
            },
            1,
            scenario_name="example",
        )


def test_canary_filters_root_markers_with_the_admitted_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    host = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)
    host_python = host.target.marker_env["python_version"]
    scenario_python = "3.10" if host_python != "3.10" else "3.11"
    seen: dict[str, object] = {}

    def fake_run_one(requirements: dict, *_args: object, **kwargs: object) -> dict:
        seen["requirements"] = set(requirements)
        seen["target_python"] = kwargs["target"].marker_env["python_version"]
        return _run_result(packages=1)

    monkeypatch.setattr(module, "run_one", fake_run_one)
    module.median_run(
        {
            "python_version": scenario_python,
            "requirements": [
                f"selected; python_version == '{scenario_python}'",
                f"excluded; python_version != '{scenario_python}'",
            ],
        },
        1,
        scenario_name="example",
        host=host,
    )

    assert seen["target_python"] == scenario_python
    assert seen["requirements"] == {"selected"}


def test_canary_explicit_resolution_overrides_declared_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    seen: list[object] = []

    def fake_run_one(*_args: object, **kwargs: object) -> dict:
        seen.append(kwargs["resolution_strategy"])
        return _run_result()

    monkeypatch.setattr(module, "run_one", fake_run_one)
    module.median_run(
        {
            "python_version": "3.11",
            "requirements": [],
            "resolution": "highest",
        },
        1,
        scenario_name="example",
        resolution_override=module.ResolutionStrategy.LOWEST,
    )

    assert seen == [module.ResolutionStrategy.LOWEST]


def test_canary_prepares_inputs_and_summarizes_repeated_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    host = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)
    returned_runs = [
        _run_result(
            decisions=1,
            distributions_seen=9,
            metadata_fetched=3,
            packages=1,
            conflicts=7,
            backjumps=4,
            wall_time_seconds=0.1,
        ),
        _run_result(
            success=False,
            decisions=9,
            distributions_seen=3,
            metadata_fetched=9,
            packages=0,
            conflicts=1,
            backjumps=8,
            wall_time_seconds=0.3,
        ),
        _run_result(
            decisions=5,
            distributions_seen=6,
            metadata_fetched=6,
            packages=3,
            conflicts=4,
            backjumps=2,
            wall_time_seconds=0.2,
        ),
    ]
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run_one(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append((args, kwargs))
        return returned_runs[len(calls) - 1]

    monkeypatch.setattr(module, "run_one", fake_run_one)
    runs, summary = module.median_run(
        {
            "python_version": "3.11",
            "requirements": ["demo>=1"],
            "constraints": ["support<3"],
            "datetime": "2025-01-02 03:04:05",
            "indexes": [{"name": "private", "url": "https://example.test/simple"}],
            "index_routes": [{"name": "demo", "index": "private"}],
            "build_packages": ["demo"],
            "resolution": "lowest-direct",
            "trust_unverified_sdist_deps": True,
        },
        3,
        scenario_name="example",
        host=host,
    )

    assert runs == returned_runs
    assert summary == {
        "success_runs": "2/3",
        "median_decisions": 5,
        "median_distributions_seen": 6,
        "median_metadata_fetched": 6,
        "median_packages": 1,
        "median_conflicts": 4,
        "median_backjumps": 4,
        "median_wall": 0.2,
        "min_decisions": 1,
        "max_decisions": 9,
        "min_wall": 0.1,
        "max_wall": 0.3,
    }

    assert len(calls) == 3
    assert calls[1:] == [calls[0], calls[0]]
    args, kwargs = calls[0]
    requirements, uploaded_prior_to, constraints = args
    assert set(requirements) == {"demo"}
    assert uploaded_prior_to == module.parse_datetime("2025-01-02 03:04:05")
    assert set(constraints) == {"support"}
    assert kwargs["indexes"] == [
        module.IndexConfig("private", "https://example.test/simple")
    ]
    assert kwargs["index_routes"] == [module.IndexRoute("demo", "private")]
    assert kwargs["build_policy_overrides"] == {"demo": module.BuildPolicy.BUILD_REMOTE}
    assert kwargs["resolution_strategy"] is module.ResolutionStrategy.LOWEST_DIRECT
    assert kwargs["trust_unverified_sdist_deps"] is True
    assert kwargs["target"].marker_env["python_version"] == "3.11"
    assert kwargs["host"] is host


def test_canary_configures_lowest_direct_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    seen: dict[str, object] = {}
    resolver_kwargs: dict[str, object] = {}

    class FakeCoordinator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    class FakeProvider:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            seen.update(kwargs)
            self.stats = SimpleNamespace(
                metadata_fetched=0,
                distributions_seen=0,
                look_ahead_rejections=0,
            )

    class FakeResolver:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            resolver_kwargs.update(kwargs)
            self.stats = SimpleNamespace(
                decisions=0,
                conflicts=0,
                backjumps=0,
                restarts=0,
                incompatibilities_learned=0,
            )

        def resolve(self, *_args: object, **_kwargs: object) -> dict:
            return {}

    monkeypatch.setattr(module, "FetchCoordinator", FakeCoordinator)
    monkeypatch.setattr(module, "HttpxAsyncTransport", object)
    monkeypatch.setattr(module, "Provider", FakeProvider)
    monkeypatch.setattr(module, "Resolver", FakeResolver)

    requirements = module.parse_requirements(["Root[feature]", "Other==1"])
    captured = module.BenchmarkHost.current(module.WALL_TIMEOUT_S)
    host = module.BenchmarkHost(captured.target, captured.python_runtime, None)
    admission = host.target_for("3.11", {}, requires_matching_host=False)
    assert admission.target is not None
    result = module.run_one(
        requirements,
        None,
        None,
        resolution_strategy=module.ResolutionStrategy.LOWEST_DIRECT,
        target=admission.target,
        host=host,
    )

    assert result["success"] is True
    settings = result["settings"]
    assert settings["resolution"] == "lowest-direct"
    assert settings["dist_policy"] == "wheel-or-sdist"
    assert settings["build_policy"] == "never"
    assert settings["trust_unverified_sdist_deps"] is False
    assert settings["max_iterations"] == module.DEFAULT_MAX_ITERATIONS
    assert settings["wall_timeout_seconds"] is None
    assert settings["runtime"]["python"] == sys.version
    assert settings["runtime"]["implementation"] == sys.implementation.name
    assert settings["direct_packages"] == ["other", "root"]
    assert settings["indexes"] == [
        {"name": module.DEFAULT_INDEX_NAME, "url": module.DEFAULT_INDEX_URL}
    ]
    assert settings["target"]["marker_environment"]["python_version"] == "3.11"
    assert settings["target"]["wheel_tags_count"] > 0
    assert seen["resolution_strategy"] is module.ResolutionStrategy.LOWEST_DIRECT
    assert seen["direct_packages"] == frozenset({"root", "other"})
    assert seen["target"] is admission.target
    assert resolver_kwargs == {
        "range_type": module.VersionRange,
        "root_version": "0",
    }


def test_canary_main_records_v2_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    source = {"commit": "source-sha", "dirty": False, "diff_hash": None}
    monkeypatch.setattr(module, "get_git_source_state", lambda: source)
    scenario = {"unsupported_reason": "test fixture"}
    input_hash = module.scenario_input_hash("quick:requests", scenario)
    monkeypatch.setattr(module, "find_scenario", lambda _name: scenario)
    monkeypatch.setattr(
        sys,
        "argv",
        ["canary.py", "--commit", "test", "--scenario", "quick:requests"],
    )

    module.main()

    result_path = next((tmp_path / "test").glob("canary_*.json"))
    data = json.loads(result_path.read_text())
    assert data == {
        "requests": {
            "contract_version": module.CANARY_CONTRACT_VERSION,
            "scenario": "quick:requests",
            "source": source,
            "input_hash": input_hash,
            "execution_hash": module.scenario_execution_hash(input_hash, None),
            "input": scenario,
            "effective_settings": None,
            "runs": [],
            "summary": {"skipped": "test fixture"},
        }
    }


def test_canary_main_preserves_v2_lowest_identity_and_effective_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    source = {"commit": "source-sha", "dirty": False, "diff_hash": None}
    monkeypatch.setattr(module, "get_git_source_state", lambda: source)
    scenario = {"python_version": "3.11", "requirements": ["demo"]}
    settings = {"resolution": "lowest", "target": "test-host"}
    run = {
        "success": True,
        "error": None,
        "decisions": 1,
        "conflicts": 0,
        "backjumps": 0,
        "restarts": 0,
        "incompatibilities_learned": 0,
        "metadata_fetched": 0,
        "distributions_seen": 0,
        "look_ahead_rejections": 0,
        "packages": 1,
        "wall_time_seconds": 0.01,
        "settings": settings,
    }
    summary = {
        "success_runs": "1/1",
        "median_decisions": 1,
        "median_distributions_seen": 0,
        "median_metadata_fetched": 0,
        "median_packages": 1,
        "median_conflicts": 0,
        "median_backjumps": 0,
        "median_wall": 0.01,
        "min_decisions": 1,
        "max_decisions": 1,
        "min_wall": 0.01,
        "max_wall": 0.01,
    }
    monkeypatch.setattr(module, "find_scenario", lambda _name: scenario)

    def median_run(
        _scenario: dict,
        _runs: int,
        *,
        scenario_name: str,
        resolution_override: object,
        host: object,
    ) -> tuple[list[dict], dict]:
        del host
        assert scenario_name == "example"
        assert resolution_override is module.ResolutionStrategy.LOWEST
        return [run], summary

    monkeypatch.setattr(module, "median_run", median_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "canary.py",
            "--commit",
            "test",
            "--runs",
            "1",
            "--scenario",
            "quick:example@lowest",
        ],
    )

    module.main()

    record = json.loads(next((tmp_path / "test").glob("canary_*.json")).read_text())[
        "example"
    ]
    expected_input = {**scenario, "resolution": "lowest"}
    expected_input_hash = module.scenario_input_hash(
        "quick-lowest:example", expected_input
    )
    assert record["contract_version"] == 2
    assert record["scenario"] == "quick-lowest:example"
    assert record["input"] == expected_input
    assert record["input_hash"] == expected_input_hash
    assert record["effective_settings"] == settings
    assert record["execution_hash"] == module.scenario_execution_hash(
        expected_input_hash, settings
    )


def test_canary_input_hash_captures_executable_definition() -> None:
    module = _harness()
    scenario = {
        "requirements": ["a", "b"],
        "marker_environment": {"sys_platform": "linux", "os_name": "posix"},
    }
    reordered_mapping = {
        "marker_environment": {"os_name": "posix", "sys_platform": "linux"},
        "requirements": ["a", "b"],
    }
    digest = module.scenario_input_hash("quick:example", scenario)

    assert digest == module.scenario_input_hash("quick:example", reordered_mapping)
    assert digest != module.scenario_input_hash("other:example", scenario)
    assert digest != module.scenario_input_hash(
        "quick:example",
        {**scenario, "requirements": ["b", "a"]},
    )
    assert digest != module.scenario_input_hash(
        "quick:example",
        {**scenario, "requirements": ["a", "c"]},
    )
    assert module.scenario_execution_hash(digest, {"target": "linux"}) != (
        module.scenario_execution_hash(digest, {"target": "windows"})
    )


@pytest.mark.parametrize(
    ("resolution", "expected_scenario", "expected_definition"),
    [
        ("highest", "quick:example", {"requirements": ["demo"]}),
        (
            "lowest",
            "quick-lowest:example",
            {"requirements": ["demo"], "resolution": "lowest"},
        ),
        (
            "lowest-direct",
            "quick-lowest-direct:example",
            {"requirements": ["demo"], "resolution": "lowest-direct"},
        ),
    ],
)
def test_canary_v2_identity_reconstructs_historical_clone_input(
    resolution: str,
    expected_scenario: str,
    expected_definition: dict[str, object],
) -> None:
    module = _harness()
    strategy = module.ResolutionStrategy(resolution)

    identity = module.canary_v2_identity(
        module.CanaryCase("quick:example", strategy),
        {"requirements": ["demo"]},
        strategy,
    )

    assert identity.scenario == expected_scenario
    assert identity.definition == expected_definition
    if strategy is not module.ResolutionStrategy.HIGHEST:
        assert next(iter(identity.definition)) == "resolution"


@pytest.mark.parametrize(
    ("status", "diff", "dirty"),
    [
        (b"", b"", False),
        (b" M canary.py\0", b"diff --git a/canary.py b/canary.py\n", True),
    ],
)
def test_canary_source_state_marks_dirty_trees(
    monkeypatch: pytest.MonkeyPatch,
    status: bytes,
    diff: bytes,
    dirty: bool,
) -> None:
    module = _harness()
    monkeypatch.setattr(module, "get_git_commit", lambda: "source-sha")

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(stdout="/repo")
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(stdout=status)
        if command[:2] == ["git", "diff"]:
            return SimpleNamespace(stdout=diff)
        if command[:2] == ["git", "ls-files"]:
            return SimpleNamespace(stdout=b"")
        raise AssertionError(command)

    monkeypatch.setattr(
        module.subprocess,
        "run",
        run,
    )

    state = module.get_git_source_state()

    assert state["commit"] == "source-sha"
    assert state["dirty"] is dirty
    expected_hash = (
        hashlib.sha256(status + b"\0" + diff + b"\0").hexdigest() if dirty else None
    )
    assert state["diff_hash"] == expected_hash


def test_canary_source_state_hashes_sibling_untracked_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = shutil.which("git")
    assert git is not None

    def run_git(*args: str) -> None:
        subprocess.run([git, *args], cwd=tmp_path, check=True)  # noqa: S603

    run_git("init", "-q")
    benchmarks_dir = tmp_path / "nab-python" / "benchmarks"
    benchmarks_dir.mkdir(parents=True)
    (benchmarks_dir / "tracked").write_text("tracked\n")
    run_git("add", "nab-python/benchmarks/tracked")
    run_git(
        "-c",
        "user.name=Canary Test",
        "-c",
        "user.email=canary@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    untracked = tmp_path / "nab-python" / "src" / "source.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("first\n")
    module = _harness()
    monkeypatch.setattr(module, "BENCHMARKS_DIR", benchmarks_dir)

    first = module.get_git_source_state()
    untracked.write_text("second\n")
    second = module.get_git_source_state()

    assert first["commit"] == second["commit"]
    assert first["dirty"] is second["dirty"] is True
    assert first["diff_hash"] != second["diff_hash"]


def test_canary_source_state_marks_a_source_export_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()

    def fail_commit() -> str:
        raise module.subprocess.CalledProcessError(128, ["git", "rev-parse"])

    monkeypatch.setattr(module, "get_git_commit", fail_commit)

    assert module.get_git_source_state() == {
        "commit": None,
        "dirty": True,
        "diff_hash": None,
    }


def test_canary_source_state_preserves_commit_when_hashing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    commit = "a" * 40
    monkeypatch.setattr(module, "get_git_commit", lambda: commit)

    def fail_hashing(*_args: object, **_kwargs: object) -> object:
        raise module.subprocess.CalledProcessError(128, ["git", "status"])

    monkeypatch.setattr(module.subprocess, "run", fail_hashing)

    assert module.get_git_source_state() == {
        "commit": commit,
        "dirty": True,
        "diff_hash": None,
    }


def test_canary_main_rejects_duplicate_output_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "canary.py",
            "--scenario",
            "quick:requests",
            "--scenario",
            "quick:requests",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        module.main()
