"""Embedded project reads own override semantics without CLI configuration records."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_project.config import plan_targets, read_pyproject_config
from nab_project.config_overrides import (
    ProjectOverride,
    ProjectOverrideError,
    ProjectOverrides,
    ProjectTableOverride,
)
from nab_project.config_values import SourceConfigError
from nab_project.input_source import InputFormat
from nab_project.inputs import ResolveInputs
from nab_provider.errors import ConfigError
from nab_provider.policy import ResolutionStrategy, ResolveMode
from nab_provider.records import IndexConfig


def project(directory: Path, settings: str = "") -> Path:
    """Write a project for a configuration-only operation."""
    path = directory / "pyproject.toml"
    path.write_text(
        '[project]\nname = "example"\nversion = "1"\n' + settings, encoding="utf-8"
    )
    return path


def test_operation_settings_have_one_owner(tmp_path: Path) -> None:
    config = read_pyproject_config(project(tmp_path))

    assert config.inputs == ResolveInputs()
    assert config.resolve_inputs() is config.inputs


def test_released_consumer_index_replacement_contract(tmp_path: Path) -> None:
    config = read_pyproject_config(
        project(tmp_path, '[tool.nab]\nresolution = "lowest"\n')
    )
    indexes = (IndexConfig("private", "https://example.invalid/simple/"),)

    changed = config.replace(indexes=indexes)

    assert changed.resolve_inputs().indexes == indexes
    assert changed.inputs.resolution is ResolutionStrategy.LOWEST
    assert changed.mode is config.mode
    assert plan_targets(changed) == plan_targets(config)
    assert config.inputs.indexes != indexes


def test_replacement_refuses_ambiguous_input_sources(tmp_path: Path) -> None:
    config = read_pyproject_config(project(tmp_path))

    with pytest.raises(TypeError, match="inputs cannot be combined"):
        config.replace(inputs=ResolveInputs(), resolution=ResolutionStrategy.LOWEST)


def test_whole_mode_override_can_rescue_a_file_matrix(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        '[tool.nab.matrix]\npython = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
    )
    overrides = ProjectOverrides(
        (ProjectOverride("mode", "specific", "selected mode"),)
    )

    config = read_pyproject_config(path, overrides=overrides)

    assert config.mode is ResolveMode.SPECIFIC
    assert config.matrix is None
    assert len(plan_targets(config)) == 1


def test_partial_table_override_preserves_other_file_fields(tmp_path: Path) -> None:
    path = project(
        tmp_path, '[tool.nab.environment]\npython = "3.11"\nplatform = "linux_x86_64"\n'
    )

    def amend(base: object | None) -> object:
        assert isinstance(base, dict)
        return {**base, "python": "3.12"}

    overrides = ProjectOverrides(
        tables=(
            ProjectTableOverride(
                "environment", amend, "selected environment", ("selected Python",)
            ),
        )
    )

    config = read_pyproject_config(path, overrides=overrides)

    assert config.environment is not None
    assert config.environment.platform is not None
    assert config.environment.python == "3.12"
    assert config.environment.platform.platform_id == "linux_x86_64"


def test_file_conflicts_cannot_be_hidden_by_overrides(tmp_path: Path) -> None:
    path = project(tmp_path, '[tool.nab]\nresolution = "highest"\n')
    (tmp_path / "nab.toml").write_text('resolution = "lowest"\n', encoding="utf-8")
    overrides = ProjectOverrides(
        (ProjectOverride("resolution", "highest", "selected resolution"),)
    )

    with pytest.raises(SourceConfigError, match="conflicting values"):
        read_pyproject_config(path, overrides=overrides)


def test_invalid_scalar_override_precedes_file_conflict_check(tmp_path: Path) -> None:
    path = project(tmp_path, '[tool.nab]\nresolution = "highest"\n')
    (tmp_path / "nab.toml").write_text('resolution = "lowest"\n', encoding="utf-8")
    overrides = ProjectOverrides(
        (ProjectOverride("build-policy", "invalid", "selected build policy"),)
    )

    with pytest.raises(SourceConfigError, match="selected build policy must be one of"):
        read_pyproject_config(path, overrides=overrides)


def test_invalid_table_override_is_identified(tmp_path: Path) -> None:
    path = project(tmp_path)
    overrides = ProjectOverrides(
        tables=(
            ProjectTableOverride(
                "environment", lambda base: {"python": False}, "selected environment"
            ),
        )
    )

    with pytest.raises(ProjectOverrideError) as error:
        read_pyproject_config(path, overrides=overrides)

    assert error.value.key == "environment"


@pytest.mark.parametrize("table", [False, True])
def test_unknown_project_override_is_refused(tmp_path: Path, table: bool) -> None:
    path = project(tmp_path)
    overrides = (
        ProjectOverrides(
            tables=(ProjectTableOverride("unknown", lambda base: {}, "unknown"),)
        )
        if table
        else ProjectOverrides((ProjectOverride("unknown", "value", "unknown"),))
    )

    with pytest.raises(SourceConfigError, match="unknown project override"):
        read_pyproject_config(path, overrides=overrides)


def test_host_extensions_are_delegated_in_file_order(tmp_path: Path) -> None:
    path = project(tmp_path)
    (tmp_path / "nab.toml").write_text(
        'offline = false\nresolution = "lowest"\ncache-dir = "cache"\n',
        encoding="utf-8",
    )
    seen = []

    def validate(key: str, value: object, label: str) -> None:
        seen.append((key, value, label))

    config = read_pyproject_config(path, validate_host=validate)

    assert [(key, value) for key, value, _ in seen] == [
        ("offline", False),
        ("cache-dir", "cache"),
    ]
    assert config.inputs.resolution is ResolutionStrategy.LOWEST


def test_host_extension_error_precedes_override_parsing(tmp_path: Path) -> None:
    path = project(tmp_path)
    (tmp_path / "nab.toml").write_text("cache-dir = false\n", encoding="utf-8")

    def validate(key: str, value: object, label: str) -> None:
        message = "host extension invalid"
        raise SourceConfigError(message)

    overrides = ProjectOverrides(
        (ProjectOverride("build-policy", "invalid", "invalid override"),)
    )
    with pytest.raises(SourceConfigError, match="host extension invalid"):
        read_pyproject_config(path, overrides=overrides, validate_host=validate)


def script(directory: Path, settings: str, *, python: str | None = ">=3.10") -> Path:
    path = directory / "script.py"
    metadata = "dependencies = []\n"
    if python is not None:
        metadata += f'requires-python = "{python}"\n'
    metadata += settings
    path.write_text(
        "# /// script\n"
        + "".join(f"# {line}\n" for line in metadata.splitlines())
        + "# ///\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "setting",
    [
        'default-groups = ["dev"]',
        'base-group = "runtime"',
        'build-group = "build"',
        'workspace = {members = ["member"]}',
    ],
)
def test_script_refuses_project_only_settings(tmp_path: Path, setting: str) -> None:
    with pytest.raises(ConfigError, match="script inputs have no project groups"):
        read_pyproject_config(
            script(tmp_path, "[tool.nab]\n" + setting), input_format=InputFormat.SCRIPT
        )


@pytest.mark.parametrize("python", [None, ">=3.10"])
@pytest.mark.parametrize("override", [False, True])
def test_script_python_bounds_keep_their_origins(
    tmp_path: Path, python: str | None, override: bool
) -> None:
    path = script(tmp_path, '[tool.nab]\nrequires-python = ">=3.11"\n', python=python)
    overrides = (
        ProjectOverrides((ProjectOverride("requires-python", "<3.14", "host Python"),))
        if override
        else None
    )
    config = read_pyproject_config(
        path, input_format=InputFormat.SCRIPT, overrides=overrides
    )
    if python is None:
        assert config.requires_python_declarations == ()
    else:
        assert (
            python,
            f"{path}: PEP 723 requires-python",
        ) in config.requires_python_declarations
        assert config.requires_python_declarations[-1][1] == (
            "host Python" if override else f"{path}: tool.nab.requires-python"
        )


def test_script_without_tool_bounds_retains_script_declaration(tmp_path: Path) -> None:
    path = script(tmp_path, "")
    config = read_pyproject_config(path, input_format=InputFormat.SCRIPT)
    assert config.requires_python_declarations == (
        (">=3.10", f"{path}: PEP 723 requires-python"),
    )


def test_malformed_script_is_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "script.py"
    path.write_text("# /// script\n# dependencies=[\n# ///\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="PEP 723"):
        read_pyproject_config(path, input_format=InputFormat.SCRIPT)


@pytest.mark.parametrize("standalone", [False, True])
def test_script_matrix_errors_name_the_declaring_format(
    tmp_path: Path, standalone: bool
) -> None:
    settings = '[tool.nab]\nmode = "universal"\n'
    if standalone:
        (tmp_path / "nab.toml").write_text('mode = "universal"\n', encoding="utf-8")
        settings = ""
    with pytest.raises(
        ConfigError, match="nab.toml" if standalone else "script's metadata"
    ):
        read_pyproject_config(
            script(tmp_path, settings), input_format=InputFormat.SCRIPT
        )


def test_script_specific_matrix_error_names_script_metadata(tmp_path: Path) -> None:
    path = script(
        tmp_path,
        '[tool.nab]\nmode = "specific"\n[tool.nab.matrix]\npython = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
    )
    with pytest.raises(ConfigError, match="script's metadata"):
        read_pyproject_config(path, input_format=InputFormat.SCRIPT)


@pytest.mark.parametrize(
    "declarations", [("selected matrix",), ("selected Python", "selected platforms")]
)
def test_table_conflicts_retain_host_declaration_labels(
    tmp_path: Path, declarations: tuple[str, ...]
) -> None:
    overrides = ProjectOverrides(
        tables=(
            ProjectTableOverride(
                "matrix",
                lambda base: {"python": "==3.12", "platforms": ["linux_x86_64"]},
                "matrix",
                declarations,
            ),
            ProjectTableOverride(
                "environment",
                lambda base: {"python": "3.12"},
                "environment",
                ("selected environment",),
            ),
        )
    )
    with pytest.raises(ConfigError) as error:
        read_pyproject_config(project(tmp_path), overrides=overrides)
    for label in (*declarations, "selected environment"):
        assert label in str(error.value)


@pytest.mark.parametrize("standalone", [False, True])
def test_missing_environment_platform_keeps_override_and_file_origins(
    tmp_path: Path, standalone: bool
) -> None:
    settings = '[tool.nab.environment]\nimplementation = "pypy"\n'
    if standalone:
        (tmp_path / "nab.toml").write_text(
            '[environment]\nimplementation = "pypy"\n', encoding="utf-8"
        )
    overrides = (
        None
        if standalone
        else ProjectOverrides(
            tables=(
                ProjectTableOverride(
                    "environment",
                    lambda base: {"implementation": "pypy"},
                    "selected environment",
                ),
            )
        )
    )
    with pytest.raises(ConfigError, match="implementation needs a platform") as error:
        read_pyproject_config(
            project(tmp_path, settings if standalone else ""), overrides=overrides
        )
    if standalone:
        assert "Set the same platform" in str(error.value)


def test_replacing_planning_settings_keeps_the_input_object(tmp_path: Path) -> None:
    config = read_pyproject_config(project(tmp_path))
    changed = config.replace(requires_python_source="host constraint")
    assert changed.inputs is config.inputs
    assert changed.requires_python_source == "host constraint"


def test_table_override_refuses_a_scalar_file_setting(tmp_path: Path) -> None:
    path = project(tmp_path, '[tool.nab]\nresolution = "lowest"\n')
    overrides = ProjectOverrides(
        tables=(
            ProjectTableOverride(
                "resolution", lambda base: "highest", "selected resolution"
            ),
        )
    )
    with pytest.raises(ProjectOverrideError, match="is not a table"):
        read_pyproject_config(path, overrides=overrides)


@pytest.mark.parametrize("tool_python", [">=3.10", "<3.12"])
def test_script_target_exclusion_names_the_offending_declaration(
    tmp_path: Path, tool_python: str
) -> None:
    path = script(
        tmp_path,
        f'[tool.nab]\nrequires-python = "{tool_python}"\n[tool.nab.environment]\npython = "3.11"\nplatform = "linux_x86_64"\n',
        python=">=3.12",
    )
    config = read_pyproject_config(path, input_format=InputFormat.SCRIPT)
    with pytest.raises(ConfigError, match="PEP 723 requires-python"):
        plan_targets(config)


def test_script_tool_target_exclusion_names_the_tool_declaration(
    tmp_path: Path,
) -> None:
    path = script(
        tmp_path,
        '[tool.nab]\nrequires-python = ">=3.12"\n[tool.nab.environment]\npython = "3.11"\nplatform = "linux_x86_64"\n',
    )
    config = read_pyproject_config(path, input_format=InputFormat.SCRIPT)
    with pytest.raises(ConfigError, match="tool.nab.requires-python"):
        plan_targets(config)


def test_python_axis_override_diagnostic_names_the_override(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        '[tool.nab]\nrequires-python = ">=3.12"\n[tool.nab.matrix]\npython = "==3.12"\nplatforms = ["linux_x86_64"]\n[tool.nab.matrix.overrides.implementations.cpython]\npython = "==3.11"\n',
    )
    config = read_pyproject_config(path)
    with pytest.raises(
        ConfigError, match="matrix.overrides.implementations.cpython.python"
    ):
        plan_targets(config)


def test_whole_and_partial_overrides_cannot_silently_replace_each_other(
    tmp_path: Path,
) -> None:
    overrides = ProjectOverrides(
        (ProjectOverride("environment", {"python": "3.11"}, "whole environment"),),
        (
            ProjectTableOverride(
                "environment", lambda base: {"python": "3.12"}, "partial environment"
            ),
        ),
    )
    with pytest.raises(SourceConfigError, match="replace and amend the same keys"):
        read_pyproject_config(project(tmp_path), overrides=overrides)
