"""Workspace discovery for member ``pyproject.toml`` files.

A "workspace" is a table of members declared by a project: either
``[tool.nab.workspace]`` in its ``pyproject.toml`` or ``[workspace]`` in
the project-dir ``nab.toml``.  Every member is synthesised into a
:class:`~nab_provider.provider.LocalSource`.  The provider then prefers
those local sources over PyPI by canonical name, so a member package
resolves against its in-tree source instead of being fetched from the
index.

A project that declares no workspace of its own walks up to an ancestor
project file that does, in either spelling, so ``nab lock`` invoked
against a member still resolves against the root's members.

Members are listed literally; globs are refused with an error.  Users
coming from other tools that allow globs get a clear migration
message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import tomli

from nab_provider._vendor.packaging.utils import InvalidName, canonicalize_name
from nab_provider.policy import LocalSource

from . import toml_io
from ._toml import tool_nab_section
from .paths import PathState, path_state, realpath, resolve_path

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


__all__ = [
    "WorkspaceConfig",
    "WorkspaceDiscoveryError",
    "discover_workspace_root",
    "merge_workspace_local_sources",
    "read_workspace_members",
    "workspace_local_sources",
]


logger = logging.getLogger(__name__)


class WorkspaceDiscoveryError(ValueError):
    """Raised when a workspace member or root is structurally invalid."""


_PROJECT_TOML = "nab.toml"


def _load_member_toml(pyproject: Path) -> dict[str, Any]:
    """Parse ``pyproject``, raising :class:`WorkspaceDiscoveryError` on a bad read.

    :func:`discover_workspace_root` swallows read and parse errors while
    walking, but a chosen root and its declared members must parse, so an
    unreadable or malformed file here is a hard error.
    """
    try:
        with pyproject.open("rb") as f:
            return toml_io.load(f)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{pyproject} is not valid TOML: {exc}"
        raise WorkspaceDiscoveryError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {pyproject}: {exc}"
        raise WorkspaceDiscoveryError(msg) from exc


def _workspace_declaration(data: dict[str, Any], project_file: Path) -> tuple[Any, str]:
    """Return the workspace table ``project_file`` declares, and its name.

    A ``pyproject.toml`` spells it ``[tool.nab.workspace]``; the
    project-dir ``nab.toml`` spells it as a top-level ``[workspace]``.
    The value is returned unvalidated, so a malformed table still counts
    as a declaration and errors when its members are read.  ``None``
    means the file declares no workspace.
    """
    if project_file.name == _PROJECT_TOML:
        return data.get("workspace"), "[workspace]"
    nab = tool_nab_section(data)
    label = "[tool.nab.workspace]"
    if not isinstance(nab, dict):
        return None, label
    return nab.get("workspace"), label


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Parsed workspace table.

    ``members`` is the literal list of paths declared at the workspace
    root.  No globs, no path resolution; those happen later in
    :func:`workspace_local_sources`.
    """

    members: tuple[str, ...]


def discover_workspace_root(member_pyproject: Path) -> Path | None:
    """Return the project file declaring the workspace for ``member_pyproject``.

    Walks upwards from ``member_pyproject``'s directory for the first
    project file that declares a workspace, checking ``pyproject.toml``
    before ``nab.toml`` in each directory.  Returns that file's path, or
    ``None`` when nothing on the walk declares one.

    The starting directory counts, so ``nab lock`` on a workspace root
    activates discovery too.  Filesystem-level errors and TOML parse
    errors during the walk are swallowed: a malformed sibling should not
    prevent discovery from finding a valid root above it.
    """
    start_dir = realpath(member_pyproject).parent
    for parent in (start_dir, *start_dir.parents):
        for candidate in (parent / "pyproject.toml", parent / _PROJECT_TOML):
            try:
                with candidate.open("rb") as f:
                    data = toml_io.load(f)
            except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError):
                continue
            declared, _label = _workspace_declaration(data, candidate)
            if declared is not None:
                return candidate
    return None


