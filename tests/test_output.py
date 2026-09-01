"""Tests for the nab output seam (streams, levels, format, colour)."""

from __future__ import annotations

import io
import logging
import sys

import pytest

from nab import env
from nab.output import (
    ColorChoice,
    OutputOptionError,
    OutputOptions,
    Printer,
    ProgressReporter,
    Verbosity,
    _isatty,
    begin,
    install_log_handler,
    logging_level_for,
    options_from_flags,
    printer,
    reset_log_handlers,
    reset_run,
    should_color,
    verbosity_from_counts,
)

RED = "\033[31m"
RESET = "\033[0m"


def _options(
    *,
    verbose: int = 0,
    quiet: int = 0,
    color: str | None = None,
    no_color: bool = False,
    no_progress: bool = False,
    environ: dict[str, str] | None = None,
) -> OutputOptions:
    """The knobs one command line's global flags fold into.

    The walk reduces every occurrence before dispatch calls this, so the
    cases here pass the reduced values rather than an argv.
    """
    return options_from_flags(
        verbose=verbose,
        quiet=quiet,
        color=color,
        no_color=no_color,
        no_progress=no_progress,
        environ={} if environ is None else environ,
    )


class _TTY(io.StringIO):
    """A StringIO that claims (or denies) being a terminal."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _printer(**kwargs: object) -> tuple[Printer, io.StringIO, io.StringIO]:
    out = io.StringIO()
    err = io.StringIO()
    printer = Printer(stdout=out, stderr=err, **kwargs)  # type: ignore[arg-type]
    return printer, out, err


@pytest.mark.parametrize(
    ("verbose", "quiet", "expected"),
    [
        (0, 0, Verbosity.NORMAL),
        (1, 0, Verbosity.VERBOSE),
        (2, 0, Verbosity.DEBUG),
        (5, 0, Verbosity.DEBUG),
        (0, 1, Verbosity.QUIET),
        (0, 2, Verbosity.SILENT),
        (0, 5, Verbosity.SILENT),
        (1, 1, Verbosity.NORMAL),
        (2, 1, Verbosity.VERBOSE),
    ],
)
def test_verbosity_from_counts(verbose: int, quiet: int, expected: Verbosity) -> None:
    assert verbosity_from_counts(verbose, quiet) is expected


def test_isatty_present_true() -> None:
    assert _isatty(_TTY(tty=True)) is True


def test_isatty_present_false() -> None:
    assert _isatty(_TTY(tty=False)) is False


def test_isatty_absent() -> None:
    class NoTty:
        pass

    assert _isatty(NoTty()) is False  # type: ignore[arg-type]


def test_should_color_always_beats_env() -> None:
    assert should_color(ColorChoice.ALWAYS, _TTY(tty=False), {"NO_COLOR": "1"}) is True


def test_should_color_never_beats_tty() -> None:
    assert should_color(ColorChoice.NEVER, _TTY(tty=True), {}) is False


def test_should_color_no_color_disables() -> None:
    assert should_color(ColorChoice.AUTO, _TTY(tty=True), {"NO_COLOR": "1"}) is False


def test_should_color_empty_no_color_is_ignored() -> None:
    assert should_color(ColorChoice.AUTO, _TTY(tty=True), {"NO_COLOR": ""}) is True


def test_should_color_force_color_enables() -> None:
    assert should_color(ColorChoice.AUTO, _TTY(tty=False), {"FORCE_COLOR": "1"}) is True


def test_should_color_term_dumb_disables() -> None:
    assert should_color(ColorChoice.AUTO, _TTY(tty=True), {"TERM": "dumb"}) is False


def test_should_color_auto_follows_tty() -> None:
    assert should_color(ColorChoice.AUTO, _TTY(tty=True), {}) is True
    assert should_color(ColorChoice.AUTO, _TTY(tty=False), {}) is False


_COLOR_TABLE = [
    ("always", False, {"NO_COLOR": "1"}, True),
    ("never", True, {}, False),
    ("auto", True, {"NO_COLOR": "1"}, False),
    ("auto", True, {"NO_COLOR": ""}, True),
    ("auto", False, {"FORCE_COLOR": "1"}, True),
    ("auto", False, {"FORCE_COLOR": ""}, False),
    ("auto", True, {"NO_COLOR": "1", "FORCE_COLOR": "1"}, False),
    ("auto", True, {"TERM": "dumb"}, False),
    ("auto", True, {"FORCE_COLOR": "1", "TERM": "dumb"}, True),
    ("auto", True, {}, True),
    ("auto", False, {}, False),
]


@pytest.mark.parametrize(("choice", "isatty", "environ", "expected"), _COLOR_TABLE)
def test_color_enabled_table(
    choice: str, isatty: bool, environ: dict[str, str], expected: bool
) -> None:
    """The colour rule, one row per way a value can decide it."""
    assert env.color_enabled(choice, isatty=isatty, environ=environ) is expected


@pytest.mark.parametrize(("choice", "isatty", "environ", "expected"), _COLOR_TABLE)
def test_should_color_agrees_with_color_enabled(
    choice: str, isatty: bool, environ: dict[str, str], expected: bool
) -> None:
    """The wrapper adds the printer's types and nothing else.

    Two implementations of one rule is the failure this pairing exists to
    catch, since ``should_color`` is what every printer goes through.
    """
    assert should_color(ColorChoice(choice), _TTY(tty=isatty), environ) is expected


@pytest.mark.parametrize("choice", [ColorChoice.ALWAYS, ColorChoice.NEVER])
def test_should_color_never_asks_a_stream_it_does_not_need(
    choice: ColorChoice,
) -> None:
    """``always`` and ``never`` answer without touching the stream.

    A closed stream still has an ``isatty`` to call, and calling it
    raises, so the guard has to be the choice rather than the attribute.
    """
    closed = io.StringIO()
    closed.close()

    assert should_color(choice, closed, {}) is (choice is ColorChoice.ALWAYS)


def test_color_enabled_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert env.color_enabled("auto", isatty=True) is False

    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert env.color_enabled("auto", isatty=False) is True


@pytest.mark.parametrize(
    ("color", "no_color", "expected"),
    [
        ("always", True, ColorChoice.ALWAYS),
        ("never", True, ColorChoice.NEVER),
        ("auto", True, ColorChoice.AUTO),
        (None, True, ColorChoice.NEVER),
        (None, False, ColorChoice.AUTO),
    ],
)
def test_color_flag_precedence(
    color: str | None, no_color: bool, expected: ColorChoice
) -> None:
    """``--color`` beats ``--no-color``, which the walk cannot decide.

    Both flags reduce to a value before they reach here, so the order they
    were written in is the walk's business and the precedence is this
    function's.
    """
    options = _options(color=color, no_color=no_color)

    assert options.color is expected


@pytest.mark.parametrize(
    ("verbosity", "level"),
    [
        (Verbosity.SILENT, logging.ERROR),
        (Verbosity.QUIET, logging.WARNING),
        (Verbosity.NORMAL, logging.WARNING),
        (Verbosity.VERBOSE, logging.INFO),
        (Verbosity.DEBUG, logging.DEBUG),
    ],
)
def test_logging_level_for(verbosity: Verbosity, level: int) -> None:
    assert logging_level_for(verbosity) == level


def test_data_always_prints_even_when_silent() -> None:
    printer, out, err = _printer(verbosity=Verbosity.SILENT)
    printer.data("pylock bytes")
    assert out.getvalue() == "pylock bytes"
    assert err.getvalue() == ""


def test_error_shows_at_silent() -> None:
    printer, _out, err = _printer(verbosity=Verbosity.SILENT)
    printer.error("boom")
    assert err.getvalue() == "error: boom\n"


def test_warning_hidden_at_silent_shown_at_quiet() -> None:
    printer, _out, err = _printer(verbosity=Verbosity.SILENT)
    printer.warning("careful")
    assert err.getvalue() == ""
    printer, _out, err = _printer(verbosity=Verbosity.QUIET)
    printer.warning("careful")
    assert err.getvalue() == "warning: careful\n"


def test_note_and_done_hidden_at_quiet_shown_at_normal() -> None:
    printer, _out, err = _printer(verbosity=Verbosity.QUIET)
    printer.note("heads up")
    printer.done("Wrote pylock.toml")
    assert err.getvalue() == ""
    printer, _out, err = _printer(verbosity=Verbosity.NORMAL)
    printer.note("heads up")
    printer.done("Wrote pylock.toml")
    assert err.getvalue() == "note: heads up\nWrote pylock.toml\n"


def test_done_single_word_message() -> None:
    printer, _out, err = _printer()
    printer.done("Done")
    assert err.getvalue() == "Done\n"


def test_colour_paints_leading_token_only() -> None:
    printer, _out, err = _printer(color=ColorChoice.ALWAYS)
    printer.error("cannot lock")
    assert err.getvalue() == f"{RED}error:{RESET} cannot lock\n"


def test_no_colour_leaves_plain_text() -> None:
    printer, _out, err = _printer(color=ColorChoice.NEVER)
    printer.error("cannot lock")
    assert err.getvalue() == "error: cannot lock\n"
    assert RED not in err.getvalue()


def test_stderr_line_gated_on_normal() -> None:
    printer, _out, err = _printer(verbosity=Verbosity.QUIET)
    printer.stderr_line("# tuple block\n")
    assert err.getvalue() == ""
    printer, _out, err = _printer(verbosity=Verbosity.NORMAL)
    printer.stderr_line("# tuple block\n")
    assert err.getvalue() == "# tuple block\n"


def test_progress_allowed_only_at_normal_on_tty() -> None:
    err = _TTY(tty=True)
    printer = Printer(stderr=err, verbosity=Verbosity.NORMAL, env={})
    assert printer.progress_allowed is True


def test_progress_blocked_when_not_tty() -> None:
    printer = Printer(stderr=_TTY(tty=False), verbosity=Verbosity.NORMAL, env={})
    assert printer.progress_allowed is False


def test_progress_blocked_under_verbose() -> None:
    printer = Printer(stderr=_TTY(tty=True), verbosity=Verbosity.VERBOSE, env={})
    assert printer.progress_allowed is False


def test_progress_blocked_by_flag() -> None:
    printer = Printer(
        stderr=_TTY(tty=True), verbosity=Verbosity.NORMAL, progress=False, env={}
    )
    assert printer.progress_allowed is False


def test_progress_blocked_by_env() -> None:
    printer = Printer(
        stderr=_TTY(tty=True),
        verbosity=Verbosity.NORMAL,
        env={"NAB_NO_PROGRESS": "1"},
    )
    assert printer.progress_allowed is False


def test_defaults_use_process_streams() -> None:
    printer = Printer(env={})
    assert printer._out is sys.stdout
    assert printer._err is sys.stderr


def test_counts_fold_into_a_verbosity() -> None:
    assert _options(verbose=2).verbosity is Verbosity.DEBUG
    assert _options(quiet=2).verbosity is Verbosity.SILENT


def test_a_cancelled_pair_is_normal() -> None:
    assert _options(verbose=1, quiet=1).verbosity is Verbosity.NORMAL


def test_color_value_wins_over_the_shorthand() -> None:
    assert _options(color="always").color is ColorChoice.ALWAYS
    assert _options(no_color=True).color is ColorChoice.NEVER


def test_progress_is_on_unless_the_flag_says_otherwise() -> None:
    assert _options().progress is True
    assert _options(no_progress=True).progress is False


def test_default_color_is_auto() -> None:
    assert _options().color is ColorChoice.AUTO


def test_a_color_value_outside_the_set_is_refused() -> None:
    """The walk pins ``--color``'s choices, and this is the second gate."""
    with pytest.raises(OutputOptionError, match="auto, always, never"):
        _options(color="rainbow")


