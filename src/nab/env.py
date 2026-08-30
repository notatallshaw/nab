"""The process environment nab reads to decide what it does.

Every such read goes through :func:`current`, so the variables that
change nab's behaviour are enumerable from one module rather than found
by grep.  A variable a subprocess is handed is not one of these: the
package that spawns the process owns that environment.

``HOME`` belongs to the list but is not named in the code: the cache and
config fallbacks reach it through ``Path.home()``, which reads it on
POSIX and ``USERPROFILE`` on Windows.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    """Return ``environ``, or the process environment when it is ``None``.

    The one door onto ``os.environ`` in this package.  A caller with a
    mapping of its own (a test, or a layer handed one) passes it through
    unchanged.
    """
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


def cache_root(environ: Mapping[str, str] | None = None) -> str | None:
    """Return ``XDG_CACHE_HOME`` as written, or ``None`` when it is unset.

    The raw string, so this module needs no ``pathlib``; the caller builds
    the path and picks the fallback.
    """
    return current(environ).get(XDG_CACHE_HOME)


def config_root(environ: Mapping[str, str] | None = None) -> str | None:
    """Return ``XDG_CONFIG_HOME`` as written, or ``None`` when it is unset."""
    return current(environ).get(XDG_CONFIG_HOME)
