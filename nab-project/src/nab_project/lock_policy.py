"""Choose how a project lock operation uses an existing output file."""

from __future__ import annotations

import enum

__all__ = ["LockPolicy"]


class LockPolicy(enum.Enum):
    """How a lock operation uses an existing output file."""

    RESOLVE = "resolve"
    """Use ordinary resolution under the current settings."""

    PREFER_EXISTING = "prefer-existing"
    """Check saved choices against current provider facts before fresh fallback."""

    REUSE = "reuse"
    """Keep a consistent supported lock unchanged without remote discovery."""
