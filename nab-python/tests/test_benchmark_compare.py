"""Contracts for fail-closed standard benchmark comparisons."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.benchmark

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
_STRATEGIES = ("highest", "lowest", "lowest-direct")
_COMPLETED_LOGICAL = ("cases:done",)
_UNSUPPORTED_LOGICAL = ("cases:nope",)
_INAPPLICABLE_LOGICAL = ("cases:host",)
_DIGIT_LIMIT_VERSION = "1." + "0" * 5_000
_REQUIRES_MATCHING_HOST_LOGICAL = ("cases:host",)


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_benchmark_compare", _BENCHMARKS / "compare.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execution_keys(logical_keys: tuple[str, ...]) -> list[str]:
    keys: list[str] = []
    for logical_key in logical_keys:
        stem, name = logical_key.split(":")
        for strategy in _STRATEGIES:
            directory = stem if strategy == "highest" else f"{stem}-{strategy}"
            keys.append(f"{directory}/{name}.json")
    return sorted(keys)


def _settings() -> dict[str, object]:
    return {
        "dist_policy": "wheel-or-sdist",
        "build_policy": "never",
        "trust_unverified_sdist_deps_default": True,
        "max_iterations": 200_000,
        "wall_timeout_seconds": 120,
        "host": {
            "python": "3.12.1 (benchmark fixture)",
            "marker_environment": {
                "implementation_name": "cpython",
                "implementation_version": "3.12.1",
                "os_name": "posix",
                "platform_machine": "x86_64",
                "platform_python_implementation": "CPython",
                "platform_release": "fixture-release",
                "platform_system": "Linux",
                "platform_version": "fixture-version",
                "python_full_version": "3.12.1",
                "python_version": "3.12",
                "sys_platform": "linux",
            },
            "wheel_tags_count": 2,
            "wheel_tags_hash": "d" * 64,
        },
    }


def _manifest(
    module: ModuleType,
    label: str,
    source: dict[str, object],
) -> dict[str, object]:
    available_logical = tuple(
        sorted(_COMPLETED_LOGICAL + _UNSUPPORTED_LOGICAL + _INAPPLICABLE_LOGICAL)
    )
    completed_execution = _execution_keys(_COMPLETED_LOGICAL)
    return {
        "benchmark_schema": module.MANIFEST_SCHEMA,
        "commit": label,
        "source_start": source,
        "source_end": source,
        "mode": "strategy-matrix",
        "strategies": list(_STRATEGIES),
        "settings": _settings(),
        "corpus_hash": "c" * 64,
        "corpus_files": ["cases"],
        "selected_files": ["cases"],
        "available_logical_keys": list(available_logical),
        "selected_logical_keys": list(available_logical),
        "completed_logical_keys": list(_COMPLETED_LOGICAL),
        "unsupported_logical_keys": list(_UNSUPPORTED_LOGICAL),
        "requires_matching_host_logical_keys": list(_REQUIRES_MATCHING_HOST_LOGICAL),
        "inapplicable_logical_keys": list(_INAPPLICABLE_LOGICAL),
        "available_execution_keys": _execution_keys(available_logical),
        "selected_execution_keys": _execution_keys(available_logical),
        "completed_execution_keys": completed_execution,
        "unsupported_execution_keys": _execution_keys(_UNSUPPORTED_LOGICAL),
        "file_execution_keys": completed_execution,
        "complete": True,
    }


def _execution_strategy(execution_key: str) -> str:
    result_directory = execution_key.partition("/")[0]
    for strategy in reversed(_STRATEGIES):
        if result_directory.endswith(f"-{strategy}"):
            return strategy
    return "highest"


def _result_payload(
    module: ModuleType,
    manifest: dict[str, object],
    execution_key: str,
    rounds: int,
) -> dict[str, object]:
    input_data = {
        "benchmark_schema": module.MANIFEST_SCHEMA,
        "commit": manifest["commit"],
        "source": manifest["source_start"],
        "corpus_hash": manifest["corpus_hash"],
        "logical_key": "cases:done",
        "execution_key": execution_key,
        "settings_hash": module._settings_hash(manifest["settings"]),
        "python_version": "3.12",
        "requirements": ["demo>=1"],
        "constraints": ["support<3"],
    }
    strategy = _execution_strategy(execution_key)
    if strategy != "highest":
        input_data["resolution"] = strategy

    stats = dict.fromkeys(module._STANDARD_COUNTER_FIELDS, 0)
    stats["rounds"] = rounds
    stats["packages_resolved"] = 1
    stats["wall_time_seconds"] = 0.01
    return {
        "input": input_data,
        "result": {"success": True, "error": None, "pins": {"demo": "1.0"}},
        "stats": stats,
    }


def _write_run(
    module: ModuleType,
    root: Path,
    label: str,
    *,
    source_commit: str,
    rounds: int = 1,
) -> Path:
    """Write one complete strategy-matrix run with all terminal states."""
    source = {"commit": source_commit, "dirty": False, "diff_hash": None}
    manifest = _manifest(module, label, source)
    run_dir = root / label
    run_dir.mkdir()
    (run_dir / module.MANIFEST_FILENAME).write_text(json.dumps(manifest))

    for execution_key in _execution_keys(_COMPLETED_LOGICAL):
        path = run_dir / execution_key
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(_result_payload(module, manifest, execution_key, rounds))
        )
    return run_dir


def _rewrite_json(path: Path, mutate: Callable[[dict], None]) -> None:
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def _remove_completed_result(run_dir: Path) -> None:
    (run_dir / "cases" / "done.json").unlink()


def _add_extra_result(run_dir: Path) -> None:
    (run_dir / "cases" / "extra.json").write_text("{}")


def test_comparison_uses_the_manifest_and_keeps_scenario_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _harness()
    first_dir = _write_run(module, tmp_path, "first", source_commit="a" * 40)
    second_dir = _write_run(
        module, tmp_path, "second", source_commit="b" * 40, rounds=2
    )
    (first_dir / "_provenance.json").write_text("{}")
    (first_dir / "universal").mkdir()
    (first_dir / "universal" / "result.json").write_text("{}")

    first = module.load_run(first_dir)
    second = module.load_run(second_dir)
    module.require_comparable(first, second)
    module.compare_scenario(
        first.results["cases/done.json"],
        second.results["cases/done.json"],
        "cases/done.json",
    )

    assert first.results["cases/done.json"]["input"]["requirements"] == ["demo>=1"]
    assert "Rounds: 1 -> 2 (+100.0%)" in capsys.readouterr().out


def test_comparison_reports_changed_pins(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _harness()
    first = module.load_run(
        _write_run(module, tmp_path, "first", source_commit="a" * 40)
    )
    second_dir = _write_run(module, tmp_path, "second", source_commit="b" * 40)
    _rewrite_json(
        second_dir / "cases" / "done.json",
        lambda data: data["result"].update(pins={"demo": "2.0"}),
    )
    second = module.load_run(second_dir)

    module.require_comparable(first, second)
    module.compare_scenario(
        first.results["cases/done.json"],
        second.results["cases/done.json"],
        "cases/done.json",
    )

    assert "Pin changed: demo 1.0 -> 2.0" in capsys.readouterr().out


def test_pin_changes_reports_each_changed_package() -> None:
    module = _harness()

    assert module._pin_changes(
        {"changed": "1.0", "removed": "2.0", "same": "3.0"},
        {"added": "4.0", "changed": "1.1", "same": "3.0"},
    ) == [
        "Pin added: added==4.0",
        "Pin changed: changed 1.0 -> 1.1",
        "Pin removed: removed==2.0",
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.update(complete=False), id="incomplete"),
        pytest.param(
            lambda data: data["source_start"].update(dirty=True),
            id="dirty-source",
        ),
        pytest.param(lambda data: data.update(commit="other"), id="wrong-label"),
        pytest.param(lambda data: data.update(mode="default"), id="wrong-mode"),
        pytest.param(
            lambda data: data["completed_logical_keys"].append("cases:host"),
            id="overlapping-partition",
        ),
        pytest.param(
            lambda data: data["requires_matching_host_logical_keys"].clear(),
            id="inapplicable-without-host-requirement",
        ),
        pytest.param(
            lambda data: data["requires_matching_host_logical_keys"].append(
                "cases:other"
            ),
            id="unselected-host-requirement",
        ),
    ],
)
def test_load_run_rejects_an_invalid_manifest(
    tmp_path: Path,
    mutate: Callable[[dict], None],
) -> None:
    module = _harness()
    run_dir = _write_run(module, tmp_path, "run", source_commit="a" * 40)
    manifest_path = run_dir / module.MANIFEST_FILENAME
    _rewrite_json(manifest_path, mutate)

    with pytest.raises(module.ComparisonError):
        module.load_run(run_dir)


def test_load_run_rejects_a_schema_three_manifest(tmp_path: Path) -> None:
    module = _harness()
    run_dir = _write_run(module, tmp_path, "run", source_commit="a" * 40)
    _rewrite_json(
        run_dir / module.MANIFEST_FILENAME,
        lambda data: data.update(benchmark_schema=3),
    )

    with pytest.raises(module.ComparisonError, match="run is not complete"):
        module.load_run(run_dir)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data["settings"].pop("host"), id="missing-host"),
        pytest.param(
            lambda data: data["settings"].update(wall_timeout_seconds=0),
            id="zero-timeout",
        ),
        pytest.param(
            lambda data: data["settings"]["host"].update(wheel_tags_count=True),
            id="boolean-tag-count",
        ),
        pytest.param(
            lambda data: data["settings"]["host"].update(wheel_tags_hash="not-a-hash"),
            id="invalid-tag-hash",
        ),
    ],
)
def test_load_run_rejects_invalid_settings(
    tmp_path: Path,
    mutate: Callable[[dict], None],
) -> None:
    module = _harness()
    run_dir = _write_run(module, tmp_path, "run", source_commit="a" * 40)
    _rewrite_json(run_dir / module.MANIFEST_FILENAME, mutate)

    with pytest.raises(module.ComparisonError):
        module.load_run(run_dir)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_remove_completed_result, id="missing"),
        pytest.param(_add_extra_result, id="extra"),
    ],
)
def test_load_run_rejects_result_file_set_changes(
    tmp_path: Path,
    mutate: Callable[[Path], None],
) -> None:
    module = _harness()
    run_dir = _write_run(module, tmp_path, "run", source_commit="a" * 40)
    mutate(run_dir)

    with pytest.raises(module.ComparisonError):
        module.load_run(run_dir)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (100, 110, "+10.0%"),
        (100, 90, "-10.0%"),
        (0, 1, "+inf%"),
        (0, 0, "0.0%"),
    ],
)
def test_percent_change_is_relative_to_the_baseline(
    old: float,
    new: float,
    expected: str,
) -> None:
    module = _harness()

    assert module.percent_change(old, new) == expected


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.update(result={}), id="result-shape"),
        pytest.param(
            lambda data: data["result"].pop("pins"),
            id="missing-pins",
        ),
        pytest.param(
            lambda data: data["result"].update(pins=[]),
            id="pins-not-a-mapping",
        ),
        pytest.param(
            lambda data: data["result"].update(pins={"Demo": "1.0"}),
            id="non-normalized-name",
        ),
        pytest.param(
            lambda data: data["result"].update(pins={"demo": True}),
            id="version-not-a-string",
        ),
        pytest.param(
            lambda data: data["result"].update(pins={"demo": "1.0-1"}),
            id="non-normalized-version",
        ),
        pytest.param(
            lambda data: data["result"].update(pins={"demo": _DIGIT_LIMIT_VERSION}),
            id="integer-digit-limit",
        ),
        pytest.param(
            lambda data: data["result"].update(success=False, error="failed"),
            id="failure-with-pins",
        ),
        pytest.param(
            lambda data: data["stats"].pop("rounds"),
            id="stats-shape",
        ),
        pytest.param(
            lambda data: data["stats"].update(rounds=-1),
            id="negative-counter",
        ),
        pytest.param(
            lambda data: data["stats"].update(wall_time_seconds="slow"),
            id="wall-time-type",
        ),
        pytest.param(
            lambda data: data["stats"].update(packages_resolved=2),
            id="pin-count-mismatch",
        ),
    ],
)
def test_load_run_rejects_an_invalid_result_payload(
    tmp_path: Path,
    mutate: Callable[[dict], None],
) -> None:
    module = _harness()
    run_dir = _write_run(module, tmp_path, "run", source_commit="a" * 40)
    _rewrite_json(run_dir / "cases" / "done.json", mutate)

    with pytest.raises(module.ComparisonError):
        module.load_run(run_dir)


@pytest.mark.parametrize("field", ["source", "settings_hash", "execution_key"])
def test_load_run_rejects_result_provenance(
    tmp_path: Path,
    field: str,
) -> None:
    module = _harness()
    run_dir = _write_run(module, tmp_path, "run", source_commit="a" * 40)
    result_path = run_dir / "cases" / "done.json"
    _rewrite_json(result_path, lambda data: data["input"].update({field: "wrong"}))

    with pytest.raises(module.ComparisonError, match="does not match its manifest"):
        module.load_run(run_dir)


def test_comparison_rejects_scenario_input_differences(
    tmp_path: Path,
) -> None:
    module = _harness()
    first = module.load_run(
        _write_run(module, tmp_path, "first", source_commit="a" * 40)
    )
    second_dir = _write_run(module, tmp_path, "second", source_commit="b" * 40)
    result_path = second_dir / "cases" / "done.json"
    _rewrite_json(
        result_path,
        lambda data: data["input"].update(requirements=["other"]),
    )
    second = module.load_run(second_dir)

    with pytest.raises(module.ComparisonError, match="inputs differ"):
        module.require_comparable(first, second)


def test_comparison_rejects_manifest_identity_differences(tmp_path: Path) -> None:
    module = _harness()
    first = module.load_run(
        _write_run(module, tmp_path, "first", source_commit="a" * 40)
    )
    second_dir = _write_run(module, tmp_path, "second", source_commit="b" * 40)
    _rewrite_json(
        second_dir / module.MANIFEST_FILENAME,
        lambda data: data["settings"].update(max_iterations=100_000),
    )
    manifest = json.loads((second_dir / module.MANIFEST_FILENAME).read_text())
    for key in manifest["completed_execution_keys"]:
        _rewrite_json(
            second_dir / key,
            lambda data: data["input"].update(
                settings_hash=module._settings_hash(manifest["settings"])
            ),
        )
    changed_identity = module.load_run(second_dir)

    with pytest.raises(module.ComparisonError, match="different identities"):
        module.require_comparable(first, changed_identity)


def test_comparison_rejects_different_host_requirements(tmp_path: Path) -> None:
    module = _harness()
    first = module.load_run(
        _write_run(module, tmp_path, "first", source_commit="a" * 40)
    )
    second_dir = _write_run(module, tmp_path, "second", source_commit="b" * 40)
    _rewrite_json(
        second_dir / module.MANIFEST_FILENAME,
        lambda data: data.update(
            requires_matching_host_logical_keys=["cases:done", "cases:host"]
        ),
    )
    second = module.load_run(second_dir)

    with pytest.raises(module.ComparisonError, match="different identities"):
        module.require_comparable(first, second)


def test_comparison_rejects_runs_without_completed_executions(tmp_path: Path) -> None:
    module = _harness()

    def make_all_inapplicable(run_dir: Path) -> None:
        def rewrite_manifest(data: dict) -> None:
            data["completed_logical_keys"] = []
            data["requires_matching_host_logical_keys"] = [
                "cases:done",
                "cases:host",
            ]
            data["inapplicable_logical_keys"] = ["cases:done", "cases:host"]
            data["completed_execution_keys"] = []
            data["file_execution_keys"] = []

        _rewrite_json(run_dir / module.MANIFEST_FILENAME, rewrite_manifest)
        for result_path in run_dir.glob("cases*/done.json"):
            result_path.unlink()

    first_dir = _write_run(module, tmp_path, "first", source_commit="a" * 40)
    second_dir = _write_run(module, tmp_path, "second", source_commit="b" * 40)
    make_all_inapplicable(first_dir)
    make_all_inapplicable(second_dir)

    first = module.load_run(first_dir)
    second = module.load_run(second_dir)
    with pytest.raises(module.ComparisonError, match="no completed executions"):
        module.require_comparable(first, second)
