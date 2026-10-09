"""Dependency declarations active in a resolved environment."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["DependencyDeclaration"]


@dataclass(frozen=True, slots=True)
class DependencyDeclaration:
    """A detached requirement with the parent extras needed to activate it.

    ``extras`` are requested on the child. ``activated_by`` lists matching
    selected extras on the parent; an empty tuple means the requirement applies
    without a parent extra. ``marker`` retains the original condition.
    """

    text: str
    specifier: str
    name: str = field(kw_only=True)
    extras: tuple[str, ...] = field(default=(), kw_only=True)
    marker: str | None = field(default=None, kw_only=True)
    activated_by: tuple[str, ...] = field(default=(), kw_only=True)
