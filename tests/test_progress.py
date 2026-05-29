"""Tests for the TTY status line reporter."""

from __future__ import annotations

import sys

from nab._progress import StderrProgressReporter


class _FakeStream:
    """A writable stream whose TTY-ness is fixed at construction."""

    def __init__(self, *, tty: bool) -> None:
        self._tty = tty
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class TestStderrProgressReporter:
    def test_renders_distinct_counts_on_tty(self) -> None:
        """Repeated names count once; the line shows both totals."""
        stream = _FakeStream(tty=True)
        reporter = StderrProgressReporter(stream)

        reporter.listing_fetched("a")
        reporter.listing_fetched("a")
        reporter.package_pinned("b")

        assert stream.writes[-1] == "\rResolving... 1 fetched, 1 pinned"

    def test_finish_clears_the_line(self) -> None:
        """finish() erases the rendered line and is idempotent."""
        stream = _FakeStream(tty=True)
        reporter = StderrProgressReporter(stream)
        reporter.listing_fetched("a")

        reporter.finish()
        cleared = stream.writes[-1]
        assert cleared.startswith("\r")
        assert cleared.endswith("\r")
        assert cleared.strip() == ""

        before = len(stream.writes)
        reporter.finish()
        assert len(stream.writes) == before

    def test_no_op_when_not_a_tty(self) -> None:
        """A non-TTY stream gets no carriage-return noise."""
        stream = _FakeStream(tty=False)
        reporter = StderrProgressReporter(stream)

        reporter.listing_fetched("a")
        reporter.package_pinned("b")
        reporter.finish()

        assert stream.writes == []

    def test_finish_without_render_is_silent(self) -> None:
        """finish() with nothing rendered writes nothing, even on a TTY."""
        stream = _FakeStream(tty=True)
        StderrProgressReporter(stream).finish()
        assert stream.writes == []

    def test_defaults_to_stderr(self) -> None:
        """No stream argument targets sys.stderr."""
        assert StderrProgressReporter()._stream is sys.stderr