def workspace_local_sources(
    members: Iterable[str], *, root_dir: Path, declared_in: str
) -> tuple[LocalSource, ...]:
    """Synthesise a :class:`LocalSource` per declared workspace member.

    ``members`` are the declared paths, resolved against ``root_dir``;
    ``declared_in`` names the declaring file and prefixes the errors.
    Each entry must be a literal path; any entry containing ``*``, ``?``
    or ``[`` raises :class:`WorkspaceDiscoveryError` with a message
    naming the offending entry.  For every member directory the function
    opens ``<member>/pyproject.toml`` and requires ``[project].name``; a
    pyproject that is missing or not a regular file, and a name that is
    missing or not a valid package name, are hard errors.

    Two members declaring the same canonical name raises
    :class:`WorkspaceDiscoveryError`.  The returned tuple preserves
    declaration order, which makes ``nab lock`` output stable when the
    list of members is itself stable.

    Members are marked ``editable``; a workspace member installs
    editably by default, matching uv.  Explicit
    ``[[tool.nab.local-sources]]`` entries default to non-editable.
    """
    sources: list[LocalSource] = []
    seen: dict[str, str] = {}

    for entry in members:
        if any(ch in entry for ch in "*?["):
            msg = (
                f"{declared_in}: globs in workspace members are not supported"
                f" in nab; list members literally."
                f"  Offending entry: {entry!r}"
            )
            raise WorkspaceDiscoveryError(msg)

        member_dir = resolve_path(root_dir, entry)
        if member_dir is None:
            msg = (
                f"{declared_in}: workspace member {entry!r}"
                " is not a usable filesystem path"
            )
            raise WorkspaceDiscoveryError(msg)

        member_pyproject = member_dir / "pyproject.toml"
        state = path_state(member_pyproject)
        if state is PathState.ABSENT:
            msg = (
                f"{declared_in}: workspace member {entry!r} has no"
                f" pyproject.toml at {member_pyproject}"
            )
            raise WorkspaceDiscoveryError(msg)

        if not state.should_read:
            msg = (
                f"{declared_in}: workspace member {entry!r}:"
                f" {member_pyproject} exists but is not a regular file"
            )
            raise WorkspaceDiscoveryError(msg)

        member_data = _load_member_toml(member_pyproject)
        project_table = member_data.get("project", {})
        if not isinstance(project_table, dict):
            msg = (
                f"{member_pyproject}: workspace member [project] must be a"
                f" table, got {type(project_table).__name__}"
            )
            raise WorkspaceDiscoveryError(msg)

        name = project_table.get("name")
        if not isinstance(name, str) or not name:
            msg = (
                f"{member_pyproject}: workspace member must declare"
                f" [project].name (got {name!r})"
            )
            raise WorkspaceDiscoveryError(msg)

        try:
            canonical = canonicalize_name(name, validate=True)
        except InvalidName as exc:
            msg = (
                f"{member_pyproject}: workspace member [project].name"
                f" {name!r} is not a valid package name"
            )
            raise WorkspaceDiscoveryError(msg) from exc

        if canonical in seen:
            msg = (
                f"{declared_in}: workspace members declare duplicate"
                f" canonical name {canonical!r} via entries {seen[canonical]!r}"
                f" and {entry!r}"
            )
            raise WorkspaceDiscoveryError(msg)

        seen[canonical] = entry
        sources.append(LocalSource(name=name, path=str(member_dir), editable=True))

    return tuple(sources)


def read_workspace_members(root_file: Path) -> tuple[LocalSource, ...]:
    """Synthesise :class:`LocalSource` entries from a workspace root.

    Reads the members ``root_file`` declares, in the spelling its name
    calls for, and resolves each member path against the root's
    directory.  This is the path taken for a root the ancestor walk
    found, which is read off disk rather than through the config
    registry.
    """
    root_data = _load_member_toml(root_file)
    raw_workspace, label = _workspace_declaration(root_data, root_file)
    if not isinstance(raw_workspace, dict):
        msg = (
            f"{root_file}: {label} must be a table, got {type(raw_workspace).__name__}"
        )
        raise WorkspaceDiscoveryError(msg)

    raw_members = raw_workspace.get("members")
    if not isinstance(raw_members, list):
        msg = (
            f"{root_file}: {label}.members must be a list of"
            f" strings, got {type(raw_members).__name__}"
        )
        raise WorkspaceDiscoveryError(msg)

    members: list[str] = []
    for entry in raw_members:
        if not isinstance(entry, str):
            msg = (
                f"{root_file}: {label}.members entries must be"
                f" strings, got {type(entry).__name__}: {entry!r}"
            )
            raise WorkspaceDiscoveryError(msg)
        members.append(entry)

    return workspace_local_sources(
        members,
        root_dir=root_file.parent,
        declared_in=str(root_file),
    )


def merge_workspace_local_sources(
    explicit: Iterable[LocalSource],
    discovered: Iterable[LocalSource],
) -> tuple[LocalSource, ...]:
    """Combine explicit and workspace-discovered local sources.

    Explicit ``[[tool.nab.local-sources]]`` entries always win.  When a
    discovered member shares a canonical name with an explicit entry,
    the discovered entry is dropped and one INFO log line records the
    shadow so the user can audit what was overridden.  The order is
    explicit entries first, then unshadowed discovered entries in the
    order they were declared.
    """
    explicit_tuple = tuple(explicit)
    explicit_names = {canonicalize_name(s.name) for s in explicit_tuple}
    out = list(explicit_tuple)
    for src in discovered:
        if canonicalize_name(src.name) in explicit_names:
            logger.info(
                "workspace member %r at %s shadowed by explicit"
                " [[tool.nab.local-sources]]",
                src.name,
                src.path,
            )
            continue
        out.append(src)
    return tuple(out)
