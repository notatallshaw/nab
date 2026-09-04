"""Track a stable sweep of queries whose candidates may be discovered later."""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic

if TYPE_CHECKING:
    from collections.abc import Callable

from .types import PackageType


class DeferredChoices(Generic[PackageType]):
    """Remember unavailable packages until resolution or availability progresses."""

    def __init__(self, generation: Callable[[], int]) -> None:
        """Start with no deferred queries at the provider's current generation."""
        self.read_generation = generation
        self.packages: dict[PackageType, None] = {}
        self.generation = generation()
        self.derivations = 0
        self.decisions = 0

    def clear(self) -> None:
        """Start another sweep after a decision, backjump or queued clause."""
        self.packages.clear()

    def refresh(self, derivations: int, decisions: int) -> None:
        """Invalidate failed queries when their allowed ranges or candidates change."""
        generation = self.read_generation()
        if (
            generation != self.generation
            or derivations != self.derivations
            or decisions != self.decisions
        ):
            self.packages.clear()
        self.generation = generation
        self.derivations = derivations
        self.decisions = decisions
