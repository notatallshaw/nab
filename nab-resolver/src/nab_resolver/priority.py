"""Shared conflict ordering and bounded dependency-precheck feedback."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .types import PackageType

__all__ = [
    "CONFLICT_THRESHOLD",
    "CULPRIT_DEMOTE_THRESHOLD",
    "MAX_PRECHECK_BACKTRACKS",
    "PRECHECK_REJECTION_THRESHOLD",
    "TIER_AFFECTED",
    "TIER_CULPRIT",
    "TIER_NORMAL",
    "compute_tier",
    "is_dominant_culprit",
]


CONFLICT_THRESHOLD = 5
PRECHECK_REJECTION_THRESHOLD = 4
MAX_PRECHECK_BACKTRACKS = 3

# Minimum culprit count and lead required for count-based demotion.
CULPRIT_DEMOTE_THRESHOLD = 5

# Lower number = higher priority.
TIER_AFFECTED = 0
TIER_NORMAL = 1
TIER_CULPRIT = 2


def compute_tier(
    package: PackageType,
    affected_count: int,
    culprit_count: int,
    culprit_counts: Mapping[PackageType, int] | None,
    *,
    force_backtracked: bool = False,
) -> int:
    """Apply conflict promotion and culprit or forced-backtrack demotion."""
    if affected_count >= CONFLICT_THRESHOLD:
        return TIER_AFFECTED
    if force_backtracked:
        return TIER_CULPRIT
    if is_dominant_culprit(package, culprit_count, culprit_counts):
        return TIER_CULPRIT
    return TIER_NORMAL


def is_dominant_culprit(
    package: PackageType,
    package_count: int,
    culprit_counts: Mapping[PackageType, int] | None,
) -> bool:
    """Identify the leading culprit when its gap reaches the demotion threshold."""
    if culprit_counts is None or package_count < CULPRIT_DEMOTE_THRESHOLD:
        return False
    second_highest = max(
        (count for other, count in culprit_counts.items() if other != package),
        default=0,
    )
    return package_count - second_highest >= CULPRIT_DEMOTE_THRESHOLD
