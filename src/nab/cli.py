"""Entry point for the nab command: read the line, run it, write the result.

:func:`run` parses ``argv`` with :mod:`nab._cli.spec`. Parsing returns
a page, a refusal, or a command to dispatch. ``run`` returns the process
exit status. The page, refusal, and a command's parting message leave
through :func:`run`'s one write per stream, so output that cannot be
written becomes a status rather than a traceback.

Nothing on this path imports a command module, ``pathlib`` or ``typing``.
The four modules :func:`_load` reaches are loaded only by the line that
needs them, and the command module itself is
:mod:`nab._cli.dispatch`'s to import.
"""

from __future__ import annotations

import io
import os
import sys
import types  # noqa: TC003 - the block TC003 asks for is a runtime import typing

from nab._cli import spec
from nab._cli.parse import Parsed, UsageError, parse
from nab._version import __version__

# Declared rather than imported from ``typing``, which this path never loads.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "console_entry",
    "main",
    "run",
]

# What the pages and the messages call the program.
_PROG = "nab"

# Conventional KeyboardInterrupt exit code: 128 + SIGINT(2).
_SIGINT_EXIT_CODE = 130

# The status CPython exits with when it cannot flush the standard streams,
# and the status this module reports when a write it owns is refused.
_FLUSH_FAILED_EXIT_CODE = 120

# What a line the walk refused exits with.
_USAGE_STATUS = 2

_INTERRUPTED = "error: interrupted\n"

# What ``--color`` says when the line named neither it nor ``--no-color``.
_AUTO = "auto"


def _nothing() -> None:
    """Stand in for the ``resume`` a caller did not supply."""


def _load(name: str) -> types.ModuleType:
    """Import one module the entry path defers, and return it.

    Help, a refusal and dispatch each reach their module through here, so
    a line that asks for none of them pays for none of them. Callers
    annotate what they take back, because a module attribute has no type
    of its own.
    """
    __import__(name)
    return sys.modules[name]


def run(argv: tuple[str, ...], resume: Callable[[], None] = _nothing) -> int:
    """Run one command line and report the status it ends with.

    Write the returned stdout and stderr payloads once each. A failed
    payload write returns status 120 instead of raising.

    ``resume`` runs when the line reaches a command module, and not at
    all on a page or a refusal.
    """
    status, out, err = _outcome(argv, resume)

    try:
        if out:
            sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
    except OSError:
        return _FLUSH_FAILED_EXIT_CODE

    return status


def _outcome(argv: tuple[str, ...], resume: Callable[[], None]) -> tuple[int, str, str]:
    """Read the line and act on it: a status, stdout text, and stderr text."""
    try:
        parsed = parse(argv, spec.ROOT, spec.COMMANDS, _PROG)
    except UsageError as error:
        refusal: str = _load("nab._cli.diagnose").diagnose(
            error, color=_painting(sys.stderr, _color_choice(error.root_options))
        )
        return _USAGE_STATUS, "", refusal

    if parsed.eager == "version":
        return 0, f"{_PROG} {__version__}\n", ""

    if parsed.eager == "help":
        page: str = _load("nab._cli.render").page(
            parsed.command,
            spec.ROOT,
            spec.COMMANDS,
            spec.HELP,
            spec.DISPATCH,
            _PROG,
            color=_painting(sys.stdout, _color_choice(parsed.options)),
        )
        return 0, page, ""

    return _dispatch(parsed, resume)


def _color_choice(options: dict[str, object]) -> str:
    """Read ``--color`` off a short-circuited line, ``--no-color`` behind it.

    A value names a choice while the flag only refuses one, so ``--color``
    wins whichever came first, as it does for the run's own printer.  An
    eager page ends the line before conversion runs, so a token no choice
    names arrives here as typed and paints by the automatic rule.
    """
    choice = options.get("color")
    if isinstance(choice, str):
        return choice
    return "never" if options.get("no_color") else _AUTO


