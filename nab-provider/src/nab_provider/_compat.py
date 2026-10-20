"""Typing decorators without a runtime typing_extensions dependency."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

__all__ = ["dataclass_transform", "override"]

_ClassT = typing.TypeVar("_ClassT")


def _identity_transform(
    **_options: object,
) -> typing.Callable[[type[_ClassT]], type[_ClassT]]:
    """Leave runtime classes unchanged when typing's decorator is unavailable."""
    return lambda cls: cls


if TYPE_CHECKING:
    from typing_extensions import dataclass_transform, override
else:
    override = getattr(typing, "override", lambda method: method)
    dataclass_transform = getattr(typing, "dataclass_transform", _identity_transform)
