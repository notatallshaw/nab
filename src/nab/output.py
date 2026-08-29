"""nab's output policy in one place: stream, level, format, and colour.

stdout carries only the requested, machine-readable output (a lockfile, a
requirements list, a config dump).  stderr carries everything a human reads:
the run summary, notes, warnings, errors, progress, and logs.  The verbosity
level and the colour decision are resolved once by :func:`begin` and shared
through a single :class:`Printer`, so no command re-invents the policy.

The design follows uv: a small printer with a level, a data channel that
survives ``--quiet``, and colour applied only to a message's leading token so
the body stays legible with colour stripped.
"""

from __future__ import annotations

import enum
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from typing_extensions import override

from .env import (
    NAB_VERBOSITY,
    color_enabled,
    progress_suppressed,
    verbosity_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "ColorChoice",
    "OutputOptionError",
    "OutputOptions",
    "Printer",
    "ProgressReporter",
    "Verbosity",
    "begin",
    "install_log_handler",
    "logging_level_for",
    "options_from_flags",
    "printer",
    "reset_log_handlers",
    "reset_run",
    "should_color",
    "verbosity_from_counts",
]


class Verbosity(enum.IntEnum):
    """How much nab writes to stderr, from ``-qq`` to ``-vv``.

    The ordering is what the message helpers compare against: a message
    shows when the active verbosity is at or above its threshold.
    """

    SILENT = -2
    QUIET = -1
    NORMAL = 0
    VERBOSE = 1
    DEBUG = 2


def verbosity_from_counts(verbose: int, quiet: int) -> Verbosity:
    """Fold repeated ``-v`` / ``-q`` counts into a clamped :class:`Verbosity`.

    ``verbosity = count(-v) - count(-q)``, clamped to the enum range so
    ``-vvv`` and ``-qqq`` saturate rather than fall off the scale.
    """
    level = max(int(Verbosity.SILENT), min(int(Verbosity.DEBUG), verbose - quiet))
    return Verbosity(level)