def _painting(stream: object, choice: str) -> bool:
    """Whether to paint ``stream``, by the rule the run's printer also uses.

    Each stream is asked separately, because the page goes to stdout and a
    refusal to stderr, so ``nab --help | less`` is plain while the refusal
    beside it on a terminal is not.
    """
    isatty = getattr(stream, "isatty", None)
    tty = isatty is not None and bool(isatty())
    return bool(_load("nab.env").color_enabled(choice, isatty=tty))


def _dispatch(parsed: Parsed, resume: Callable[[], None]) -> tuple[int, str, str]:
    """Run the command the line named, reporting Ctrl-C as an interrupt."""
    try:
        outcome: tuple[int, str] = _load("nab._cli.dispatch").dispatch(
            parsed, spec.DISPATCH, spec.PATH_DESTS, resume=resume
        )
    except KeyboardInterrupt:
        return _SIGINT_EXIT_CODE, "", _INTERRUPTED

    status, message = outcome
    return status, "", f"{message}\n" if message else ""


def main(argv: list[str] | None = None, resume: Callable[[], None] = _nothing) -> None:
    """Run the CLI over ``argv``, defaulting to the process's own.

    Raises :class:`SystemExit` with the status the run produced, and
    returns normally on success, so a caller that owns the process can
    still do its own teardown.

    ``resume`` runs when the line reaches a command module, and not at
    all on a page or a refusal.
    """
    _replace_closed_std_streams()

    status = run(tuple(sys.argv[1:] if argv is None else argv), resume)
    if _output_was_dropped():
        status = _FLUSH_FAILED_EXIT_CODE

    if status:
        raise SystemExit(status)


def _system_exit_status(code: object) -> int:
    """Map a ``SystemExit`` code to the status the interpreter would exit with.

    ``os._exit`` skips the interpreter's own handling of the exception, so
    a code that is not an integer is read here the way CPython reads it.
    """
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    sys.stderr.write(f"{code}\n")
    return 1


class _ClosedStream(io.StringIO):
    """Stands in for a standard stream CPython left unset.

    ``sys.stdout`` and ``sys.stderr`` are ``None`` when their descriptor was
    closed before the process started. Text written here reaches no
    descriptor; the subclass exists so a buffer nab installed can be told
    apart from one a caller redirected the stream to.
    """

    __slots__ = ()


def _replace_closed_std_streams() -> None:
    """Give each standard stream CPython left unset something to write to."""
    if sys.stdout is None:
        sys.stdout = _ClosedStream()
    if sys.stderr is None:
        sys.stderr = _ClosedStream()


def _output_was_dropped() -> bool:
    """Report whether the run wrote to a stream that could not take it.

    A write advances the buffer's position, so a stream still at zero took
    nothing and lost nothing.
    """
    return any(
        isinstance(stream, _ClosedStream) and stream.tell() > 0
        for stream in (sys.stdout, sys.stderr)
    )


def _flush_std_streams() -> bool:
    """Flush stdout and stderr, reporting whether both landed.

    stderr is flushed even when stdout fails, so a command that could not
    write its result still gets its error out.
    """
    flushed = True

    try:
        sys.stdout.flush()
    except OSError:
        flushed = False

    try:
        sys.stderr.flush()
    except OSError:
        flushed = False

    return flushed


def console_entry(resume: Callable[[], None] = _nothing) -> None:
    """Run the CLI, then end the process without freeing the resolve graph.

    Both ``nab`` and ``python -m nab`` end here; :func:`main` returns
    normally for every other caller. No ``atexit`` hook and no finalizer runs
    after this, so a command has to finish any work it cannot lose before
    :func:`main` returns.

    ``resume`` runs when the line reaches a command module and again on
    the way out, so it has to be safe to call twice.
    """
    status = 0
    try:
        main(resume=resume)
    except SystemExit as exc:
        status = _system_exit_status(exc.code)
    finally:
        resume()

    if not _flush_std_streams():
        status = _FLUSH_FAILED_EXIT_CODE

    os._exit(status)
