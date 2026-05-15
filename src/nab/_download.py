"""``nab download`` subcommand.

Resolves a project once with the single-environment resolver and
fetches every wheel and sdist into a local directory.  Universal
mode is rejected: the per-tuple lock is the install-time contract,
so a one-environment download would not represent the resolved
universe.

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
from .cli import (
    HttpBackend,
    PathArg,
    app,
)


@app.command
def download(
    path: PathArg = Path("pyproject.toml"),
    *,
    output: Path = Path("wheels"),
    http_backend: HttpBackend = "urllib3",
    cache_dir: Path | None = None,
    no_cache: bool = False,
    offline: bool = False,
    max_concurrency: int = 8,
    no_workspace_discovery: bool = False,
) -> None:
    """Resolve and download every wheel/sdist into a local directory.

    Output files are named after the recorded artefact filename.  The
    download is idempotent: files whose sha256 already matches are
    left alone.  Local and VCS pins are skipped.
    """
    config = _cli._load_config(  # noqa: SLF001
        path, discover_workspace=not no_workspace_discovery
    )
    if config.mode is ResolveMode.UNIVERSAL:
        sys.stderr.write("Error: `nab download` is single-environment only.\n")
        sys.exit(1)

    effective_cache_dir = _cli._resolve_effective_cache_dir(  # noqa: SLF001
        cache_dir, no_cache=no_cache
    )
    transport = _cli._make_transport(http_backend)  # noqa: SLF001
    result = _cli._resolve_specific(  # noqa: SLF001
        path,
        config=config,
        cache_dir=effective_cache_dir,
        offline=offline,
        transport=transport,
        failure_prefix="Cannot download",
    )

    download_transport = _cli._make_transport(http_backend)  # noqa: SLF001
    try:
        outcome = _cli.download_lock(
            result.lock_input,
            download_transport,
            output,
            max_concurrency=max_concurrency,
        )
    except DownloadError as e:
        sys.stderr.write(f"Download failed: {e}\n")
        sys.exit(1)

    sys.stderr.write(
        f"Downloaded {len(outcome.written)} files,"
        f" {len(outcome.skipped)} already present, into {output}\n"
    )