class ColorChoice(enum.Enum):
    """The ``--color`` choice: auto-detect, force on, or force off."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


def _isatty(stream: IO[str]) -> bool:
    """Whether ``stream`` is a terminal, tolerant of streams without ``isatty``."""
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if isatty is not None else False


def should_color(
    choice: ColorChoice, stream: IO[str], env: Mapping[str, str] | None = None
) -> bool:
    """Decide whether to colour output on ``stream``.

    The rule itself is :func:`nab.env.color_enabled`; this states it in the
    printer's own terms, a :class:`ColorChoice` and the stream written to.
    Reading the standard 16-colour slots it enables lets the user's own
    terminal theme set the actual contrast.

    ``always`` and ``never`` decide without the stream, so a stream that
    cannot answer ``isatty()`` is never asked.
    """
    tty = choice is ColorChoice.AUTO and _isatty(stream)
    return color_enabled(choice.value, isatty=tty, environ=env)


# Standard ANSI SGR codes; the terminal theme remaps these, so we never
# hardcode 24-bit colours a user's palette cannot override.
_SGR = {
    "red": "31",
    "green": "32",
    "yellow": "33",
    "bold": "1",
    "dim": "2",
}
_RESET = "\033[0m"

# Carriage return plus "erase to end of line": repaints the progress line in
# place and wipes it before any other stderr write.
_CLEAR_LINE = "\r\033[K"


_LOG_LEVELS: dict[Verbosity, int] = {
    Verbosity.SILENT: logging.ERROR,
    Verbosity.QUIET: logging.WARNING,
    Verbosity.NORMAL: logging.WARNING,
    Verbosity.VERBOSE: logging.INFO,
    Verbosity.DEBUG: logging.DEBUG,
}


def logging_level_for(verbosity: Verbosity) -> int:
    """Return the ``logging`` level the engine's records show at.

    Normal and quiet surface engine ``WARNING`` records (the dropped-marker
    and base-attribution notices); ``-v`` adds ``INFO``, ``-vv`` adds
    ``DEBUG``, and ``-qq`` keeps only ``ERROR``.
    """
    return _LOG_LEVELS[verbosity]


class Printer:
    """The single output seam: stream routing, level gating, and colour.

    ``data`` is the stdout channel and always prints, even under ``--quiet``,
    because it is the output the user ran the command to get.  ``error``,
    ``warning``, ``note`` and ``done`` write to stderr, gated on the verbosity
    level, with only the leading token coloured; ``stderr_line`` takes the
    same gate without a token, for text that brings its own.
    """

    def __init__(
        self,
        *,
        verbosity: Verbosity = Verbosity.NORMAL,
        color: ColorChoice = ColorChoice.AUTO,
        progress: bool = True,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Build a printer from the resolved run knobs.

        ``stdout`` / ``stderr`` / ``env`` default to the process streams and
        environment; tests inject their own.  ``progress`` is the
        ``--no-progress`` switch (with ``NAB_NO_PROGRESS``); animated progress
        is additionally gated on the normal level and an stderr terminal.
        """
        self.verbosity = verbosity
        self._out = stdout if stdout is not None else sys.stdout
        self._err = stderr if stderr is not None else sys.stderr
        self.color_enabled = should_color(color, self._err, env)
        self.progress_allowed = (
            progress
            and not progress_suppressed(env)
            and verbosity is Verbosity.NORMAL
            and _isatty(self._err)
        )
        self._err_lock = threading.Lock()
        self._progress_drawn = False

    def _paint(self, token: str, name: str) -> str:
        """Wrap ``token`` in the ``name`` SGR code when colour is on."""
        if not self.color_enabled:
            return token
        return f"\033[{_SGR[name]}m{token}{_RESET}"

    def _emit(self, token: str, color: str, message: str, threshold: Verbosity) -> None:
        """Write ``token message`` to stderr when the level allows it."""
        if self.verbosity < threshold:
            return
        self.stderr_write(f"{self._paint(token, color)} {message}\n")

    def data(self, text: str) -> None:
        """Write requested output to stdout verbatim (the pipeable channel).

        Wipes a live progress line first, since both streams can share a
        terminal and the artefact would otherwise print on top of it.
        """
        self.clear_progress()
        self._out.write(text)

    def error(self, message: str) -> None:
        """Report an error on stderr; shown at every level."""
        self._emit("error:", "red", message, Verbosity.SILENT)

    def warning(self, message: str) -> None:
        """Report a warning on stderr; suppressed only by ``-qq``."""
        self._emit("warning:", "yellow", message, Verbosity.QUIET)

    def note(self, message: str) -> None:
        """Print a normal-level note on stderr."""
        self._emit("note:", "bold", message, Verbosity.NORMAL)

    def done(self, message: str) -> None:
        """Print a normal-level success line, its leading word tinted green.

        The whole line is one message (``Wrote pylock.toml (21 packages)``);
        only the first word is coloured, so the detail stays default-foreground.
        """
        if self.verbosity < Verbosity.NORMAL:
            return
        head, sep, tail = message.partition(" ")
        self.stderr_write(f"{self._paint(head, 'green')}{sep}{tail}\n")

    def stderr_line(self, message: str) -> None:
        """Write a raw normal-level line to stderr with no token or colour.

        For text that carries its own prefix, such as the reproducibility
        notice ``nab lock`` and ``nab config`` emit, where a second leading
        token would read as ``note: notice:``.
        """
        if self.verbosity >= Verbosity.NORMAL:
            self.stderr_write(message)

    def stderr_write(self, text: str) -> None:
        """Write ``text`` to stderr, wiping any live progress line first.

        Takes the same lock as the progress repaints, so a diagnostic never
        interleaves with one.
        """
        with self._err_lock:
            if self._progress_drawn:
                self._err.write(_CLEAR_LINE)
                self._progress_drawn = False
            self._err.write(text)

    def progress_line(self, line: str) -> None:
        """Repaint the live progress line in place, without a newline."""
        with self._err_lock:
            self._err.write(_CLEAR_LINE + line)
            self._err.flush()
            self._progress_drawn = True

    def clear_progress(self) -> None:
        """Wipe the progress line so the next write starts on a clean line."""
        with self._err_lock:
            if self._progress_drawn:
                self._err.write(_CLEAR_LINE)
                self._err.flush()
                self._progress_drawn = False

    def flush_stderr(self) -> None:
        """Flush stderr for a caller writing through :meth:`stderr_write`.

        The stream stays private, so nothing can write around the printer.
        """
        self._err.flush()


_printer: Printer | None = None


def printer() -> Printer:
    """Return the run's :class:`Printer`.

    :func:`begin` installs the one the global output flags resolved to.  A
    caller that bypassed the CLI, as many tests do, gets a fresh default
    printer reading the current process streams.
    """
    return _printer if _printer is not None else Printer()


