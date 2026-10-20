"""Dependency declarations active in a resolved environment."""

from __future__ import annotations

from typing import ClassVar

from .value import Record

__all__ = ["DependencyDeclaration"]


class DependencyDeclaration(Record, frozen=True):
    """One parent's requirement on a dependency in a resolved environment.

    ``dependency_extras`` are requested on the dependency package.
    ``required_for_parent_extras`` lists selected features of the parent that
    require this dependency. Empty means the parent requires it without extras.
    """

    __record_fields__: ClassVar[tuple[str, ...] | None]

    __slots__ = __record_fields__ = (
        "requirement_text",
        "dependency_specifier",
        "parent_name",
        "dependency_name",
        "dependency_extras",
        "requirement_condition",
        "required_for_parent_extras",
    )

    __match_args__ = ("requirement_text", "dependency_specifier")

    requirement_text: str
    dependency_specifier: str
    parent_name: str
    dependency_name: str
    dependency_extras: tuple[str, ...]
    requirement_condition: str | None
    required_for_parent_extras: tuple[str, ...]

    def __init__(
        self,
        requirement_text: str,
        dependency_specifier: str,
        *,
        parent_name: str,
        dependency_name: str,
        dependency_extras: tuple[str, ...] = (),
        requirement_condition: str | None = None,
        required_for_parent_extras: tuple[str, ...] = (),
    ) -> None:
        """Parse a requirement once for repeated dependency selection."""
        object.__setattr__(self, "requirement_text", requirement_text)
        object.__setattr__(self, "dependency_specifier", dependency_specifier)
        object.__setattr__(self, "parent_name", parent_name)
        object.__setattr__(self, "dependency_name", dependency_name)
        object.__setattr__(self, "dependency_extras", dependency_extras)
        object.__setattr__(self, "requirement_condition", requirement_condition)
        object.__setattr__(
            self, "required_for_parent_extras", required_for_parent_extras
        )

    @property
    def required_without_parent_extras(self) -> bool:
        """Whether the parent requires this dependency without selecting an extra."""
        return not self.required_for_parent_extras
