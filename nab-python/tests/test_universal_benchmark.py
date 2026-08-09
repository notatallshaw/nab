"""Regression tests for universal benchmark result contracts."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from typing_extensions import Self

from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import IndexPin, TargetLock, WheelArtifact
from nab_python.resolve import ResolveResult, TargetResult
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget

pytestmark = pytest.mark.benchmark

_BENCHMARK = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "universal_scenarios.py"
)
if str(_BENCHMARK.parent) not in sys.path:
    sys.path.insert(0, str(_BENCHMARK.parent))
_CLEAN_SOURCE = {"commit": "a" * 40, "dirty": False, "diff_hash": None}
_WINDOWS_DEVICE_NAMES = (
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
)
_WINDOWS_DEVICE_NAMES_WITH_EXTENSIONS = (
    "AuX.txt",
    "cOn.lock",
    "NuL.json",
    "pRn.data",
    "cOm1.json",
    "COM9.txt",
    "lPt1.lock",
    "LPT9.data",
)
_PORTABLE_WINDOWS_DEVICE_NEAR_MISSES = (
    "AUX1",
    "CONSOLE",
    "NULL",
    "PRINTER",
    "COM0",
    "COM10",
    "LPT0",
    "LPT10",
    "x.AUX",
    "prefix.COM1",
    "_AUX",
    "COM1-file",
)


class _FakeCoordinator:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


def _universal_scenario(*platforms: str) -> dict[str, object]:
    return {
        "python": ">=3.11,<3.12",
        "platforms": list(platforms),
        "requirements": ["foo"],
    }


def _successful_result() -> tuple[ResolveTarget, ResolveResult]:
    target = ResolveTarget.for_declared(
        python_version="3.11",
        spec=PlatformSpec("linux_x86_64"),
    )
    target_result = TargetResult(
        target=target,
        success=True,
        error=None,
        decisions=2,
        rounds=3,
        conflicts=0,
        backjumps=0,
        metadata_fetched=1,
        distributions_seen=4,
        wall_time=0.1,
        pins={"Foo": Version("1.0")},
    )
    result = ResolveResult(
        targets=(target,),
        target_results=[target_result],
    )
    return target, result


def _result_data(result: dict, *, schema: int = 3) -> dict:
    complete_result = {
        "success": False,
        "resolution_success": None,
        "expected_failure": False,
        "skip_on_fail": False,
        "lock_consistent": None,
        "lock_inconsistencies": [],
        "timed_out": False,
        "error": None,
        **result,
    }
    if complete_result["lock_consistent"] is False:
        complete_result["lock_inconsistencies"] = ["projection mismatch"]
    resolution_success = complete_result["resolution_success"]
    if resolution_success is True:
        per_tuple = [
            {
                "label": "cp311-linux_x86_64",
                "python_version": "3.11",
                "platform_id": "linux_x86_64",
                "success": True,
                "error": None,
                "decisions": 2,
                "rounds": 3,
                "conflicts": 0,
                "backjumps": 0,
                "metadata_fetched": 1,
                "distributions_seen": 4,
                "wall_time_seconds": 0.1,
                "package_count": 1,
                "pins": {"foo": "1.0"},
            }
        ]
        merged_pins = {"foo": [{"version": "1.0", "target": "cp311-linux_x86_64"}]}
    elif resolution_success is False:
        per_tuple = [
            {
                "label": "cp311-linux_x86_64",
                "python_version": "3.11",
                "platform_id": "linux_x86_64",
                "success": False,
                "error": "resolution failed",
                "decisions": 0,
                "rounds": 0,
                "conflicts": 0,
                "backjumps": 0,
                "metadata_fetched": 0,
                "distributions_seen": 0,
                "wall_time_seconds": 0.0,
                "package_count": 0,
                "pins": {},
            }
        ]
        merged_pins = {}
    else:
        per_tuple = []
        merged_pins = {}
    tuples_ok = sum(item["success"] is True for item in per_tuple)
    tuples_fail = len(per_tuple) - tuples_ok
    return {
        "input": {
            "benchmark_schema": schema,
            "scenario": "example",
            "commit": "run",
            "source": _CLEAN_SOURCE,
            "python": ">=3.11,<3.12",
            "platforms": ["linux_x86_64"],
            "python_order": "asc",
            "requirements": ["foo"],
            "align_across_tuples": True,
            "resolution_strategy": "highest",
            "dist_policy": "wheel-or-sdist",
            "build_policy": "never",
            "trust_unverified_sdist_deps": False,
            "skip_on_fail": complete_result["skip_on_fail"],
            "reason": "",
        },
        "reason": "",
        "result": complete_result,
        "merged_pins": merged_pins,
        "stats": {
            "wall_time_seconds": 0.0,
            "tuples_total": 1,
            "tuples_recorded": len(per_tuple),
            "tuples_ok": tuples_ok,
            "tuples_fail": tuples_fail,
            "merged_packages": len(merged_pins),
            "diverging_packages": 0,
            "decisions_total": sum(item["decisions"] for item in per_tuple),
            "rounds_total": sum(item["rounds"] for item in per_tuple),
            "conflicts_total": 0,
            "backjumps_total": 0,
            "metadata_fetched_total": sum(
                item["metadata_fetched"] for item in per_tuple
            ),
            "distributions_seen_total": sum(
                item["distributions_seen"] for item in per_tuple
            ),
        },
        "per_tuple": per_tuple,
    }


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_universal_benchmark", _BENCHMARK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _summary_harness() -> ModuleType:
    path = _BENCHMARK.with_name("universal_summary.py")
    spec = importlib.util.spec_from_file_location("_universal_summary", path)
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
        assert alarms == [module.SCENARIO_WALL_TIMEOUT_SECONDS]

    assert alarms == [module.SCENARIO_WALL_TIMEOUT_SECONDS, 0]
    assert handlers == [
        (14, module._alarm_handler),
        (14, previous_handler),
    ]


@pytest.mark.parametrize(
    ("data", "accepted"),
    [
        (
            _result_data(
                {
                    "success": True,
                    "resolution_success": True,
                    "lock_consistent": True,
                    "timed_out": False,
                }
            ),
            True,
        ),
        (
            _result_data(
                {
                    "success": True,
                    "resolution_success": True,
                    "lock_consistent": False,
                    "timed_out": False,
                }
            ),
            False,
        ),
        (
            _result_data(
                {
                    "success": False,
                    "resolution_success": False,
                    "expected_failure": True,
                    "skip_on_fail": True,
                    "timed_out": False,
                }
            ),
            True,
        ),
        (
            _result_data(
                {
                    "success": False,
                    "resolution_success": False,
                    "expected_failure": True,
                    "skip_on_fail": True,
                    "timed_out": True,
                }
            ),
            False,
        ),
        (
            _result_data(
                {
                    "success": False,
                    "resolution_success": False,
                    "expected_failure": True,
                    "timed_out": False,
                }
            ),
            False,
        ),
        (_result_data({"success": True}, schema=1), False),
        ({}, False),
    ],
)
def test_result_is_accepted(data: dict, accepted: bool) -> None:
    assert _harness().result_is_accepted(data) is accepted
    assert _summary_harness().result_is_accepted(data) is accepted


def test_result_contract_rejects_contradictory_aggregates() -> None:
    module = _harness()
    result = {
        "success": True,
        "resolution_success": True,
        "lock_consistent": True,
    }

    wrong_total = _result_data(result)
    wrong_total["stats"]["decisions_total"] += 1
    assert module.result_is_well_formed(wrong_total) is False

    wrong_pin = _result_data(result)
    wrong_pin["merged_pins"]["foo"][0]["version"] = "2.0"
    assert module.result_is_well_formed(wrong_pin) is False

    vacuous = _result_data(result)
    vacuous["per_tuple"] = []
    vacuous["merged_pins"] = {}
    for field in (
        "backjumps_total",
        "conflicts_total",
        "decisions_total",
        "distributions_seen_total",
        "diverging_packages",
        "merged_packages",
        "metadata_fetched_total",
        "rounds_total",
        "tuples_fail",
        "tuples_ok",
        "tuples_recorded",
    ):
        vacuous["stats"][field] = 0
    assert module.result_is_well_formed(vacuous) is False


def test_result_contract_requires_the_complete_input_shape() -> None:
    module = _harness()
    result = _result_data(
        {
            "success": True,
            "resolution_success": True,
            "lock_consistent": True,
        }
    )

    for field in tuple(result["input"]):
        missing = json.loads(json.dumps(result))
        missing["input"].pop(field)
        assert module.result_is_well_formed(missing) is False, field

    unexpected = json.loads(json.dumps(result))
    unexpected["input"]["unused"] = True
    assert module.result_is_well_formed(unexpected) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", {"commit": "a" * 40, "dirty": False}),
        ("python_order", "newest-first"),
        ("requirements", []),
        ("platforms", ["linux_x86_64", "linux_x86_64"]),
        ("align_across_tuples", "true"),
        ("resolution_strategy", "newest"),
        ("resolution_strategy", []),
        ("dist_policy", "anything"),
        ("build_policy", "always"),
        ("trust_unverified_sdist_deps", "false"),
        ("skip_on_fail", "false"),
    ],
)
def test_result_contract_validates_input_fields(field: str, value: object) -> None:
    module = _harness()
    data = _result_data(
        {
            "success": True,
            "resolution_success": True,
            "lock_consistent": True,
        }
    )
    data["input"][field] = value

    assert module.result_is_well_formed(data) is False


def test_result_contract_validates_optional_input_fields() -> None:
    module = _harness()
    data = _result_data(
        {
            "success": True,
            "resolution_success": True,
            "lock_consistent": True,
        }
    )
    data["input"]["constraints"] = ["foo<2"]
    for datetime_value in (
        "2025-06-01",
        "2025-06-01 00:00:00",
        "2025-06-01T00:00:00.123456789Z",
        "2025-06-01T00:00:00.1+05:30",
        "2025-06-01T00:00:00.1-05:30",
        "2025-06-01T00:00:00.1+0000",
        "2025-06-01T00:00:00.1+00",
        "2025-06-01T23:59:59.9+23:59",
    ):
        data["input"]["datetime"] = datetime_value
        assert module.result_is_well_formed(data) is True

    for field, value in (
        ("constraints", []),
        ("datetime", "not-a-date"),
        ("datetime", "2025-W22-7T00:00:00Z"),
        ("datetime", "20250601T000000Z"),
        ("datetime", "2025-06-01T00:00:00,1Z"),
        ("datetime", "2025-06-01T24:00:00Z"),
        ("datetime", "2025-06-01T23:60:00Z"),
        ("datetime", "2025-06-01T23:59:60Z"),
        ("datetime", "2025-06-01T23:59:59+24"),
        ("datetime", "2025-06-01T23:59:59+12:60"),
    ):
        invalid = json.loads(json.dumps(data))
        invalid["input"][field] = value
        assert module.result_is_well_formed(invalid) is False


def test_result_contract_allows_multiple_slices_per_declared_cell() -> None:
    module = _harness()
    data = _result_data(
        {
            "success": True,
            "resolution_success": True,
            "lock_consistent": True,
        }
    )
    second = json.loads(json.dumps(data["per_tuple"][0]))
    second["label"] = "cp311-linux_x86_64-slice-2"
    second["pins"]["foo"] = "2.0"
    data["per_tuple"].append(second)
    data["merged_pins"]["foo"].append({"version": "2.0", "target": second["label"]})
    data["stats"].update(
        {
            "tuples_recorded": 2,
            "tuples_ok": 2,
            "decisions_total": 4,
            "rounds_total": 6,
            "metadata_fetched_total": 2,
            "distributions_seen_total": 8,
            "diverging_packages": 1,
        }
    )

    assert module.result_is_well_formed(data) is True
    assert module.result_is_accepted(data) is True


def test_git_source_state_hashes_untracked_contents(
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
        "user.name=Benchmark Test",
        "-c",
        "user.email=benchmark@example.invalid",
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


def test_lock_consistency_requires_a_valid_emitted_pylock() -> None:
    module = _harness()
    target = ResolveTarget.for_declared(
        python_version="3.11",
        spec=PlatformSpec("linux_x86_64"),
    )
    wheel = WheelArtifact(
        filename="foo-2.0-py3-none-any.whl",
        url="https://example.test/foo-2.0-py3-none-any.whl",
        hashes=(("sha256", "a" * 64),),
    )
    pin = IndexPin(
        name="foo",
        version="1.0",
        index="https://pypi.org/simple",
        wheels=(wheel,),
    )
    target_result = TargetResult(
        target=target,
        success=True,
        pins={"foo": Version("1.0")},
        lock=TargetLock(target=target, pins={"foo": pin}),
    )
    result = ResolveResult(
        targets=(target,),
        target_results=[target_result],
    )

    consistent, problems = module.check_lock_consistency(result)

    assert consistent is False
    assert len(problems) == 1
    assert "foo-2.0" in problems[0]
    assert "package version '1.0'" in problems[0]


def test_lock_inconsistency_fails_and_pins_are_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _harness()
    target, resolve_result = _successful_result()

    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(module, "Urllib3AsyncTransport", object)
    monkeypatch.setattr(module, "FetchCoordinator", _FakeCoordinator)
    monkeypatch.setattr(
        module,
        "resolve_with_coordinator",
        lambda *_args, **_kwargs: resolve_result,
    )
    monkeypatch.setattr(
        module,
        "check_lock_consistency",
        lambda _result: (False, ["projection mismatch"]),
    )

    accepted = module.process_scenario(
        "example",
        {
            "python": ">=3.11,<3.12",
            "platforms": ["linux_x86_64"],
            "requirements": ["foo"],
        },
        "commit",
        force=True,
        source=_CLEAN_SOURCE,
    )

    data = json.loads((tmp_path / "commit" / "universal" / "example.json").read_text())
    assert accepted is False
    assert data["input"]["benchmark_schema"] == module.RESULT_SCHEMA_VERSION
    assert data["input"]["scenario"] == "example"
    assert data["input"]["source"] == _CLEAN_SOURCE
    assert data["input"]["build_policy"] == "never"
    assert data["input"]["dist_policy"] == "wheel-or-sdist"
    assert data["input"]["trust_unverified_sdist_deps"] is False
    assert data["input"]["skip_on_fail"] is False
    assert data["input"]["reason"] == ""
    assert data["result"]["resolution_success"] is True
    assert data["result"]["success"] is False
    assert data["per_tuple"][0]["pins"] == {"foo": "1.0"}
    assert data["merged_pins"] == {"foo": [{"version": "1.0", "target": target.label}]}

    capsys.readouterr()
    assert (
        module.process_scenario(
            "example",
            {
                "python": ">=3.11,<3.12",
                "platforms": ["linux_x86_64"],
                "requirements": ["foo"],
            },
            "commit",
            force=False,
            source=_CLEAN_SOURCE,
        )
        is False
    )
    assert "example CACHED FAILURE" in capsys.readouterr().out


def test_skip_on_fail_does_not_accept_post_resolution_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    target, resolve_result = _successful_result()
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(module, "Urllib3AsyncTransport", object)
    monkeypatch.setattr(module, "FetchCoordinator", _FakeCoordinator)
    monkeypatch.setattr(
        module,
        "resolve_with_coordinator",
        lambda *_args, **_kwargs: resolve_result,
    )

    def fail_lock_check(_result: object) -> tuple[bool, list[str]]:
        raise RuntimeError("projection crashed")

    monkeypatch.setattr(module, "check_lock_consistency", fail_lock_check)

    accepted = module.process_scenario(
        "example",
        {
            "python": ">=3.11,<3.12",
            "platforms": ["linux_x86_64"],
            "requirements": ["foo"],
            "skip_on_fail": True,
        },
        "commit",
        force=True,
        source=_CLEAN_SOURCE,
    )

    data = json.loads((tmp_path / "commit" / "universal" / "example.json").read_text())
    assert accepted is False
    assert data["result"]["resolution_success"] is True
    assert data["result"]["expected_failure"] is False
    assert data["result"]["timed_out"] is False
    assert data["result"]["error"] == "RuntimeError: projection crashed"
    assert data["stats"]["tuples_total"] == 1
    assert data["stats"]["tuples_recorded"] == 1
    assert data["per_tuple"][0]["pins"] == {"foo": "1.0"}
    assert data["merged_pins"] == {"foo": [{"version": "1.0", "target": target.label}]}


def test_cached_result_must_match_the_shape_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    _target, resolve_result = _successful_result()
    calls = 0

    def resolve(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return resolve_result

    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(module, "Urllib3AsyncTransport", object)
    monkeypatch.setattr(module, "FetchCoordinator", _FakeCoordinator)
    monkeypatch.setattr(module, "resolve_with_coordinator", resolve)
    monkeypatch.setattr(module, "check_lock_consistency", lambda _result: (True, []))
    scenario = {
        "python": ">=3.11,<3.12",
        "platforms": ["linux_x86_64"],
        "requirements": ["foo"],
    }

    assert module.process_scenario(
        "example", scenario, "label", force=True, source=_CLEAN_SOURCE
    )
    result_path = tmp_path / "label" / "universal" / "example.json"
    malformed = json.loads(result_path.read_text())
    malformed.pop("stats")
    result_path.write_text(json.dumps(malformed))
    assert module.process_scenario(
        "example", scenario, "label", force=False, source=_CLEAN_SOURCE
    )
    other_source = {"commit": "b" * 40, "dirty": False, "diff_hash": None}
    assert module.process_scenario(
        "example", scenario, "label", force=False, source=other_source
    )
    unknown_source = {"commit": "c" * 40, "dirty": True, "diff_hash": None}
    assert module.process_scenario(
        "example", scenario, "label", force=False, source=unknown_source
    )
    assert module.process_scenario(
        "example", scenario, "label", force=False, source=unknown_source
    )

    assert calls == 5
    data = json.loads(result_path.read_text())
    assert data["input"]["source"] == unknown_source


def test_unknown_universal_setting_is_rejected() -> None:
    module = _harness()
    with pytest.raises(
        ValueError,
        match=r"example: unknown scenario setting\(s\): parallel",
    ):
        module.validate_scenario("example", {"parallel": True})


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("a", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("A.b-c_1", True),
        ("_manifest", False),
        ("_MANIFEST", False),
        ("_manifest.json", True),
        ("../escape", False),
        (r"parent\child", False),
        (".hidden", False),
        ("-flag", False),
        ("trailing.", False),
        ("café", False),
        *((name, False) for name in _WINDOWS_DEVICE_NAMES),
        *((name, False) for name in _WINDOWS_DEVICE_NAMES_WITH_EXTENSIONS),
        *((name, True) for name in _PORTABLE_WINDOWS_DEVICE_NEAR_MISSES),
    ],
)
def test_universal_scenario_names_produce_portable_result_files(
    name: str,
    valid: bool,
) -> None:
    module = _harness()

    assert module.is_portable_scenario_name(name) is valid


@pytest.mark.parametrize(
    ("name", "well_formed"),
    [
        ("example", True),
        ("_manifest", False),
        ("_manifest.json", True),
    ],
)
def test_universal_result_contract_uses_portable_scenario_names(
    name: str,
    well_formed: bool,
) -> None:
    module = _harness()
    data = _result_data(
        {
            "success": True,
            "resolution_success": True,
            "lock_consistent": True,
            "timed_out": False,
        }
    )
    data["input"]["scenario"] = name

    assert module.result_is_well_formed(data) is well_formed


def test_universal_scenario_names_cannot_collide_case_insensitively() -> None:
    module = _harness()

    with pytest.raises(
        ValueError,
        match=(
            "universal scenario names collide on case-insensitive filesystems: "
            "'Example' / 'example'"
        ),
    ):
        module._validate_scenario_names(["Example", "example"])


def test_duplicate_universal_platforms_are_rejected_in_declaration_order() -> None:
    module = _harness()

    with pytest.raises(
        ValueError,
        match=r"^example: platforms has duplicate entry: 'windows_amd64'$",
    ):
        module.validate_scenario(
            "example",
            _universal_scenario(
                "linux_x86_64",
                "windows_amd64",
                "macos_arm64",
                "windows_amd64",
                "linux_x86_64",
            ),
        )


@pytest.mark.parametrize(
    ("scenario_name", "scenario", "message"),
    [
        (
            "example",
            _universal_scenario(
                "linux_x86_64",
                "macos_arm64",
                "linux_x86_64",
            ),
            r"^example: platforms has duplicate entry: 'linux_x86_64'$",
        ),
        (
            "_manifest",
            _universal_scenario("linux_x86_64"),
            r"^invalid universal scenario name\(s\): '_manifest'$",
        ),
    ],
)
def test_process_rejects_invalid_scenarios_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_name: str,
    scenario: dict[str, object],
    message: str,
) -> None:
    module = _harness()

    def unexpected_transport() -> None:
        pytest.fail("transport constructed for an invalid scenario")

    monkeypatch.setattr(module, "Urllib3AsyncTransport", unexpected_transport)
    output_dir = tmp_path / "results"

    with pytest.raises(ValueError, match=message):
        module.process_scenario(
            scenario_name,
            scenario,
            "commit",
            force=True,
            output_dir=output_dir,
            source=_CLEAN_SOURCE,
        )

    assert not output_dir.exists()


def test_universal_boolean_settings_do_not_coerce_strings() -> None:
    module = _harness()
    with pytest.raises(
        TypeError,
        match="example: align_across_tuples must be a boolean",
    ):
        module.validate_scenario(
            "example",
            {
                "python": ">=3.11,<3.12",
                "platforms": ["linux_x86_64"],
                "requirements": ["foo"],
                "align_across_tuples": "false",
            },
        )


def test_main_preflights_every_universal_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "universal.toml").write_text(
        """[valid]
