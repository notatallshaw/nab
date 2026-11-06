"""Rank resolution decisions using shared conflict and culprit evidence."""

from nab_resolver import priority

__all__ = ["compute_tier"]

CONFLICT_THRESHOLD = priority.CONFLICT_THRESHOLD
CULPRIT_DEMOTE_THRESHOLD = priority.CULPRIT_DEMOTE_THRESHOLD
TIER_AFFECTED = priority.TIER_AFFECTED
TIER_CULPRIT = priority.TIER_CULPRIT
TIER_NORMAL = priority.TIER_NORMAL
compute_tier = priority.compute_tier
