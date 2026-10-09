"""Dependency declarations active in a resolved environment."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["DependencyDeclaration"]


@dataclass(frozen=True, slots=True)
class DependencyDeclaration:
    """One parent's requirement on a dependency in a resolved environment.

    ``dependency_extras`` are requested on the dependency package.
    ``required_for_parent_extras`` lists selected features of the parent that
    require this dependency. Empty means the parent requires it without extras.
    """

    requirement_text: str
    dependency_specifier: str
    parent_name: str = field(kw_only=True)
    dependency_name: str = field(kw_only=True)
    dependency_extras: tuple[str, ...] = field(default=(), kw_only=True)
    requirement_condition: str | None = field(default=None, kw_only=True)
    required_for_parent_extras: tuple[str, ...] = field(default=(), kw_only=True)

    @property
    def required_without_parent_extras(self) -> bool:
        """Whether the parent requires this dependency without selecting an extra."""
        return not self.required_for_parent_extras