def test_verbosity_comes_from_the_environment_when_no_flag_was_touched() -> None:
    assert _options(environ={"NAB_VERBOSITY": "debug"}).verbosity is Verbosity.DEBUG


def test_flags_beat_env_verbosity() -> None:
    options = _options(quiet=1, environ={"NAB_VERBOSITY": "debug"})

    assert options.verbosity is Verbosity.QUIET


def test_a_touched_counter_beats_env_verbosity_even_when_it_cancels() -> None:
    """``-v -q`` is NORMAL, not DEBUG: the test is touched, not non-zero."""
    options = _options(verbose=1, quiet=1, environ={"NAB_VERBOSITY": "debug"})

    assert options.verbosity is Verbosity.NORMAL


def test_bad_env_verbosity() -> None:
    with pytest.raises(OutputOptionError, match="NAB_VERBOSITY"):
        _options(environ={"NAB_VERBOSITY": "loud"})


def _emit_record(name: str, level: int, message: str) -> None:
    logging.getLogger(name).log(level, message)


def _handler_printer(
    verbosity: Verbosity = Verbosity.NORMAL,
    *,
    color: ColorChoice = ColorChoice.NEVER,
) -> tuple[Printer, io.StringIO]:
    stream = io.StringIO()
    printer = Printer(verbosity=verbosity, color=color, stderr=stream, env={})
    return printer, stream


