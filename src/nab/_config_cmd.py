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
    include_rejected: bool = False,
) -> None:
    """Inspect the effective layered configuration.

    ``list`` shows every option with its value, scope, and origin. ``get``
    prints one value. ``explain`` prints one option's source stack.

    Without ``--include-rejected``, a refused config file is a fatal config
    error. An unknown or renamed ``NAB_*`` variable is never fatal: it is
    dropped with a warning.

    With the flag, ``list`` appends refusals. ``explain`` adds a ``rejected``
    row for its key; a refusal without a key appears only on ``list``. ``get``
    still prints only the value.

    Runtime and ``--project-*`` flags layer above files, as they do for run
    commands.
    """
    # Reject an invalid path before discovery can treat it as an absent source.
    require_pyproject_file(path)

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
        cli_matrix_python=project_matrix_python,
        cli_matrix_platforms=project_matrix_platforms,
        cli_matrix_implementations=project_matrix_implementations,
        cli_matrix_python_order=project_matrix_python_order,
        cli_matrix_python_patches=project_matrix_python_patches,
        cli_environment_python=project_environment_python,
        cli_environment_platform=project_environment_platform,
        cli_environment_implementation=project_environment_implementation,
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
