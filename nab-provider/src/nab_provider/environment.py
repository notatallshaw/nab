"""Build and evaluate a marker environment for one resolution target."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.markers import (
    UndefinedComparison,
    UndefinedEnvironmentName,
    default_environment,
)

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from nab_provider._vendor.packaging.markers import Marker

__all__ = [
    "EnvironmentSource",
    "UnevaluableMarkerError",
    "evaluate_prepared",
    "host_environment",
    "marker_evaluation_error",
]

EnvironmentSource = Callable[[], Mapping[str, object]]


class UnevaluableMarkerError(ValueError):
    """A dependency marker parses but its environment or operator cannot decide it."""


def marker_evaluation_error(
    marker: Marker, exc: UndefinedComparison | UndefinedEnvironmentName
) -> UnevaluableMarkerError:
    """Return an evaluation error naming the complete dependency marker."""
    return UnevaluableMarkerError(f"marker {marker} cannot be evaluated: {exc}")


def evaluate_prepared(
    marker: Marker, environment: dict[str, str | AbstractSet[str]]
) -> bool:
    """Evaluate a prepared marker environment, naming the marker if evaluation fails."""
    try:
        return marker.evaluate_prepared(environment)
    except (UndefinedComparison, UndefinedEnvironmentName) as exc:
        raise marker_evaluation_error(marker, exc) from exc


def host_environment(
    env_source: EnvironmentSource = default_environment,
) -> dict[str, str]:
    """Return the host's PEP 508 environment, keeping only string-valued entries."""
    return {key: value for key, value in env_source().items() if isinstance(value, str)}
