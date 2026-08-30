"""What nab hands nab-project when it asks for a resolve.

nab-project resolves against a list of targets and a
:class:`~nab_project.inputs.ResolveInputs`; these two calls are where the host
turns its :class:`~nab_project.config.NabProjectConfig` into them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_project.config import plan_targets
from nab_project.resolve import inputs_for_build_requirements

if TYPE_CHECKING:
    from nab_project.config import NabProjectConfig
    from nab_project.inputs import ResolveInputs
    from nab_provider.target import ResolveTarget

__all__ = ["resolve_inputs", "resolve_targets"]


def resolve_targets(config: NabProjectConfig) -> tuple[ResolveTarget, ...]:
    """Return the environments ``config`` asks for, one target each."""
    return plan_targets(config)


def resolve_inputs(
    config: NabProjectConfig, *, build_requirements: bool = False
) -> ResolveInputs:
    """Return the settings a resolve of ``config`` runs under.

    A build-requirements run drops the settings that describe a selection
    ``[build-system].requires`` does not have.
    """
    inputs = config.resolve_inputs()
    if build_requirements:
        return inputs_for_build_requirements(inputs)
    return inputs
