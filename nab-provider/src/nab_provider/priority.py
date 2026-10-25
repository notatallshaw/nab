"""Rank resolution decisions using shared conflict and culprit evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping

__all__ = ["compute_tier"]

_Package = TypeVar("_Package", bound="Hashable")

CONFLICT_THRESHOLD = 5

# Demotion requires a runaway gap to the second-highest culprit, so
# co-dominant culprits keep standard ordering.
CULPRIT_DEMOTE_THRESHOLD = 5

# Lower number = higher priority.
TIER_AFFECTED = 0
TIER_NORMAL = 1
TIER_CULPRIT = 2


def compute_tier(
    package: _Package,
    affected_count: int,
    culprit_count: int,
    culprit_counts: Mapping[_Package, int] | None,
    *,
    force_backtracked: bool = False,
) -> int:
    """Decide the priority tier from conflict and culprit counts.

    ``force_backtracked`` short-circuits the gap rule: the look-ahead
    abort is a precise enough culprit signal on its own.
    """
    if affected_count >= CONFLICT_THRESHOLD:
        return TIER_AFFECTED
    if force_backtracked:
        return TIER_CULPRIT
    if _is_dominant_culprit(package, culprit_count, culprit_counts):
        return TIER_CULPRIT
    return TIER_NORMAL


def _is_dominant_culprit(
    package: _Package,
    package_count: int,
    culprit_counts: Mapping[_Package, int] | None,
) -> bool:
    """Return True when ``package`` is the runaway top culprit.

    Demote only when the gap to the next culprit is >= CULPRIT_DEMOTE_THRESHOLD;
    co-dominant culprits stay within ~1 of each other so the standard ordering
    wins.
    """
    if culprit_counts is None or package_count < CULPRIT_DEMOTE_THRESHOLD:
        return False
    second_highest = max(
        (count for other, count in culprit_counts.items() if other != package),
        default=0,
    )
    return package_count - second_highest >= CULPRIT_DEMOTE_THRESHOLD
