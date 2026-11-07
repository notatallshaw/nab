"""Read project settings and retain the sources used by configuration validation."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any

import tomli

from nab_provider.errors import ConfigError
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion

from . import config_schema, toml_io
from .config_overrides import (
    ProjectOverrideError,
    ProjectOverrides,
    ProjectTableOverride,
)
from .config_schema import project_file_conflict
from .config_values import ProjectFileConflictError, SourceConfigError, matrix_mode
from .input_source import InputFormat
from .paths import PathState, path_state
from .value import ValueType

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime
    from pathlib import Path


class SourceKind(enum.Enum):
    """The project source or host override that supplied a setting."""

    DEFAULT = "default"
    PYPROJECT = "pyproject"
    SCRIPT = "script"
    PROJECT_TOML = "project"
    OVERRIDE = "override"


_PRECEDENCE = {
    SourceKind.DEFAULT: 0,
    SourceKind.PYPROJECT: 1,
    SourceKind.SCRIPT: 1,
    SourceKind.PROJECT_TOML: 1,
    SourceKind.OVERRIDE: 2,
}


class Origin(ValueType):
    """A configuration source and the label used in its diagnostics."""

    __slots__ = __match_args__ = ("kind", "label")

    kind: SourceKind
    label: str

    def __init__(self, kind: SourceKind, label: str) -> None:
        """Record the source and its diagnostic label."""
        self.kind = kind
        self.label = label

    def outranks(self, other: Origin) -> bool:
        """Return whether this source takes precedence over the other source."""
        return _PRECEDENCE[self.kind] > _PRECEDENCE[other.kind]


class ConfigValue(ValueType):
    """A parsed setting with provenance supplied by a file reader or host."""

    __slots__ = __match_args__ = (
        "name",
        "value",
        "origin",
        "stack",
        "declarations",
        "display_name",
    )

    name: str
    value: Any
    origin: Origin
    stack: tuple[tuple[Origin, Any], ...]
    declarations: tuple[str, ...]
    display_name: str

    def __init__(
        self,
        name: str,
        value: Any,
        origin: Origin,
        stack: tuple[tuple[Origin, Any], ...],
        *,
        declarations: tuple[str, ...] = (),
        display_name: str | None = None,
    ) -> None:
        """Retain one parsed value and its declaration history."""
        self.name = name
        self.value = value
        self.origin = origin
        self.stack = stack
        self.declarations = declarations
        self.display_name = name if display_name is None else display_name


# These names are reserved by the standalone file format for host configuration.
# Embedded readers leave their values to the host that owns those settings.
HOST_KEYS = frozenset(key.name for key in config_schema.HOST_SETTINGS)
BY_KEY = {setting.name: setting for setting in config_schema.SETTINGS}


def project_key_path(key: str, kind: SourceKind) -> str:
    """Return the setting's path in its declaring project file."""
    return key if kind is SourceKind.PROJECT_TOML else f"tool.nab.{key}"


def tool_nab_section(document: Mapping[str, Any]) -> Any:
    """Return the project settings table when the document declares one."""
    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        empty: dict[str, object] = {}
        return empty
    return tool.get("nab", {})


def validate_project_keys(
    document: Mapping[str, Any], *, kind: SourceKind = SourceKind.PYPROJECT
) -> None:
    """Reject unknown project settings and host settings in a project table."""
    raw = tool_nab_section(document)
    if not isinstance(raw, dict):
        msg = f"[tool.nab] must be a table, got {type(raw).__name__}"
        raise ConfigError(msg)
    for key in raw:
        if key in HOST_KEYS:
            where = (
                "script [tool.nab]"
                if kind is SourceKind.SCRIPT
                else "pyproject [tool.nab]"
            )
            msg = (
                f"{key!r} is a user-scope option and cannot be set in {where} "
                "(project-scope only)"
            )
            raise SourceConfigError(msg)
    unknown = sorted(set(raw) - BY_KEY.keys())
    if unknown:
        msg = (
            f"unknown [tool.nab] keys: {unknown!r}; expected one of {sorted(BY_KEY)!r}"
        )
        raise ConfigError(msg)


def parse_project_table(
    raw: Mapping[str, Any],
    path: Path,
    kind: SourceKind,
    validate_host: Callable[[str, object, str], None] | None = None,
    anchor: datetime | None = None,
) -> dict[str, Any]:
    """Parse one project's settings with paths relative to its declaring file."""
    values = {}
    for key, raw_value in raw.items():
        if kind is SourceKind.PROJECT_TOML and key in HOST_KEYS:
            if validate_host is not None:
                validate_host(key, raw_value, f"{path}: {key}")
            continue
        setting = BY_KEY.get(key)
        if setting is None:
            msg = (
                f"{path}: {key!r} is not a valid nab setting; the known keys "
                f"are {sorted((*BY_KEY, *HOST_KEYS))!r}."
            )
            raise SourceConfigError(msg)
        values[key] = setting.parse(
            raw_value,
            f"{path}: {key}",
            context=config_schema.ProjectValueContext(
                path.parent,
                prefix=project_key_path("", kind),
                anchor=anchor,
                comparison_anchor=anchor,
            ),
        )
    return values


def _read_standalone(path: Path) -> dict[str, Any] | None:
    """Read an adjacent configuration file, returning None when it is absent."""
    state = path_state(path)
    if state is PathState.ABSENT:
        return None
    if not state.should_read:
        msg = f"{path} exists but is not a regular file"
        raise SourceConfigError(msg)
    try:
        with path.open("rb") as stream:
            raw = toml_io.load(stream)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise SourceConfigError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise SourceConfigError(msg) from exc
    return raw


