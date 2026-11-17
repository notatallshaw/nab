"""Read named lock declarations and determine their output paths."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from nab_project.dependency_inputs import input_table
from nab_provider.requirements_file import require_string_list

from .ladder import Layer, plan_settings_layer

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class LockPlan(NamedTuple):
    """Selections fixed by one named dependency lock."""

    name: str
    groups: tuple[str, ...]
    extras: tuple[str, ...]
    dependencies: bool
    build_requirements: bool
    settings: dict[str, Any]
    output: str

    @classmethod
    def read(cls, name: str, plans: Mapping[str, Any], format_name: str) -> LockPlan:
        """Validate the selected declaration before accessing its inputs."""
        if name not in plans:
            msg = f"unknown lock plan {name!r}; declared: {sorted(plans)}"
            raise ValueError(msg)
        table = input_table(plans[name], f"lock plan {name!r}")
        unknown = table.keys() - {
            "groups",
            "extras",
            "dependencies",
            "build-requirements",
            "settings",
            "output",
        }
        if unknown:
            msg = f"unknown lock plan keys: {sorted(unknown)}"
            raise ValueError(msg)

        build = table.get("build-requirements", False)
        dependencies = table.get("dependencies", not build)
        if not isinstance(build, bool) or not isinstance(dependencies, bool):
            msg = "build-requirements and dependencies must be booleans"
            raise TypeError(msg)
        default = (
            f"pylock.{name}.toml"
            if format_name == "pylock"
            else f"{name}.requirements.txt"
        )
        output = table.get("output", default)
        if not isinstance(output, str) or not output or output == "-":
            msg = "a lock plan needs a file output"
            raise ValueError(msg)
        return cls(
            name,
            tuple(require_string_list(table.get("groups", []), "plan groups")),
            tuple(require_string_list(table.get("extras", []), "plan extras")),
            dependencies,
            build,
            input_table(table.get("settings", {}), "plan settings"),
            output,
        )

    def settings_layer(self, path: Path, *, check_conflicts: bool = False) -> Layer:
        """Apply the plan's explicit roots and target policy to the project."""
        layer = plan_settings_layer(self.name, self.settings, path.parent)
        values = {
            **layer.values,
            "default-groups": (),
            "base-group": None,
            "build-group": None,
        }
        if not check_conflicts:
            # The project planner has already checked the selected groups and extras.
            values["conflicts"] = ()
        return Layer(layer.origin, values, layer.raw)


def validate_plan_outputs(
    path: Path, plans: Mapping[str, Any], format_name: str
) -> None:
    """Refuse colliding batch destinations before writing the first file."""
    seen = set()
    for name in plans:
        plan = LockPlan.read(name, plans, format_name)
        destination = (path.parent / plan.output).resolve()
        if destination in seen:
            msg = f"multiple lock plans write {destination}"
            raise ValueError(msg)
        seen.add(destination)
