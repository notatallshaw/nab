"""Helpers the command modules and :mod:`nab.cli` share.

``require_pyproject_file`` guards the project path for every command that
takes one, and the selection helpers turn ``nab lock``'s and ``nab
download``'s group and extra flags into canonical tuples.  A helper
belongs here once a second command module needs it, so that neither has
to import the other.

This module and ``cli`` import each other, so each binds the other module
rather than a name off it: a name import on either side of that cycle
fails in a fresh interpreter.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import tomli

from nab_project.paths import PathState, path_state
from nab_project.pyproject_files import (
    read_pyproject_groups,
    read_pyproject_optional_dependencies,
)

from . import cli as _cli

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


def require_pyproject_file(path: Path) -> None:
    """Exit 1 if ``path`` is not a readable pyproject file.

    Shared by every command that takes a project path, so the rejection
    wording lives in one place.  A ``--path`` that is missing, a
    directory, or not a regular file is a hard error, not a
    silently-skipped source.  A path whose stat fails passes: the config
    read reports it, naming the errno.
    """
    state = path_state(path)

    if state is PathState.DIRECTORY:
        _cli.printer().error(f"{path} is a directory")
        sys.exit(1)

    if state is PathState.OTHER:
        _cli.printer().error(f"{path} exists but is not a regular file")
        sys.exit(1)

    if state is PathState.ABSENT:
        _cli.printer().error(f"{path} not found")
        sys.exit(1)

    if _cli._is_pylock(path):  # noqa: SLF001
        _cli.printer().error(
            f"{path} is a PEP 751 lockfile, not a pyproject.  nab resolves"
            " from project inputs, so pass the pyproject.toml instead."
        )
        sys.exit(1)


def _read_selection_table_or_exit(
    path: Path,
    reader: Callable[[Path], Mapping[str, object]],
) -> Mapping[str, object]:
    """Read the table a selection flag expands over, exiting 1 on a bad file.

    ``nab download`` selects groups and extras before it loads the config, so
    this read can be the first to touch the pyproject and runs the path guards
    itself rather than relying on the config load having run.
    """
    require_pyproject_file(path)

    try:
        return reader(path)
    except OSError as e:
        _cli.printer().error(f"cannot read {path}: {e}")
        sys.exit(1)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        _cli.printer().error(f"{path} is not valid TOML: {e}")
        sys.exit(1)
    except TypeError as e:
        _cli.printer().error(f"in {path}: {e}")
        sys.exit(1)


def resolve_group_selection(
    path: Path,
    *,
    groups: tuple[str, ...],
    all_groups: bool,
) -> tuple[str, ...]:
    """Return the canonical, deduplicated group selection for this run.

    ``groups`` is the user-supplied list (already split by tyro on
    commas).  ``all_groups`` overrides it: when set, every group
    defined in the project's ``[dependency-groups]`` table is
    selected.  An ``--all-groups`` paired with a non-empty
    ``--groups`` list raises a clean error rather than silently
    preferring one over the other.
    """
    if all_groups and groups:
        _cli.printer().error("--all-groups and --groups are mutually exclusive")
        sys.exit(1)
    if not (all_groups or groups):
        return ()

    defined = _read_selection_table_or_exit(path, read_pyproject_groups)
    return tuple(defined.keys()) if all_groups else tuple(dict.fromkeys(groups))


def resolve_extra_selection(
    path: Path,
    *,
    extras: tuple[str, ...],
    all_extras: bool,
) -> tuple[str, ...]:
    """Return the canonical, deduplicated extras selection for this run."""
    if all_extras and extras:
        _cli.printer().error("--all-extras and --extras are mutually exclusive")
        sys.exit(1)
    if not (all_extras or extras):
        return ()

    defined = _read_selection_table_or_exit(path, read_pyproject_optional_dependencies)
    return tuple(defined.keys()) if all_extras else tuple(dict.fromkeys(extras))