def _prepare_overrides(
    overrides: ProjectOverrides | None,
    path: Path,
    anchor: datetime | None,
) -> tuple[dict[str, ConfigValue], dict[str, ProjectTableOverride]]:
    """Parse replacements before checking cross-source conflicts and table updates."""
    whole_overrides: dict[str, ConfigValue] = {}
    if overrides is not None:
        for key in (
            *(item.key for item in overrides.values),
            *(item.key for item in overrides.tables),
        ):
            if key not in BY_KEY:
                message = (
                    f"unknown project override {key!r}; "
                    f"expected one of {sorted(BY_KEY)!r}"
                )
                raise SourceConfigError(message)
    table_overrides: dict[str, ProjectTableOverride] = (
        {} if overrides is None else {item.key: item for item in overrides.tables}
    )
    if overrides is not None:
        overlap = table_overrides.keys() & {item.key for item in overrides.values}
        if overlap:
            message = (
                "project overrides replace and amend the same keys: "
                f"{sorted(overlap)!r}"
            )
            raise SourceConfigError(message)
        for item in overrides.values:
            setting = BY_KEY[item.key]
            value = setting.parse(
                item.value,
                item.label,
                context=config_schema.ProjectValueContext(
                    path.parent, anchor=anchor, comparison_anchor=anchor
                ),
            )
            origin = Origin(SourceKind.OVERRIDE, item.label)
            whole_overrides[item.key] = ConfigValue(
                item.key, value, origin, (), display_name=item.label
            )
    return whole_overrides, table_overrides


def read_project_values(
    path: Path,
    document: Mapping[str, Any],
    *,
    input_format: InputFormat,
    overrides: ProjectOverrides | None = None,
    validate_host: Callable[[str, object, str], None] | None = None,
    anchor: datetime | None = None,
) -> dict[str, ConfigValue]:
    """Read the input and adjacent nab.toml without process or user settings."""
    kind = (
        SourceKind.SCRIPT
        if input_format is InputFormat.SCRIPT
        else SourceKind.PYPROJECT
    )
    validate_project_keys(document, kind=kind)
    project_raw = tool_nab_section(document)
    layers = [
        (
            Origin(kind, str(path)),
            parse_project_table(project_raw, path, kind, anchor=anchor),
        )
    ]
    standalone = path.parent / "nab.toml"
    raw = _read_standalone(standalone)
    if raw is not None:
        layers.append(
            (
                Origin(SourceKind.PROJECT_TOML, str(standalone)),
                parse_project_table(
                    raw, standalone, SourceKind.PROJECT_TOML, validate_host, anchor
                ),
            )
        )
    whole_overrides, table_overrides = _prepare_overrides(overrides, path, anchor)
    effective = {}
    for setting in config_schema.SETTINGS:
        stack = tuple(
            (origin, values[setting.name])
            for origin, values in layers
            if setting.name in values
        )
        conflict = project_file_conflict(
            (origin.kind.value, value) for origin, value in stack
        )
        if conflict is not None:
            input_kind, input_value, project_value = conflict
            raise ProjectFileConflictError(
                DiagnosticMessage(
                    f"config {setting.name!r} is set to conflicting values in "
                    f"{stack[0][0].label} and project-dir nab.toml. Both files sit "
                    "at the same precedence level",
                    suggestions=(
                        Suggestion(
                            "set the key in only one, or set them to the same value."
                        ),
                    ),
                ),
                key=setting.name,
                input_kind=input_kind,
                input_value=input_value,
                project_value=project_value,
            )
        override = whole_overrides.get(setting.name)
        table_override = table_overrides.get(setting.name)
        if override is not None:
            stack = (*stack, (override.origin, override.value))
        if table_override is not None:
            base = (
                raw.get(setting.name)
                if raw is not None and setting.name in raw
                else project_raw.get(setting.name)
            )
            if base is not None and not isinstance(base, dict):
                message = f"{table_override.label}: {setting.name!r} is not a table"
                raise ProjectOverrideError(setting.name, message)
            merged = table_override.apply(base)
            try:
                value = setting.parse(
                    merged,
                    table_override.label,
                    context=config_schema.ProjectValueContext(
                        path.parent, anchor=anchor, comparison_anchor=anchor
                    ),
                )
            except SourceConfigError as exc:
                raise ProjectOverrideError(setting.name, exc.args[0]) from exc
            origin = Origin(SourceKind.OVERRIDE, table_override.label)
            stack = (*stack, (origin, value))
        if stack:
            origin, value = stack[-1]
        else:
            origin, value = (
                Origin(SourceKind.DEFAULT, "builtin-default"),
                setting.default,
            )
            stack = ((origin, value),)
        effective[setting.name] = ConfigValue(
            setting.name,
            value,
            origin,
            stack,
            declarations=() if table_override is None else table_override.declarations,
            display_name=None if override is None else override.display_name,
        )
    infer_mode(effective)
    return effective


def infer_mode(effective: dict[str, ConfigValue]) -> None:
    """Infer an unset resolve mode from the selected target matrix."""
    mode = effective["mode"]
    matrix = effective["matrix"].value
    if mode.origin.kind is not SourceKind.DEFAULT or matrix is None:
        return
    value = matrix_mode(matrix)
    origin = Origin(SourceKind.DEFAULT, "inferred from matrix")
    effective["mode"] = ConfigValue("mode", value, origin, ((origin, value),))
