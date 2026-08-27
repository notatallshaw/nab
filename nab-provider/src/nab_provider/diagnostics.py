"""What one package's ``Diagnostics:`` entry says, at both depths.

A failed resolve prints one line per package by default and the whole
record at ``-v``, so the provider hands the host both depths rather than
one baked sentence, and the host prints the depth its verbosity asks for.
"""

from __future__ import annotations

__all__ = ["Diagnostic"]


class Diagnostic:
    """One package's entry in the report a failed resolve appends.

    ``short`` is the single line the default report prints after the
    package name.  ``detail`` replaces the ``try:`` line at ``-v``: one
    clause per cause, then any ``note:`` line.  ``remedy`` is the
    assignment the ``try:`` line states, and is ``None`` where no
    configuration change would admit what was refused.
    """

    __slots__ = ("detail", "remedy", "short")

    def __init__(
        self,
        short: str,
        detail: tuple[str, ...] = (),
        remedy: str | None = None,
    ) -> None:
        """Record ``short`` as the line, with the depth and remedy behind it."""
        self.short = short
        self.detail = detail
        self.remedy = remedy
