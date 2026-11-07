"""Whole-setting overrides use the same parser and consumers as project files."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab import _run
from nab._cli import spec as cli_spec
from nab._cli.definition.model import Kind, Opt, VType
from nab._cli.parse import parse
from nab.cli import run
from nab.config.ladder import (
    OPTIONS,
    Layer,
    Origin,
    Scope,
    SourceKind,
    build_cli_layer,
    build_cli_overrides,
    resolve_config,
)
from nab.config.model import read_pyproject_config
from nab_project import toml_io
from nab_project.input_source import InputFormat
from nab_provider.errors import ConfigError

from .test_prefer_existing_cli import Registry

_STRUCTURED = (
    ("environment", '{python="3.12", platform="linux_x86_64"}'),
    ("marker-environment", '{os_name="posix"}'),
    ("vcs", '{policy="block"}'),
    ("workspace", "{members=[]}"),
    ("indexes", '[{name="test",url="https://example.com/simple/"}]'),
    ("local-sources", '[{name="a",path="/source/a"}]'),
    ("vcs-sources", '[{name="a",url="git+https://example.com/a.git"}]'),
    (
        "archive-sources",
        '[{name="a",url="https://example.com/a.tar.gz#sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]',
    ),
    ("packages", "{a={dependencies=[]}}"),
    ("package-rules", '[{match=["a"],dependencies=[]}]'),
    ("index", '{pypi={dist-policy="wheel-only"}}'),
    ("conflicts", '[[{extra="a"},{extra="b"}]]'),
    ("matrix", '{python="==3.12",platforms=["linux_x86_64"]}'),
)


def project(directory: Path) -> Path:
    """Create the input whose file and CLI forms are compared."""
    path = directory / "pyproject.toml"
    path.write_text('[project]\nname="x"\nversion="0"\n')
    return path


def test_every_structured_setting_has_a_roundtrip_case() -> None:
    assert {key for key, _value in _STRUCTURED} == {
        row.name
        for row in OPTIONS
        if row.scope is Scope.PROJECT and row.cli_flag is None
    }


@pytest.mark.parametrize(("key", "value"), _STRUCTURED)
def test_structured_cli_value_matches_file_value(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
    value: str,
) -> None:
    path = project(hermetic_roots)
    file = hermetic_roots / "nab.toml"
    file.write_text(f"{key} = {value}\n")
    arguments = ("config", "get", key, "--path", str(path))
    assert run(arguments) == 0
    from_file = capsys.readouterr().out
    file.unlink()

    assert run((*arguments, "--setting", f"project.{key}={value}")) == 0
    assert capsys.readouterr().out == from_file


@pytest.mark.parametrize(
    "assignment",
    [
        "offline=true",
        "wrong.offline=true",
        "user.unknown=true",
        'user.resolution="highest"',
        "user.offline=not-toml",
        "user.offline=true\nextra=1",
    ],
)
def test_invalid_assignment_reports_its_cli_source(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str], assignment: str
) -> None:
    path = project(hermetic_roots)
    assert (
        run(("config", "get", "offline", "--path", str(path), "--setting", assignment))
        == 1
    )
    assert "--setting" in capsys.readouterr().err


def test_user_assignment_overrides_file_and_environment(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = project(hermetic_roots)
    (hermetic_roots / "nab.toml").write_text('lock-policy="resolve"\n')
    monkeypatch.setenv("NAB_LOCK_POLICY", "prefer-existing")

    assert (
        run(
            (
                "config",
                "get",
                "lock-policy",
                "--path",
                str(path),
                "--setting",
                'user.lock-policy="reuse"',
            )
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "reuse"


def test_competing_override_forms_are_refused(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project(hermetic_roots)
    assert (
        run(
            (
                "config",
                "get",
                "resolution",
                "--path",
                str(path),
                "--project-resolution",
                "highest",
                "--setting",
                'project.resolution="lowest"',
            )
        )
        == 1
    )
    assert "use one form" in capsys.readouterr().err


def test_whole_value_replaces_file_table_and_explain_reports_winner(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project(hermetic_roots)
    (hermetic_roots / "nab.toml").write_text('[packages.a]\ndependencies=["b"]\n')
    argument = "project.packages={c={dependencies=[]}}"
    assert (
        run(
            (
                "config",
                "explain",
                "packages",
                "--path",
                str(path),
                "--setting",
                argument,
            )
        )
        == 0
    )
    output = capsys.readouterr().out
    assert argument in output
    assert "shadowed" in output
    assert "merged" not in output


def test_metadata_assignment_reaches_resolution_and_replay(
    hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(hermetic_roots)
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", registry.coordinator)
    argument = "project.packages={a={dependencies=[]}}"

    assert registry.command("--setting", argument) == 0
    assert registry.versions() == {"a": "1"}
    document = toml_io.load_path(registry.output)
    assert registry.command(*document["tool"]["nab"]["cli-project-overrides"]) == 0
    assert registry.versions() == {"a": "1"}
    overrides = _run._cli_overrides(
        cli_resolution=None,
        cli_offline=None,
        cli_cache_dir=None,
        cli_settings=(argument,),
    )
    assert registry.command(*_run.config_override_arguments(overrides)) == 0
    assert registry.versions() == {"a": "1"}


def test_user_assignment_selects_reuse_without_transport(
    hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(hermetic_roots)
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", registry.coordinator)
    assert registry.command() == 0
    registry.add_release()
    ports_before = len(registry.ports)

    assert registry.command("--setting", 'user.lock-policy="reuse"') == 0
    assert registry.versions() == {"a": "1", "b": "1"}
    assert len(registry.ports) == ports_before


@pytest.mark.parametrize(
    "spec",
    [row for row in OPTIONS if row.scope is Scope.USER],
    ids=lambda row: row.name,
)
def test_every_user_setting_reaches_the_run_controls(spec: Opt, tmp_path: Path) -> None:
    """The declaration supplies the field name at this schema introspection boundary."""
    if spec.vtype is VType.BOOL:
        raw = not spec.rdefault
    elif spec.kind is Kind.STAR:
        raw = [spec.sample]
    elif spec.choices:
        raw = next(
            choice
            for choice in spec.choices
            if spec.parse(choice, "candidate") != spec.rdefault
        )
    elif spec.vtype is VType.PATH:
        raw = str(tmp_path / "alternate-cache")
    else:
        raw = spec.rdefault + 1
    expected = spec.parse(raw, "test CLI value")
    effective = resolve_config(
        [], Layer(Origin(SourceKind.ENV, "env"), {}), build_cli_layer({spec.name: raw})
    )
    controls = _run._layered_run_settings(effective)
    assert getattr(controls, spec.name.replace("-", "_")) == expected


@pytest.mark.parametrize("source", ["file", "environment"])
@pytest.mark.parametrize("select_all", [False, True])
def test_configured_group_and_extra_selection_changes_the_lock(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    select_all: bool,
) -> None:
    registry = Registry(hermetic_roots)
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", registry.coordinator)
    registry.declarations.update({"c": {"1": ()}, "d": {"1": ()}})
    with registry.project.open("a") as f:
        f.write(
            '\n[project.optional-dependencies]\nextra=["c==1"]\n[dependency-groups]\ndev=["d==1"]\n'
        )
    options = (
        {"all-groups": "true", "all-extras": "true"}
        if select_all
        else {"groups": '["dev"]', "extras": '["extra"]'}
    )
    if source == "file":
        registry.project.with_name("nab.toml").write_text(
            "\n".join(f"{key}={value}" for key, value in options.items())
        )
    else:
        for key, value in options.items():
            monkeypatch.setenv("NAB_" + key.upper().replace("-", "_"), value)

    assert registry.command() == 0
    assert registry.versions() == {"a": "1", "b": "1", "c": "1", "d": "1"}
    document = toml_io.load_path(registry.output)
    assert document["extras"] == ["extra"]
    assert document["dependency-groups"] == ["dev"]


def test_configured_output_format_changes_the_written_artifact(
    hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(hermetic_roots)
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", registry.coordinator)
    registry.output = hermetic_roots / "requirements-{platform_id}.txt"
    registry.project.with_name("nab.toml").write_text(
        'format="requirements-without-hashes"\n'
    )

    assert registry.command() == 0
    outputs = list(hermetic_roots.glob("requirements-*.txt"))
    assert len(outputs) == 2
    for output in outputs:
        requirements = output.read_text()
        assert "a==1" in requirements
        assert "b==1" in requirements
        assert "--hash" not in requirements


def test_replayed_user_flags_preserve_explicit_false_and_whole_selections() -> None:
    overrides = _run._cli_overrides(
        cli_resolution=None,
        cli_offline=None,
        cli_cache_dir=None,
        cli_cache=False,
        cli_all_groups=False,
        cli_groups=("dev", "test"),
        cli_workspace_discovery=True,
        cli_no_emit_workspace=False,
    )
    arguments = _run.config_override_arguments(overrides)
    parsed = parse(("lock", *arguments), cli_spec.ROOT, cli_spec.COMMANDS, "nab")
    parameters = {spec.cli_param: spec.default for spec in OPTIONS if spec.cli_param}
    parameters.update(parsed.values)
    overrides = build_cli_overrides(parameters, {})
    file = Layer(
        Origin(SourceKind.PROJECT_TOML, "nab.toml"),
        {
            "cache": True,
            "all-groups": True,
            "groups": ("other",),
            "workspace-discovery": False,
            "no-emit-workspace": True,
        },
    )
    controls = _run._layered_run_settings(
        resolve_config(
            [file], Layer(Origin(SourceKind.ENV, "env"), {}), build_cli_layer(overrides)
        )
    )
    assert controls.cache is False
    assert controls.all_groups is False
    assert controls.groups == ("dev", "test")
    assert controls.workspace_discovery is True
    assert controls.no_emit_workspace is False


def test_assignment_provenance_and_notice_omit_url_credentials(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = Registry(hermetic_roots)
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", registry.coordinator)
    argument = 'project.indexes=[{name="pypi",url="https://example-user:example-password@current.example/simple/"}]'

    assert registry.command("--setting", argument) == 0
    assert "example-password" not in capsys.readouterr().err
    assert "example-password" not in registry.output.read_text()


def test_an_empty_native_selection_has_no_tokens() -> None:
    assert _run.config_override_arguments({"groups": ()}) == []


def test_invalid_toggle_values_are_not_rendered_as_commands() -> None:
    with pytest.raises(TypeError, match="requires a boolean"):
        _run.config_override_arguments({"cache": "false"})


@pytest.mark.parametrize("value", ['"unknown"', "17"])
def test_invalid_configured_format_stops_before_resolution(
    hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    registry = Registry(hermetic_roots)
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", registry.coordinator)
    registry.project.with_name("nab.toml").write_text(f"format={value}\n")

    assert registry.command() == 1
    assert not registry.ports


def test_invalid_assignment_url_is_not_echoed_into_the_notice(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project(hermetic_roots)
    argument = (
        'project.indexes=[{name="bad",url="https://user:example-password@[broken"}]'
    )
    assert (
        run(("config", "get", "indexes", "--path", str(path), "--setting", argument))
        == 1
    )
    error = capsys.readouterr().err
    assert "invalid URL" in error
    assert "example-password" not in error


def test_dependency_urls_are_recorded_without_passwords(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project(hermetic_roots)
    argument = 'project.packages={a={dependencies=["b @ https://user:example-password@example.com/b.whl"]}}'
    assert (
        run(("config", "get", "packages", "--path", str(path), "--setting", argument))
        == 0
    )
    assert "example-password" not in capsys.readouterr().err


def test_scoped_boolean_metadata_assertion_has_a_cli_form(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project(hermetic_roots)
    argument = "project.packages={a={assume-consistent-metadata=true}}"
    assert (
        run(("config", "get", "packages", "--path", str(path), "--setting", argument))
        == 0
    )
    overrides = _run._cli_overrides(
        cli_resolution=None,
        cli_offline=None,
        cli_cache_dir=None,
        cli_settings=(argument,),
    )
    config = read_pyproject_config(
        path,
        cli_overrides=_run.project_config_overrides(overrides),
        discover_workspace=False,
    )
    assert config.package_overrides[0].assume_consistent_metadata is True


def test_incompatible_environment_assignment_names_its_cli_form(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = Registry(hermetic_roots)
    assert registry.command("--setting", 'project.environment={python="3.12"}') == 1
    assert "--setting" in capsys.readouterr().err


def test_script_metadata_errors_keep_the_configuration_exception(
    tmp_path: Path,
) -> None:
    script = tmp_path / "example.py"
    script.write_text('print("example")\n')
    with pytest.raises(ConfigError):
        read_pyproject_config(script, input_format=InputFormat.SCRIPT)


def test_plain_dependency_override_keeps_its_replay_value() -> None:
    argument = 'project.packages={a={dependencies=["b>=1"]}}'
    overrides = _run._cli_overrides(
        cli_resolution=None,
        cli_offline=None,
        cli_cache_dir=None,
        cli_settings=(argument,),
    )
    assert _run.config_override_arguments(overrides) == ["--setting", argument]
