"""Resolution error types.

Defines the exception raised when the resolver proves that no valid
solution exists, along with the derivation tree for error reporting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import Incompatibility

__all__ = [
    "ResolutionError",
]


class ResolutionError(Exception):
    """Resolution failed: no valid solution exists.

    The ``incompatibility`` attribute holds the root incompatibility
    whose derivation tree explains why resolution failed. Walk
    ``cause_left`` and ``cause_right`` to trace the full proof.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#error-reporting
    """

    def __init__(
        self,
        message: str,
        incompatibility: Incompatibility[Any, Any] | None = None,
    ) -> None:
        """Create a resolution error with an optional incompatibility proof."""
        super().__init__(message)
        self.incompatibility = incompatibility
