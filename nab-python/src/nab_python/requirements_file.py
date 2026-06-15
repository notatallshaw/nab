"""Read dependencies and dependency groups from pyproject.toml files."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import tomli

from nab_resolver.errors import ResolutionError

from ._vendor.packaging.dependency_groups import resolve_dependency_groups
from ._vendor.packaging.markers import Marker
from ._vendor.packaging.requirements import InvalidRequirement, Requirement
from ._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from ._vendor.packaging.ranges import VersionRange

__all__ = [
    "InvalidProjectRequirementError",
    "expand_extra_requirements",
    "expand_group_includes",
    "expand_self_extras",
    "raise_for_unsatisfiable",
    "read_pyproject_dependencies",
    "read_pyproject_groups",
    "read_pyproject_name",
    "read_pyproject_optional_dependencies",
    "resolve_groups_to_requirements",
    "select_optional_dependencies",
]


class InvalidProjectRequirementError(ValueError):
    """A requirement string in pyproject.toml is not valid PEP 508."""


def _parse_requirements(strings: Sequence[str], source: str) -> list[Requirement]:
    """Parse PEP 508 strings, naming ``source`` if one is malformed."""
    try:
        return [Requirement(s) for s in strings]
    except InvalidRequirement as exc:
        msg = f"invalid requirement in {source}: {exc}"
        raise InvalidProjectRequirementError(msg) from exc


def _require_string_list(value: object, source: str) -> list[str]:
    """Validate that a PEP 621 dependency value is an array of strings.

    A bare string passes the type checker as ``Sequence[str]`` but
    iterates character by character, so ``dependencies = "requests"``
    would parse as eight single-character requirements rather than fail.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{source} must be an array of strings"
        raise InvalidProjectRequirementError(msg)
    return value


def read_pyproject_dependencies(path: Path) -> list[Requirement]:
    """Read [project].dependencies from a pyproject.toml file.

    Returns a list of Requirement objects parsed from the dependency
    strings.  Raises FileNotFoundError if the file doesn't exist,
    KeyError if [project] or [project].dependencies is missing, or
    InvalidProjectRequirementError if a dependency string is malformed.
    """
    with path.open("rb") as f:
        data = tomli.load(f)

    source = "[project].dependencies"
    dep_strings = _require_string_list(data["project"]["dependencies"], source)
    return _parse_requirements(dep_strings, source)


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


