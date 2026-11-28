"""Range constructors for a solver query whose only versions are integer tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_resolver.ranges import Range

from ._compat import override

if TYPE_CHECKING:
    from collections.abc import Iterable


class CandidateTokenRange(Range[int]):
    """Represent token n by [n, n+1) throughout a private query.

    Touching cells coalesce under the resolver's ordinary range operations.
    Neither a later token nor an omitted token lies between consecutive IDs.
    """

    @classmethod
    @override
    def singleton(cls, version: int) -> Range[int]:
        """Return the cell for one token, including a decision on the trail."""
        return Range.between(version, version + 1)

    @classmethod
    @override
    def from_versions(cls, versions: Iterable[int]) -> Range[int]:
        """Build one half-open interval for each run of consecutive tokens."""
        ordered = iter(sorted(set(versions)))
        start = next(ordered, None)
        if start is None:
            return Range.empty()

        runs = []
        end = start
        for value in ordered:
            if value != end + 1:
                runs.append((start, True, end + 1, False))
                start = value
            end = value

        runs.append((start, True, end + 1, False))
        return Range(tuple(runs))
