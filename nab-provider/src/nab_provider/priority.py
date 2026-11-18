"""Compatibility imports for conflict-driven decision priority."""

from nab_resolver import priority as _priority

__all__ = ["compute_tier"]

compute_tier = _priority.compute_tier
CONFLICT_THRESHOLD = _priority.CONFLICT_THRESHOLD
CULPRIT_DEMOTE_THRESHOLD = _priority.CULPRIT_DEMOTE_THRESHOLD
TIER_AFFECTED = _priority.TIER_AFFECTED
TIER_NORMAL = _priority.TIER_NORMAL
TIER_CULPRIT = _priority.TIER_CULPRIT
