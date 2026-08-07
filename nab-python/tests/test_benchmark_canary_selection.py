from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_CANARY = Path(__file__).resolve().parents[1] / "benchmarks" / "canary.py"
_EXPECTED_CANARY_CASES = 19


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_benchmark_canary_selection", _CANARY
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_scenario(path: Path, name: str = "example") -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'[{name}]\npython_version = "3.11"\nrequirements = ["demo"]\n',
        encoding="utf-8",
    )
    return {"python_version": "3.11", "requirements": ["demo"]}


def _resolve_path_as(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    target: Path,
) -> None:
    original_resolve = Path.resolve
    resolved_target = target.resolve()

    def resolve(candidate: Path, strict: bool = False) -> Path:
        if candidate == path:
            return resolved_target
        return original_resolve(candidate, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)


def test_default_canary_selection_remains_19_cases() -> None:
    module = _harness()

    selected = module.select_scenarios(list(module.CANARY_SCENARIOS))

    assert len(selected) == _EXPECTED_CANARY_CASES
    assert selected == module.CANARY_SCENARIOS


@pytest.mark.parametrize(
    "label",
    [
        "6110d2d3",
        "verify-base-6110d2d3",
        "verify-branch-7669d712",
        "benchmark_1",
        "a" * 128,
    ],
)
def test_result_directory_label_accepts_one_component(label: str) -> None:
    module = _harness()

    assert module._result_directory_label(label) == label


@pytest.mark.parametrize(
    "label",
    [
        "",
        ".",
        "..",
        "../escape",
        "/absolute",
        "nested/path",
        "nested\\path",
        "..\\escape",
        "C:\\absolute",
        "C:/absolute",
        "C:drive-relative",
        "\\rooted",
        "\\\\server\\share",
        "a" * 129,
    ],
)
def test_result_directory_label_rejects_escaping_or_nested_paths(label: str) -> None:
    module = _harness()

    with pytest.raises(
        argparse.ArgumentTypeError,
        match="commit label must use a portable ASCII filename",
    ):
        module._result_directory_label(label)


@pytest.mark.parametrize(
    "label",
    [
        "CON",
        "con.txt",
        "PRN",
        "AUX.log",
        "NUL",
        "COM1",
        "com9.json",
        "LPT1",
        "lpt9.txt",
        "label.",
        "label ",
        "space label",
        ".hidden",
        "-leading-hyphen",
        "label<",
        "label>",
        'label"',
        "label|",
        "label?",
        "label*",
        "résumé",
    ],
)
def test_result_directory_label_rejects_nonportable_windows_names(
    label: str,
) -> None:
    module = _harness()

    with pytest.raises(argparse.ArgumentTypeError):
        module._result_directory_label(label)


def test_invalid_commit_label_exits_before_creating_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _harness()
    results_dir = tmp_path / "results"
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(sys, "argv", ["canary.py", "--commit", "../escape"])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
    assert "commit label must use a portable ASCII filename" in capsys.readouterr().err
    assert not results_dir.exists()


def test_result_directory_rejects_symlinked_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    link = results_dir / "safe"
    original_is_symlink = Path.is_symlink

    def is_symlink(path: Path) -> bool:
        return path == link or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)

    with pytest.raises(ValueError, match="must be a direct child of RESULTS_DIR"):
        module._result_directory("safe")


def test_existing_summary_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "inside.toml").write_text(
        '[skipped]\nunsupported_reason = "test fixture"\n',
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    out_dir = results_dir / "safe"
    out_dir.mkdir(parents=True)
    existing = out_dir / "canary_123.json"
    existing.write_text("sentinel\n", encoding="utf-8")
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(module.time, "time", lambda: 123)
    monkeypatch.setattr(
        sys,
        "argv",
        ["canary.py", "--commit", "safe", "--scenario", "inside:skipped"],
    )

    with pytest.raises(FileExistsError):
        module.main()

    assert existing.read_text(encoding="utf-8") == "sentinel\n"


def test_find_scenario_reads_a_real_toml_inside_scenarios_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    expected = _write_scenario(scenarios_dir / "inside.toml")
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)

    assert module.find_scenario("inside:example") == expected


def test_find_scenario_rejects_parent_traversal_to_a_real_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    _write_scenario(tmp_path / "outside.toml")
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)

    with pytest.raises(ValueError, match="scenario file stem must use a portable"):
        module.find_scenario("../outside:example")


def test_find_scenario_rejects_absolute_path_to_a_real_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    outside = tmp_path / "outside.toml"
    _write_scenario(outside)
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)

    with pytest.raises(ValueError, match="scenario file stem must use a portable"):
        module.find_scenario(f"{outside.with_suffix('')}:example")


def test_find_scenario_rejects_windows_parent_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)

    with pytest.raises(ValueError, match="scenario file stem must use a portable"):
        module.find_scenario("..\\outside:example")


def test_find_scenario_rejects_symlink_outside_scenarios_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    outside = tmp_path / "outside.toml"
    _write_scenario(outside)
    link = scenarios_dir / "linked.toml"
    _write_scenario(link)
    _resolve_path_as(monkeypatch, link, outside)
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)

    with pytest.raises(ValueError, match="resolves outside SCENARIOS_DIR"):
        module.find_scenario("linked:example")


def test_bare_scenario_rejects_symlink_outside_scenarios_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    outside = tmp_path / "outside.toml"
    _write_scenario(outside)
    link = scenarios_dir / "linked.toml"
    _write_scenario(link)
    _resolve_path_as(monkeypatch, link, outside)
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)

    with pytest.raises(ValueError, match="resolves outside SCENARIOS_DIR"):
        module.find_scenario("example")


def test_missing_scenario_exits_before_creating_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "inside.toml").write_text(
        '[known]\nunsupported_reason = "test fixture"\n',
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "canary.py",
            "--commit",
            "safe",
            "--scenario",
            "inside:known",
            "--scenario",
            "missing:example",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
    assert "scenario not found: 'missing:example'" in capsys.readouterr().err
    assert not results_dir.exists()


def test_empty_scenarios_list_exits_before_creating_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _harness()
    scenarios_list = tmp_path / "empty.txt"
    scenarios_list.write_text("\n  \n", encoding="utf-8")
    results_dir = tmp_path / "results"
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "canary.py",
            "--commit",
            "safe",
            "--scenarios-list",
            str(scenarios_list),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
    assert "at least one scenario must be selected" in capsys.readouterr().err
    assert not results_dir.exists()
