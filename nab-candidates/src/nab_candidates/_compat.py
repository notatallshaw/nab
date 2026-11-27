"""Use typing.override when available and the identity decorator on older Python."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

__all__ = ["override"]

if TYPE_CHECKING:
    from typing_extensions import override
else:
    override = getattr(typing, "override", lambda method: method)