def test_log_handler_normal_prefixes_warning() -> None:
    printer, stream = _handler_printer()
    try:
        install_log_handler(printer)
        _emit_record("nab_project.demo", logging.WARNING, "dropped marker")
        _emit_record("nab_project.demo", logging.INFO, "hidden at normal")
    finally:
        reset_log_handlers()
    assert stream.getvalue() == "warning: dropped marker\n"


def test_log_handler_colours_token() -> None:
    printer, stream = _handler_printer(color=ColorChoice.ALWAYS)
    try:
        install_log_handler(printer)
        _emit_record("nab_index.demo", logging.ERROR, "boom")
    finally:
        reset_log_handlers()
    assert stream.getvalue() == f"{RED}error:{RESET} boom\n"


def test_log_handler_verbose_shows_info_with_source() -> None:
    printer, stream = _handler_printer(Verbosity.VERBOSE)
    try:
        install_log_handler(printer)
        _emit_record("nab_project.demo", logging.INFO, "fetching")
    finally:
        reset_log_handlers()
    assert stream.getvalue() == "INFO nab_project.demo: fetching\n"


def test_log_handler_reinstall_does_not_stack() -> None:
    printer, stream = _handler_printer()
    try:
        install_log_handler(printer)
        install_log_handler(printer)
        _emit_record("nab_resolver.demo", logging.WARNING, "once")
    finally:
        reset_log_handlers()
    assert stream.getvalue() == "warning: once\n"


