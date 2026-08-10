from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

# canary.py imports its siblings by bare name.
pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.usefixtures("benchmark_import_path"),
]

_CANARY = Path(__file__).resolve().parents[2] / "benchmarks" / "canary.py"
_EXPECTED_CANARY_CASES = 19
_EXPECTED_V2_INPUT_HASHES = {
    "uv-lowest:boto3-urllib3-transient": "ad435caf32fab80e8454ccdd649197d94c39cf034c9d28d3bc2c36bac0005275",
    "pip-lowest:trustllm": "df30174b6d056d4935f4fa1509b0793b54802abcc6d32d54291f4ca4cc1c6956",
    "pip-lowest:copick": "31499574be189da527d8e14ae82f894ea78aede42bb5698805686a68ba441668",
    "pip-lowest:promptflow-vectordb": "df451641b6115b623a42e7cef6d0e8942c191a11d42575289fb4f627a37b0f64",
    "pip:ultralytics-export": "fe9f57216b10878fec77925c7bac99cb98995625f5946646629b7e7eacde7098",
    "pip-lowest:datacontract-cli": "e4f3cf317d15791206d1379d4917aeae91a9ce0306c3624ec375483b96a7fcd5",
    "pip-lowest:pandas-aws-boto3-dandi-frenzy": "87c9501504c42ecbcf90c610391252dac18e2ab39fde4d7a949410f0f43f9e2b",
    "ai-stack:vllm-transformers-floor": "38b7cba134e62de586d4d19a02dac092df50b4ad8aa16e1f0555abf3eb7877bc",
    "pip-lowest:google-bigquery-soda": "c893c6d79b1392087a61037be649ec710168be62b0bafd720f179108f040e5bf",
    "pip-lowest:langchain-ml-course": "912ec2b847a2535dc0304762379bd6a1e54dc3272a9733f93141330aa4126ee5",
    "airflow-lowest-direct:airflow-3-0-2-awswrangler": "c2fec4a4613356cf2d5090eb8ed8d41c3f04336e25e04930d088183f364bdbb5",
    "airflow-lowest-direct:airflow-3-0-3-pandas-sqlalchemy": "696323cc97972a7eb637b06ca422f8857cb5e71fd4d11f65f6ecff02be237a18",
    "airflow-lowest-direct:airflow-portalocker-qdrant": "c509603496ef81bd1e25484c2a845ccd410897e43438b2d98500e1957727e38c",
    "airflow-lowest-direct:airflow-fastapi-121": "1bf1b79027a7d1eb78d9adfa4e368a385f62dcc09e6619dfd68738611bd2dbd1",
    "forums-lowest-direct:so-dbt-core-snowflake-79744735": "2f409a250216abc0c5f89f74bf752db4c2b6b1564264d2df2d22dbb8f3a8d541",
    "uv-lowest:uv-issue-16601-xinference": "a6aa7fbec8e1291349a311745800095e4ed52c02805180da8517299860fb5476",
    "uv-lowest:uv-issue-16601-xinference-fixed": "109bb4cd7dd756a96f5b04edd007f3312fe5853c442ea7ee1b64ae6ea234903f",
    "ai-stack:rag-chroma-langchain": "0aeb86e88d6f7410ef858b51c4104d1f5c79349b9c25a1b71e7b59e053b642fe",
    "ai-stack:streamlit-langchain": "145f34978276b96146e589c080ece06229d3b4817e619f2dfbf7ae95b247f784",
}


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_benchmark_canary_selection", _CANARY
    )
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


def _assert_main_preflight_error(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: dict[str, object],
    message: str,
) -> None:
    """Assert selection fails before canary host capture or result creation."""
    results_dir = tmp_path / "results"
    monkeypatch.setattr(module, "find_scenario", lambda _name: scenario)
    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(
        module,
        "get_git_source_state",
        lambda: {"commit": "a" * 40, "dirty": False, "diff_hash": None},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["canary.py", "--commit", "safe", "--scenario", "quick:example"],
    )

    def fail_host(*_args: object, **_kwargs: object) -> object:
        pytest.fail("scenario preflight reached host capture")

    monkeypatch.setattr(module.BenchmarkHost, "current", classmethod(fail_host))

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err
    assert not results_dir.exists()


def test_default_canary_manifest_preserves_19_strategies() -> None:
    module = _harness()

    cases = module.load_canary_manifest()
    selected = module.select_scenarios(cases)

    assert len(selected) == _EXPECTED_CANARY_CASES
    assert selected == cases
    assert len({case.scenario for case in cases}) == _EXPECTED_CANARY_CASES
    assert Counter(case.resolution.value for case in cases) == {
        "highest": 4,
        "lowest": 10,
        "lowest-direct": 5,
    }
    assert all("-lowest" not in case.scenario.split(":", 1)[0] for case in cases)