# The engine logs through ``logging.getLogger(__name__)``; these are the
# top-level names those records live under, so one handler on each captures
# them all without also grabbing third-party logs (urllib3, httpx).
_NAB_LOGGERS = (
    "nab",
    "nab_markersets",
    "nab_provider",
    "nab_project",
    "nab_index",
    "nab_resolver",
)

_LEVEL_TOKENS: dict[int, tuple[str, str]] = {
    logging.WARNING: ("warning:", "yellow"),
    logging.ERROR: ("error:", "red"),
    logging.CRITICAL: ("error:", "red"),
}


class _PrinterStream:
    """File-like wrapper so the log handler writes via :meth:`Printer.stderr_write`."""

    def __init__(self, printer: Printer) -> None:
        self._printer = printer

    def write(self, text: str) -> None:
        self._printer.stderr_write(text)

    def flush(self) -> None:
        self._printer.flush_stderr()


class _NabLogHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker subclass so a re-install can find and drop nab's own handler."""

    def __init__(self, printer: Printer) -> None:
        super().__init__(_PrinterStream(printer))


class _LevelFormatter(logging.Formatter):
    """Format engine log records to match the printer's message shapes.

    At normal verbosity a ``WARNING`` / ``ERROR`` record reads like a printer
    ``warning:`` / ``error:`` line (coloured token, no level noise on lower
    records).  Under ``-v`` it switches to the log-file shape,
    ``LEVEL logger: message``, which is what verbose output is for.
    """

    def __init__(self, *, verbose: bool, color_enabled: bool) -> None:
        super().__init__()
        self._verbose = verbose
        self._color = color_enabled

    @override
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if self._verbose:
            return f"{record.levelname} {record.name}: {message}"
        token = _LEVEL_TOKENS.get(record.levelno)
        if token is None:
            return message
        name, color = token
        if self._color:
            name = f"\033[{_SGR[color]}m{name}{_RESET}"
        return f"{name} {message}"


def _remove_nab_handlers(logger: logging.Logger) -> None:
    logger.handlers = [h for h in logger.handlers if not isinstance(h, _NabLogHandler)]


def install_log_handler(printer: Printer) -> None:
    """Route the engine's ``logging`` records through ``printer``'s stderr.

    One handler is shared across the nab top-level loggers, so engine records
    follow the printer's verbosity instead of Python's uncontrolled
    ``lastResort`` fallback, and reach stderr through the printer so they
    never land on the live progress line.  Idempotent: a prior nab handler is
    dropped first, so calling this again (as the test suite does) does not
    stack handlers.
    """
    level = logging_level_for(printer.verbosity)
    handler = _NabLogHandler(printer)
    handler.setLevel(level)
    handler.setFormatter(
        _LevelFormatter(
            verbose=printer.verbosity >= Verbosity.VERBOSE,
            color_enabled=printer.color_enabled,
        )
    )
    for name in _NAB_LOGGERS:
        logger = logging.getLogger(name)
        _remove_nab_handlers(logger)
        logger.addHandler(handler)
        logger.setLevel(level)


def reset_log_handlers() -> None:
    """Drop nab's log handlers and reset the loggers (test-suite cleanup)."""
    for name in _NAB_LOGGERS:
        logger = logging.getLogger(name)
        _remove_nab_handlers(logger)
        logger.setLevel(logging.NOTSET)


def reset_run() -> None:
    """Undo :func:`begin`: drop the run's printer and its log handlers.

    Both are process-wide, so the test suite clears them between tests.
    """
    global _printer  # noqa: PLW0603 - the run's printer is a module singleton
    _printer = None
    reset_log_handlers()


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_PROGRESS_MIN_INTERVAL = 0.05


