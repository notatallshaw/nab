"""Read the process environment that controls nab.

``HOME`` and ``USERPROFILE`` are read indirectly through ``Path.home()``.
Help and refusal paths import this module, so it avoids :mod:`typing`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping  # noqa: TC003 - that block imports typing

NO_COLOR = "NO_COLOR"
FORCE_COLOR = "FORCE_COLOR"
TERM = "TERM"
NAB_VERBOSITY = "NAB_VERBOSITY"
NAB_NO_PROGRESS = "NAB_NO_PROGRESS"
XDG_CACHE_HOME = "XDG_CACHE_HOME"
XDG_CONFIG_HOME = "XDG_CONFIG_HOME"

OUTPUT_OWNED = frozenset({NAB_VERBOSITY, NAB_NO_PROGRESS})
"""The ``NAB_*`` names the output layer owns, and the config ladder skips."""


def current(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """Return ``environ``, or ``os.environ`` when it is ``None``."""
    return os.environ if environ is None else environ


def verbosity_name(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the raw ``NAB_VERBOSITY`` value, unvalidated, or ``None``.

    Mapping a name to a level is :mod:`nab.output`'s, which raises on one
    it does not recognize; this module holds no level type.
    """
    return current(environ).get(NAB_VERBOSITY)


def color_enabled(
    choice: str,
    *,
    isatty: bool,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Decide whether to colour output, the one place that rule lives.

    ``choice`` is the ``--color`` value: ``always`` and ``never`` win
    outright.  Otherwise ``NO_COLOR`` (non-empty) disables, ``FORCE_COLOR``
    (non-empty) forces, ``TERM=dumb`` disables, and the fallback is
    ``isatty``, whether the stream being written to is a terminal.
    """
    if choice == "always":
        return True
    if choice == "never":
        return False

    env = current(environ)
    if env.get(NO_COLOR):
        return False
    if env.get(FORCE_COLOR):
        return True
    if env.get(TERM) == "dumb":
        return False
    return isatty


def progress_suppressed(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``NAB_NO_PROGRESS`` switches the live progress line off."""
    return bool(current(environ).get(NAB_NO_PROGRESS))


def _absolute_root(environ: Mapping[str, str] | None, name: str) -> str | None:
    """Return ``name`` as written, or ``None`` when unset or relative.

    The XDG base directory specification calls a relative value invalid.
    """
    value = current(environ).get(name)

    # Keep pathlib off the help and refusal startup path.
    if value is None or not os.path.isabs(value):  # noqa: PTH117
        return None
    return value


def cache_root(environ: Mapping[str, str] | None = None) -> str | None:
    """Return ``XDG_CACHE_HOME`` when it names an absolute path."""
    return _absolute_root(environ, XDG_CACHE_HOME)


def config_root(environ: Mapping[str, str] | None = None) -> str | None:
    """Return ``XDG_CONFIG_HOME`` when it names an absolute path."""
    return _absolute_root(environ, XDG_CONFIG_HOME)
