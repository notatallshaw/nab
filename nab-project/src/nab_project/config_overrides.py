"""Project-setting overrides supplied by an embedding application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config_values import SourceConfigError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from nab_resolver.diagnostics import DiagnosticMessage


class ProjectOverride:
    """Replace one project setting with a raw value and a diagnostic label."""

    __slots__ = ("key", "label", "value")

    def __init__(self, key: str, value: object, label: str) -> None:
        """Record a whole-setting replacement for project interpretation."""
        self.key = key
        self.value = value
        self.label = label


class ProjectTableOverride:
    """Update a project table using the host's field-selection rules."""

    __slots__ = ("apply", "declarations", "key", "label")

    def __init__(
        self,
        key: str,
        apply: Callable[[Mapping[str, Any] | None], object],
        label: str,
        declarations: tuple[str, ...] = (),
    ) -> None:
        """Retain a table transform and the declarations used in diagnostics."""
        self.key = key
        self.apply = apply
        self.label = label
        self.declarations = declarations


class ProjectOverrides:
    """Whole-setting replacements and partial-table updates for one read."""

    __slots__ = ("tables", "values")

    def __init__(
        self,
        values: tuple[ProjectOverride, ...] = (),
        tables: tuple[ProjectTableOverride, ...] = (),
    ) -> None:
        """Keep the host's replacement order and table transforms."""
        self.values = values
        self.tables = tables


class ProjectOverrideError(SourceConfigError):
    """A project override supplies an invalid table value."""

    def __init__(self, key: str, message: DiagnosticMessage | str) -> None:
        """Identify the override so its host can format the failure."""
        super().__init__(message)
        self.key = key