class ProgressReporter:
    """The live ``Resolving... N fetched, M pinned`` line on stderr (#35).

    Driven from two threads: the fetcher bumps ``on_fetch`` as listings
    arrive, the resolver bumps ``on_pin`` as it decides packages.  Rendering
    is gated on :attr:`Printer.progress_allowed` (normal level, an stderr
    terminal, progress not switched off), throttled to keep off the hot path,
    and guarded by a lock since the two callers run on different threads.
    When it is not allowed every method is a cheap no-op, so a piped, quiet,
    or verbose run pays nothing and stdout stays clean.  The line is painted
    through :meth:`Printer.progress_line`, so a mid-resolve diagnostic wipes
    it instead of landing on it.
    """

    def __init__(
        self,
        printer: Printer,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = _PROGRESS_MIN_INTERVAL,
    ) -> None:
        """Build a reporter that renders through ``printer``'s stderr."""
        self._enabled = printer.progress_allowed
        self._printer = printer
        self._color = printer.color_enabled
        self._clock = clock
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._fetched = 0
        self._pinned = 0
        self._frame = 0
        self._last = 0.0
        self._painted = False

    def on_fetch(self) -> None:
        """Record one more package listing fetched, then repaint."""
        if not self._enabled:
            return
        with self._lock:
            self._fetched += 1
            self._render()

    def on_pin(self, decided: int) -> None:
        """Record the current count of decided (pinned) packages, then repaint."""
        if not self._enabled:
            return
        with self._lock:
            self._pinned = decided
            self._render()

    def _render(self) -> None:
        """Repaint the line, unless the throttle window has not elapsed."""
        now = self._clock()
        if self._painted and now - self._last < self._min_interval:
            return
        self._last = now
        frame = _SPINNER[self._frame % len(_SPINNER)]
        self._frame += 1
        line = f"{frame} Resolving... {self._fetched} fetched, {self._pinned} pinned"
        if self._color:
            line = f"\033[2m{line}\033[0m"
        self._printer.progress_line(line)
        self._painted = True

    def clear(self) -> None:
        """Wipe the progress line so the next stderr write starts clean."""
        if self._enabled:
            self._printer.clear_progress()


class OutputOptionError(ValueError):
    """A malformed ``--color`` value or ``NAB_VERBOSITY``."""


@dataclass(frozen=True, slots=True)
class OutputOptions:
    """The output knobs parsed from the global flags, before the subcommand."""

    verbosity: Verbosity
    color: ColorChoice
    progress: bool


_VERBOSITY_NAMES: dict[str, Verbosity] = {v.name.lower(): v for v in Verbosity}


def _verbosity_from_env(environ: Mapping[str, str] | None) -> Verbosity | None:
    """Read ``NAB_VERBOSITY`` as a level name, or ``None`` when unset."""
    raw = verbosity_name(environ)
    if raw is None:
        return None
    name = raw.strip().lower()
    if name not in _VERBOSITY_NAMES:
        allowed = ", ".join(_VERBOSITY_NAMES)
        msg = f"{NAB_VERBOSITY}={raw!r} is not one of {allowed}"
        raise OutputOptionError(msg)
    return _VERBOSITY_NAMES[name]


def _color_choice(value: str) -> ColorChoice:
    try:
        return ColorChoice(value)
    except ValueError:
        msg = f"--color {value!r} is not one of auto, always, never"
        raise OutputOptionError(msg) from None


def options_from_flags(
    *,
    verbose: int,
    quiet: int,
    color: str | None,
    no_color: bool,
    no_progress: bool,
    environ: Mapping[str, str] | None = None,
) -> OutputOptions:
    """Fold the five global output flags into the knobs a printer takes.

    A touched ``-v`` or ``-q`` beats ``NAB_VERBOSITY`` even when the two
    cancel out, and ``--color`` beats ``--no-color`` whichever came first,
    because a value names a choice while the flag only refuses one.
    ``color`` is the flag's raw value; one that names no
    :class:`ColorChoice` raises :class:`OutputOptionError`.
    """
    if verbose or quiet:
        verbosity = verbosity_from_counts(verbose, quiet)
    else:
        from_env = _verbosity_from_env(environ)
        verbosity = from_env if from_env is not None else Verbosity.NORMAL

    if color is not None:
        choice = _color_choice(color)
    elif no_color:
        choice = ColorChoice.NEVER
    else:
        choice = ColorChoice.AUTO

    return OutputOptions(verbosity=verbosity, color=choice, progress=not no_progress)


def begin(options: OutputOptions) -> Printer:
    """Start the run's output from the resolved global flags.

    Builds the run's printer, makes it the one :func:`printer` returns, and
    routes the engine's log records through it so a record emitted mid-run
    lands on the same stderr instead of Python's ``lastResort`` fallback.
    """
    global _printer  # noqa: PLW0603 - the run's printer is a module singleton
    _printer = Printer(
        verbosity=options.verbosity, color=options.color, progress=options.progress
    )
    install_log_handler(_printer)
    return _printer
