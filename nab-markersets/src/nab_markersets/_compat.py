"""Runtime fallback for :func:`typing.override`, so ``typing_extensions`` is not needed.

``override`` runs at class-body time, so unlike the annotation-only helpers it
cannot hide under ``TYPE_CHECKING``.  Looking it up by name rather than by
interpreter version keeps this free of a version-gated branch.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

__all__ = ["override"]

if TYPE_CHECKING:
    from typing_extensions import override
else:
    override = getattr(typing, "override", lambda method: method)