def _canonicalize_optional_deps(
    optional_deps: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """Map each extra name to its requirements under PEP 685 normalization."""
    canonical: dict[str, list[str]] = {}
    for name, reqs in optional_deps.items():
        source = f"[project.optional-dependencies] extra {name!r}"
        canonical.setdefault(canonicalize_name(name), []).extend(
            _require_string_list(reqs, source)
        )
    return canonical


def select_optional_dependencies(
    optional_deps: Mapping[str, Sequence[str]],
    selected: Sequence[str],
) -> list[Requirement]:
    """Return the union of requirement strings for ``selected`` extras.

    Extra names are compared under PEP 685 normalization, so
    ``My_Extra`` selects a declared ``my-extra``.  Unknown extra names
    raise ``LookupError``.  Returns an empty list when ``selected`` is
    empty.
    """
    if not selected:
        return []
    canonical_deps = _canonicalize_optional_deps(optional_deps)
    out: list[Requirement] = []
    for name in selected:
        canonical = canonicalize_name(name)
        if canonical not in canonical_deps:
            msg = (
                f"extra {name!r} is not declared in"
                f" [project.optional-dependencies]; defined: {sorted(optional_deps)!r}"
            )
            raise LookupError(msg)
        out.extend(
            _parse_requirements(
                canonical_deps[canonical],
                f"[project.optional-dependencies] extra {name!r}",
            )
        )
    return out


def expand_self_extras(
    optional_deps: Mapping[str, Sequence[str]],
    project_name: str | None,
    selected: Sequence[str],
    environment: dict[str, str] | None = None,
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

    A self-reference carrying a PEP 508 marker (``{name}[fast];
    python_version < "3.10"``) activates its extra only when the marker
    evaluates true under ``environment``.  ``environment`` ``None`` skips
    that check and walks every self-reference, which is what the
    universal path wants (it defers marker evaluation to each tuple).

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
    canonical_deps = _canonicalize_optional_deps(optional_deps)
    out: list[str] = []
    seen: set[str] = set()
    worklist: list[str] = [canonicalize_name(s) for s in selected]
    while worklist:
        extra = worklist.pop(0)
        if extra in seen:
            continue
        seen.add(extra)
        out.append(extra)
        for req_str in canonical_deps.get(extra, ()):
            try:
                req = Requirement(req_str)
            except (ValueError, TypeError):
                continue
            if canonicalize_name(req.name) != canonical_project:
                continue
            if (
                environment is not None
                and req.marker is not None
                and not req.marker.evaluate(environment)
            ):
                continue
            worklist.extend(
                canonicalize_name(sub)
                for sub in req.extras
                if canonicalize_name(sub) not in seen
            )
    return out


def _and_markers(marker: Marker | None, gates: frozenset[str]) -> Marker:
    """AND a non-empty set of marker strings onto ``marker``."""
    parts = [str(marker)] if marker is not None else []
    parts.extend(sorted(gates))
    return Marker(" and ".join(f"({p})" for p in parts))


def expand_extra_requirements(
    optional_deps: Mapping[str, Sequence[str]],
    project_name: str | None,
    selected: Sequence[str],
) -> list[Requirement]:
    """Flatten ``selected`` extras to requirements, propagating self-ref markers.

    This is :func:`select_optional_dependencies` over the self-reference
    closure :func:`expand_self_extras` walks, except a self-reference's
    PEP 508 marker is carried onto the requirements it pulls in.  With
    ``all = ["pkg[fast]; python_version < '3.10'"]`` and ``fast =
    ["dep"]``, selecting ``all`` yields ``dep; python_version < '3.10'``
    rather than a bare ``dep`` that survives on every environment, so the
    per-tuple universal parser drops the dep on the tuples it excludes.

    Each activation path is walked separately, so a dep reachable through
    two markers is required under their disjunction.  Unknown extras
    raise ``LookupError``.
    """
    if not selected:
        return []
    canonical_project = (
        canonicalize_name(project_name) if project_name is not None else None
    )
    canonical_deps = _canonicalize_optional_deps(optional_deps)
    out: list[Requirement] = []
    visited: set[tuple[str, frozenset[str]]] = set()
    worklist: list[tuple[str, frozenset[str]]] = [
        (canonicalize_name(s), frozenset()) for s in selected
    ]
    while worklist:
        extra, gates = worklist.pop(0)
        if (extra, gates) in visited:
            continue
        visited.add((extra, gates))
        if extra not in canonical_deps:
            msg = (
                f"extra {extra!r} is not declared in"
                f" [project.optional-dependencies]; defined: {sorted(canonical_deps)!r}"
            )
            raise LookupError(msg)
        for req in _parse_requirements(
            canonical_deps[extra],
            f"[project.optional-dependencies] extra {extra!r}",
        ):
            if canonical_project is not None and (
                canonicalize_name(req.name) == canonical_project
            ):
                edge = gates if req.marker is None else gates | {str(req.marker)}
                worklist.extend((canonicalize_name(sub), edge) for sub in req.extras)
            if gates:
                req.marker = _and_markers(req.marker, gates)
            out.append(req)
    return out


def expand_group_includes(
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    selected: Sequence[str],
) -> list[str]:
    """Return ``selected`` plus every group reached through ``include-group``.

    PEP 735 lets one group pull in another with
    ``{include-group = "other"}``.  A conflict declared on a group must
    see the groups an umbrella group includes, so the membership test
    runs over the transitive closure rather than the literal selection.
    Group names compare canonicalised (PEP 503), matching the loaders.

    Unknown or cyclic includes are tolerated here;
    :func:`resolve_groups_to_requirements` raises on them when the
    requirements themselves are loaded.
    """
    canonical_groups: dict[str, list[str | Mapping[str, str]]] = {}
    for name, entries in groups.items():
        canonical_groups.setdefault(canonicalize_name(name), []).extend(entries)

    out: list[str] = []
    seen: set[str] = set()
    worklist = [canonicalize_name(s) for s in selected]
    while worklist:
        group = worklist.pop(0)
        if group in seen:
            continue
        seen.add(group)
        out.append(group)
        for entry in canonical_groups.get(group, ()):
            if isinstance(entry, Mapping):
                include = entry.get("include-group")
                # A malformed (non-string) include is left for the group
                # loader to report when the requirements are read.
                if isinstance(include, str):
                    worklist.append(canonicalize_name(include))
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
    from the vendored resolver and a malformed requirement string as
    :class:`InvalidProjectRequirementError`; a cyclic include raises the
    vendored resolver's error.  Returns an empty list when
    ``selected`` is empty.
    """
    if not selected:
        return []
    try:
        resolved = resolve_dependency_groups(groups, *selected)
    except InvalidRequirement as exc:
        msg = f"invalid requirement in [dependency-groups]: {exc}"
        raise InvalidProjectRequirementError(msg) from exc
    return [Requirement(s) for s in resolved]


def raise_for_unsatisfiable(
    ranges: Mapping[str, VersionRange],
    sources: Mapping[str, Sequence[str]],
    *,
    kind: str,
) -> None:
    """Raise :class:`ResolutionError` if any folded range is empty.

    ``ranges`` holds one intersected range per package and ``sources``
    the requirement strings folded into each.  An empty range means
    those requirements share no version; the error lists them.

    ``kind`` ("requirement" or "constraint") only shapes the wording.
    """
    unsatisfiable = [name for name, range_ in ranges.items() if range_.is_empty]
    if not unsatisfiable:
        return
    detail = "\n".join(
        f"  {name}: {', '.join(sources[name])}" for name in unsatisfiable
    )
    msg = f"conflicting {kind}s leave no satisfiable version:\n{detail}"
    raise ResolutionError(msg)
