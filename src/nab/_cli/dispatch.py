"""Return a parsed command's status and optional ``SystemExit`` message.

Command output remains owned by :mod:`nab.output`. Command imports are
deferred until after parsing.
"""

from __future__ import annotations

__lazy_modules__ = (
    "nab",
    "nab.config.values",
    "os.path",
    "pathlib",
)

from os.path import basename
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .. import output
from ..config.values import parse_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from .parse import Parsed

__all__ = ["dispatch"]

# The status a value the run's printer refuses exits with.
_USAGE_STATUS = 2

# What a command that ended with a message rather than a status exits with.
_FAILED_STATUS = 1

_FILE_DESTS = {
    "config": ("path", "script"),
    "lock": ("path", "script", "output"),
    "download": ("path", "script"),
}


def _nothing() -> None:
    """Stand in for the ``resume`` a caller did not supply."""


def dispatch(
    parsed: Parsed,
    table: dict[str, tuple[str, str, str]],
    path_dests: dict[str, tuple[str, ...]],
    *,
    resume: Callable[[], None] = _nothing,
) -> tuple[int, str]:
    """Start the run's output, then run the command, and report how it ended.

    The module named in ``table`` is imported here rather than at the top
    of the CLI, so a command's own imports are paid by the command that
    was asked for and by no other.

    ``resume`` runs between that import and the command, so a caller that
    paused something for startup has a place to put it back.
    """
    try:
        output.begin(_options(parsed.options))
    except output.OutputOptionError as exc:
        return _USAGE_STATUS, f"error: {exc}"

    module_name, function_name, _summary = table[parsed.command]
    module = __import__(module_name, fromlist=(function_name,))
    resume()
    command = getattr(module, function_name)

    values = dict(parsed.values)
    if values.get("script") is not None and "path" in parsed.supplied:
        return _USAGE_STATUS, "error: a project path and --script cannot be combined"
    for dest in path_dests.get(parsed.command, ()):
        value = values[dest]
        if isinstance(value, str):
            if dest == "script" and value.startswith(("http://", "https://")):
                return _USAGE_STATUS, "error: --script requires a local file path"
            try:
                values[dest] = _command_path(parsed.command, dest, value)
            except ValueError as exc:
                return _FAILED_STATUS, f"error: {exc}"

    try:
        command(**values)
    except SystemExit as exc:
        return _status(exc.code)

    return 0, ""


def _command_path(command: str, dest: str, value: str) -> Path:
    """Check the original path argument before Path discards parts of it."""
    if dest == "cache_dir":
        return parse_path(value, "--cache-dir")
    if dest in _FILE_DESTS.get(command, ()) and basename(value) in ("", "."):  # noqa: PTH119
        msg = f"expected a file path, got a directory path: {value}"
        raise ValueError(msg)

    path = Path(value)
    if command == "lock" and dest == "output" and value != "-" and path == Path("-"):
        # Path strips the explicit ./ prefix; only bare - selects stdout.
        return path.absolute()
    return path


def _options(values: dict[str, object]) -> output.OutputOptions:
    """Fold the root flags into the knobs the run's printer takes."""
    return output.options_from_flags(
        verbose=cast("int", values["verbose"]),
        quiet=cast("int", values["quiet"]),
        color=cast("str | None", values["color"]),
        no_color=cast("bool", values["no_color"]),
        no_progress=cast("bool", values["no_progress"]),
    )


def _status(code: object) -> tuple[int, str]:
    """Read a command's ``SystemExit`` as a status and a message to write.

    ``sys.exit`` takes a string as well as a status, and the interpreter
    prints that string and exits 1.
    """
    if code is None:
        return 0, ""
    if isinstance(code, int):
        return code, ""
    return _FAILED_STATUS, str(code)