python = ">=3.11,<3.12"
platforms = ["linux_x86_64"]
requirements = ["foo"]

[duplicate]
python = ">=3.11,<3.12"
platforms = ["linux_x86_64", "linux_x86_64"]
requirements = ["bar"]

[valid-after]
python = ">=3.11,<3.12"
platforms = ["macos_arm64"]
requirements = ["baz"]
"""
    )
    results_dir = tmp_path / "results"
    processed: list[str] = []

    def record_process(name: str, *_args: object, **_kwargs: object) -> bool:
        processed.append(name)
        return True

    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(module, "get_git_source_state", lambda: _CLEAN_SOURCE)
    monkeypatch.setattr(module, "process_scenario", record_process)
    monkeypatch.setattr(sys, "argv", [str(_BENCHMARK), "--commit", "label"])

    with pytest.raises(
        ValueError,
        match=r"^duplicate: platforms has duplicate entry: 'linux_x86_64'$",
    ):
        module.main()

    assert processed == []
    assert not (results_dir / "label" / "universal").exists()


def test_main_rejects_an_unselected_manifest_scenario_name_before_any_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "universal.toml").write_text(
        """[valid]
python = ">=3.11,<3.12"
platforms = ["linux_x86_64"]
requirements = ["foo"]

[_manifest]
python = ">=3.11,<3.12"
platforms = ["linux_x86_64"]
requirements = ["bar"]

