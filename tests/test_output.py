"""Tests for the nab output seam (streams, levels, format, colour)."""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest

from nab.output import (
    ColorChoice,
    OutputOptionError,
    Printer,
    ProgressReporter,
    Verbosity,
    _isatty,
    install_log_handler,
    logging_level_for,
    parse_output_options,
    reset_log_handlers,
    should_color,
    verbosity_from_counts,
)

RED = "\033[31m"
RESET = "\033[0m"


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


def test_parse_counts_and_passthrough() -> None:
    opts, rest = parse_output_options(["lock", "-vv", "pyproject.toml"], {})
    assert opts.verbosity is Verbosity.DEBUG
    assert rest == ["lock", "pyproject.toml"]


def test_parse_quiet_counts() -> None:
    opts, rest = parse_output_options(["-q", "-q", "lock"], {})
    assert opts.verbosity is Verbosity.SILENT
    assert rest == ["lock"]


def test_parse_long_verbose_quiet() -> None:
    opts, _rest = parse_output_options(["--verbose", "--quiet", "lock"], {})
    assert opts.verbosity is Verbosity.NORMAL


def test_parse_color_value_form() -> None:
    opts, rest = parse_output_options(["--color", "always", "lock"], {})
    assert opts.color is ColorChoice.ALWAYS
    assert rest == ["lock"]


def test_parse_color_equals_form() -> None:
    opts, _rest = parse_output_options(["--color=never", "lock"], {})
    assert opts.color is ColorChoice.NEVER


def test_parse_no_color() -> None:
    opts, _rest = parse_output_options(["--no-color", "lock"], {})
    assert opts.color is ColorChoice.NEVER


def test_parse_no_progress() -> None:
    opts, _rest = parse_output_options(["--no-progress", "lock"], {})
    assert opts.progress is False


def test_parse_default_color_is_auto() -> None:
    opts, _rest = parse_output_options(["lock"], {})
    assert opts.color is ColorChoice.AUTO


def test_parse_color_missing_value() -> None:
    with pytest.raises(OutputOptionError, match="needs a value"):
        parse_output_options(["--color"], {})


def test_parse_color_bad_value() -> None:
    with pytest.raises(OutputOptionError, match="auto, always, never"):
        parse_output_options(["--color", "rainbow"], {})


def test_parse_verbosity_from_env() -> None:
    opts, _rest = parse_output_options(["lock"], {"NAB_VERBOSITY": "debug"})
    assert opts.verbosity is Verbosity.DEBUG


def test_flags_beat_env_verbosity() -> None:
    opts, _rest = parse_output_options(["-q", "lock"], {"NAB_VERBOSITY": "debug"})
    assert opts.verbosity is Verbosity.QUIET


def test_parse_bad_env_verbosity() -> None:
    with pytest.raises(OutputOptionError, match="NAB_VERBOSITY"):
        parse_output_options(["lock"], {"NAB_VERBOSITY": "loud"})


def test_short_dash_v_not_confused_with_capital() -> None:
    opts, rest = parse_output_options(["-V", "lock"], {})
    assert opts.verbosity is Verbosity.NORMAL
    assert rest == ["-V", "lock"]


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
        _emit_record("nab_python.demo", logging.WARNING, "dropped marker")
        _emit_record("nab_python.demo", logging.INFO, "hidden at normal")
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
        _emit_record("nab_python.demo", logging.INFO, "fetching")
    finally:
        reset_log_handlers()
    assert stream.getvalue() == "INFO nab_python.demo: fetching\n"


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
        _emit_record("nab_python.demo", logging.WARNING + 5, "custom level")
    finally:
        reset_log_handlers()
    assert stream.getvalue() == "custom level\n"


def test_reset_log_handlers_detaches() -> None:
    printer, stream = _handler_printer()
    install_log_handler(printer)
    reset_log_handlers()
    _emit_record("nab_python.demo", logging.WARNING, "after reset")
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


def test_progress_pin_updates_line() -> None:
    times = iter([0.0, 10.0])
    reporter, err = _enabled_reporter(clock=lambda: next(times), min_interval=1.0)
    reporter.on_fetch()
    reporter.on_pin(4)
    assert "1 fetched, 4 pinned" in err.getvalue()


def test_progress_throttle_skips_rapid_repaint() -> None:
    reporter, err = _enabled_reporter(clock=lambda: 5.0, min_interval=1.0)
    reporter.on_fetch()
    first = err.getvalue()
    reporter.on_fetch()
    assert err.getvalue() == first


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


def test_log_record_wipes_live_progress_line() -> None:
    printer, err = _live_progress_printer()
    reporter = ProgressReporter(printer, clock=lambda: 0.0)
    try:
        install_log_handler(printer)
        reporter.on_fetch()
        _emit_record("nab_python.demo", logging.WARNING, "offline skip")
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


_CLI_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "reference" / "cli.md"

_SUBCOMMANDS = ("lock", "download", "config", "cache")


def _cli_reference_text() -> str:
    return _CLI_REFERENCE.read_text(encoding="utf-8")


def _section_body(text: str, heading: str) -> str:
    after = text.partition(f"\n{heading}\n")[2]
    return after.partition("\n## ")[0]


class TestCliReferenceDocumentsOutputPolicy:
    """The CLI reference must list every output-policy flag and env var the CLI accepts.

    ``parse_output_options`` defines the flags and ``Printer`` reads the env vars.
    """

    def test_verbosity_flags_documented(self) -> None:
        text = _cli_reference_text()
        for flag in ("-v", "-vv", "-q", "-qq", "--verbose", "--quiet"):
            assert f"`{flag}`" in text, f"CLI reference omits verbosity flag {flag}"

    def test_color_flags_documented(self) -> None:
        text = _cli_reference_text()
        assert "`--color`" in text
        assert "`--no-color`" in text
        for choice in ColorChoice:
            assert f"`{choice.value}`" in text, (
                f"CLI reference omits --color value {choice.value}"
            )

    def test_progress_documented(self) -> None:
        text = _cli_reference_text()
        assert "`--no-progress`" in text
        assert "Resolving" in text

    def test_output_env_vars_documented(self) -> None:
        text = _cli_reference_text()
        for var in ("NAB_VERBOSITY", "NAB_NO_PROGRESS", "NO_COLOR", "FORCE_COLOR"):
            assert var in text, f"CLI reference omits env var {var}"

    def test_nab_verbosity_values_documented(self) -> None:
        text = _cli_reference_text()
        for level in Verbosity:
            name = level.name.lower()
            assert f"`{name}`" in text, (
                f"CLI reference omits NAB_VERBOSITY value {name!r}"
            )

    def test_output_control_scope_covers_every_subcommand(self) -> None:
        """The scope paragraph must name every subcommand the flags reach.

        ``main`` extracts a global ``-q`` before dispatching to any
        subcommand, so the doc's enumeration must include ``cache``.
        """
        for sub in _SUBCOMMANDS:
            _opts, rest = parse_output_options(["-q", sub], {})
            assert rest == [sub], f"global -q not extracted before {sub!r}"

        text = _cli_reference_text()
        scope = next(
            para
            for para in _section_body(text, "## Output control").split("\n\n")
            if "before the subcommand" in para
        )
        for sub in _SUBCOMMANDS:
            assert f"`{sub}`" in scope, (
                f"Output control scope omits the {sub!r} subcommand"
            )
