"""Runtime stand-ins for typing features newer than the 3.10 floor.

Defined here rather than taken from a backport package, so the solver core
keeps no runtime dependency.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "override",
]

_F = TypeVar("_F", bound="Callable[..., Any]")


def _override(method: _F, /) -> _F:
    """Stand in for :func:`typing.override` below 3.12, where it arrived.

    Both are no-ops at run time; a type checker does the checking.
    """
    return method


# A checker reads the typing_extensions spelling, so decorated methods keep
# being checked against their base class. The interpreter's own is asked for by
# name, not by version, so there is no branch only one version can take.
if TYPE_CHECKING:
    from typing_extensions import override
else:
    override = getattr(typing, "override", _override)
