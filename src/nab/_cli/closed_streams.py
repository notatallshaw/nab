"""Temporary output streams for a command invoked without stdout or stderr."""

from __future__ import annotations

import io
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from typing import TYPE_CHECKING

from .._compat import override

if TYPE_CHECKING:
    from collections.abc import Generator


class _DiscardedOutput(io.StringIO):
    """Record whether any text was written without keeping its contents."""

    def __init__(self) -> None:
        super().__init__()
        self.written = False

    @override
    def write(self, text: str, /) -> int:
        if text:
            self.written = True
        return len(text)


@contextmanager
def redirect_unavailable(
    *, stdout: bool, stderr: bool
) -> Generator[_DiscardedOutput, None, None]:
    """Restore the caller's streams when this invocation finishes."""
    sink = _DiscardedOutput()
    with ExitStack() as stack:
        if stdout:
            stack.enter_context(redirect_stdout(sink))
        if stderr:
            stack.enter_context(redirect_stderr(sink))
        yield sink
