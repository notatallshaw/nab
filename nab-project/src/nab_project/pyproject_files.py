"""Read a project's declarations off disk.

These readers parse TOML, so they sit here rather than beside the pure
requirement algebra in :mod:`nab_provider.requirements_file`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    InvalidProjectTableError,
    parse_requirements,
    require_string_list,
)

from . import toml_io

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from nab_provider._vendor.packaging.requirements import Requirement

__all__ = [
    "read_pyproject_build_requires",
    "read_pyproject_dependencies",
    "read_pyproject_groups",
    "read_pyproject_name",
    "read_pyproject_optional_dependencies",
]


def _load_project_table(path: Path) -> Mapping[str, object]:
    """Load a pyproject.toml and return its ``[project]`` table.

    An absent ``[project]`` reads as an empty mapping (a workspace-root
    pyproject without its own distribution).  One that is not a table raises
    :class:`InvalidProjectTableError`.
    """
    with path.open("rb") as f:
        data = toml_io.load(f)
    project = data.get("project", {})
    if not isinstance(project, dict):
        msg = f"[project] must be a table, got {type(project).__name__}"
        raise InvalidProjectTableError(msg)
    return project


def read_pyproject_dependencies(path: Path) -> list[Requirement]:
    """Read [project].dependencies from a pyproject.toml file.

    PEP 621 makes the key optional, so an absent ``dependencies`` reads as an
    empty list.  A missing ``[project]`` raises :class:`KeyError` and one that
    is not a table :class:`InvalidProjectTableError`.  A malformed dependency
    string raises :class:`InvalidProjectRequirementError`, as does a dynamic
    ``dependencies``.
    """
    with path.open("rb") as f:
        data = toml_io.load(f)
    project = data["project"]
    if not isinstance(project, dict):
        msg = f"[project] must be a table, got {type(project).__name__}"
        raise InvalidProjectTableError(msg)

    source = "[project].dependencies"
    if "dependencies" not in project:
        dynamic = project.get("dynamic")
        if isinstance(dynamic, list) and "dependencies" in dynamic:
            msg = (
                "[project].dependencies is declared dynamic; computing it"
                " requires the build backend, which the root-project lock"
                " path does not support"
            )
            raise InvalidProjectRequirementError(msg)
    dep_strings = require_string_list(project.get("dependencies", []), source)
    return parse_requirements(dep_strings, source)


def read_pyproject_build_requires(path: Path) -> list[Requirement]:
    """Read [build-system].requires from a pyproject.toml file (PEP 518).

    There is no fallback to the PEP 517 default backend: pinning an implied
    ``setuptools`` would lock a build requirement the project never asked for.
    An absent ``[build-system]`` and a table without the mandatory ``requires``
    key both raise :class:`InvalidProjectRequirementError`; a
    ``[build-system]`` that is not a table raises
    :class:`InvalidProjectTableError`.

    Only the static list is read; what ``get_requires_for_build_wheel`` adds is
    known only once the backend runs.
    """
    with path.open("rb") as f:
        data = toml_io.load(f)

    if "build-system" not in data:
        msg = (
            f"{path} declares no [build-system], so it has no build"
            " requirements to lock"
        )
        raise InvalidProjectRequirementError(msg)

    table = data["build-system"]
    if not isinstance(table, dict):
        msg = f"[build-system] must be a table, got {type(table).__name__}"
        raise InvalidProjectTableError(msg)

    source = "[build-system].requires"
    if "requires" not in table:
        msg = f"{source} is required by PEP 518 and {path} does not declare it"
        raise InvalidProjectRequirementError(msg)

    return parse_requirements(require_string_list(table["requires"], source), source)


def read_pyproject_name(path: Path) -> str | None:
    """Read [project].name from a pyproject.toml file.

    ``None`` when the file has no ``[project]`` table or no ``name`` key (a
    workspace-root pyproject without its own distribution).
    """
    name = _load_project_table(path).get("name")
    return name if isinstance(name, str) else None


def read_pyproject_optional_dependencies(
    path: Path,
) -> Mapping[str, Sequence[str]]:
    """Read [project.optional-dependencies] from a pyproject.toml file.

    The requirement strings come back unparsed; an absent table reads as an
    empty dict.
    """
    raw = _load_project_table(path).get("optional-dependencies", {})
    if not isinstance(raw, dict):
        msg = (
            f"[project.optional-dependencies] must be a table, got {type(raw).__name__}"
        )
        raise InvalidProjectTableError(msg)
    return raw


def read_pyproject_groups(
    path: Path,
) -> Mapping[str, Sequence[str | Mapping[str, str]]]:
    """Read [dependency-groups] from a pyproject.toml file (PEP 735).

    The group table comes back unparsed, so an entry is either a requirement
    string or an include record (``{"include-group": "other-group"}``).  An
    absent table reads as an empty dict.
    """
    with path.open("rb") as f:
        data = toml_io.load(f)
    raw = data.get("dependency-groups", {})
    if not isinstance(raw, dict):
        msg = f"[dependency-groups] must be a table, got {type(raw).__name__}"
        raise InvalidProjectTableError(msg)
    return raw
