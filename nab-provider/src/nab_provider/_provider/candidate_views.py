"""Operation-scoped candidate preparation without metadata or solver state."""

from __future__ import annotations

__lazy_modules__ = ("nab_provider.records", "nab_provider.tags")

from typing import TYPE_CHECKING

from ..records import WheelFile
from ..tags import wheel_tag_set

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider._vendor.packaging.tags import Tag
    from nab_provider._vendor.packaging.version import Version

    from ..records import DistFile
    from ..tags import TagSet


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

    __slots__ = ("_groups", "_rows", "_views")

    def __init__(self, distributions: Sequence[tuple[Version, DistFile]]) -> None:
        """Classify the cache-owned snapshot without evaluating metadata."""
        groups: dict[frozenset[Tag] | None, int] = {}
        rows: list[tuple[tuple[Version, DistFile], int | None]] = []
        for pair in distributions:
            _, dist = pair
            if isinstance(dist, WheelFile):
                group = groups.setdefault(wheel_tag_set(dist.filename), len(groups))
                rows.append((pair, group))
            else:
                rows.append((pair, None))

        self._groups = tuple(groups)
        self._rows = tuple(rows)
        self._views: dict[tuple[bool, ...], CandidateView] = {}

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

    def _project(self, accepted: tuple[bool, ...]) -> CandidateView:
        """Filter rows in their original order and retain the diagnostic deltas."""
        result: list[tuple[Version, DistFile]] = []
        exclusions: list[tuple[Version, int]] = []
        run_version: Version | None = None
        run_length = 0
        for pair, group in self._rows:
            version, _ = pair
            if group is None or accepted[group]:
                result.append(pair)
                continue
            if version is not run_version:
                if run_version is not None:
                    exclusions.append((run_version, run_length))
                run_version = version
                run_length = 0
            run_length += 1
        if run_version is not None:
            exclusions.append((run_version, run_length))

        empty_versions = 0
        if exclusions:
            rejected = {version for version, _ in exclusions}
            kept = {version for version, _ in result}
            empty_versions = len(rejected - kept)
        return CandidateView(result, exclusions, empty_versions)
