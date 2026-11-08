"""Visit lazy candidate groups while retaining an optional incumbent."""

from __future__ import annotations

from collections.abc import Callable, Collection, Hashable, Iterable, Iterator
from typing import TypeVar

__all__ = ["CandidateGroupRejectedError", "ordered_candidates"]

CandidateType = TypeVar("CandidateType")
GroupType = TypeVar("GroupType", bound=Hashable)


class CandidateGroupRejectedError(Exception):
    """Preparation established that no alternate in this group should be read."""


def ordered_candidates(
    groups: Iterable[tuple[GroupType, Callable[[], CandidateType | None]]],
    *,
    incumbent: tuple[GroupType, CandidateType] | None,
    prefer_incumbent: bool,
    precedes: Callable[[GroupType, GroupType], bool],
    excluded: Collection[CandidateType] = (),
) -> Iterator[CandidateType]:
    """Yield the first usable candidate in each group, with native incumbent order."""
    if incumbent is not None and incumbent[1] in excluded:
        incumbent = None
    seen: set[GroupType] = set()
    if incumbent is not None and prefer_incumbent:
        yield incumbent[1]
        seen.add(incumbent[0])

    for group, prepare in groups:
        if group in seen:
            continue

        if (
            incumbent is not None
            and incumbent[0] not in seen
            and precedes(incumbent[0], group)
        ):
            yield incumbent[1]
            seen.add(incumbent[0])

        try:
            candidate = prepare()
        except CandidateGroupRejectedError:
            seen.add(group)
            continue

        if candidate is not None and candidate not in excluded:
            yield candidate
            seen.add(group)

    if incumbent is not None and incumbent[0] not in seen:
        yield incumbent[1]
