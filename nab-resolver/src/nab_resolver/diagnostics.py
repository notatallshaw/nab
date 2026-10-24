"""IO-free diagnostic values and failure-time suggestion rendering for hosts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._compat import override

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["DiagnosticMessage", "Suggestion", "error_message", "suggestion_lines"]


class Suggestion:
    """An actionable correction, with optional context for verbose output."""

    __slots__ = ("action", "detail")

    def __init__(self, action: str, *, detail: str | None = None) -> None:
        """Record the correction and its optional context."""
        self.action = action
        self.detail = detail


def suggestion_lines(
    suggestions: Iterable[Suggestion],
    *,
    verbose: bool = False,
    seen: set[str] | None = None,
    indent: str = "    ",
) -> tuple[str, ...]:
    """Render each action once within the caller's optional emission state."""
    emitted = set() if seen is None else seen
    lines: list[str] = []
    for suggestion in suggestions:
        if suggestion.action in emitted:
            continue
        emitted.add(suggestion.action)
        action, *continuation = suggestion.action.split("\n")
        lines.append(f"{indent}try: {action}")
        lines.extend(f"{indent}     {line}" for line in continuation)
        if verbose and suggestion.detail is not None:
            lines.extend(
                f"{indent}     {line}" for line in suggestion.detail.splitlines()
            )
    return tuple(lines)


class DiagnosticMessage:
    """Keep an error's cause separate from suggestions until its caller renders it."""

    __slots__ = ("message", "suggestions")

    def __init__(
        self, message: str, *, suggestions: tuple[Suggestion, ...] = ()
    ) -> None:
        """Record a cause and its applicable corrections."""
        self.message = message
        self.suggestions = suggestions

    def render(self, *, verbose: bool = False, seen: set[str] | None = None) -> str:
        """Render this message within the caller's optional output lifetime."""
        return "\n".join(
            (
                self.message,
                *suggestion_lines(self.suggestions, verbose=verbose, seen=seen),
            )
        )

    def prefixed(self, prefix: str) -> DiagnosticMessage:
        """Add a caller's cause context without flattening the suggestions."""
        return DiagnosticMessage(prefix + self.message, suggestions=self.suggestions)

    @override
    def __str__(self) -> str:
        """Render without sharing emission state with another message."""
        return self.render()

    @override
    def __repr__(self) -> str:
        """Keep exception representations readable and deterministic."""
        return repr(str(self))


def error_message(error: BaseException) -> DiagnosticMessage:
    """Recover a structured exception argument or preserve ordinary error text."""
    if len(error.args) == 1 and isinstance(error.args[0], DiagnosticMessage):
        return error.args[0]
    return DiagnosticMessage(str(error))