[valid-after]
python = ">=3.11,<3.12"
platforms = ["macos_arm64"]
requirements = ["baz"]
"""
    )
    results_dir = tmp_path / "results"
    processed: list[str] = []

    def record_process(name: str, *_args: object, **_kwargs: object) -> bool:
        processed.append(name)
        return True

    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(module, "get_git_source_state", lambda: _CLEAN_SOURCE)
    monkeypatch.setattr(module, "process_scenario", record_process)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(_BENCHMARK), "--commit", "label", "--scenario", "valid"],
    )

    with pytest.raises(
        ValueError,
        match=r"^invalid universal scenario name\(s\): '_manifest'$",
    ):
        module.main()

    assert processed == []
    assert not (results_dir / "label").exists()


def test_main_exits_nonzero_for_unexpected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "get_git_source_state", lambda: _CLEAN_SOURCE)
    full_dir = tmp_path / "label" / "universal"
    full_dir.mkdir(parents=True)
    full_manifest = full_dir / "_manifest.json"
    full_manifest.write_text('{"existing": true}')

    def fake_process(
        name: str,
        _scenario: dict,
        _commit: str,
        *,
        force: bool,
        output_dir: Path,
        source: dict,
    ) -> bool:
        assert force is False
        assert source == _CLEAN_SOURCE
        (output_dir / f"{name}.json").write_text("{}")
        return False

    monkeypatch.setattr(module, "process_scenario", fake_process)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(_BENCHMARK), "--commit", "label", "--scenario", "marker-heavy"],
    )

    with pytest.raises(SystemExit, match="1"):
        module.main()

    manifest = json.loads(
        (tmp_path / "label" / "universal-selected" / "_manifest.json").read_text()
    )
    assert manifest["run_kind"] == "selected"
    assert manifest["complete"] is True
    assert manifest["completed_scenarios"] == ["marker-heavy"]
    assert full_manifest.read_text() == '{"existing": true}'


def test_main_records_the_complete_full_scenario_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(module, "get_git_source_state", lambda: _CLEAN_SOURCE)

    def fake_process(
        name: str,
        _scenario: dict,
        commit: str,
        *,
        force: bool,
        output_dir: Path,
        source: dict,
    ) -> bool:
        assert force is False
        assert source == _CLEAN_SOURCE
        assert output_dir == tmp_path / commit / "universal"
        (output_dir / f"{name}.json").write_text("{}")
        return True

    monkeypatch.setattr(module, "process_scenario", fake_process)
    monkeypatch.setattr(sys, "argv", [str(_BENCHMARK)])

    module.main()

    manifest = json.loads(
        (
            tmp_path / _CLEAN_SOURCE["commit"] / "universal" / "_manifest.json"
        ).read_text()
    )
    assert manifest["run_kind"] == "full"
    assert manifest["complete"] is True
    assert manifest["available_scenarios"] == manifest["selected_scenarios"]
    assert manifest["available_scenarios"] == manifest["completed_scenarios"]
    assert manifest["source"] == _CLEAN_SOURCE
    assert len(manifest["available_scenarios"]) == 18


def test_universal_scenarios_only_override_alignment_for_noalign_cases() -> None:
    module = _harness()
    scenarios = module.tomllib.loads(
        (module.SCENARIOS_DIR / "universal.toml").read_text()
    )

    assert {
        name
        for name, scenario in scenarios.items()
        if scenario.get("align_across_tuples") is False
    } == {
        "apache-airflow-universal-noalign",
        "legacy-36-root-noalign",
        "numpy-wide-python-diverge",
    }
    assert not any(
        scenario.get("align_across_tuples") is True for scenario in scenarios.values()
    )


@pytest.mark.parametrize(
    ("names", "valid"),
    [
        (["example"], True),
        (["_manifest"], False),
        (["_manifest.json"], True),
        (["Example", "example"], False),
    ],
)
def test_summary_requires_portable_distinct_scenario_names(
    names: list[str],
    valid: bool,
) -> None:
    module = _summary_harness()

    assert module._valid_scenario_names(names) is valid


@pytest.mark.parametrize(
    ("result", "schema", "exit_code"),
    [
        (
            {
                "success": False,
                "resolution_success": None,
                "expected_failure": False,
                "skip_on_fail": False,
                "timed_out": True,
                "lock_consistent": None,
                "error": "scenario exceeded wall-clock budget",
            },
            3,
            1,
        ),
        (
            {
                "success": False,
                "resolution_success": False,
                "expected_failure": True,
                "skip_on_fail": True,
                "timed_out": False,
                "lock_consistent": None,
            },
            3,
            0,
        ),
        (
            {
                "success": False,
                "resolution_success": False,
                "expected_failure": True,
                "skip_on_fail": False,
                "timed_out": False,
                "lock_consistent": None,
            },
            3,
            1,
        ),
        (
            {
                "success": True,
                "resolution_success": True,
                "expected_failure": False,
                "skip_on_fail": False,
                "timed_out": False,
                "lock_consistent": True,
            },
            2,
            1,
        ),
    ],
)
def test_summary_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict,
    schema: int,
    exit_code: int,
) -> None:
    module = _summary_harness()
    result_dir = tmp_path / "run" / "universal"
    result_dir.mkdir(parents=True)
    data = _result_data(result, schema=schema)
    data["input"].update(
        {"scenario": "example", "commit": "run", "source": _CLEAN_SOURCE}
    )
    (result_dir / "example.json").write_text(json.dumps(data))
    (result_dir / "_manifest.json").write_text(
        json.dumps(
            {
                "benchmark_schema": 3,
                "commit": "run",
                "source": _CLEAN_SOURCE,
                "run_kind": "full",
                "available_scenarios": ["example"],
                "selected_scenarios": ["example"],
                "completed_scenarios": ["example"],
                "complete": True,
            }
        )
    )
    (result_dir / "removed.json").write_text(
        json.dumps(
            {
                "input": {"benchmark_schema": 2, "commit": "run"},
                "result": {"success": True},
                "stats": {"decisions_total": 999},
            }
        )
    )
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["universal_summary.py", "run"])

    assert module.main() == exit_code


def test_summary_exits_nonzero_without_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _summary_harness()
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["universal_summary.py", "missing"])

    assert module.main() == 1


def test_summary_rejects_a_selected_run_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _summary_harness()
    result_dir = tmp_path / "run" / "universal"
    result_dir.mkdir(parents=True)
    (result_dir / "_manifest.json").write_text(
        json.dumps(
            {
                "benchmark_schema": 3,
                "commit": "run",
                "source": _CLEAN_SOURCE,
                "run_kind": "selected",
                "available_scenarios": ["a", "b"],
                "selected_scenarios": ["a"],
                "completed_scenarios": ["a"],
                "complete": True,
            }
        )
    )
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["universal_summary.py", "run"])

    assert module.main() == 1


def test_summary_rejects_a_dirty_full_run(
    tmp_path: Path,
) -> None:
    module = _summary_harness()
    result_dir = tmp_path / "run" / "universal"
    result_dir.mkdir(parents=True)
    dirty_source = {"commit": "a" * 40, "dirty": True, "diff_hash": "b" * 64}
    (result_dir / "_manifest.json").write_text(
        json.dumps(
            {
                "benchmark_schema": 3,
                "commit": "run",
                "source": dirty_source,
                "run_kind": "full",
                "available_scenarios": ["example"],
                "selected_scenarios": ["example"],
                "completed_scenarios": ["example"],
                "complete": True,
            }
        )
    )

    assert module.authoritative_result_paths(result_dir) is None
