"""Prepare a named plan for the ordinary lock and saved-lock checks."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from nab_project.dependency_inputs import input_table
from nab_project.prepared_input import PreparedProjectInput
from nab_project.resolve import complete_project_requirements

from .config.model import plan_targets, read_pyproject_config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .config.lock_plans import LockPlan


def prepare_lock_input(
    path: Path,
    document: Mapping[str, Any],
    plan: LockPlan,
    *,
    workspace_discovery: bool,
    project_overrides: Mapping[str, object],
) -> PreparedProjectInput:
    """Check declared conflicts before compiling selected requirements into roots."""
    config = read_pyproject_config(
        path,
        discover_workspace=workspace_discovery,
        cli_overrides=project_overrides,
        plan_settings=plan.settings_layer(path, check_conflicts=True),
    )
    roots = complete_project_requirements(
        path,
        document,
        config.resolve_inputs(),
        plan_targets(config),
        groups=plan.groups,
        extras=plan.extras,
        dependencies=plan.dependencies,
        build_requirements=plan.build_requirements,
    )
    requirements = {str(req) for req in roots}
    local_inputs: dict[Path, str | None] = {}
    configuration = path.parent / "nab.toml"
    try:
        local_inputs[configuration] = configuration.read_text(encoding="utf-8")
    except FileNotFoundError:
        local_inputs[configuration] = None
    view = copy.deepcopy(dict(document))
    project = input_table(view.setdefault("project", {}), "project")
    project["dependencies"] = sorted(requirements)
    view.pop("dependency-groups", None)
    return PreparedProjectInput(
        path,
        document,
        view,
        identity={"plan": plan.name},
        local_inputs=local_inputs,
    )
