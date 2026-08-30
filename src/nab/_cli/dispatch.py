"""Run the command a parsed line names.

Loaded only once a command name has parsed, which is also the condition
for starting the run's output, so this is where :mod:`nab.output` and
:mod:`pathlib` first reach the process.

Nothing here writes or exits: a status, and sometimes a message, go back to
the caller, which owns both write sites.  The dispatch table is a parameter
rather than a module global so a fixture table can drive both failure paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from nab import output

if TYPE_CHECKING:
    from nab._cli.parse import Parsed

__all__ = ["dispatch"]

# The status a value the run's printer refuses exits with.
_USAGE_STATUS = 2

# What a command that ended with a message rather than a status exits with.
_FAILED_STATUS = 1


def dispatch(
    parsed: Parsed,
    table: dict[str, tuple[str, str, str]],
    path_dests: dict[str, tuple[str, ...]],
) -> tuple[int, str]:
    """Start the run's output, then run the command, and report how it ended.

    The module named in ``table`` is imported here rather than at the top
    of the CLI, so a command's own imports are paid by the command that
    was asked for and by no other.
    """
    try:
        output.begin(_options(parsed.options))
    except output.OutputOptionError as exc:
        return _USAGE_STATUS, f"error: {exc}"

    module_name, function_name, _summary = table[parsed.command]
    module = __import__(module_name, fromlist=(function_name,))
    command = getattr(module, function_name)

    values = dict(parsed.values)
    for dest in path_dests.get(parsed.command, ()):
        value = values[dest]
        if isinstance(value, str):
            values[dest] = Path(value)

    try:
        command(**values)
    except SystemExit as exc:
        return _status(exc.code)

    return 0, ""


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
