"""Live resolve status line for ``nab lock`` / ``nab download``."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TextIO


class StderrProgressReporter:
    """Render a running resolve status line on a TTY stderr.

    Counts distinct fetched and pinned packages and rewrites one
    ``Resolving... N fetched, M pinned`` line with a carriage return.
    Every method is a no-op when the stream is not a TTY, so redirected
    or piped stderr stays free of carriage-return noise.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = self._stream.isatty()
        self._fetched: set[str] = set()
        self._pinned: set[str] = set()
        self._last_len = 0

    def listing_fetched(self, package: str) -> None:
        self._fetched.add(package)
        self._render()

    def package_pinned(self, package: str) -> None:
        self._pinned.add(package)
        self._render()

    def finish(self) -> None:
        """Erase the status line so the next stderr write starts clean."""
        if self._enabled and self._last_len:
            self._stream.write("\r" + " " * self._last_len + "\r")
            self._stream.flush()
            self._last_len = 0

    def _render(self) -> None:
        if not self._enabled:
            return
        line = f"Resolving... {len(self._fetched)} fetched, {len(self._pinned)} pinned"
        self._stream.write("\r" + line)
        self._stream.flush()
        self._last_len = len(line)
