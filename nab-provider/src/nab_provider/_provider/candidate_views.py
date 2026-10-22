"""Operation-scoped candidate preparation without metadata or solver state."""

from __future__ import annotations

__lazy_modules__ = ("nab_provider.records", "nab_provider.tags")

import sys
from itertools import compress
from typing import TYPE_CHECKING

from ..records import WheelFile
from ..tags import parse_wheel_tag_set

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider._vendor.packaging.tags import Tag
    from nab_provider._vendor.packaging.version import Version

    from ..records import DistFile
    from ..tags import TagSet


_GROUP_CODEPOINT_MAX = sys.maxunicode


class CandidateView:
    """One exact tag-admission projection of a prepared listing."""

    __slots__ = (
        "_last_matching",
        "_version_counts",
        "distributions",
        "empty_versions",
        "tag_exclusions",
    )

    _version_counts: tuple[tuple[Version, ...], dict[Version, int]] | None
    _last_matching: tuple[VersionRange, int] | None

    def __init__(
        self,
        distributions: list[tuple[Version, DistFile]],
        tag_exclusions: list[tuple[Version, int]],
        empty_versions: int,
    ) -> None:
        """Retain one projection and initialize its derived count views."""
        self.distributions = tuple(distributions)
        self.tag_exclusions = tuple(tag_exclusions)
        self.empty_versions = empty_versions
        self._version_counts = None
        self._last_matching = None

    def count_matching(self, version_range: VersionRange) -> int:
        """Count the view's records, reusing its last exact range query."""
        previous = self._last_matching
        if previous is not None and version_range == previous[0]:
            return previous[1]

        prepared = self._version_counts
        if prepared is None:
            counts: dict[Version, int] = {}
            for version, _ in self.distributions:
                counts[version] = counts.get(version, 0) + 1
            prepared = (tuple(counts), counts)
            self._version_counts = prepared

        versions, counts = prepared
        count = sum(
            counts[version]
            for version in version_range.filter(versions, prereleases=True)
        )
        self._last_matching = (version_range, count)
        return count


class CandidatePreparation:
    """Group a fixed pre-tag listing once, then project actual target tags."""

    __slots__ = ("_codes", "_groups", "_rows", "_runs", "_views")

    def __init__(self, distributions: Sequence[tuple[Version, DistFile]]) -> None:
        """Classify the cache-owned snapshot without evaluating metadata."""
        rows = tuple(distributions)
        groups: dict[frozenset[Tag] | None, int] = {}
        group_ids: list[int] = []
        for pair in rows:
            _, dist = pair
            if isinstance(dist, WheelFile):
                group = groups.setdefault(
                    parse_wheel_tag_set(dist.filename), len(groups) + 1
                )
                group_ids.append(group)
            else:
                group_ids.append(0)

        self._groups = tuple(groups)
        self._rows = rows
        # One code point per group; retain integer IDs beyond Unicode's capacity.
        self._codes = (
            "".join(map(chr, group_ids))
            if len(groups) <= _GROUP_CODEPOINT_MAX
            else tuple(group_ids)
        )
        self._runs: tuple[tuple[Version, int, int], ...] | None = None
        self._views: dict[tuple[bool, ...], CandidateView] = {}

    def matches_records(
        self, distributions: Sequence[tuple[Version, DistFile]]
    ) -> bool:
        """Match the same row objects in order, without record equality callbacks."""
        return len(distributions) == len(self._rows) and all(
            pair is row for pair, row in zip(distributions, self._rows, strict=True)
        )

    def for_tags(self, tags: TagSet) -> CandidateView:
        """Reuse a view only when every tag group has the same admission answer."""
        signature = tuple(
            group is not None and tags.accepts_tags(group) for group in self._groups
        )
        view = self._views.get(signature)
        if view is None:
            view = self._project(signature)
            self._views[signature] = view
        return view

    def _version_runs(self) -> tuple[tuple[Version, int, int], ...]:
        """Cache version-object runs in a nonempty snapshot with rejected rows."""
        if self._runs is None:
            runs: list[tuple[Version, int, int]] = []
            previous = self._rows[0][0]
            start = 0
            for index, (version, _) in enumerate(self._rows):
                if version is not previous:
                    runs.append((previous, start, index))
                    previous, start = version, index
            runs.append((previous, start, len(self._rows)))
            self._runs = tuple(runs)
        return self._runs

    def _project(self, accepted: tuple[bool, ...]) -> CandidateView:
        """Project original rows and retain the same diagnostic deltas."""
        # Code zero keeps sdists without consulting wheel tags.
        if isinstance(self._codes, str):
            table = {index + 1: int(value) for index, value in enumerate(accepted)}
            table[0] = 1
            mask = self._codes.translate(table).encode("ascii")
        else:
            mask = bytes(group == 0 or accepted[group - 1] for group in self._codes)

        result = list(compress(self._rows, mask))
        if not mask.count(b"\0"):
            return CandidateView(result, [], 0)

        exclusions: list[tuple[Version, int]] = []
        rejected: set[Version] = set()
        kept: set[Version] = set()
        for version, start, stop in self._version_runs():
            accepted_count = mask.count(b"\1", start, stop)
            refused_count = stop - start - accepted_count
            if accepted_count:
                kept.add(version)
            if refused_count:
                rejected.add(version)
                if exclusions and exclusions[-1][0] is version:
                    exclusions[-1] = (version, exclusions[-1][1] + refused_count)
                else:
                    exclusions.append((version, refused_count))

        return CandidateView(result, exclusions, len(rejected - kept))
