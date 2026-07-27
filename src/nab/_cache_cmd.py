"""``nab cache`` subcommand: inspect and clear the on-disk cache.

``dir`` prints the resolved cache root, whether or not it exists.
``verify`` walks the record buckets read-only and reports corrupt
entries by path and reason. ``clear`` removes every bucket nab owns,
including the cloned and extracted source trees.

``verify`` and ``clear`` descend only into the buckets nab owns, never
follow a symlink out of the root, and refuse a root that holds foreign
files.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import tyro

from nab_index.cache import OnDiskCache, is_recognized_bucket
from nab_python.config_sources import SourceConfigError

from .cli import (
    _default_cache_dir,
    _fail_config,
    app,
    effective_config,
    printer,
)

ActionArg = Annotated[str, tyro.conf.Positional]


@app.command(name="cache")
def cache_command(
    action: ActionArg,
    *,
    cache_dir: Path | None = None,
) -> None:
    """Inspect and clear nab's on-disk cache.

    ``nab cache dir`` prints the resolved cache root. ``nab cache verify``
    walks the cache read-only and reports corrupt entries. ``nab cache
    clear`` removes every recognized bucket.
    """
    root = _cache_root(cache_dir)
    if action == "dir":
        sys.stdout.write(f"{root}\n")
        return
    if action == "verify":
        _verify(root)
        return
    if action == "clear":
        _clear(root)
        return
    printer().error(
        f"unknown cache action {action!r}; expected one of 'dir', 'verify', 'clear'"
    )
    sys.exit(1)


def _cache_root(cache_dir: Path | None) -> Path:
    """Resolve the cache root a run in this directory would use.

    ``--cache-dir`` is answered without reading any config, so the
    maintenance verbs still work when a project file is broken.
    Otherwise ``cache-dir`` comes off the ladder rooted at the current
    directory, minus the pyproject layer: the key is USER scope, which
    the category gate bars pyproject from setting.
    """
    if cache_dir is not None:
        return cache_dir

    try:
        effective = effective_config(Path("pyproject.toml"), read_pyproject=False)
    except SourceConfigError as exc:
        _fail_config(exc)

    configured = effective["cache-dir"].value
    return _default_cache_dir() if configured is None else configured


def _verify(root: Path) -> None:
    _refuse_foreign_root(root)
    cache = OnDiskCache(root, "")
    for entry in cache.iter_cache_entries():
        reason = cache.read_cache_entry(entry)
        if reason is not None:
            printer().error(f"{entry}: {reason}")


def _clear(root: Path) -> None:
    _refuse_foreign_root(root)
    cache = OnDiskCache(root, "")
    removed = cache.clear_cache()
    printer().done(f"Cleared {root} ({len(removed)} buckets)")


def _refuse_foreign_root(root: Path) -> None:
    """Exit 1 when ``root`` is a file or a populated non-cache directory.

    A directory holding other files but no recognized bucket is not a nab
    cache, so a maintenance verb refuses it. A recognized name on a plain
    file does not make the root a cache. A missing or empty root passes.
    """
    if root.exists() and not root.is_dir():
        printer().error(f"{root} is not a directory")
        sys.exit(1)
    if root.is_dir():
        children = list(root.iterdir())
        if children and not any(
            is_recognized_bucket(c.name) and c.is_dir() for c in children
        ):
            printer().error(f"{root} does not look like a nab cache directory")
            sys.exit(1)
