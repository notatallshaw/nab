"""Import dependency declarations without installing or executing their projects."""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlsplit

from nab_index.archive_files import source_relative_path
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.requirements_file import (
    expand_project_requirements,
    parse_requirements,
    require_string_list,
    resolve_groups_to_requirements,
)

from . import toml_io
from .pyproject_files import (
    build_system_requires,
    dependency_groups,
    project_dependencies,
    project_name,
    project_optional_dependencies,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from nab_provider._vendor.packaging.requirements import Requirement

__all__ = ["dependency_input_requirements", "input_table"]


def input_table(value: object, where: str) -> dict[str, Any]:
    """Validate a TOML table before interpreting its declarations."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        msg = f"{where} must be a table"
        raise ValueError(msg)
    return value


class _Project(NamedTuple):
    path: str
    extras: tuple[str, ...]
    groups: tuple[str, ...]
    dependencies: bool
    build_requirements: bool


def _projects(declaration: Mapping[str, object]) -> list[_Project]:
    """Parse independently selected dependency categories from source projects."""
    root = {
        key: value
        for key, value in declaration.items()
        if key not in {"projects", "requirements-files"}
    }
    if "projects" in declaration:
        if root:
            msg = "choose root project requirements or list the selected projects"
            raise ValueError(msg)
        value = declaration["projects"]
    else:
        # A files-only import does not need a pyproject.toml.
        value = [root] if root or "requirements-files" not in declaration else []
    if not isinstance(value, list):
        msg = "dependency input projects must be an array"
        raise TypeError(msg)
    result = []
    for entry in value:
        table = input_table(entry, "dependency input project")
        unknown = table.keys() - {
            "path",
            "extras",
            "groups",
            "dependencies",
            "build-requirements",
        }
        if unknown:
            msg = f"unknown dependency input project keys: {sorted(unknown)}"
            raise ValueError(msg)
        path = table.get("path", ".")
        if not isinstance(path, str):
            msg = "dependency input project path must be a string"
            raise TypeError(msg)
        extras = tuple(require_string_list(table.get("extras", []), "input extras"))
        groups = tuple(require_string_list(table.get("groups", []), "input groups"))
        build = table.get("build-requirements", False)
        # Selecting a category does not also select unrelated runtime requirements.
        dependencies = table.get("dependencies", not (extras or groups or build))
        if not isinstance(dependencies, bool) or not isinstance(build, bool):
            msg = "input dependencies and build-requirements must be booleans"
            raise TypeError(msg)
        result.append(
            _Project(
                source_relative_path(path),
                extras,
                groups,
                dependencies,
                build,
            )
        )
    return result


def _requirements_file(
    path: str,
    read: Callable[[str], str],
    active: tuple[str, ...] = (),
) -> list[Requirement]:
    """Read PEP 508 lines and relative -r includes; reject pip-only options."""
    path = source_relative_path(path)
    if path in active:
        msg = f"cyclic requirements file include: {(*active, path)}"
        raise ValueError(msg)
    result = []
    for line in read(path).splitlines():
        text = line.split(" #", 1)[0].strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith(("-r ", "--requirement ")):
            relative = text.split(maxsplit=1)[1]
            child = source_relative_path(
                posixpath.normpath(str(PurePosixPath(path).parent / relative))
            )
            result.extend(_requirements_file(child, read, (*active, path)))
        else:
            result.extend(parse_requirements([text], path))
    return result


def _provided_project(
    document: Mapping[str, Any],
    path: str,
    optional: dict[str, Mapping[str, Sequence[str]]],
) -> str:
    """Record extras only for a project the caller supplies separately."""
    name = project_name(document)
    if name is None:
        msg = f"{path} must declare its project name"
        raise ValueError(msg)
    name = canonicalize_name(name)
    if name in optional:
        msg = f"duplicate imported project {name!r}"
        raise ValueError(msg)
    optional[name] = project_optional_dependencies(document)
    return name


def dependency_input_requirements(
    declaration: Mapping[str, object],
    read: Callable[[str], str],
    *,
    root: Path,
) -> list[str]:
    """Read selected dependency declarations from source projects.

    ``read`` binds one local tree or hash-verified source archive. Configuration
    from imported projects is ignored; selected dynamic dependencies are refused.
    """
    projects = _projects(declaration)
    optional: dict[str, Mapping[str, Sequence[str]]] = {}
    roots: list[Requirement] = []
    for project in projects:
        path = str(PurePosixPath(project.path) / "pyproject.toml")
        document = toml_io.loads(read(path))
        if project.dependencies or project.extras:
            name = _provided_project(document, path, optional)
            if project.extras:
                roots.extend(
                    parse_requirements([f"{name}[{','.join(project.extras)}]"], path)
                )
        if project.dependencies:
            roots.extend(project_dependencies(document))
        if project.build_requirements:
            roots.extend(build_system_requires(document, root / path))
        roots.extend(
            resolve_groups_to_requirements(dependency_groups(document), project.groups)
        )

    for path in require_string_list(
        declaration.get("requirements-files", []), "input requirements-files"
    ):
        roots.extend(_requirements_file(path, read))
    result = expand_project_requirements(roots, optional)
    for requirement in result:
        if requirement.url is not None and urlsplit(requirement.url).scheme in {
            "",
            "file",
        }:
            msg = (
                "dependency inputs cannot rebind local direct references; "
                "declare them as explicit project sources"
            )
            raise ValueError(msg)
    return sorted({str(requirement) for requirement in result})
