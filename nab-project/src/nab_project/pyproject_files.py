"""Read a project's declarations off disk.

These readers parse TOML, so they sit here rather than beside the pure
requirement algebra in :mod:`nab_provider.requirements_file`.

Every table has a reader named for it, taking an already-parsed document
(``build_system_requires`` also takes the path, which names the file in its
errors), and a ``read_pyproject_*`` wrapper that parses the file for it.  A
caller reading several tables of one file parses it once and passes the
document to each, since a wrapper per table parses the file per table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    "build_system_requires",
    "dependency_groups",
    "project_dependencies",
    "project_name",
    "project_optional_dependencies",
    "read_pyproject_build_requires",
    "read_pyproject_dependencies",
    "read_pyproject_groups",
    "read_pyproject_name",
    "read_pyproject_optional_dependencies",
]


def _project_table(document: Mapping[str, Any]) -> Mapping[str, object]:
    """Return the ``[project]`` table of an already-parsed pyproject.

    An absent ``[project]`` reads as an empty mapping (a workspace-root
    pyproject without its own distribution).  One that is not a table raises
    :class:`InvalidProjectTableError`.
    """
    project = document.get("project", {})
    if not isinstance(project, dict):
        msg = f"[project] must be a table, got {type(project).__name__}"
        raise InvalidProjectTableError(msg)
    return project


def project_dependencies(document: Mapping[str, Any]) -> list[Requirement]:
    """Read [project].dependencies from a parsed pyproject.

    PEP 621 makes the key optional, so an absent ``dependencies`` reads as an
    empty list.  A missing ``[project]`` raises :class:`KeyError` and one that
    is not a table :class:`InvalidProjectTableError`.  A malformed dependency
    string raises :class:`InvalidProjectRequirementError`, as does a dynamic
    ``dependencies``.
    """
    project = document["project"]
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


def build_system_requires(document: Mapping[str, Any], path: Path) -> list[Requirement]:
    """Read [build-system].requires from a parsed pyproject (PEP 518).

    ``path`` names the file in the errors.  There is no fallback to the PEP
    517 default backend: pinning an implied ``setuptools`` would lock a build
    requirement the project never asked for.  An absent ``[build-system]`` and
    a table without the mandatory ``requires`` key both raise
    :class:`InvalidProjectRequirementError`; a ``[build-system]`` that is not
    a table raises :class:`InvalidProjectTableError`.

    Only the static list is read; what ``get_requires_for_build_wheel`` adds is
    known only once the backend runs.
    """
    if "build-system" not in document:
        msg = (
            f"{path} declares no [build-system], so it has no build"
            " requirements to lock"
        )
        raise InvalidProjectRequirementError(msg)

    table = document["build-system"]
    if not isinstance(table, dict):
        msg = f"[build-system] must be a table, got {type(table).__name__}"
        raise InvalidProjectTableError(msg)

    source = "[build-system].requires"
    if "requires" not in table:
        msg = f"{source} is required by PEP 518 and {path} does not declare it"
        raise InvalidProjectRequirementError(msg)

    return parse_requirements(require_string_list(table["requires"], source), source)


def project_name(document: Mapping[str, Any]) -> str | None:
    """Read [project].name from a parsed pyproject.

    ``None`` when the file has no ``[project]`` table or no ``name`` key (a
    workspace-root pyproject without its own distribution).
    """
    name = _project_table(document).get("name")
    return name if isinstance(name, str) else None


def project_optional_dependencies(
    document: Mapping[str, Any],
) -> Mapping[str, Sequence[str]]:
    """Read [project.optional-dependencies] from a parsed pyproject.

    The requirement strings come back unparsed; an absent table reads as an
    empty dict.
    """
    raw = _project_table(document).get("optional-dependencies", {})
    if not isinstance(raw, dict):
        msg = (
            f"[project.optional-dependencies] must be a table, got {type(raw).__name__}"
        )
        raise InvalidProjectTableError(msg)
    return raw


def dependency_groups(
    document: Mapping[str, Any],
) -> Mapping[str, Sequence[str | Mapping[str, str]]]:
    """Read [dependency-groups] from a parsed pyproject (PEP 735).

    The group table comes back unparsed, so an entry is either a requirement
    string or an include record (``{"include-group": "other-group"}``).  An
    absent table reads as an empty dict.
    """
    raw = document.get("dependency-groups", {})
    if not isinstance(raw, dict):
        msg = f"[dependency-groups] must be a table, got {type(raw).__name__}"
        raise InvalidProjectTableError(msg)
    return raw


def read_pyproject_dependencies(path: Path) -> list[Requirement]:
    """Parse ``path`` and read [project].dependencies from it."""
    return project_dependencies(toml_io.load_path(path))


def read_pyproject_build_requires(path: Path) -> list[Requirement]:
    """Parse ``path`` and read [build-system].requires from it."""
    return build_system_requires(toml_io.load_path(path), path)


def read_pyproject_name(path: Path) -> str | None:
    """Parse ``path`` and read [project].name from it."""
    return project_name(toml_io.load_path(path))


def read_pyproject_optional_dependencies(
    path: Path,
) -> Mapping[str, Sequence[str]]:
    """Parse ``path`` and read [project.optional-dependencies] from it."""
    return project_optional_dependencies(toml_io.load_path(path))


def read_pyproject_groups(
    path: Path,
) -> Mapping[str, Sequence[str | Mapping[str, str]]]:
    """Parse ``path`` and read [dependency-groups] from it."""
    return dependency_groups(toml_io.load_path(path))
