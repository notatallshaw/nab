"""Process entry for the ``nab`` command and for ``python -m nab``."""

from __future__ import annotations

import gc


def console_entry() -> None:
    """Import the CLI with the cyclic collector off, then hand the process to it.

    Both entry paths name this module rather than :mod:`nab.cli` so that no
    part of the CLI's import graph is built while the collector is on: the
    import allocates a graph the process keeps, and every pass over it frees
    almost none of it. Freezing empties the generations the import filled, so
    re-enabling does not trigger a pass over the whole graph.

    Importing :mod:`nab.cli` leaves the collector as it found it.
    """
    gc.disable()
    try:
        from nab.cli import console_entry as run_cli  # noqa: PLC0415
    finally:
        if hasattr(gc, "freeze"):  # PyPy has no gc.freeze
            gc.freeze()
        gc.enable()

    run_cli()
