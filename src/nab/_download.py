"""``nab download`` subcommand.

Resolves a project and fetches every wheel, sdist, and direct-URL
archive into a local directory: the union of every target's
artefacts, deduplicated by URL, which for a declared matrix
pre-populates a directory for offline deployment across platforms.

External callers (the resolver entry point and the download
helper) are accessed through :mod:`nab.cli` so the test suite's
``patch("nab.cli.download_lock")`` style of monkey patches keeps
working after the per-command split.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import tyro

from nab_python.download import DownloadError

from . import cli as _cli
from ._lock import resolve_extra_selection, resolve_group_selection
from .cli import (
    BuildPolicyFlag,
    DistPolicyFlag,
    HttpBackend,
    ModeFlag,
    OfflineFlag,
    PathArg,
    ResolutionFlag,
    app,
)


@app.command
def download(  # noqa: PLR0913 - tyro maps each kwarg to a CLI flag so a config object would hide the user-facing surface
    path: PathArg = Path("pyproject.toml"),
    *,
    output: Path = Path("wheels"),
    http_backend: HttpBackend | None = None,
    cache_dir: Path | None = None,
    cache: bool = True,
    offline: OfflineFlag = None,
    python: str | None = None,
    max_concurrency: int | None = None,
    workspace_discovery: bool = True,
    groups: tuple[str, ...] = (),
    all_groups: bool = False,
    extras: tuple[str, ...] = (),
    all_extras: bool = False,
    project_resolution: ResolutionFlag | None = None,
    project_mode: ModeFlag | None = None,
    project_requires_python: str | None = None,
    project_uploaded_prior_to: str | None = None,
    project_dist_policy: DistPolicyFlag | None = None,
    project_build_policy: BuildPolicyFlag | None = None,
    project_constraint: Annotated[tuple[str, ...], tyro.conf.UseAppendAction] = (),
    project_default_group: Annotated[tuple[str, ...], tyro.conf.UseAppendAction] = (),
) -> None:
    """Resolve and download every wheel, sdist, and direct-URL archive.

    Output files are named after the recorded artefact filename.  The
    download is idempotent: files whose sha256 already matches are
    left alone.  Local and VCS pins are skipped.

    ``--groups`` / ``--all-groups`` and ``--extras`` / ``--all-extras``
    mirror ``nab lock``: a project declaring an ``exactly-one`` or
    ``at-least-one`` conflict needs at least one member selected for
    the resolve to start, so these flags also gate the download.

    ``--python X.Y`` resolves for that Python on this machine instead of
    the running interpreter, as on ``nab lock``.

    ``--offline``, ``--cache-dir``, ``--http-backend``,
    ``--max-concurrency`` and ``--project-resolution`` flow through the
    same config sources ``nab lock`` uses, so a ``NAB_*`` env var or a
    system/user/project ``nab.toml`` is read for ``nab download`` as for
    ``nab lock``.
    """
    selected_groups = resolve_group_selection(
        path, groups=groups, all_groups=all_groups
    )
    selected_extras = resolve_extra_selection(
        path, extras=extras, all_extras=all_extras
    )

    overrides = _cli._cli_overrides(  # noqa: SLF001
        cli_resolution=project_resolution,
        cli_offline=offline,
        cli_cache_dir=cache_dir,
        cli_http_backend=http_backend,
        cli_max_concurrency=max_concurrency,
        cli_mode=project_mode,
        cli_requires_python=project_requires_python,
        cli_uploaded_prior_to=project_uploaded_prior_to,
        cli_dist_policy=project_dist_policy,
        cli_build_policy=project_build_policy,
        cli_constraint=project_constraint,
        cli_default_group=project_default_group,
    )
    config = _cli._load_config(  # noqa: SLF001
        path,
        discover_workspace=workspace_discovery,
        cli_overrides=_cli.project_config_overrides(overrides),
    )
    settings = _cli._layered_run_settings_or_exit(  # noqa: SLF001
        path, overrides, produces_lock=False
    )
    effective_cache_dir = _cli._resolve_effective_cache_dir(  # noqa: SLF001
        settings.cache_dir, cache=cache
    )
    _cli._reject_python_override_in_universal(config, python)  # noqa: SLF001
    transport = _cli._make_transport(settings.http_backend)  # noqa: SLF001
    result = _cli._resolve(  # noqa: SLF001
        path,
        config=config,
        cache_dir=effective_cache_dir,
        offline=settings.offline,
        transport=transport,
        failure_prefix="Cannot download",
        python=python,
        groups=selected_groups,
        extras=selected_extras,
        resolution_strategy=settings.resolution,
    )
    lock_input = _cli.build_lock_input(
        result,
        config=config,
        extras=selected_extras,
        dependency_groups=selected_groups,
    )

    download_transport = _cli._make_transport(settings.http_backend)  # noqa: SLF001
    try:
        outcome = _cli.download_lock(
            lock_input,
            download_transport,
            output,
            max_concurrency=settings.max_concurrency,
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
