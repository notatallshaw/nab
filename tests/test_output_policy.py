"""What nab's output policy promises, read off the two streams.

stdout carries the artefact the command was asked to produce and stderr
carries everything about the run, at a level ``-q`` and ``-v`` act on.  Each
case runs a real command line through :func:`nab.cli.run` with the streams
apart and reads both back, so a write counts for where it lands rather than
for how it was spelled.

The source-level half of the policy is ruff's ``TID251`` ban on naming a
process stream, configured in ``pyproject.toml``; ``T201`` covers ``print``.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
import tomli

from nab.cli import run

if TYPE_CHECKING:
    from pathlib import Path

_PROJECT = '[project]\nname = "probe"\nversion = "0.1"\ndependencies = []\n'

# A dependency no index can offer, so an offline resolve fails on it.
_UNRESOLVABLE = (
    '[project]\nname = "probe"\nversion = "0.1"\ndependencies = ["nosuchpkg-xyz"]\n'
)

# Two interpreters on one platform, so a failure renders the per-target
# block report rather than a single line.
_UNIVERSAL = (
    'mode = "universal"\n\n[matrix]\npython = ">=3.11,<3.13"\n'
    'platforms = ["linux_x86_64"]\n'
)

_TYPO_WARNING = "NAB_OFLINE is not a recognized nab setting"

_YELLOW = "\033[33m"

# What a message about the run leads with, on whichever stream it lands.
_RUN_TOKENS = ("error:", "warning:", "note:", "notice:", "Wrote", "Downloaded")


@pytest.fixture
def project(hermetic_roots: Path) -> Path:
    """A project directory holding a lockable pyproject with no dependencies."""
    (hermetic_roots / "pyproject.toml").write_text(_PROJECT, encoding="utf-8")
    return hermetic_roots


def _lock_to_stdout(project: Path, *flags: str) -> int:
    """Lock ``project`` to stdout under ``flags``, returning the status."""
    return run(
        (
            *flags,
            "lock",
            str(project / "pyproject.toml"),
            "--output",
            "-",
            "--offline",
            "--cache-dir",
            str(project / "cache"),
        )
    )


def _streams(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """The run's stdout and stderr, read back apart."""
    captured = capsys.readouterr()
    return captured.out, captured.err


def _assert_only_the_artefact(out: str) -> None:
    """Fail if anything the run had to say about itself reached stdout."""
    for token in _RUN_TOKENS:
        assert token not in out, f"{token!r} reached stdout"


def test_a_lock_to_stdout_is_the_lockfile_and_nothing_else(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``nab lock --output -`` writes a pylock a pipe can read."""
    status = _lock_to_stdout(project)
    out, err = _streams(capsys)

    assert status == 0
    assert tomli.loads(out)["lock-version"] == "1.0"

    _assert_only_the_artefact(out)
    assert err == ""


def test_a_failed_lock_leaves_stdout_empty(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resolve that fails produces no artefact, so stdout takes nothing.

    The diagnosis is the per-target block report, which is what nab#128 was
    about: redirected to a file it would fill the lock the run never wrote.
    """
    (project / "pyproject.toml").write_text(_UNRESOLVABLE, encoding="utf-8")
    (project / "nab.toml").write_text(_UNIVERSAL, encoding="utf-8")

    status = _lock_to_stdout(project)
    out, err = _streams(capsys)

    assert status == 1
    assert out == ""

    assert "error: resolution failed:" in err
    assert "py311-linux_x86_64: FAILED" in err


def test_cache_dir_prints_the_path_alone(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``$(nab cache dir)`` has to be the path, so stderr stays empty."""
    cache = project / "cache"
    monkeypatch.setenv("NAB_CACHE_DIR", str(cache))

    status = run(("cache", "dir"))
    out, err = _streams(capsys)

    assert status == 0
    assert out == f"{cache}\n"
    assert err == ""


def test_config_list_puts_the_table_on_stdout_and_the_notice_on_stderr(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listing is what was asked for; the notice is about the run."""
    status = run(
        (
            "config",
            "list",
            "--path",
            str(project / "pyproject.toml"),
            "--project-resolution",
            "lowest",
        )
    )
    out, err = _streams(capsys)

    assert status == 0
    assert out.startswith("key ")
    assert "resolution           lowest" in out

    _assert_only_the_artefact(out)
    assert err.startswith("notice: project-scope overrides were applied from the CLI")


def test_config_get_prints_the_value_alone(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run(
        ("config", "get", "resolution", "--path", str(project / "pyproject.toml"))
    )
    out, err = _streams(capsys)

    assert status == 0
    assert out == "highest\n"
    assert err == ""


def test_download_reports_on_stderr_because_its_artefact_is_on_disk(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``nab download`` writes files, so its summary has no claim on stdout."""
    status = run(
        (
            "download",
            str(project / "pyproject.toml"),
            "--output",
            str(project / "wheels"),
            "--offline",
            "--cache-dir",
            str(project / "cache"),
        )
    )
    out, err = _streams(capsys)

    assert status == 0
    assert out == ""
    assert err.startswith("Downloaded 0 files")


def test_quiet_keeps_the_artefact_and_drops_the_run_summary(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``-q`` is a level on stderr, and the artefact is not on the level."""
    monkeypatch.setenv("NAB_OFLINE", "1")

    status = _lock_to_stdout(project, "-q")
    out, err = _streams(capsys)

    assert status == 0
    assert tomli.loads(out)["lock-version"] == "1.0"
    assert _TYPO_WARNING in err


def test_the_second_quiet_drops_the_warning_too(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``-qq`` reaches the warning, and still not the artefact."""
    monkeypatch.setenv("NAB_OFLINE", "1")

    status = _lock_to_stdout(project, "-qq")
    out, err = _streams(capsys)

    assert status == 0
    assert tomli.loads(out)["lock-version"] == "1.0"
    assert err == ""


class _Stream(io.StringIO):
    """A stream that answers ``isatty()`` with what it was built with."""

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_a_warning_is_coloured_and_the_piped_artefact_is_not(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each stream is asked on its own, so a redirect decides only its own colour."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("NAB_OFLINE", "1")

    out_stream, err_stream = _Stream(tty=False), _Stream(tty=True)
    monkeypatch.setattr("sys.stdout", out_stream)
    monkeypatch.setattr("sys.stderr", err_stream)

    status = _lock_to_stdout(project)
    out, err = out_stream.getvalue(), err_stream.getvalue()

    assert status == 0
    assert err.startswith(f"{_YELLOW}warning:")
    assert _TYPO_WARNING in err

    assert "\033[" not in out
    assert tomli.loads(out)["lock-version"] == "1.0"
