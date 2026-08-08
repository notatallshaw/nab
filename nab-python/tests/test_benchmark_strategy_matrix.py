"""Contracts for the canonical benchmark corpus and explicit strategy matrix."""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal

import pytest

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
_STANDARD_FILES = 14
_STANDARD_SCENARIOS = 558
_RUNNABLE_SCENARIOS = 536
_UNSUPPORTED_SCENARIOS = 22
_TOTAL_EXECUTION_IDENTITIES = 1_674
_RUNNABLE_STRATEGY_EXECUTIONS = 1_608
_MARKER_BUILD_SCENARIOS = {
    "ai-stack:llama-index-experimental-gpt5",
    "ai-stack:open-r1",
    "forums:so-gluonts-mxnet-pin-68451898",
    "pip:pip-11760-torchgeo-min",
    "pip:pip-11760-torchgeo-nbconvert-pin",
    "pip:pip-9572-textract-pypdf2",
    "uv:uv-issue-13321-axolotl-stack",
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
    "available_execution_keys",
    "selected_execution_keys",
    "completed_execution_keys",
    "unsupported_execution_keys",
    "file_execution_keys",
    "complete",
}


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
    spec.loader.exec_module(module)
    return module


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
    input_data = module.prepare_standard_execution(
        execution,
        commit=commit,
        source=source,
        corpus_hash=corpus_hash,
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

    def process(
        execution: object,
        commit: str,
        *,
        force: bool,
        source: dict[str, object],
        corpus_hash: str,
    ) -> bool:
        del force
        seen.append(execution)
        if result_kind == "missing":
            return True
        payload = _result_payload(module, execution, commit, source, corpus_hash)
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
        return True

    monkeypatch.setattr(module, "process_scenario", process)
    argv = ["--commit", "run"]
    if select_quick:
        argv.extend(("--toml", "quick"))
    if matrix:
        argv.append("--strategy-matrix")
    if force:
        argv.append("--force")
    module.main(argv)
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
    executions = module.standard_execution_plan(rows, module.STANDARD_STRATEGIES)
    execution_keys = module.standard_execution_keys(rows, module.STANDARD_STRATEGIES)

    assert len(executions) == _RUNNABLE_STRATEGY_EXECUTIONS
    assert len(execution_keys) == _TOTAL_EXECUTION_IDENTITIES


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
    supported = module.StandardScenario("quick", "example", {})
    unsupported = module.StandardScenario(
        "quick", "unsupported", {"unsupported_reason": "reviewed"}
    )

    assert [
        execution.result_key
        for execution in module.standard_execution_plan(
            [supported, unsupported], module.STANDARD_STRATEGIES
        )
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
    expected_input = payload["input"]
    assert isinstance(expected_input, dict)
    target = expected_input
    for key in field_path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = forged_value

    prepared = module.prepare_standard_execution(
        execution,
        commit="run",
        source=dict(_CLEAN_SOURCE),
        corpus_hash="f" * 64,
    )
    assert not module._standard_result_data_valid(
        payload,
        prepared.expected_input,
    )


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
    assert manifest["corpus_files"] == ["other", "quick"]
    assert manifest["selected_files"] == ["quick"]
    assert manifest["available_logical_keys"] == [
        "other:other",
        "quick:example",
        "quick:unsupported",
    ]
    assert manifest["selected_logical_keys"] == [
        "quick:example",
        "quick:unsupported",
    ]
    assert manifest["completed_logical_keys"] == ["quick:example"]
    assert manifest["unsupported_logical_keys"] == ["quick:unsupported"]
    assert manifest["completed_execution_keys"] == expected_completed
    assert manifest["file_execution_keys"] == expected_completed
    assert (
        sorted(
            manifest["completed_execution_keys"]
            + manifest["unsupported_execution_keys"]
        )
        == manifest["selected_execution_keys"]
    )
    assert manifest["complete"] is True


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

    assert default["resolution_strategy"] is module.sc.ResolutionStrategy.HIGHEST
    assert lowest["resolution_strategy"] is module.sc.ResolutionStrategy.LOWEST


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
