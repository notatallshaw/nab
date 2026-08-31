"""Entry point for the nab command.

Holds the tyro :class:`SubcommandApp` registration, the flag types the
command signatures annotate with, and the global flags :func:`main`
handles before any subcommand runs: the version line, the output flags,
and the standard streams.

The subcommands live in :mod:`nab._lock`, :mod:`nab._download`,
:mod:`nab._config_cmd`, and :mod:`nab._cache_cmd`; this module imports
them so their ``@app.command`` decorators run before :func:`main` runs
the CLI.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn

import tyro
from typing_extensions import override
from tyro.extras import SubcommandApp

from nab._version import __version__

from . import env
from .output import OutputOptionError, begin, parse_output_options

if TYPE_CHECKING:
    from typing import TextIO

__all__ = [
    "main",
]


# A pyproject.toml positional that may also be omitted to default to ./pyproject.toml.
PathArg = Annotated[Path, tyro.conf.Positional]

# Lowercase Literal types so --http-backend and --format render lowercase
# choices in --help rather than the uppercase enum names.
HttpBackend = Literal["urllib3", "httpx"]
LockFormat = Literal["pylock", "requirements", "requirements-without-hashes"]
ResolutionFlag = Literal["highest", "lowest", "lowest-direct"]
ModeFlag = Literal["specific", "universal"]
DistPolicyFlag = Literal[
    "wheel-only", "prefer-wheel", "wheel-or-sdist", "sdist-only", "sdist-install"
]
BuildPolicyFlag = Literal["never", "build-local", "build-remote"]
DecisionOrderFlag = Literal["arrival", "stable"]

# --offline is layered (an nab.toml or NAB_OFFLINE may set it), so it stays
# tri-state: an explicit value overrides the lower layers while an absent flag
# defers to them.  tyro renders that as a value-taking choice; main() also
# accepts the bare --offline / --no-offline forms (_normalize_layered_bool_flags).
OfflineFlag = Annotated[
    bool | None,
    tyro.conf.arg(
        metavar="{True,False}",
        help="never hit the network; bare --offline / --no-offline also work",
    ),
]

# Conventional KeyboardInterrupt exit code: 128 + SIGINT(2).
_SIGINT_EXIT_CODE = 130

# The status CPython exits with when it cannot flush the standard streams.
_FLUSH_FAILED_EXIT_CODE = 120

app = SubcommandApp()

# Layered boolean flags (currently just --offline) are tri-state, which tyro
# renders as a value-taking --flag {True,False} rather than a --flag / --no-flag
# pair.  main() rewrites the bare forms into that value form before tyro parses.
_LAYERED_BOOL_FLAGS = frozenset({"offline"})

# The tokens that count as a value already spelled out after the flag.
_BOOL_FLAG_VALUES = frozenset({"True", "False", "None"})


def _normalize_layered_bool_flags(argv: list[str]) -> list[str]:
    """Rewrite bare ``--offline`` / ``--no-offline`` into tyro's value form.

    A layered boolean then reads like ``--cache`` / ``--no-cache`` at the
    CLI: ``--offline`` becomes ``--offline True`` and ``--no-offline`` becomes
    ``--offline False``.  An absent flag is left alone and still defers to the
    config layers, and an explicit ``--offline True`` / ``--offline False`` is
    passed through unchanged.
    """
    normalized: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]

        # --offline [value]: keep an explicit True/False/None, else it is bare.
        if token.startswith("--") and token[2:] in _LAYERED_BOOL_FLAGS:
            following = argv[i + 1] if i + 1 < len(argv) else None
            if following is not None and following in _BOOL_FLAG_VALUES:
                normalized += [token, following]
                i += 2
            else:
                normalized += [token, "True"]
                i += 1

        # --no-offline is shorthand for --offline False.
        elif token.startswith("--no-") and token[5:] in _LAYERED_BOOL_FLAGS:
            normalized += [f"--{token[5:]}", "False"]
            i += 1

        # Any other token (subcommand, path, unrelated flag) passes through.
        else:
            normalized.append(token)
            i += 1

    return normalized


# Side-effect imports: each module's @app.command decorators register the
# subcommand.  Placed at the bottom so ``app`` and the flag types above bind
# before the command modules import them back, and bound as modules because a
# name import here would raise when the package is entered through one of them.
from . import _cache_cmd as _cache_module  # noqa: E402, F401 - side-effect
from . import _config_cmd as _config_module  # noqa: E402, F401 - side-effect
from . import _download as _download_module  # noqa: E402, F401 - side-effect
from . import _lock as _lock_module  # noqa: E402, F401 - side-effect


def main() -> None:
    """Run the CLI, exiting 120 when output went to a stream closed at startup."""
    _replace_closed_std_streams()

    try:
        _run_cli()
    except SystemExit:
        if _output_was_dropped():
            raise SystemExit(_FLUSH_FAILED_EXIT_CODE) from None
        raise

    if _output_was_dropped():
        raise SystemExit(_FLUSH_FAILED_EXIT_CODE)


def _run_cli() -> None:
    """Parse the global flags and run the requested subcommand."""
    # Tyro's SubcommandApp does not surface global flags, so ``--version`` and
    # the output flags (-v/-q, --color, --no-progress) are parsed before
    # ``app.cli()`` sees the sub-command.
    argv = sys.argv[1:]
    if argv and argv[0] in {"--version", "-V"}:
        sys.stdout.write(f"nab {__version__}\n")
        return

    try:
        options, rest = parse_output_options(argv, env.current())
    except OutputOptionError as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)

    run_printer = begin(options)

    try:
        app.cli(prog="nab", args=_normalize_layered_bool_flags(rest))
    except KeyboardInterrupt:
        run_printer.error("interrupted")
        sys.exit(_SIGINT_EXIT_CODE)


def _system_exit_status(code: object) -> int:
    """Map a ``SystemExit`` code to the status the interpreter would exit with."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    sys.stderr.write(f"{code}\n")
    return 1


