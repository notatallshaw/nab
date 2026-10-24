"""Suggestion emission belongs to one command's stderr and verbosity policy."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from nab._resolve import _error_text
from nab.cli import run
from nab.output import Printer, Verbosity, _LevelFormatter, install_log_handler
from nab_project.diagnostics import ResolutionMessage
from nab_provider.diagnostics import Diagnostic
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion
from nab_resolver.errors import ResolutionError


def _message(cause: str = "refused") -> DiagnosticMessage:
    """Return distinct causes sharing one actionable policy change."""
    return DiagnosticMessage(cause, suggestions=(Suggestion("change policy"),))


@pytest.mark.parametrize("verbosity", list(Verbosity))
def test_errors_emit_shared_advice_once_at_every_verbosity(
    verbosity: Verbosity,
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    printer = Printer(verbosity=verbosity, stdout=stdout, stderr=stderr)
    printer.error(_message("first"))
    printer.error(ValueError(_message("second")), prefix="operation: ")

    assert stdout.getvalue() == ""
    assert "error: first" in stderr.getvalue()
    assert "error: operation: second" in stderr.getvalue()
    assert stderr.getvalue().count("try: change policy") == 1


def test_suppressed_warnings_do_not_consume_later_error_suggestions() -> None:
    stream = io.StringIO()
    printer = Printer(verbosity=Verbosity.SILENT, stderr=stream)
    printer.warning(_message())

    assert stream.getvalue() == ""

    printer.error(_message())

    assert "try: change policy" in stream.getvalue()


def test_each_command_gets_fresh_emission_state() -> None:
    stream = io.StringIO()
    for _ in range(2):
        Printer(stderr=stream).error(_message())

    assert stream.getvalue().count("try: change policy") == 2


@pytest.mark.parametrize("style", ["direct", "tuple", "mapping"])
def test_logging_interpolation_and_direct_errors_share_suggestion_state(
    style: str,
) -> None:
    stream = io.StringIO()
    printer = Printer(stderr=stream)
    install_log_handler(printer)
    logger = logging.getLogger("nab_project.suggestion_test")
    if style == "direct":
        logger.warning(_message("logged"))
    elif style == "tuple":
        logger.warning("wrapped: %s (%d)", _message("logged"), 2)
    else:
        logger.warning("wrapped: %(cause)s", {"cause": _message("logged")})
    printer.error(_message("terminal"))

    output = stream.getvalue()

    assert "logged" in output
    assert "terminal" in output
    assert output.count("try: change policy") == 1


def test_standalone_log_formatter_owns_only_its_own_emission_state() -> None:
    formatter = _LevelFormatter(verbose=True, color_enabled=False)
    record = logging.LogRecord(
        "nab_project", logging.WARNING, __file__, 1, _message(), (), None
    )

    assert "try: change policy" in formatter.format(record)
    assert "try:" not in formatter.format(record)
    assert "try: change policy" in _LevelFormatter(
        verbose=False, color_enabled=False
    ).format(record)


def test_verbose_logging_keeps_suggestion_context() -> None:
    stream = io.StringIO()
    printer = Printer(verbosity=Verbosity.VERBOSE, stderr=stream)
    install_log_handler(printer)
    logging.getLogger("nab_provider.suggestion_test").warning(
        DiagnosticMessage(
            "refused",
            suggestions=(Suggestion("edit policy", detail="This source sets it."),),
        )
    )

    assert "try: edit policy" in stream.getvalue()
    assert "This source sets it." in stream.getvalue()


def test_typed_resolution_reports_share_the_command_renderer() -> None:
    stream = io.StringIO()
    printer = Printer(stderr=stream)
    message = ResolutionMessage(
        "unsatisfiable", (("pkg", Diagnostic("refused", remedy="edit policy")),)
    )
    for target in ("linux", "macos"):
        printer.error(ResolutionError(message), prefix=f"{target}: ")
    output = stream.getvalue()

    assert "linux: unsatisfiable" in output
    assert "macos: unsatisfiable" in output
    assert output.count("pkg: refused") == 2
    assert output.count("try: edit policy") == 1


def test_matrix_failures_emit_an_action_once_and_the_next_command_emits_it_again(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1"\ndependencies = ["absent==1"]\n'
        '[tool.nab]\nmode = "universal"\n'
        '[tool.nab.matrix]\npython = "==3.13"\n'
        'platforms = ["linux_x86_64", "macos_arm64"]\n'
    )
    for _ in range(2):
        assert (
            run(["lock", str(project), "--offline", "--no-cache", "--output", "-"]) == 1
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.count("try: re-run with --no-offline") == 1
        assert (
            captured.err.count("offline mode skipped an index with no cached listing")
            == 2
        )
        assert "# py313-linux_x86_64: FAILED" in captured.err
        assert "# py313-macos_arm64: FAILED" in captured.err


def test_legacy_resolution_hosts_keep_their_verbose_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ResolutionError("compact legacy report")
    error.verbose_message = "detailed legacy report"
    monkeypatch.setattr("nab.output._printer", Printer(verbosity=Verbosity.VERBOSE))

    assert _error_text(error) == "detailed legacy report"

    monkeypatch.setattr("nab.output._printer", Printer())

    assert _error_text(error) == "compact legacy report"
