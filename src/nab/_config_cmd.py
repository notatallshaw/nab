"""Read-only ``nab config`` commands for layered settings.

``list`` shows every effective option, ``get`` prints one value, and
``explain`` shows one option's source stack. The registry owns their
rendering; these commands discover the layers and print the result.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ._run import (
    _fail_config,
    command_overrides,
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
    ImplementationFlag,
    MatrixOrderFlag,
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
    project_matrix_python: str | None = None,
    project_matrix_platforms: tuple[str, ...] = (),
    project_matrix_implementations: tuple[str, ...] = (),
    project_matrix_python_order: MatrixOrderFlag | None = None,
    project_matrix_python_patches: tuple[str, ...] = (),
    project_environment_python: str | None = None,
    project_environment_platform: tuple[str, ...] = (),
    project_environment_implementation: ImplementationFlag | None = None,
    sources: bool | None = None,
    workspace_discovery: bool | None = None,
    include_rejected: bool = False,
) -> None:
    """Inspect the effective layered configuration.

    ``list`` shows every option with its value, scope, and origin. ``get``
    prints one value. ``explain`` prints one option's source stack.

    Without ``--include-rejected``, a refused config file is a fatal config
    error. An unknown ``NAB_*`` variable is never fatal: it is
    dropped with a warning.

    With the flag, ``list`` appends refusals. ``explain`` adds a ``rejected``
    row for its key; a refusal without a key appears only on ``list``. ``get``
    still prints only the value.

    Runtime and ``--project-*`` flags layer above files, as they do for run
    commands.
    """
    # Reject an invalid path before discovery can treat it as an absent source.
    require_pyproject_file(path)

    cli_overrides = command_overrides(locals())

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

    notice = project_cli_override_notice(effective, produces_lock=False)
    if notice is not None:
        printer().stderr_line(notice)

    if action == "list":
        printer().data(render_list(effective, rejected=rejected))
        return
    if action not in {"get", "explain"}:  # pragma: no cover - the parser refuses it
        msg = f"unreachable config action {action!r}"
        raise AssertionError(msg)

    _require_key_arg(key, action)
    try:
        if action == "get":
            rendered = render_get(effective, key)
        else:
            rendered = render_explain(effective, key, include_rejected=include_rejected)
    except SourceConfigError as exc:
        _fail_config(exc)
    printer().data(rendered)


def _require_key_arg(key: str, action: str) -> None:
    if not key:
        printer().error(f"`nab config {action}` requires a <key>")
        sys.exit(1)
