"""``nab cache`` subcommand: inspect and clear the on-disk cache.

``dir`` prints the resolved cache root, whether or not it exists.
``verify`` walks the record buckets read-only and lists corrupt entries
by path and reason, exiting 1 when it found any. ``clear`` removes every
bucket nab owns, including the cloned and extracted source trees.

A populated root must contain a recognized cache bucket. Both commands leave
unrelated entries in that root alone.
"""

from __future__ import annotations

__lazy_modules__ = (
    "nab._run",
    "nab.config.values",
    "nab.output",
    "nab_index.cache",
    "pathlib",
    "sys",
)

import sys
from pathlib import Path
from stat import S_ISDIR
from typing import get_args

from nab_index.cache import OnDiskCache, is_recognized_bucket

from ._run import _default_cache_dir, _fail_config, effective_config
from .config.values import SourceConfigError
from .flagtypes import CacheAction
from .output import printer


def cache_command(
    action: CacheAction,
    *,
    cache_dir: Path | None = None,
) -> None:
    """Inspect and clear nab's on-disk cache.

    ``nab cache dir`` prints the resolved cache root. ``nab cache verify``
    walks the cache read-only and lists corrupt entries. ``nab cache
    clear`` removes every recognized bucket.
    """
    root = _cache_root(cache_dir)
    if action == "dir":
        printer().data(f"{root}\n")
    elif action == "verify":
        _verify(root)
    elif action == "clear":
        _clear(root)
    else:
        choices = ", ".join(get_args(CacheAction))
        msg = f"unknown cache action {action!r} (choose from {choices})"
        raise ValueError(msg)


def _cache_root(cache_dir: Path | None) -> Path:
    """Resolve the cache root a run in this directory would use.

    ``--cache-dir`` is answered without reading any config, so the
    maintenance verbs still work when a project file is broken.
    Otherwise ``cache-dir`` comes off the ladder rooted at the current
    directory, minus the pyproject layer: the key is USER scope, so
    pyproject cannot set it.
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
    """List every corrupt entry on stdout, and exit 1 when there was one.

    The listing is what the verb was run to get, so it is the artefact and
    ``nab cache verify > report.txt`` captures it.  A clean cache prints
    nothing, so the status is what a script reads to learn the cache is
    not clean.
    """
    _refuse_foreign_root(root)
    cache = OnDiskCache(root, "")
    corrupt = 0

    def unreadable_directory(error: OSError) -> None:
        nonlocal corrupt
        printer().data(
            f"{error.filename or root}: unreadable: {error.strerror or error}\n"
        )
        corrupt += 1

    for entry in cache.iter_cache_entries(on_error=unreadable_directory):
        reason = cache.read_cache_entry(entry)
        if reason is not None:
            printer().data(f"{entry}: {reason}\n")
            corrupt += 1

    if corrupt:
        sys.exit(1)


def _clear(root: Path) -> None:
    _refuse_foreign_root(root)
    cache = OnDiskCache(root, "")
    try:
        removed = cache.clear_cache()
    except OSError as exc:
        printer().error(
            f"cannot clear cache: {exc.filename or root}: {exc.strerror or exc}"
        )
        raise SystemExit(1) from None
    printer().done(f"Cleared {root} ({len(removed)} buckets)")


def _is_cache_bucket(path: Path) -> bool:
    if not is_recognized_bucket(path.name):
        return False
    try:
        return S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _refuse_foreign_root(root: Path) -> None:
    """Exit 1 when ``root`` is a file or a populated non-cache directory.

    A directory holding other files but no recognized bucket is not a nab
    cache, so a maintenance verb refuses it. A recognized name on a plain
    file does not make the root a cache. A missing or empty root passes.
    """
    try:
        children = list(root.iterdir())
        if not children or any(_is_cache_bucket(child) for child in children):
            return
    except FileNotFoundError:
        return
    except NotADirectoryError:
        printer().error(f"{root} is not a directory")
        raise SystemExit(1) from None
    except OSError as exc:
        printer().error(f"cannot inspect cache {root}: {exc.strerror or exc}")
        raise SystemExit(1) from None

    printer().error(f"{root} does not look like a nab cache directory")
    sys.exit(1)
