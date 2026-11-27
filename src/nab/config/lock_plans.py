"""Read named lock declarations and determine their output paths."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from nab_project.dependency_inputs import input_table
from nab_provider.requirements_file import require_string_list

from .ladder import Layer, SourceKind, plan_settings_layer
from .values import parse_input_tables, parse_lock_format

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .ladder import EffectiveValue


class LockPlan(NamedTuple):
    """Selections fixed by one named dependency lock."""

    name: str
    groups: tuple[str, ...]
    extras: tuple[str, ...]
    dependencies: bool
    build_requirements: bool
    imports: Mapping[str, Mapping[str, object]]
    settings: dict[str, object]
    output: str | None
    format: str | None

    @classmethod
    def read(cls, name: str, plans: Mapping[str, object]) -> LockPlan:
        """Validate the selected declaration before accessing its inputs."""
        if name not in plans:
            msg = f"unknown lock plan {name!r}; declared: {sorted(plans)}"
            raise ValueError(msg)
        where = f"lock plan {name!r}"
        table = input_table(plans[name], where)
        unknown = table.keys() - {
            "groups",
            "extras",
            "dependencies",
            "build-requirements",
            "inputs",
            "settings",
            "output",
            "format",
        }
        if unknown:
            msg = f"{where}: unknown keys {sorted(unknown)}"
            raise ValueError(msg)

        build = table.get("build-requirements", False)
        dependencies = table.get("dependencies", not build)
        if not isinstance(build, bool) or not isinstance(dependencies, bool):
            msg = f"{where}: build-requirements and dependencies must be booleans"
            raise TypeError(msg)

        format_name = (
            parse_lock_format(table["format"], f"{where}: format")
            if "format" in table
            else None
        )

        output = table.get("output")
        if output is not None and (
            not isinstance(output, str) or not output or output == "-"
        ):
            msg = f"{where}: output must be a file path"
            raise ValueError(msg)

        return cls(
            name,
            tuple(require_string_list(table.get("groups", []), f"{where} groups")),
            tuple(require_string_list(table.get("extras", []), f"{where} extras")),
            dependencies,
            build,
            parse_input_tables(table.get("inputs", {}), f"{where} inputs"),
            input_table(table.get("settings", {}), f"{where} settings"),
            output,
            format_name,
        )

    def output_name(self, format_setting: EffectiveValue) -> str:
        """Choose the file for the effective format after overrides."""
        if self.output is not None:
            return self.output

        # Batch preflight runs before the plan enters the configuration ladder.
        format_name = format_setting.value
        if self.format is not None and format_setting.origin.kind not in (
            SourceKind.CLI,
            SourceKind.ENV,
        ):
            format_name = self.format

        if format_name == "pylock":
            return f"pylock.{self.name}.toml"

        if format_name == "pdm":
            return f"{self.name}.pdm.lock"

        return f"{self.name}.requirements.txt"

    def settings_layer(self, path: Path, *, check_conflicts: bool = False) -> Layer:
        """Apply the plan's explicit roots and target policy to the project."""
        layer = plan_settings_layer(self.name, self.settings, path.parent)
        values = {
            **layer.values,
            "default-groups": (),
            "base-group": None,
            "build-group": None,
        }
        if self.format is not None:
            values["format"] = self.format

        if not check_conflicts:
            # The project planner has already checked the selected groups and extras.
            values["conflicts"] = ()

        return Layer(layer.origin, values, layer.raw)


def read_lock_plans(
    path: Path,
    declarations: Mapping[str, object],
    sources: Mapping[str, object],
    format_setting: EffectiveValue,
    *,
    selected: str | None,
) -> tuple[LockPlan, ...]:
    """Read one named plan or check all batch declarations before the first write."""
    names = declarations if selected is None else (selected,)
    plans = []
    destinations: dict[Path, str] = {}

    for name in names:
        plan = LockPlan.read(name, declarations)
        for source in plan.imports:
            if source not in sources:
                msg = f"unknown input source {source!r} in plan {name!r}"
                raise ValueError(msg)

        if selected is None:
            # A later plan's settings can be checked without reading its source files.
            plan_settings_layer(name, plan.settings, path.parent)

            # Compare resolved paths so aliases cannot overwrite an earlier plan.
            destination = (path.parent / plan.output_name(format_setting)).resolve()
            if (previous := destinations.get(destination)) is not None:
                msg = f"lock plans {previous!r} and {name!r} both write {destination}"
                raise ValueError(msg)
            destinations[destination] = name

        plans.append(plan)

    return tuple(plans)
