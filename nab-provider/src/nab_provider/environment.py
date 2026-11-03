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
    "classify_dependency",
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
    env = {key: value for key, value in env_source().items() if isinstance(value, str)}
    # Legacy CPython source builds report an empty local version segment.
    if env.get("python_full_version", "").endswith("+"):
        env["python_full_version"] += "local"
    return env


def classify_dependency(
    marker: Marker | None,
    environment: dict[str, str | AbstractSet[str]],
    normalized_extras: AbstractSet[str],
) -> set[str] | None:
    """Classify a dependency against a prepared provider environment.

    Empty means a base dependency; None means inactive. Extras are evaluated
    individually, as the provider's extra proxies evaluate them.
    """
    if marker is None or evaluate_prepared(marker, environment):
        return set()
    if "extra" not in str(marker):
        return None
    env = dict(environment)
    matched: set[str] = set()
    for extra in normalized_extras:
        env["extra"] = extra
        if evaluate_prepared(marker, env):
            matched.add(extra)
    return matched or None
