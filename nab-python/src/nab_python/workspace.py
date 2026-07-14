"""Workspace discovery for member ``pyproject.toml`` files.

A "workspace" is a table of members declared by a project: either
``[tool.nab.workspace]`` in its ``pyproject.toml`` or ``[workspace]`` in
the project-dir ``nab.toml``.  Every member is synthesised into a
:class:`~nab_python.provider.LocalSource`.  The provider then prefers
those local sources over PyPI by canonical name, so a member package
resolves against its in-tree source instead of being fetched from the
index.

A project that declares no workspace of its own walks up to an ancestor
``pyproject.toml`` that does, so ``nab lock`` invoked against a member
still resolves against the root's members.

Members are listed literally; globs are refused with an error.  Users
coming from other tools that allow globs get a clear migration
message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import tomli

from ._toml import tool_nab_section
from ._vendor.packaging.utils import canonicalize_name
from .provider import BuildPolicy, LocalSource

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


__all__ = [
    "WorkspaceConfig",
    "WorkspaceDiscoveryError",
    "auto_promote_build_policy_for_workspace",
    "discover_workspace_root",
    "merge_workspace_local_sources",
    "read_workspace_members",
    "workspace_local_sources",
]


logger = logging.getLogger(__name__)


_PERMISSIVENESS = {
    BuildPolicy.NEVER: 0,
    BuildPolicy.BUILD_LOCAL: 1,
    BuildPolicy.BUILD_REMOTE: 2,
}


class WorkspaceDiscoveryError(ValueError):
    """Raised when a workspace member or root is structurally invalid."""


def _load_member_toml(pyproject: Path) -> dict[str, Any]:
    """Parse ``pyproject``, raising :class:`WorkspaceDiscoveryError` on bad TOML.

    :func:`discover_workspace_root` swallows parse errors while walking,
    but a chosen root and its declared members must parse, so malformed
    TOML here is a hard error.
    """
    try:
        with pyproject.open("rb") as f:
            return tomli.load(f)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{pyproject} is not valid TOML: {exc}"
        raise WorkspaceDiscoveryError(msg) from exc


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Parsed ``[tool.nab.workspace]`` table.

    ``members`` is the literal list of paths declared at the workspace
    root.  No globs, no path resolution; those happen later in
    :func:`read_workspace_members`.
    """

    members: tuple[str, ...]


def discover_workspace_root(member_pyproject: Path) -> Path | None:
    """Return the workspace root pyproject for ``member_pyproject``.

    Walks from ``member_pyproject``'s directory upwards looking for the
    first ``pyproject.toml`` whose ``[tool.nab.workspace]`` table is
    present.  Returns that pyproject's path, or ``None`` when no such
    ancestor (or self) exists.

    The input ``member_pyproject`` is itself considered: a user invoking
    ``nab lock`` on a workspace root sees discovery activate.
    Filesystem-level errors and TOML parse errors during the walk are
    swallowed: a malformed sibling pyproject should not prevent
    discovery from finding a valid root above it.
    """
    start_dir = member_pyproject.resolve().parent
    for parent in (start_dir, *start_dir.parents):
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            with candidate.open("rb") as f:
                data = tomli.load(f)
        except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError):
            continue
        nab = tool_nab_section(data)
        if isinstance(nab, dict) and "workspace" in nab:
            return candidate
    return None


def workspace_local_sources(
    members: Iterable[str], *, root_dir: Path, declared_in: str
) -> tuple[LocalSource, ...]:
    """Synthesise a :class:`LocalSource` per declared workspace member.

    ``members`` are the paths the workspace declared, resolved against
    ``root_dir``; ``declared_in`` names the file that declared them and
    prefixes the errors.  Each entry must be a literal path; any entry
    containing ``*``, ``?`` or ``[`` raises
    :class:`WorkspaceDiscoveryError` with a message naming the offending
    entry.  For every member directory the function opens
    ``<member>/pyproject.toml`` and requires ``[project].name``; missing
    pyproject or missing name is a hard error.

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
        member_dir = (root_dir / entry).resolve()
        member_pyproject = member_dir / "pyproject.toml"
        if not member_pyproject.is_file():
            msg = (
                f"{declared_in}: workspace member {entry!r} has no"
                f" pyproject.toml at {member_pyproject}"
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
        canonical = canonicalize_name(name)
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


def read_workspace_members(root_pyproject: Path) -> tuple[LocalSource, ...]:
    """Synthesise :class:`LocalSource` entries from a workspace root.

    Reads ``[tool.nab.workspace].members`` from ``root_pyproject`` and
    materialises them with :func:`workspace_local_sources`, resolving each
    member path against the root's directory.  Used for the root an
    ancestor walk finds, which is read straight off disk rather than
    through the config registry.
    """
    root_data = _load_member_toml(root_pyproject)
    raw_workspace = root_data.get("tool", {}).get("nab", {}).get("workspace")
    if not isinstance(raw_workspace, dict):
        msg = (
            f"{root_pyproject}: [tool.nab.workspace] must be a table,"
            f" got {type(raw_workspace).__name__}"
        )
        raise WorkspaceDiscoveryError(msg)
    raw_members = raw_workspace.get("members")
    if not isinstance(raw_members, list):
        msg = (
            f"{root_pyproject}: [tool.nab.workspace].members must be a list of"
            f" strings, got {type(raw_members).__name__}"
        )
        raise WorkspaceDiscoveryError(msg)
    members: list[str] = []
    for entry in raw_members:
        if not isinstance(entry, str):
            msg = (
                f"{root_pyproject}: [tool.nab.workspace].members entries must be"
                f" strings, got {type(entry).__name__}: {entry!r}"
            )
            raise WorkspaceDiscoveryError(msg)
        members.append(entry)
    return workspace_local_sources(
        members,
        root_dir=root_pyproject.parent,
        declared_in=str(root_pyproject),
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


def auto_promote_build_policy_for_workspace(current: BuildPolicy) -> BuildPolicy:
    """Floor ``current`` at :attr:`BuildPolicy.BUILD_LOCAL`.

    Workspace members frequently use ``dynamic = ["version"]`` (hatch's
    pattern) and other dynamic fields, which require the local backend
    path.  The user's setting wins when it is already at least as
    permissive as :attr:`BuildPolicy.BUILD_LOCAL`.
    """
    if _PERMISSIVENESS[current] < _PERMISSIVENESS[BuildPolicy.BUILD_LOCAL]:
        return BuildPolicy.BUILD_LOCAL
    return current
