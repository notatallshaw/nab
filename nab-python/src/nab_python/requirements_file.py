"""Read dependencies and dependency groups from pyproject.toml files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import tomli

from nab_resolver.errors import ResolutionError

from ._conflict_kind import dependency_marker_holds, marker_set
from ._vendor.packaging.dependency_groups import resolve_dependency_groups
from ._vendor.packaging.errors import ExceptionGroup
from ._vendor.packaging.markers import Marker
from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.utils import canonicalize_name
from .metadata import validate_specifier_versions

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ._vendor.packaging.ranges import VersionRange

__all__ = [
    "InvalidProjectRequirementError",
    "InvalidProjectTableError",
    "expand_extra_requirements",
    "expand_group_includes",
    "expand_self_extras",
    "parse_project_requirement",
    "parse_requirements",
    "raise_for_unsatisfiable",
    "read_pyproject_build_requires",
    "read_pyproject_dependencies",
    "read_pyproject_groups",
    "read_pyproject_name",
    "read_pyproject_optional_dependencies",
    "require_string_list",
    "resolve_groups_to_requirements",
    "self_extra_markers",
]


class InvalidProjectRequirementError(ValueError):
    """A pyproject.toml dependency or metadata value is invalid or unresolvable."""


class InvalidProjectTableError(TypeError):
    """A pyproject.toml table such as ``[project]`` is not a table.

    A subclass of :class:`TypeError`, so existing ``except TypeError`` and
    ``pytest.raises(TypeError)`` sites keep working, but the CLI catches it
    specifically so an unrelated internal ``TypeError`` is not mislabelled
    as a user-file error.
    """


def _add_extra_marker(dep_str: str, extra_name: str) -> str:
    """Append ``extra == "name"`` to a :pep:`508` dep string.

    Parses with :class:`Requirement` rather than splitting on the first
    ``;`` so a semicolon inside a direct-reference URL is not mistaken
    for the marker separator; an existing marker is combined with ``and``.

    ``extra_name`` is a table key interpolated into the quoted marker, so
    it is canonicalised with ``validate=True`` (PEP 685). A key that is
    not a valid name (say one containing a quote) then raises
    :class:`InvalidName` instead of producing a marker that gates the dep
    wrongly.
    """
    req = Requirement(dep_str)
    canonical_extra = canonicalize_name(extra_name, validate=True)
    extra_marker = f'extra == "{canonical_extra}"'
    if req.marker is not None:
        marker = f"({req.marker}) and {extra_marker}"
    else:
        marker = extra_marker
    req.marker = None
    return f"{req} ; {marker}"


def parse_project_requirement(
    dep_str: str, source: str, *, extra: str | None = None
) -> Requirement:
    """Parse one PEP 508 dependency string, raising if it is malformed.

    An ``extra`` name is folded in as an ``extra == "name"`` marker. A string
    that is not valid PEP 508, or one whose specifier carries a version that
    will not convert, raises :class:`InvalidProjectRequirementError`, so a
    candidate declaring one malformed dependency is rejected whole rather
    than resolved with the dependency silently dropped.
    """
    try:
        text = _add_extra_marker(dep_str, extra) if extra is not None else dep_str
        req = Requirement(text)
        validate_specifier_versions(req.specifier)
    except ValueError as exc:
        msg = f"invalid requirement in {source}: {exc}"
        raise InvalidProjectRequirementError(msg) from exc
    return req


def parse_requirements(strings: Sequence[str], source: str) -> list[Requirement]:
    """Parse PEP 508 strings, naming ``source`` if one is malformed."""
    return [parse_project_requirement(s, source) for s in strings]


def require_string_list(value: object, source: str) -> list[str]:
    """Validate that a PEP 621 dependency value is an array of strings.

    A bare string passes the type checker as ``Sequence[str]`` but
    iterates character by character, so ``dependencies = "requests"``
    would parse as eight single-character requirements rather than fail.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{source} must be an array of strings"
        raise InvalidProjectRequirementError(msg)
    return value


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


