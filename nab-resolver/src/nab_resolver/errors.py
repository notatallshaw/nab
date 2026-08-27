"""Resolution error types.

Defines the exception the resolver raises when it stops without a
solution, along with the derivation tree for error reporting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import Incompatibility

__all__ = [
    "ResolutionError",
]


class ResolutionError(Exception):
    """Resolution stopped without a solution.

    As raised by this package, ``str(error)`` is the finished report: the
    derivation rendered through the provider's ``narrow_for_display`` and the
    resolver's ``format_range``, so re-rendering it means supplying both again.

    The ``incompatibility`` attribute holds the root of that derivation, and is
    None where the resolver stopped before proving one. Walk ``cause_left`` and
    ``cause_right`` to trace the full proof.

    Two paths raise without a report: exceeding ``max_iterations``, which
    leaves ``incompatibility`` None, and a stalled conflict-resolution loop,
    which attaches one but reports a resolver bug. Neither proves the
    requirements unsatisfiable.

    ``verbose_message`` is the same report at more depth, set by whatever
    augments the error with what the resolve knows beyond the derivation.
    It is None when nothing did, and ``str(error)`` is then all there is.

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
        self.verbose_message: str | None = None
