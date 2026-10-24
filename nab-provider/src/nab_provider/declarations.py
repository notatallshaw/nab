"""Dependency declarations active in a resolved environment."""

from __future__ import annotations

__lazy_modules__ = ("typing",)

from typing import ClassVar, Final

from .value import HashableRecord

__all__ = ["DependencyDeclaration"]


class DependencyDeclaration(HashableRecord):
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

    requirement_text: Final[str]
    dependency_specifier: Final[str]
    parent_name: Final[str]
    dependency_name: Final[str]
    dependency_extras: Final[tuple[str, ...]]
    requirement_condition: Final[str | None]
    required_for_parent_extras: Final[tuple[str, ...]]

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
        """Retain one normalized dependency declaration."""
        self.requirement_text = requirement_text
        self.dependency_specifier = dependency_specifier
        self.parent_name = parent_name
        self.dependency_name = dependency_name
        self.dependency_extras = dependency_extras
        self.requirement_condition = requirement_condition
        self.required_for_parent_extras = required_for_parent_extras

    @property
    def required_without_parent_extras(self) -> bool:
        """Whether the parent requires this dependency without selecting an extra."""
        return not self.required_for_parent_extras