def test_log_handler_untokened_level_is_bare() -> None:
    printer, stream = _handler_printer()
    try:
        install_log_handler(printer)
        _emit_record("nab_project.demo", logging.WARNING + 5, "custom level")
    finally:
        reset_log_handlers()
    assert stream.getvalue() == "custom level\n"


def test_reset_log_handlers_detaches() -> None:
    handler_printer, stream = _handler_printer()
    install_log_handler(handler_printer)
    reset_log_handlers()
    _emit_record("nab_project.demo", logging.WARNING, "after reset")
    assert stream.getvalue() == ""


def _begin_run(*, quiet: int = 0) -> Printer:
    """Start a run's output the way the CLI does, with colour off."""
    return begin(
        options_from_flags(
            verbose=0,
            quiet=quiet,
            color="never",
            no_color=False,
            no_progress=True,
            environ={},
        )
    )


def test_begin_installs_the_run_printer() -> None:
    started = _begin_run(quiet=1)

    assert printer() is started
    assert started.verbosity is Verbosity.QUIET


def test_begin_routes_log_records_through_the_run_printer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A record logged mid-run reads as a printer line.

    ``logging.lastResort`` would write the bare message, so the token is
    what says the handler is installed.
    """
    _begin_run()
    _emit_record("nab_project.demo", logging.WARNING, "mid-run")

    assert capsys.readouterr().err == "warning: mid-run\n"


def test_reset_run_drops_the_run_printer() -> None:
    started = _begin_run(quiet=1)
    reset_run()

    assert printer() is not started


def test_reset_run_detaches_the_log_handler() -> None:
    handler_printer, stream = _handler_printer()
    install_log_handler(handler_printer)
    reset_run()
    _emit_record("nab_project.demo", logging.WARNING, "after reset")

    assert stream.getvalue() == ""


def _enabled_reporter(
    *,
    color: ColorChoice = ColorChoice.NEVER,
    clock: object = None,
    min_interval: float = 0.05,
) -> tuple[ProgressReporter, _TTY]:
    err = _TTY(tty=True)
    printer = Printer(stderr=err, verbosity=Verbosity.NORMAL, color=color, env={})
    reporter = ProgressReporter(
        printer,
        clock=clock if clock is not None else (lambda: 0.0),  # type: ignore[arg-type]
        min_interval=min_interval,
    )
    return reporter, err


def test_progress_disabled_off_tty_is_noop() -> None:
    err = _TTY(tty=False)
    printer = Printer(stderr=err, verbosity=Verbosity.NORMAL, env={})
    reporter = ProgressReporter(printer)
    reporter.on_fetch()
    reporter.on_pin(2)
    reporter.clear()
    assert err.getvalue() == ""


def test_progress_renders_counter_line() -> None:
    reporter, err = _enabled_reporter()
    reporter.on_fetch()
    out = err.getvalue()
    assert out.startswith("\r\033[K")
    assert "Resolving... 1 fetched, 0 pinned" in out


def test_progress_pin_replaces_the_count() -> None:
    times = iter([0.0, 10.0, 20.0])
    reporter, err = _enabled_reporter(clock=lambda: next(times), min_interval=1.0)
    reporter.on_fetch()
    reporter.on_pin(4)
    assert err.getvalue().endswith("1 fetched, 4 pinned")

    reporter.on_pin(2)
    assert err.getvalue().endswith("1 fetched, 2 pinned")


def test_progress_fetch_counts_every_call() -> None:
    times = iter([0.0, 10.0])
    reporter, err = _enabled_reporter(clock=lambda: next(times), min_interval=1.0)
    reporter.on_fetch()
    reporter.on_fetch()
    assert err.getvalue().endswith("2 fetched, 0 pinned")


def test_progress_throttle_skips_rapid_repaint() -> None:
    times = iter([5.0, 5.5, 7.0])
    reporter, err = _enabled_reporter(clock=lambda: next(times), min_interval=1.0)
    reporter.on_fetch()
    first = err.getvalue()
    reporter.on_fetch()
    assert err.getvalue() == first

    reporter.on_fetch()
    assert err.getvalue().endswith("3 fetched, 0 pinned")


def test_progress_throttled_pin_shows_on_next_repaint() -> None:
    times = iter([5.0, 5.5, 7.0])
    reporter, err = _enabled_reporter(clock=lambda: next(times), min_interval=1.0)
    reporter.on_fetch()
    first = err.getvalue()
    reporter.on_pin(5)
    assert err.getvalue() == first

    reporter.on_fetch()
    assert err.getvalue().endswith("2 fetched, 5 pinned")


def test_progress_colour_dims_the_line() -> None:
    reporter, err = _enabled_reporter(color=ColorChoice.ALWAYS)
    reporter.on_fetch()
    assert "\033[2m" in err.getvalue()


def test_progress_clear_wipes_then_is_noop() -> None:
    reporter, err = _enabled_reporter()
    reporter.on_fetch()
    err.truncate(0)
    err.seek(0)
    reporter.clear()
    assert err.getvalue() == "\r\033[K"
    err.truncate(0)
    err.seek(0)
    reporter.clear()
    assert err.getvalue() == ""


def _live_progress_printer() -> tuple[Printer, _TTY]:
    err = _TTY(tty=True)
    printer = Printer(
        stderr=err, verbosity=Verbosity.NORMAL, color=ColorChoice.NEVER, env={}
    )
    return printer, err


def test_printer_message_wipes_live_progress_line() -> None:
    printer, err = _live_progress_printer()
    reporter = ProgressReporter(printer, clock=lambda: 0.0)
    reporter.on_fetch()
    printer.warning("metadata cannot be parsed")
    assert "pinnedwarning:" not in err.getvalue()
    assert err.getvalue().endswith("\r\033[Kwarning: metadata cannot be parsed\n")


def test_stdout_data_wipes_live_progress_line() -> None:
    """The artefact shares a terminal with the progress line it must not land on.

    ``nab lock --output -`` paints progress on stderr while the lock goes
    to stdout, so ``data`` wipes the line the way a stderr message does.
    """
    out = io.StringIO()
    err = _TTY(tty=True)
    printer = Printer(
        stdout=out,
        stderr=err,
        verbosity=Verbosity.NORMAL,
        color=ColorChoice.NEVER,
        env={},
    )
    reporter = ProgressReporter(printer, clock=lambda: 0.0)
    reporter.on_fetch()

    printer.data('lock-version = "1.0"\n')

    assert err.getvalue().endswith("\r\033[K")
    assert out.getvalue() == 'lock-version = "1.0"\n'


def test_log_record_wipes_live_progress_line() -> None:
    printer, err = _live_progress_printer()
    reporter = ProgressReporter(printer, clock=lambda: 0.0)
    try:
        install_log_handler(printer)
        reporter.on_fetch()
        _emit_record("nab_project.demo", logging.WARNING, "offline skip")
    finally:
        reset_log_handlers()
    assert "pinnedwarning:" not in err.getvalue()
    assert err.getvalue().endswith("\r\033[Kwarning: offline skip\n")


def test_progress_repaints_after_diagnostic() -> None:
    times = iter([0.0, 10.0])
    printer, err = _live_progress_printer()
    reporter = ProgressReporter(printer, clock=lambda: next(times), min_interval=1.0)
    reporter.on_fetch()
    printer.warning("careful")
    reporter.on_pin(1)
    assert err.getvalue().endswith("\r\033[K⠙ Resolving... 1 fetched, 1 pinned")


class TestColourPrecedence:
    """The order the three variables are read in, as the CLI page states it."""

    _PLAIN = _TTY(tty=False)

    def test_no_color_beats_force_color(self) -> None:
        env = {"NO_COLOR": "1", "FORCE_COLOR": "1"}
        assert should_color(ColorChoice.AUTO, self._PLAIN, env) is False

    def test_force_color_beats_a_dumb_terminal(self) -> None:
        env = {"FORCE_COLOR": "1", "TERM": "dumb"}
        assert should_color(ColorChoice.AUTO, self._PLAIN, env) is True

    def test_force_color_zero_still_forces(self) -> None:
        assert should_color(ColorChoice.AUTO, self._PLAIN, {"FORCE_COLOR": "0"}) is True

    def test_a_dumb_terminal_disables_on_its_own(self) -> None:
        assert should_color(ColorChoice.AUTO, self._PLAIN, {"TERM": "dumb"}) is False
