"""Read a project's declarations off disk.

The readers that take a path.  Every one of them parses TOML, which is why
they sit here rather than beside the pure requirement algebra in
:mod:`nab_provider.requirements_file`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tomli

from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    InvalidProjectTableError,
    parse_requirements,
    require_string_list,
)

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

    Returns an empty mapping when ``[project]`` is absent (a
    workspace-root pyproject without its own distribution).  Raises
    :class:`TypeError` when ``[project]`` is present but not a table, so
    the readers below fail with a named diagnostic instead of a raw
    subscript or attribute error.
    """
    with path.open("rb") as f:
        data = tomli.load(f)
    project = data.get("project", {})
    if not isinstance(project, dict):
        msg = f"[project] must be a table, got {type(project).__name__}"
        raise InvalidProjectTableError(msg)
    return project


def read_pyproject_dependencies(path: Path) -> list[Requirement]:
    """Read [project].dependencies from a pyproject.toml file.

    Returns a list of Requirement objects parsed from the dependency
    strings. The key is optional under PEP 621, so an absent
    ``dependencies`` reads as an empty list. Raises FileNotFoundError if
    the file doesn't exist, KeyError if [project] is missing, TypeError
    if [project] is not a table, and InvalidProjectRequirementError if a
    dependency string is malformed or if ``dependencies`` is declared
    dynamic. The root-project lock path cannot run the build backend
    that would compute a dynamic value.
    """
    with path.open("rb") as f:
        data = tomli.load(f)
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

    A project that declares no ``[build-system]`` gets no fallback to the
    PEP 517 default backend: pinning an implied ``setuptools`` would put a
    build requirement in the lock that the project never asked for.
    Absent ``[build-system]`` and a table without the mandatory
    ``requires`` key both raise
    :class:`InvalidProjectRequirementError`; a ``[build-system]`` that is
    not a table raises :class:`InvalidProjectTableError`.

    Only the static list is read.  What a backend adds from
    ``get_requires_for_build_wheel`` is known only once that backend runs,
    and nothing runs this project's own backend to find out.
    """
    with path.open("rb") as f:
        data = tomli.load(f)

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

    Returns the project name as a string, or ``None`` when the file
    has no ``[project]`` table or no ``name`` key (a workspace-root
    pyproject without its own distribution).
    """
    name = _load_project_table(path).get("name")
    return name if isinstance(name, str) else None


def read_pyproject_optional_dependencies(
    path: Path,
) -> Mapping[str, Sequence[str]]:
    """Read [project.optional-dependencies] from a pyproject.toml file.

    Returns the raw mapping of extra name to requirement strings.
    Returns an empty dict when ``[project.optional-dependencies]``
    is absent.
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

    Returns the raw group table: a mapping of group name to a list
    of requirement strings or include records
    (``{"include-group": "other-group"}``).  Returns an empty dict
    when the table is absent so callers can pass the result to
    :func:`resolve_groups_to_requirements` unconditionally.
    """
    with path.open("rb") as f:
        data = tomli.load(f)
    raw = data.get("dependency-groups", {})
    if not isinstance(raw, dict):
        msg = f"[dependency-groups] must be a table, got {type(raw).__name__}"
        raise InvalidProjectTableError(msg)
    return raw