class _ClosedStream(io.StringIO):
    """Stands in for a standard stream CPython left unset.

    ``sys.stdout`` and ``sys.stderr`` are ``None`` when their descriptor was
    closed before the process started. Text written here goes nowhere, and
    ``dropped`` records that a write reached it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dropped = False

    @override
    def write(self, text: str, /) -> int:
        self.dropped = True
        return len(text)


def _replace_closed_std_streams() -> None:
    """Give each standard stream CPython left unset something to write to."""
    # typeshed types these as never None, so widen before testing.
    stdout: TextIO | None = sys.stdout
    stderr: TextIO | None = sys.stderr
    if stdout is None:
        sys.stdout = _ClosedStream()
    if stderr is None:
        sys.stderr = _ClosedStream()


def _output_was_dropped() -> bool:
    """Report whether the run wrote to a stream that could not take it."""
    return any(
        isinstance(stream, _ClosedStream) and stream.dropped
        for stream in (sys.stdout, sys.stderr)
    )


def _flush_stream(stream: TextIO) -> bool:
    """Flush one stream, reporting whether its buffered output landed."""
    try:
        stream.flush()
    except OSError:
        return False
    return True


def _flush_std_streams() -> bool:
    """Flush stdout and stderr, reporting whether both landed.

    stderr is flushed even when stdout fails, so a command that could not
    write its result still gets its error out.
    """
    out_flushed = _flush_stream(sys.stdout)
    err_flushed = _flush_stream(sys.stderr)
    return out_flushed and err_flushed


def console_entry() -> NoReturn:
    """Run the CLI, then end the process without freeing the resolve graph.

    Only the installed ``nab`` command takes this path, because it owns its
    process; :func:`main` returns normally for every other caller. No
    ``atexit`` hook and no finalizer runs after this, so a command has to
    finish any work it cannot lose before :func:`main` returns.
    """
    status = 0
    try:
        main()
    except SystemExit as exc:
        status = _system_exit_status(exc.code)

    if not _flush_std_streams():
        status = _FLUSH_FAILED_EXIT_CODE

    os._exit(status)