def test_default_canaries_do_not_build_packages() -> None:
    module = _harness()
    cases = module.load_canary_manifest()

    assert all(
        "build_packages" not in module.find_scenario(case.scenario) for case in cases
    )


def test_default_canary_manifest_preserves_v2_input_identities() -> None:
    module = _harness()
    actual: dict[str, str] = {}
    labels: list[str] = []

    for case in module.load_canary_manifest():
        scenario = module.find_scenario(case.scenario)
        assert scenario is not None
        resolution = module.scenario_resolution(
            scenario,
            scenario_name=case.scenario,
            override=case.resolution,
        )
        identity = module.canary_v2_identity(case, scenario, resolution)
        actual[identity.scenario] = module.scenario_input_hash(
            identity.scenario,
            identity.definition,
        )
        labels.append(identity.scenario.split(":", 1)[-1])

    assert actual == _EXPECTED_V2_INPUT_HASHES
    assert labels == [
        case.scenario.split(":", 1)[-1] for case in module.load_canary_manifest()
    ]


def test_canary_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    module = _harness()
    manifest = tmp_path / "canary.toml"
    manifest.write_text(
        "schema = 1\n"
        "extra = true\n"
        "[[case]]\n"
        'scenario = "quick:requests"\n'
        'resolution = "highest"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly 'schema' and 'case'"):
        module.load_canary_manifest(manifest)


def test_canary_manifest_rejects_unknown_resolution(tmp_path: Path) -> None:
    module = _harness()
    manifest = tmp_path / "canary.toml"
    manifest.write_text(
        'schema = 1\n[[case]]\nscenario = "quick:requests"\nresolution = "middle"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="resolution must be one of"):
        module.load_canary_manifest(manifest)


def test_canary_manifest_rejects_duplicate_scenarios(tmp_path: Path) -> None:
    module = _harness()
    manifest = tmp_path / "canary.toml"
    manifest.write_text(
        "schema = 1\n"
        "[[case]]\n"
        'scenario = "quick:requests"\n'
        'resolution = "highest"\n'
        "[[case]]\n"
        'scenario = "quick:requests"\n'
        'resolution = "lowest"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate scenarios: quick:requests"):
        module.load_canary_manifest(manifest)


def test_manual_canary_strategy_is_explicit() -> None:
    module = _harness()

    explicit = module.parse_canary_case("quick:requests@lowest-direct")
    default = module.parse_canary_case("quick:requests")

    assert explicit == module.CanaryCase(
        "quick:requests", module.ResolutionStrategy.LOWEST_DIRECT
    )
    assert default == module.CanaryCase("quick:requests", None)


def test_retired_clone_selector_has_actionable_replacement() -> None:
    module = _harness()

    with pytest.raises(
        ValueError,
        match=r"use 'pip:trustllm@lowest'",
    ):
        module.select_scenarios([module.CanaryCase("pip-lowest:trustllm", None)])


def test_bare_canary_discovery_rejects_a_strategy_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios_dir = tmp_path / "scenarios"
    _write_scenario(scenarios_dir / "quick.toml")
    _write_scenario(scenarios_dir / "quick-lowest.toml")
    monkeypatch.setattr(module, "SCENARIOS_DIR", scenarios_dir)

    with pytest.raises(ValueError, match="legacy strategy-clone"):
        module.find_scenario("example")


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
        '[skipped]\nrequirements = []\nunsupported_reason = "test fixture"\n',
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


def test_selection_validates_requirement_lists_before_vcs_pin_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios = {
        "quick:first": {
            "requirements": [],
            "vcs_require_pin": "false",
            "vcs_policy": "permit",
            "vcs_allowed_schemes": {"git+https": False},
            "vcs_allowed_repos": {"https://example.test/repo": False},
        },
        "quick:second": {"requirements": "demo"},
    }
    monkeypatch.setattr(module, "find_scenario", scenarios.get)

    with pytest.raises(
        TypeError,
        match="quick:second: requirements must be a list, got str",
    ):
        module.select_scenarios(
            [
                module.CanaryCase("quick:first", None),
                module.CanaryCase("quick:second", None),
            ]
        )


@pytest.mark.parametrize(
    "unsupported_reason",
    [None, "not runnable"],
    ids=("supported", "unsupported"),
)
def test_selection_validates_unknown_settings_across_the_whole_selection(
    monkeypatch: pytest.MonkeyPatch,
    unsupported_reason: str | None,
) -> None:
    module = _harness()
    second: dict[str, object] = {
        "requirements": [],
        "trust_unverified_sdist_dependencies": False,
    }
    if unsupported_reason is not None:
        second["unsupported_reason"] = unsupported_reason
    scenarios = {
        "quick:first": {
            "requirements": [],
            "trust_unverified_sdist_deps": "false",
        },
        "quick:second": second,
    }
    monkeypatch.setattr(module, "find_scenario", scenarios.get)

    with pytest.raises(
        ValueError,
        match=re.escape(
            "quick:second: unknown scenario settings: "
            "['trust_unverified_sdist_dependencies']"
        ),
    ):
        module.select_scenarios(
            [
                module.CanaryCase("quick:first", None),
                module.CanaryCase("quick:second", None),
            ]
        )


@pytest.mark.parametrize(
    ("vcs_settings", "message"),
    [
        (
            {"vcs_require_pin": "false"},
            "quick:first: vcs_require_pin must be a boolean, got str",
        ),
        (
            {"vcs_policy": "permit"},
            "quick:first: vcs_policy must be one of ['allow', 'block'], got 'permit'",
        ),
        (
            {"vcs_allowed_schemes": {"git+https": False}},
            "quick:first: vcs_allowed_schemes must be a list, got dict",
        ),
        (
            {"vcs_allowed_repos": {"https://example.test/repo": False}},
            "quick:first: vcs_allowed_repos must be a list, got dict",
        ),
    ],
    ids=("pin", "policy", "schemes", "repos"),
)
def test_selection_validates_vcs_settings_before_project_metadata(
    monkeypatch: pytest.MonkeyPatch,
    vcs_settings: dict[str, object],
    message: str,
) -> None:
    module = _harness()
    scenarios = {
        "quick:first": {
            "requirements": [],
            **vcs_settings,
        },
        "quick:second": {
            "requirements": [],
            "project_name": "demo-project",
            "project_extras": ["all"],
            "optional_dependencies": {"all": "demo"},
        },
    }
    monkeypatch.setattr(module, "find_scenario", scenarios.get)

    with pytest.raises(
        (TypeError, ValueError),
        match=re.escape(message),
    ):
        module.select_scenarios(
            [
                module.CanaryCase("quick:first", None),
                module.CanaryCase("quick:second", None),
            ]
        )


@pytest.mark.parametrize(
    ("first_scenario", "second_scenario", "message"),
    [
        (
            {"vcs_policy": "permit"},
            {"vcs_require_pin": "false"},
            "quick:second: vcs_require_pin must be a boolean, got str",
        ),
        (
            {"vcs_allowed_schemes": {"git+https": False}},
            {"vcs_policy": "permit"},
            "quick:second: vcs_policy must be one of ['allow', 'block'], got 'permit'",
        ),
        (
            {"vcs_allowed_repos": {"https://example.test/repo": False}},
            {"vcs_allowed_schemes": {"git+https": False}},
            "quick:second: vcs_allowed_schemes must be a list, got dict",
        ),
        (
            {"indexes": "private"},
            {
                "project_name": "demo-project",
                "project_extras": ["all"],
                "optional_dependencies": {"all": "demo"},
            },
            "quick:second: optional_dependencies['all'] must be a list, got str",
        ),
    ],
    ids=(
        "pin-before-policy",
        "policy-before-schemes",
        "schemes-before-repos",
        "project-before-indexes",
    ),
)
def test_selection_validates_fields_across_the_whole_selection(
    monkeypatch: pytest.MonkeyPatch,
    first_scenario: dict[str, object],
    second_scenario: dict[str, object],
    message: str,
) -> None:
    module = _harness()
    scenarios = {
        "quick:first": {
            "requirements": [],
            **first_scenario,
        },
        "quick:second": {
            "requirements": [],
            **second_scenario,
        },
    }
    monkeypatch.setattr(module, "find_scenario", scenarios.get)

    with pytest.raises((TypeError, ValueError), match=re.escape(message)):
        module.select_scenarios(
            [
                module.CanaryCase("quick:first", None),
                module.CanaryCase("quick:second", None),
            ]
        )


def test_selection_validates_index_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenario = {
        "requirements": [],
        "indexes": [{"name": "private", "url": "https://example.test/simple"}],
        "index_routes": [{"name": "demo>=1", "index": "private"}],
    }
    monkeypatch.setattr(module, "find_scenario", lambda _name: scenario)
    message = (
        "quick:example: index_routes[0].name must be a valid distribution name, "
        "got 'demo>=1'"
    )

    with pytest.raises(ValueError, match=re.escape(message)):
        module.select_scenarios([module.CanaryCase("quick:example", None)])


def test_selection_validates_all_indexes_before_any_index_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenarios = {
        "quick:first": {
            "requirements": [],
            "index_routes": "private",
        },
        "quick:second": {
            "requirements": [],
            "indexes": "private",
        },
    }
    monkeypatch.setattr(module, "find_scenario", scenarios.get)

    with pytest.raises(
        TypeError,
        match="quick:second: indexes must be an array of tables, got str",
    ):
        module.select_scenarios(
            [
                module.CanaryCase("quick:first", None),
                module.CanaryCase("quick:second", None),
            ]
        )


@pytest.mark.parametrize(
    ("first", "second", "error", "message"),
    [
        (
            {"build_packages": "demo"},
            {"marker_environment": "Linux"},
            TypeError,
            "quick:second: marker_environment must be a table of strings",
        ),
        (
            {
                "platform_system": "Linux",
                "build_packages": ["demo"],
            },
            {"build_packages": "demo"},
            TypeError,
            ("quick:second: build_packages must be a list of package names, got str"),
        ),
        (
            {"build_packages": "demo"},
            {"marker_environment": {"platform_codename": "Windows"}},
            ValueError,
            (
                "quick:second: unknown marker_environment variables: ['platform_codename']"
            ),
        ),
    ],
    ids=(
        "marker-shapes-before-build",
        "build-shapes-before-compatibility",
        "marker-names-before-build",
    ),
)
def test_selection_validates_field_phases_across_the_whole_selection(
    monkeypatch: pytest.MonkeyPatch,
    first: dict[str, object],
    second: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    module = _harness()
    scenarios = {
        "quick:first": {"requirements": [], **first},
        "quick:second": {"requirements": [], **second},
    }
    monkeypatch.setattr(module, "find_scenario", scenarios.get)

    with pytest.raises(error, match=re.escape(message)):
        module.select_scenarios(
            [
                module.CanaryCase("quick:first", None),
                module.CanaryCase("quick:second", None),
            ]
        )


def test_selection_validates_build_compatibility_before_resolution_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenario = {
        "requirements": [],
        "platform_system": "Linux",
        "build_packages": ["demo"],
        "resolution": "middle",
        "requires_matching_host": "yes",
    }
    monkeypatch.setattr(module, "find_scenario", lambda _name: scenario)
    message = (
        "quick:example: build_packages cannot be combined "
        "with a marker environment overlay"
    )

    with pytest.raises(ValueError, match=re.escape(message)):
        module.select_scenarios([module.CanaryCase("quick:example", None)])


def test_selection_validates_unsupported_build_shape_but_not_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    valid_quarantine = {
        "requirements": [],
        "unsupported_reason": "marker/build metadata is unsound",
        "platform_system": "Linux",
        "build_packages": ["Demo_Pkg"],
    }
    invalid_quarantine = {
        **valid_quarantine,
        "build_packages": ["demo>=1"],
    }
    selected = [module.CanaryCase("quick:example", None)]

    monkeypatch.setattr(
        module,
        "find_scenario",
        lambda _name: valid_quarantine,
    )
    assert module.select_scenarios(selected) == selected

    monkeypatch.setattr(module, "find_scenario", lambda _name: invalid_quarantine)
    with pytest.raises(ValueError, match="valid distribution name"):
        module.select_scenarios(selected)


def test_selection_rejects_unknown_marker_variables_on_unsupported_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    scenario = {
        "requirements": [],
        "unsupported_reason": "not runnable",
        "marker_environment": {"platform_codename": "Windows"},
    }
    monkeypatch.setattr(module, "find_scenario", lambda _name: scenario)

    with pytest.raises(
        ValueError,
        match=re.escape(
            "quick:example: unknown marker_environment variables: ['platform_codename']"
        ),
    ):
        module.select_scenarios([module.CanaryCase("quick:example", None)])


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        (
            {
                "requirements": [],
                "trust_unverified_sdist_deps": "false",
                "trust_unverified_sdist_dependencies": False,
            },
            "unknown scenario settings: ['trust_unverified_sdist_dependencies']",
        ),
        (
            {"requirements": [], "build_packages": "demo"},
            "build_packages must be a list of package names",
        ),
        (
            {"requirements": [], "resolution": "middle"},
            "resolution must be one of ['highest', 'lowest', 'lowest-direct']",
        ),
        (
            {"requirements": [], "requires_matching_host": "yes"},
            "requires_matching_host must be a boolean",
        ),
        (
            {
                "requirements": [],
                "marker_environment": {"platform_codename": "Windows"},
            },
            "unknown marker_environment variables: ['platform_codename']",
        ),
    ],
    ids=(
        "unknown-setting",
        "build-packages",
        "resolution",
        "host-requirement",
        "marker-variable",
    ),
)
def test_selected_scenario_preflight_fails_before_host_and_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: dict[str, object],
    message: str,
) -> None:
    module = _harness()
    _assert_main_preflight_error(
        module,
        tmp_path,
        monkeypatch,
        capsys,
        scenario,
        message,
    )


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
