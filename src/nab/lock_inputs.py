"""Prepare a named plan for the ordinary lock and saved-lock checks."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from nab_index.urls import strip_userinfo
from nab_project.dependency_inputs import input_table
from nab_project.lock_validation import validation_digest
from nab_project.prepared_input import PreparedProjectInput
from nab_project.resolve import ResolveBatch, complete_project_requirements

from .config.model import plan_targets, read_pyproject_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import ExitStack
    from pathlib import Path

    from nab_index.transport import AsyncHttpTransport
    from nab_project.dependency_sources import DependencySourceReader
    from nab_project.inputs import ResolveInputs
    from nab_project.lockfile import LockInput

    from .config.ladder import EffectiveValue
    from .config.lock_plans import LockPlan
    from .output import ProgressReporter


class PlanOutputs:
    """Share unfiltered lock input between formats of the same named selection."""

    def __init__(self, formats: Sequence[str]) -> None:
        """Keep dependency declarations if any writer needs uv edges."""
        self.prepared: PreparedProjectInput | None = None
        self.needs_edges = "uv" in formats
        self.solutions: dict[str, tuple[ResolveInputs, LockInput]] = {}

    def resolve(
        self,
        inputs: ResolveInputs,
        snapshot: Mapping[str, Any],
        solve: Callable[[], LockInput],
        *,
        reconcile: bool,
    ) -> LockInput:
        """Return the graph for matching effective policy, targets and local inputs."""
        # The plan name identifies the output for --locked, not the solver inputs.
        key = validation_digest(
            (
                {k: v for k, v in snapshot.items() if k != "prepared-inputs"},
                reconcile,
            )
        )
        saved = self.solutions.get(key)
        if saved is not None and saved[0] == inputs:
            return saved[1]

        lock = solve()
        self.solutions[key] = (inputs, lock)
        return lock


class LockPlanBatch:
    """Own fetched data and finished selections until the batch's last consumer."""

    def __init__(
        self,
        plans: Sequence[LockPlan],
        format_setting: EffectiveValue,
        stack: ExitStack,
        transport: Callable[[], AsyncHttpTransport],
        progress: ProgressReporter,
    ) -> None:
        """Group matching declared selections before the first writer runs."""
        self.fetch = stack.enter_context(ResolveBatch(transport))
        self.progress = progress
        self.now = datetime.now(timezone.utc)
        groups: dict[str, list[LockPlan]] = defaultdict(list)
        for plan in plans:
            selection = plan._replace(name="", format=None, output=None)
            groups[validation_digest(selection)].append(plan)

        # A unique selection needs only the shared fetcher. Release each saved
        # graph after the last peer has taken its entry, even in interleaved batches.
        self.outputs: dict[str, PlanOutputs] = {}
        for peers in groups.values():
            if len(peers) > 1:
                outputs = PlanOutputs(
                    [plan.effective_format(format_setting) for plan in peers]
                )
                self.outputs.update((plan.name, outputs) for plan in peers)


def prepare_lock_input(
    path: Path,
    document: Mapping[str, object],
    plan: LockPlan,
    *,
    sources: Mapping[str, Mapping[str, object]],
    reader: DependencySourceReader,
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
    identities = {}
    for name, selection in plan.imports.items():
        source = dict(sources[name])
        requirements.update(reader.requirements(source, selection))
        url = source.get("url")
        if isinstance(url, str):
            source["url"] = strip_userinfo(url)
        identities[name] = source

    local_inputs: dict[Path, str | None] = dict(reader.local_inputs)
    configuration = path.parent / "nab.toml"
    try:
        local_inputs[configuration] = configuration.read_text(encoding="utf-8")
    except FileNotFoundError:
        local_inputs[configuration] = None
    view = dict(document)
    project = dict(input_table(view.get("project", {}), "project"))
    view["project"] = project
    project["dependencies"] = sorted(requirements)

    # The plan supplies a complete list even when it excludes runtime dependencies.
    dynamic = project.get("dynamic")
    if isinstance(dynamic, list) and "dependencies" in dynamic:
        project["dynamic"] = [field for field in dynamic if field != "dependencies"]

    view.pop("dependency-groups", None)
    return PreparedProjectInput(
        path,
        document,
        view,
        identity={"plan": plan.name, "sources": identities},
        local_inputs=local_inputs,
    )
