"""Runtime fallback for :func:`typing.override`, so this package needs no dependency.

``override`` runs at class-body time, so unlike the annotation-only helpers it
cannot hide under ``TYPE_CHECKING``.  It is looked up by name rather than by
interpreter version, which keeps this free of a version-gated branch.  Before
3.12 the fallback is the identity and so does not set ``__override__``, which
nothing here reads.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

__all__ = ["override"]

if TYPE_CHECKING:
    from typing_extensions import override
else:
    override = getattr(typing, "override", lambda method: method)
