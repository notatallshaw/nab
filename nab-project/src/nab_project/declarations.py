"""Immutable declaration values exported by successful resolves."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DependencyDeclaration"]


@dataclass(frozen=True, slots=True)
class DependencyDeclaration:
    """A normalized declaration and its specifier, detached from parser state."""

    text: str
    specifier: str
