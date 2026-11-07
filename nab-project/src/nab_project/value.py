"""The compatibility base for this package's declared value fields."""

from __future__ import annotations

__lazy_modules__ = ("typing",)

from typing import ClassVar

from nab_resolver.values import SlottedValue

__all__ = ["ValueType"]


class ValueType(SlottedValue):
    """Retain the project value class and its empty-field default."""

    __slots__ = ()
    __match_args__: ClassVar[tuple[str, ...]] = ()
