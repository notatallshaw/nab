"""Process entry for the ``nab`` command and for ``python -m nab``."""

from __future__ import annotations

import gc


def _resume_collector() -> None:
    """Freeze what startup built, then turn the cyclic collector back on.

    A second call does nothing, so the CLI's call and
    :func:`console_entry`'s cannot both freeze.
    """
    if gc.isenabled():
        return

    if hasattr(gc, "freeze"):  # PyPy has no gc.freeze
        gc.freeze()
    gc.enable()


def console_entry() -> None:
    """Hand the process to the CLI with the cyclic collector off.

    Both entry paths name this module rather than :mod:`nab.cli`, so neither
    the CLI's imports nor the command module's are built while the collector
    is on: they allocate a graph the process keeps, and a pass over it frees
    almost none of it.

    The CLI calls back once the command module is in, and freezing there
    empties the generations those imports filled, so enabling the collector
    does not start a pass over the whole graph.

    The collector is on again on the way out, whichever way the CLI ended.
    """
    gc.disable()
    try:
        from nab.cli import console_entry as run_cli  # noqa: PLC0415

        run_cli(_resume_collector)
    finally:
        _resume_collector()
