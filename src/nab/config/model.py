"""Apply command-line overrides to the shared project configuration reader."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from nab_project.config import (
    ConfigConflictError,
    ConfigConflictKind,
    ConfigError,
    EnvironmentConfig,
    NabProjectConfig,
    ResolveMode,
    plan_targets,
)
from nab_project.config import read_pyproject_config as read_project_config
from nab_project.config_overrides import (
    ProjectOverride,
    ProjectOverrideError,
    ProjectOverrides,
    ProjectTableOverride,
)
from nab_project.config_values import ProjectFileConflictError
from nab_project.input_source import InputFormat
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion

from .ladder import BY_KEY, project_file_conflict_message
from .subflags import CliTable, cli_table_label, fold_cli_table
from .values import CliTableError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from .ladder import Opt


__all__ = [
    "ConfigError",
    "EnvironmentConfig",
    "NabProjectConfig",
    "ResolveMode",
    "plan_targets",
    "read_pyproject_config",
]


def read_pyproject_config(
    path: Path,
    *,
    discover_workspace: bool = True,
    anchor: datetime | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    input_format: InputFormat = InputFormat.PROJECT,
) -> NabProjectConfig:
    """Read project settings with command-line overrides and CLI diagnostics."""
    values = []
    tables = []
    for key, value in (cli_overrides or {}).items():
        spec = BY_KEY[key]
        if isinstance(value, CliTable):
            tables.append(
                ProjectTableOverride(
                    key,
                    partial(_fold_table, spec, value),
                    cli_table_label(spec),
                    tuple(item.flag for item in value.keys),
                )
            )
        else:
            if not spec.commands:
                message = f"{key} takes no command line, so no CLI layer can set it"
                raise ValueError(message)
            raw = list(value) if isinstance(value, tuple) else value
            values.append(ProjectOverride(key, raw, str(spec.cli_flag)))
    overrides = ProjectOverrides(tuple(values), tuple(tables))
    try:
        return read_project_config(
            path,
            discover_workspace=discover_workspace,
            anchor=anchor,
            input_format=input_format,
            overrides=overrides,
            validate_host=_validate_host_setting,
        )
    except ProjectFileConflictError as exc:
        raise ConfigError(
            project_file_conflict_message(
                BY_KEY[exc.key], exc.input_kind, exc.input_value, exc.project_value
            )
        ) from exc
    except ProjectOverrideError as exc:
        raise CliTableError(*exc.args) from exc
    except ConfigConflictError as exc:
        if exc.kind is ConfigConflictKind.MISSING_MATRIX and any(
            item.key == "mode" for item in values
        ):
            raise ConfigError(
                _missing_matrix_message(input_format=input_format)
            ) from exc
        if exc.kind is ConfigConflictKind.SPECIFIC_MATRIX:
            matrix = next(
                (item for item in tables if item.key == "matrix" and item.declarations),
                None,
            )
            if matrix is not None:
                raise ConfigError(_specific_mode_message(matrix.declarations)) from exc
        raise


def _fold_table(spec: Opt, table: CliTable, base: Mapping[str, Any] | None) -> object:
    """Apply CLI table-key selection to the project's selected raw table."""
    return fold_cli_table(spec, table, base)


def _validate_host_setting(key: str, value: object, where: str) -> None:
    """Validate a standalone host setting through its CLI-owned parser."""
    BY_KEY[key].parse(value, where)


def _declares(flags: tuple[str, ...]) -> str:
    """Agree the verb with the subject: several flags declare, one name declares."""
    return "declare" if len(flags) > 1 else "declares"


def _missing_matrix_message(*, input_format: InputFormat) -> DiagnosticMessage:
    """Explain the missing matrix using the command-line override names."""
    input_location = (
        "script's metadata"
        if input_format is InputFormat.SCRIPT
        else "project's pyproject.toml"
    )
    return DiagnosticMessage(
        "--project-mode universal needs a matrix",
        suggestions=(
            Suggestion(
                "pass --project-matrix-python and "
                "--project-matrix-platforms, add [tool.nab.matrix] to the "
                f"{input_location}, or a top-level [matrix] table "
                "to the project's nab.toml, or drop --project-mode "
                "universal."
            ),
        ),
    )


def _specific_mode_message(flags: tuple[str, ...]) -> DiagnosticMessage:
    """Describe a matrix override incompatible with specific mode."""
    named = " and ".join(flags)
    return DiagnosticMessage(
        (
            f"{named} {_declares(flags)} a matrix with multiple targets but mode "
            "is 'specific'"
        ),
        suggestions=(
            Suggestion(
                "pass --project-mode universal as well, or drop the matrix flags."
            ),
        ),
    )
