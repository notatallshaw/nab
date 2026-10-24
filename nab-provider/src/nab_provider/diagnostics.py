"""Package rejection summaries, evidence and corrective actions for hosts."""

from __future__ import annotations

from nab_resolver.diagnostics import Suggestion, suggestion_lines

__all__ = ["Diagnostic"]


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
