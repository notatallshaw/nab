"""Render terminal resolution diagnostics without repeating corrective actions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_resolver.diagnostics import DiagnosticMessage

from ._compat import override

if TYPE_CHECKING:
    from nab_provider.diagnostics import Diagnostic

__all__ = ["ResolutionMessage"]


class ResolutionMessage(DiagnosticMessage):
    """A solver explanation followed by the provider's per-package evidence."""

    __slots__ = ("entries",)

    def __init__(
        self, message: str, entries: tuple[tuple[str, Diagnostic], ...]
    ) -> None:
        """Retain the solver explanation and attributed provider diagnostics."""
        super().__init__(message)
        self.entries = entries

    @override
    def render(self, *, verbose: bool = False, seen: set[str] | None = None) -> str:
        """Render every cause and emit each correction once."""
        emitted = set() if seen is None else seen
        deeper = not verbose and any(
            diagnostic.has_detail for _, diagnostic in self.entries
        )
        lines = [
            self.message,
            "",
            "Diagnostics: (-v for detail)" if deeper else "Diagnostics:",
        ]
        for package, diagnostic in self.entries:
            summary, *detail = diagnostic.lines(verbose=verbose, seen=emitted)
            lines.append(f"  - {package}: {summary}")
            lines.extend(f"    {line}" for line in detail)
        return "\n".join(lines)

    @override
    def prefixed(self, prefix: str) -> ResolutionMessage:
        """Preserve per-package diagnostics when an outer operation adds context."""
        return ResolutionMessage(prefix + self.message, self.entries)
