"""``nab config`` subcommand: inspect the layered config registry.

Read-only v1.  ``list`` shows every effective option with its value,
scope and origin; ``get`` prints one effective value; ``explain`` prints
the full shadowed stack for one key, the winner marked with a ``>``
gutter.  All three are derived from the registry in
:mod:`nab.config.ladder`; this module only discovers the layers and prints
what the renderers return.  There is no set/unset/edit: v1 never writes
config.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ._run import (
    _cli_overrides,
    _fail_config,
    effective_config,
    require_pyproject_file,
)
from .config.ladder import (
    project_cli_override_notice,
    render_explain,
    render_get,
    render_list,
)
from .config.values import SourceConfigError
from .flagtypes import (  # noqa: TC001 - get_type_hints resolves these at runtime
    BuildPolicyFlag,
    DecisionOrderFlag,
    DistPolicyFlag,
    HttpBackend,
    ModeFlag,
    ResolutionFlag,
)
from .output import printer

if TYPE_CHECKING:
    from .config.ladder import RejectedLayer


def config_command(  # noqa: PLR0913 - one keyword per flag is the public surface
    action: str,
    key: str = "",
    *,
    path: Path = Path("pyproject.toml"),
    project_resolution: ResolutionFlag | None = None,
    offline: bool | None = None,
    cache_dir: Path | None = None,
    http_backend: HttpBackend | None = None,
    max_concurrency: int | None = None,
    project_mode: ModeFlag | None = None,
    project_requires_python: str | None = None,
    project_uploaded_prior_to: str | None = None,
    project_dist_policy: DistPolicyFlag | None = None,
    project_build_policy: BuildPolicyFlag | None = None,
    project_build_requires_depth: int | None = None,
    project_decision_order: DecisionOrderFlag | None = None,
    project_constraint: tuple[str, ...] = (),
    project_default_group: tuple[str, ...] = (),
    project_base_group: str | None = None,
    project_build_group: str | None = None,
    include_rejected: bool = False,
) -> None:
    """Inspect the effective layered configuration.

    ``nab config list`` lists every option with value/scope/origin.
    ``nab config get <key>`` prints one effective value.  ``nab config
    explain <key>`` prints the full shadowed source stack.

    ``--include-rejected`` sits on the command, so every action takes it.
    Without it, a config file that sets an unknown key or a key outside its
    scope is a fatal config error; the flag collects that refusal instead
    and the command runs.  An unknown or renamed ``NAB_*`` var is never
    fatal: it is dropped with a stderr warning, and the flag only moves
    that warning into the collected refusals.

    ``list`` prints the collected refusals in a trailing section, and
    ``explain`` prints a ``rejected`` row only under the key a refusal
    names, so one that names no key shows on ``list`` alone.  ``get``
    prints the value and never a refusal.

    The same per-option flags the run commands accept (the USER
    ``--offline`` / ``--cache-dir`` / ``--http-backend`` /
    ``--max-concurrency`` and the ``--project-*`` PROJECT overrides) layer a
    CLI value on top, so the inspector reflects the same effective values a
    run would see.
    """
    # Validate the pyproject path the same way the run commands do: a
    # --path that is missing, a directory, or not a regular file is a hard
    # error, not a silently-skipped source that prints all-built-in defaults.
    require_pyproject_file(path)

    # _cli_overrides maps each registry row's cli_param to the same-named
    # parameter above, so a new row needs no branch here.
    cli_overrides = _cli_overrides(
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

    rejected: list[RejectedLayer] = []
    try:
        effective = effective_config(
            path,
            cli_overrides=cli_overrides,
            collect_rejected=include_rejected,
            rejected_out=rejected,
        )
    except SourceConfigError as exc:
        _fail_config(exc)

    # Reproducibility: a PROJECT option set on the CLI changes the
    # resolved set, so it is never silent.  This inspector produces no
    # lock, so the notice is worded for inspection (produces_lock=False).
    notice = project_cli_override_notice(effective, produces_lock=False)
    if notice is not None:
        printer().stderr_line(notice)

    if action == "list":
        printer().data(render_list(effective, rejected=rejected))
        return
    if action in {"get", "explain"}:
        _require_key_arg(key, action)
        try:
            if action == "get":
                rendered = render_get(effective, key)
            else:
                rendered = render_explain(
                    effective, key, include_rejected=include_rejected
                )
        except SourceConfigError as exc:
            _fail_config(exc)
        printer().data(rendered)
        return

    printer().error(
        f"unknown config action {action!r}; expected one of 'list', 'get', 'explain'"
    )
    sys.exit(1)


def _require_key_arg(key: str, action: str) -> None:
    if not key:
        printer().error(f"`nab config {action}` requires a <key>")
        sys.exit(1)
