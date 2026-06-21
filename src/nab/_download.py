"""``nab download`` subcommand.

Resolves a project and fetches every wheel and sdist into a local
directory.  Single-environment mode downloads the one resolved
environment's artefacts; universal mode re-resolves across the
matrix and downloads the union of every tuple's artefacts,
deduplicated by URL, to pre-populate a directory for offline
deployment across platforms.

External callers (the resolver entry point and the download
helper) are accessed through :mod:`nab.cli` so the test suite's
``patch("nab.cli.download_lock")`` style of monkey patches keeps
working after the per-command split.
"""

from __future__ import annotations

import sys
from pathlib import Path

from nab_python.config import ResolveMode
from nab_python.download import DownloadError

from . import cli as _cli
from ._lock import resolve_extra_selection, resolve_group_selection
from .cli import (
    HttpBackend,
    PathArg,
    app,
)


@app.command
def download(  # noqa: PLR0913 - tyro maps each kwarg to a CLI flag so a config object would hide the user-facing surface
    path: PathArg = Path("pyproject.toml"),
    *,
    output: Path = Path("wheels"),
    http_backend: HttpBackend = "urllib3",
    cache_dir: Path | None = None,
    cache: bool = True,
    offline: bool = False,
    max_concurrency: int = 8,
    workspace_discovery: bool = True,
    groups: tuple[str, ...] = (),
    all_groups: bool = False,
    extras: tuple[str, ...] = (),
    all_extras: bool = False,
) -> None:
    """Resolve and download every wheel/sdist into a local directory.

    Output files are named after the recorded artefact filename.  The
    download is idempotent: files whose sha256 already matches are
    left alone.  Local and VCS pins are skipped.

    ``--groups`` / ``--all-groups`` and ``--extras`` / ``--all-extras``
    mirror ``nab lock``: a project declaring an ``exactly-one`` or
    ``at-least-one`` conflict needs at least one member selected for
    the resolve to start, so these flags also gate the download.
    """
    if max_concurrency < 1:
        sys.stderr.write("Error: --max-concurrency must be at least 1.\n")
        sys.exit(1)

    selected_groups = resolve_group_selection(
        path, groups=groups, all_groups=all_groups
    )
    selected_extras = resolve_extra_selection(
        path, extras=extras, all_extras=all_extras
    )

    config = _cli._load_config(  # noqa: SLF001
        path, discover_workspace=workspace_discovery
    )
    effective_cache_dir = _cli._resolve_effective_cache_dir(  # noqa: SLF001
        cache_dir, cache=cache
    )
    transport = _cli._make_transport(http_backend)  # noqa: SLF001
    if config.mode is ResolveMode.UNIVERSAL:
        universal = _cli._resolve_universal(  # noqa: SLF001
            path,
            config=config,
            cache_dir=effective_cache_dir,
            offline=offline,
            transport=transport,
            groups=selected_groups,
            extras=selected_extras,
        )
        lock_input = _cli.merge_universal_lock_inputs(universal)
    else:
        result = _cli._resolve_specific(  # noqa: SLF001
            path,
            config=config,
            cache_dir=effective_cache_dir,
            offline=offline,
            transport=transport,
            failure_prefix="Cannot download",
            groups=selected_groups,
            extras=selected_extras,
        )
        lock_input = result.lock_input

    download_transport = _cli._make_transport(http_backend)  # noqa: SLF001
    try:
        outcome = _cli.download_lock(
            lock_input,
            download_transport,
            output,
            max_concurrency=max_concurrency,
        )
    except DownloadError as e:
        sys.stderr.write(f"Download failed: {e}\n")
        sys.exit(1)
    except OSError as e:
        sys.stderr.write(f"Error: cannot write to output directory {output}: {e}\n")
        sys.exit(1)

    sys.stderr.write(
        f"Downloaded {len(outcome.written)} files,"
        f" {len(outcome.skipped)} already present, into {output}\n"
    )