def _canonicalize_optional_deps(
    optional_deps: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """Map each extra name to its requirements under PEP 685 normalization."""
    canonical: dict[str, list[str]] = {}
    for name, reqs in optional_deps.items():
        source = f"[project.optional-dependencies] extra {name!r}"
        canonical.setdefault(canonicalize_name(name), []).extend(
            require_string_list(reqs, source)
        )
    return canonical


def expand_self_extras(
    optional_deps: Mapping[str, Sequence[str]],
    project_name: str | None,
    selected: Sequence[str],
    environment: Mapping[str, str] | None = None,
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
    evaluates true under ``environment``.  ``extra`` binds to the
    one-name set of the extra being walked, so ``extra == "all"``
    resolves against it.  ``environment`` ``None`` skips that check and
    walks every self-reference, which is what a caller that defers
    marker evaluation to each target wants.

    The original ``selected`` order is preserved at the front of the
    result; reachable extras are appended in BFS order without
    duplicates.  ``project_name`` ``None`` short-circuits to the
    input list (no project name = nothing to self-reference).
    Unknown extras are tolerated here; the caller is expected to
    feed the result into :func:`expand_extra_requirements`, which raises
    if an extra is not declared.
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
        for req in _self_references(canonical_deps, canonical_project, extra):
            if (
                environment is not None
                and req.marker is not None
                and not dependency_marker_holds(
                    req.marker, {**environment, "extra": frozenset({extra})}
                )
            ):
                continue
            worklist.extend(
                canonicalize_name(sub)
                for sub in sorted(req.extras)
                if canonicalize_name(sub) not in seen
            )
    return out


def _self_references(
    canonical_deps: Mapping[str, Sequence[str]],
    canonical_project: str,
    extra: str,
) -> Iterator[Requirement]:
    """Yield the requirements of ``extra`` that name the project itself.

    An unparseable requirement is skipped rather than raised on: this walk
    only decides which extras are reachable.
    """
    for req_str in canonical_deps.get(extra, ()):
        try:
            req = Requirement(req_str)
        except (ValueError, TypeError):
            continue
        if canonicalize_name(req.name) == canonical_project:
            yield req


def self_extra_markers(
    optional_deps: Mapping[str, Sequence[str]],
    project_name: str | None,
    selected: Sequence[str],
) -> list[Marker]:
    """Return the markers gating the self-references ``selected`` reaches.

    The closure is walked without an environment, so the result holds every
    clause :func:`expand_self_extras` could read under any environment.
    """
    if project_name is None:
        return []
    canonical_project = canonicalize_name(project_name)
    canonical_deps = _canonicalize_optional_deps(optional_deps)
    return [
        req.marker
        for extra in expand_self_extras(optional_deps, project_name, selected)
        for req in _self_references(canonical_deps, canonical_project, extra)
        if req.marker is not None
    ]


def _and_markers(marker: Marker | None, gates: frozenset[str]) -> Marker:
    """AND a non-empty set of marker strings onto ``marker``."""
    parts = [str(marker)] if marker is not None else []
    parts.extend(sorted(gates))
    return Marker(" and ".join(f"({p})" for p in parts))


def _environment_residual(marker: Marker, extra: str) -> str | bool:
    """Reduce a self-ref activation marker against a bound ``extra``.

    A self-reference is reached only because its extra is selected, so its
    ``extra == "<extra>"`` clause is already decided at expansion.  Restricts
    the marker's environment set with ``extra`` bound to that one name and
    reads off what survives: ``True`` (tautology, a bare dep), ``False``
    (contradiction, does not activate), or a residual marker string of the
    surviving environment conditions.

    ``extra`` binds as a one-name set, the PEP 685 set model the algebra
    evaluates ``extra ==`` / ``extra !=`` under.  Environment variables the
    binding leaves untouched stay in the residual, so a
    variable-vs-variable clause naming ``extra`` (``sys_platform ==
    extra``) is kept as a residual atom over the target's own value rather
    than decided against the machine running nab.
    """
    residual = marker_set(marker).restrict(
        {"extra": frozenset({extra})}, on_unknown_variable="residual"
    )

    if residual.is_empty():
        return False

    text = residual.to_marker_string()
    return True if text is None else text


def expand_extra_requirements(
    optional_deps: Mapping[str, Sequence[str]],
    project_name: str | None,
    selected: Sequence[str],
) -> list[Requirement]:
    """Flatten ``selected`` extras to requirements, propagating self-ref markers.

    Flattens each selected extra over the self-reference closure
    :func:`expand_self_extras` walks, carrying a self-reference's PEP 508
    marker onto the requirements it pulls in.  With
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
        for req in parse_requirements(
            canonical_deps[extra],
            f"[project.optional-dependencies] extra {extra!r}",
        ):
            if canonical_project is not None and (
                canonicalize_name(req.name) == canonical_project
            ):
                worklist.extend(_self_ref_edges(req, extra, gates))
                continue
            if gates:
                req.marker = _and_markers(req.marker, gates)
            out.append(req)
    return out


def _self_ref_edges(
    req: Requirement, extra: str, gates: frozenset[str]
) -> list[tuple[str, frozenset[str]]]:
    """Worklist entries for the extras a self-reference activates.

    The self-ref's own marker is reduced against the walked ``extra``: a
    contradiction means it does not activate (no entries), a tautology
    propagates the inherited ``gates`` unchanged, and an environment
    residual is added to the gate carried onto the reached extras.
    """
    edge = gates
    if req.marker is not None:
        residual = _environment_residual(req.marker, extra)
        if residual is False:
            return []
        if isinstance(residual, str):
            edge = gates | {residual}
    return [(canonicalize_name(sub), edge) for sub in sorted(req.extras)]


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
    requirements themselves are loaded.  A group whose value is not a
    list is skipped, and the loader reports it when that group is
    selected.
    """
    canonical_groups: dict[str, list[str | Mapping[str, str]]] = {}
    for name, entries in groups.items():
        # str is a Sequence, so a bare string would expand into characters.
        if isinstance(entries, str) or not isinstance(entries, Sequence):
            continue
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
        raise InvalidProjectTableError(msg)
    return raw


def resolve_groups_to_requirements(
    groups: Mapping[str, Sequence[str | Mapping[str, str]]],
    selected: Sequence[str],
) -> list[Requirement]:
    """Resolve PEP 735 group includes and return the union of requirements.

    ``selected`` names the groups whose requirements should be
    expanded.  An unknown group name surfaces as :class:`LookupError`;
    a malformed requirement string, cyclic include, or duplicate group
    name surfaces as :class:`InvalidProjectRequirementError`.  Returns
    an empty list when ``selected`` is empty.
    """
    if not selected:
        return []
    try:
        resolved = resolve_dependency_groups(groups, *selected)
    except ExceptionGroup as group:
        detail = "; ".join(str(e) for e in group.exceptions)
        if all(isinstance(e, LookupError) for e in group.exceptions):
            raise LookupError(detail) from group
        msg = f"invalid [dependency-groups]: {detail}"
        raise InvalidProjectRequirementError(msg) from group
    return parse_requirements(resolved, "[dependency-groups]")


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
