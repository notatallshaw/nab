"""PEP 503 name canonicalisation for nab-index.

Wraps :func:`packaging.utils.canonicalize_name` so the local- and
multi-index modules share a single helper.  ``packaging`` is already
a runtime dependency (see ``pyproject.toml``), so the helpers defer
to it.
"""

from __future__ import annotations

from packaging.utils import canonicalize_name

__all__ = [
    "canonical",
]


def canonical(name: str) -> str:
    """Return ``name`` as its PEP 503 canonical form.

    Lower-cases the input and collapses runs of ``-``, ``_``, ``.``
    to a single ``-`` per PEP 503.  Leading and trailing dashes are
    preserved (consistent with :func:`packaging.utils.canonicalize_name`).
    """
    return canonicalize_name(name)
