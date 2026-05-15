"""Read dependencies and dependency groups from pyproject.toml files."""

from __future__ import annotations

from typing import TYPE_CHECKING

import tomli

from ._vendor.packaging.dependency_groups import resolve_dependency_groups
from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "expand_self_extras",
    "read_pyproject_dependencies",
    "read_pyproject_groups",
    "read_pyproject_name",
    "read_pyproject_optional_dependencies",
    "resolve_groups_to_requirements",
    "select_optional_dependencies",
]


def read_pyproject_dependencies(path: Path) -> list[Requirement]:
    """Read [project].dependencies from a pyproject.toml file.

    Returns a list of Requirement objects parsed from the dependency
    strings.  Raises FileNotFoundError if the file doesn't exist, or
    KeyError if [project] or [project].dependencies is missing.
    """
    with path.open("rb") as f:
        data = tomli.load(f)

    dep_strings: list[str] = data["project"]["dependencies"]
    return [Requirement(s) for s in dep_strings]


def read_pyproject_name(path: Path) -> str | None:
    """Read [project].name from a pyproject.toml file.

    Returns the project name as a string, or ``None`` when the file
    has no ``[project]`` table or no ``name`` key (a workspace-root
    pyproject without its own distribution).
    """
    with path.open("rb") as f:
        data = tomli.load(f)
    name = data.get("project", {}).get("name")
    return name if isinstance(name, str) else None


def read_pyproject_optional_dependencies(
    path: Path,
) -> Mapping[str, Sequence[str]]:
    """Read [project.optional-dependencies] from a pyproject.toml file.

    Returns the raw mapping of extra name to requirement strings.
    Returns an empty dict when ``[project.optional-dependencies]``
    is absent.
    """
    with path.open("rb") as f:
        data = tomli.load(f)
    raw = data.get("project", {}).get("optional-dependencies", {})
    if not isinstance(raw, dict):
        msg = (
            f"[project.optional-dependencies] must be a table, got {type(raw).__name__}"
        )
        raise TypeError(msg)
    return raw


def select_optional_dependencies(
    optional_deps: Mapping[str, Sequence[str]],
    selected: Sequence[str],
) -> list[Requirement]:
    """Return the union of requirement strings for ``selected`` extras.

    Unknown extra names raise ``LookupError``.  Returns an empty
    list when ``selected`` is empty.
    """
    if not selected:
        return []
    out: list[Requirement] = []
    for name in selected:
        if name not in optional_deps:
            msg = (
                f"extra {name!r} is not declared in"
                f" [project.optional-dependencies]; defined: {sorted(optional_deps)!r}"
            )
            raise LookupError(msg)
        out.extend(Requirement(req_str) for req_str in optional_deps[name])
    return out


def expand_self_extras(
    optional_deps: Mapping[str, Sequence[str]],
    project_name: str | None,
    selected: Sequence[str],
) -> list[str]:
    """Return ``selected`` plus every extra reachable through self-references.

    When an extra's contents include a requirement of the form
    ``{project_name}[a, b]`` (the project depending on itself with
    other extras activated), the referenced extras are walked
    transitively.  Without this, an ``[all] = ["{name}[graphviz, otel,
    ...]"]`` self-reference leaves the actual third-party deps
    (graphviz, opentelemetry-api, etc.) out of the resolver's root
    requirements and look-ahead loses the ability to predict
    candidates.

    The original ``selected`` order is preserved at the front of the
    result; reachable extras are appended in BFS order without
    duplicates.  ``project_name`` ``None`` short-circuits to the
    input list (no project name = nothing to self-reference).
    Unknown extras are tolerated here; the caller is expected to
    feed the result into :func:`select_optional_dependencies`, which
    raises if an extra is not declared.
    """
    if project_name is None:
        return list(selected)
    canonical_project = canonicalize_name(project_name)
    out: list[str] = []
    seen: set[str] = set()
    worklist: list[str] = list(selected)
    while worklist:
        extra = worklist.pop(0)
        if extra in seen:
            continue
        seen.add(extra)
        out.append(extra)
        for req_str in optional_deps.get(extra, ()):
            try:
                req = Requirement(req_str)
            except (ValueError, TypeError):
                continue
            if canonicalize_name(req.name) != canonical_project:
                continue
            worklist.extend(sub for sub in req.extras if sub not in seen)
    return out


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
        raise TypeError(msg)
    return raw


def resolve_groups_to_requirements(
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    selected: Sequence[str],
) -> list[Requirement]:
    """Resolve PEP 735 group includes and return the union of requirements.

    ``selected`` names the groups whose requirements should be
    expanded.  Unknown group names surface as :class:`LookupError`
    from the vendored resolver.  Cyclic or malformed groups raise
    the matching packaging error.  Returns an empty list when
    ``selected`` is empty.
    """
    if not selected:
        return []
    resolved = resolve_dependency_groups(groups, *selected)
    return [Requirement(s) for s in resolved]
