"""Package rejection summaries, evidence and corrective actions for hosts."""

from __future__ import annotations

from nab_resolver.diagnostics import DiagnosticMessage, Suggestion, suggestion_lines

from ._compat import override

__all__ = ["Diagnostic", "ResolutionMessage"]


class Diagnostic:
    """One package's summary, evidence and actionable corrections.

    Hosts choose verbosity and own any cross-message emission state. The
    ``remedy`` constructor argument and property preserve the earlier interface.
    """

    __slots__ = ("detail", "short", "suggestions")

    def __init__(
        self,
        short: str,
        detail: tuple[str, ...] = (),
        remedy: str | None = None,
        *,
        suggestions: tuple[Suggestion, ...] = (),
    ) -> None:
        """Record ``short`` as the line, with the depth and remedy behind it."""
        self.short = short
        self.detail = detail
        self.suggestions = (
            (Suggestion(remedy), *suggestions) if remedy is not None else suggestions
        )

    @property
    def remedy(self) -> str | None:
        """Return the primary action for hosts using the earlier interface."""
        return self.suggestions[0].action if self.suggestions else None

    @property
    def has_detail(self) -> bool:
        """Whether verbose output adds evidence or secondary corrections."""
        return bool(
            self.detail
            or len(self.suggestions) > 1
            or any(suggestion.detail for suggestion in self.suggestions)
        )

    def lines(
        self, *, verbose: bool = False, seen: set[str] | None = None
    ) -> tuple[str, ...]:
        """Render the summary followed by evidence and actionable corrections."""
        suggestions = self.suggestions if verbose else self.suggestions[:1]
        return (
            self.short,
            *(self.detail if verbose else ()),
            *suggestion_lines(suggestions, verbose=verbose, seen=seen, indent=""),
        )


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
        if not self.entries:
            return self.message

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
