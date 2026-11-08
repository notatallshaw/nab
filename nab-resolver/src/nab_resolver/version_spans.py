"""Cache-only dependency equivalence over a fixed ordered version listing."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from typing_extensions import Self

__all__ = ["OrderedVersion", "cached_dependency_span"]


class OrderedVersion(Protocol):
    """A version value that can be bisected within its own ordered domain."""

    def __lt__(self, other: Self, /) -> bool:
        """Compare versions from the same listing."""
        ...


VersionType = TypeVar("VersionType", bound=OrderedVersion)


def cached_dependency_span(
    version: VersionType,
    universe: Sequence[VersionType],
    dependency_at: Callable[[VersionType], object | None],
    *,
    expand_higher: bool = True,
) -> tuple[VersionType | None, VersionType | None]:
    """Return open bounds around cached equivalents without reading metadata.

    ``universe`` must be sorted ascending and remain fixed for the resolution.
    ``dependency_at`` returns a cached dependency value, or None for unread metadata.
    """
    below = bisect_left(universe, version)
    above = bisect_right(universe, version)
    dependencies = dependency_at(version)

    # Unknown metadata fences a span even when both cache lookups are missing.
    if dependencies is not None:
        while below and dependency_at(universe[below - 1]) == dependencies:
            below -= 1

        if expand_higher:
            while (
                above < len(universe) and dependency_at(universe[above]) == dependencies
            ):
                above += 1

    return (
        universe[below - 1] if below else None,
        universe[above] if above < len(universe) else None,
    )
