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

_CANARY = Path(__file__).resolve().parents[1] / "benchmarks" / "canary.py"


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_benchmark_canary", _CANARY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wall_timeout_noops_without_posix_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    monkeypatch.delattr(module.signal, "SIGALRM", raising=False)
    monkeypatch.delattr(module.signal, "alarm", raising=False)

    with module._scenario_wall_timeout():
        pass


def test_wall_timeout_installs_and_restores_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    previous_handler = object()
    handlers: list[tuple[int, object]] = []
    alarms: list[int] = []

    def fake_signal(signum: int, handler: object) -> object:
        handlers.append((signum, handler))
        return previous_handler

    def fake_alarm(seconds: int) -> None:
        alarms.append(seconds)

    monkeypatch.setattr(module.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(module.signal, "signal", fake_signal)
    monkeypatch.setattr(module.signal, "alarm", fake_alarm, raising=False)

    with module._scenario_wall_timeout():
        assert alarms == [module.WALL_TIMEOUT_S]

    assert alarms == [module.WALL_TIMEOUT_S, 0]
    assert handlers == [
        (14, module._alarm_handler),
        (14, previous_handler),
    ]


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
        return {
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
            "packages": 0,
            "wall_time_seconds": 0.0,
        }

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


def test_canary_configures_lowest_direct_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    seen: dict[str, object] = {}

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
        def __init__(self, *_args: object, **_kwargs: object) -> None:
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
    result = module.run_one(
        requirements,
        "3.11",
        None,
        None,
        resolution_strategy=module.ResolutionStrategy.LOWEST_DIRECT,
    )

    assert result["success"] is True
    settings = result["settings"]
    assert settings["resolution"] == "lowest-direct"
    assert settings["dist_policy"] == "wheel-or-sdist"
    assert settings["build_policy"] == "never"
    assert settings["trust_unverified_sdist_deps"] is False
    assert settings["max_iterations"] == module.MAX_ITERATIONS
    assert settings["wall_timeout_seconds"] == module.WALL_TIMEOUT_S
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
            "quick-lowest:requests",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        module.main()
