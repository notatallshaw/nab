"""``nab download`` subcommand.

Resolves a project and fetches every wheel, sdist, and direct-URL
archive into a local directory: the union of every target's
artefacts, deduplicated by URL, which for a declared matrix
pre-populates a directory for offline deployment across platforms.

The helpers this shares with :mod:`nab._lock` live in :mod:`nab._run`,
and the run's printer in :mod:`nab.output`; everything else is imported
from the module that defines it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import tyro

from nab_project.download import DownloadError, download_lock
from nab_project.resolve import build_lock_input

from ._run import (
    _cli_overrides,
    _layered_run_settings_or_exit,
    _load_config,
    _make_transport,
    _project_cli_overrides_or_exit,
    _reject_python_override_in_universal,
    _resolve,
    _resolve_effective_cache_dir,
    project_config_overrides,
    read_config_ladder,
    resolve_extra_selection,
    resolve_group_selection,
)
from .cli import (
    BuildPolicyFlag,
    DecisionOrderFlag,
    DistPolicyFlag,
    HttpBackend,
    ModeFlag,
    OfflineFlag,
    PathArg,
    ResolutionFlag,
    app,
)
from .output import ProgressReporter, printer


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
    project_build_requires_depth: int | None = None,
    project_decision_order: DecisionOrderFlag | None = None,
    project_constraint: Annotated[tuple[str, ...], tyro.conf.UseAppendAction] = (),
    project_default_group: Annotated[tuple[str, ...], tyro.conf.UseAppendAction] = (),
    project_base_group: str | None = None,
    project_build_group: str | None = None,
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

    overrides = _cli_overrides(
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
        cli_build_requires_depth=project_build_requires_depth,
        cli_decision_order=project_decision_order,
        cli_constraint=project_constraint,
        cli_default_group=project_default_group,
        cli_base_group=project_base_group,
        cli_build_group=project_build_group,
    )
    project_overrides = project_config_overrides(overrides)
    _project_cli_overrides_or_exit(project_overrides)
    config = _load_config(
        path,
        discover_workspace=workspace_discovery,
        cli_overrides=project_overrides,
    )
    ladder = read_config_ladder(path, overrides)
    settings = _layered_run_settings_or_exit(ladder, produces_lock=False)
    effective_cache_dir = _resolve_effective_cache_dir(settings.cache_dir, cache=cache)
    _reject_python_override_in_universal(config, python)
    transport = _make_transport(settings.http_backend)
    result = _resolve(
        path,
        config=config,
        cache_dir=effective_cache_dir,
        offline=settings.offline,
        transport=transport,
        failure_prefix="cannot download",
        python=python,
        groups=selected_groups,
        extras=selected_extras,
        resolution_strategy=settings.resolution,
        progress=ProgressReporter(printer()),
    )
    lock_input = build_lock_input(
        result,
        config=config,
        extras=selected_extras,
        dependency_groups=selected_groups,
    )

    download_transport = _make_transport(settings.http_backend)
    try:
        outcome = download_lock(
            lock_input,
            download_transport,
            output,
            max_concurrency=settings.max_concurrency,
            offline=settings.offline,
        )
    except DownloadError as e:
        printer().error(f"download failed: {e}")
        sys.exit(1)
    except OSError as e:
        printer().error(f"cannot write to output directory {output}: {e}")
        sys.exit(1)

    printer().done(
        f"Downloaded {len(outcome.written)} files,"
        f" {len(outcome.skipped)} already present, into {output}"
    )
