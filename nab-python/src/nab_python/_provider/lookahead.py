"""Decision-aware look-ahead for :class:`nab_python.provider.Provider`.

Owns ``_look_ahead_ok`` and the pending-block tables that record
"this candidate is incompatible with this decision/positive range"
rejections.  Each rejection becomes a grouped binary
incompatibility (``{candidate range, blocker range}``) when
``flush_pending_blocks`` runs at the end of ``choose_version``.
Version-derived terms are widened onto the listing's gaps, which
leaves the selectable versions they name unchanged.  The blocker term
widens further when every rejection in the group recorded a dependency
range: each fired because the blocker sat outside that range, so every
blocker version outside their union repeats the same rejections.
Groups queued without ranges, such as the extras block path, keep the
narrower term.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple

from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from .._vendor.packaging.version import Version
    from ..provider import Provider


class DepRangeUnion(NamedTuple):
    """The blocker constraints recorded by one pending group.

    ``union`` accumulates the dependency range each rejected candidate
    declared on the blocker; ``covered`` counts the rejections that
    contributed one.  Widening needs the whole group, so a flush declines
    when ``covered`` falls short of the group's rejection count.
    """

    covered: int
    union: VersionRange

    @classmethod
    def zero(cls) -> DepRangeUnion:
        """Return an accumulator with nothing recorded yet."""
        return cls(0, VersionRange.empty())

    def record(self, dep_range: VersionRange) -> DepRangeUnion:
        """Return this accumulator with one more rejection's range folded in."""
        return DepRangeUnion(self.covered + 1, self.union | dep_range)


def look_ahead_ok(
    provider: Provider,
    package: str,
    version: Version,
    *,
    check_decisions: bool = True,
) -> bool:
    """Check candidate compatibility with root reqs and decisions.

    With ``check_decisions=False`` only the root-requirement check runs;
    used for "subsequent candidate" iterations to avoid per-candidate clause
    growth on tight version-locks.  Extras proxies are skipped (the base's
    look-ahead is sufficient).

    ``MetadataError`` (including ``UnsupportedSdistError``) is treated as a
    rejection so the resolver moves on; the message is recorded for the
    eventual no-versions diagnostic.
    """
    # Late import: provider imports this module at module load.
    from ..provider import MetadataError

    if provider.split_and_normalize(package)[1] is not None:
        return True

    cache_key = (package, version)
    if cache_key not in provider.deps_cache:
        try:
            provider.get_dependencies(package, version)
        except MetadataError as exc:
            provider.pending_metadata_blocks[canonicalize_name(package)].setdefault(
                version, str(exc)
            )
            return False

    deps = provider.deps_cache.get(cache_key, {})
    decisions = provider.solution_decisions if check_decisions else None

    for dep_name, dep_range in deps.items():
        dep_normalized = canonicalize_name(dep_name)

        # Root-requirement disagreement: diagnostic-only (the resolver
        # already has the clause via its root_requirements input).
        if dep_normalized in provider.root_requirements:
            root_range = provider.root_requirements[dep_normalized]
            if (dep_range & root_range).is_empty:
                provider.pending_root_blocks[
                    (package, dep_normalized, dep_range, root_range)
                ].append(version)
                return False

        if decisions is not None:
            decided_version = decisions.get(dep_normalized)
            if decided_version is not None and decided_version not in dep_range:
                decision_key = (package, dep_normalized, decided_version)
                provider.pending_blocks[decision_key].append(version)
                provider.pending_decision_dep_ranges[decision_key] = (
                    provider.pending_decision_dep_ranges[decision_key].record(dep_range)
                )
                return False

            # Positive-range disagreement: {candidate==v, dep in pos_range}
            # is impossible.  Sound across backjumps because the
            # ``dep in pos_range`` term goes UNDETERMINED if the supporting
            # derivation is reverted.
            pos_range = provider.solution_ranges.get(dep_normalized)
            if (
                pos_range is not None
                and decided_version is None
                and (dep_range & pos_range).is_empty
            ):
                range_key = (package, dep_normalized, pos_range)
                provider.pending_range_blocks[range_key].append(version)
                provider.pending_range_dep_ranges[range_key] = (
                    provider.pending_range_dep_ranges[range_key].record(dep_range)
                )
                return False

    return True


def _widen_or_singleton(
    provider: Provider, package: str, version: Version
) -> VersionRange:
    """Return ``version``'s widened neighbor gap, or its singleton without one.

    Look-ahead needs the gap rather than a ``widen_decision`` span: the gap
    contains ``version`` and no other listed version, so unions and merge
    keys name exactly the versions the scan rejected.
    """
    widened = provider.widen_decision_gap(package, version)
    return VersionRange.singleton(version) if widened is None else widened


def _membership_widened(
    accumulated: DepRangeUnion, rejections: int
) -> VersionRange | None:
    """Return the blocker range every rejection in the group rules out.

    Each rejection recorded the candidate's dependency range on the blocker
    and fired because the blocker sat outside it, so a blocker version
    outside all of them reproduces the whole group and the complement of
    the union is the largest sound term.  Returns ``None`` when a rejection
    contributed no range, leaving the group on its narrower term.

    Dependency ranges never admit arbitrary strings and
    :meth:`VersionRange.complement` drops the pre-release opt-in region, so
    the complement opens no admission the rejections did not already have.
    """
    if accumulated.covered != rejections:
        return None
    return accumulated.union.complement()


def flush_pending_blocks(provider: Provider) -> None:
    """Convert queued rejections into grouped binary incompatibilities.

    For each ``(candidate_pkg, blocker_pkg, blocker_key)`` group we add
    ``{candidate_pkg in {v1,v2,...}, blocker_pkg in R}``, with each candidate
    version widened through ``widen_decision_gap``: a version's open neighbor
    gap holds no other listed version, so adjacent gaps coalesce without
    changing which versions the clause names.  ``R`` is the membership widening
    when the group recorded a range for every rejection and that widening still
    covers the blocker, otherwise the decided version's gap for decision-keyed
    groups and the captured positive range for range-keyed ones.  Sound across
    backjumps
    because the blocker term goes UNDETERMINED when the supporting decision is
    reverted, so the candidate range can be reconsidered.
    """
    # Decision-keyed rejections: the blocker term covers the decided version.
    for (
        candidate_pkg,
        blocker_pkg,
        blocker_version,
    ), versions in provider.pending_blocks.items():
        range_union = VersionRange.empty()
        for v in versions:
            range_union = range_union | _widen_or_singleton(provider, candidate_pkg, v)
        # The decided version lies outside every recorded dependency range, so
        # the widening contains it; the check fences a group whose ranges
        # disagree with its blocker.  Asked as a subset test rather than ``in``
        # because ``in`` matches a ``===`` literal by string, while the resolver
        # compares versions when deciding whether the clause asserts.
        membership = _membership_widened(
            provider.pending_decision_dep_ranges[
                (candidate_pkg, blocker_pkg, blocker_version)
            ],
            len(versions),
        )
        blocker_term = (
            membership
            if membership is not None
            and VersionRange.singleton(blocker_version).is_subset(membership)
            else _widen_or_singleton(provider, blocker_pkg, blocker_version)
        )
        provider.pending_clauses.append(
            Incompatibility(
                [
                    Term(candidate_pkg, range_union, positive=True),
                    Term(blocker_pkg, blocker_term, positive=True),
                ],
                cause=IncompatibilityCause.DEPENDENCY,
            )
        )
    provider.pending_blocks = defaultdict(list)
    provider.pending_decision_dep_ranges = defaultdict(DepRangeUnion.zero)

    # Range-keyed rejections: the blocker term starts from the positive range.
    for (
        candidate_pkg,
        blocker_pkg,
        blocker_range,
    ), versions in provider.pending_range_blocks.items():
        range_union = VersionRange.empty()
        for v in versions:
            range_union = range_union | _widen_or_singleton(provider, candidate_pkg, v)
        # Every recorded dependency range was disjoint from the positive range,
        # so the widening covers it; the check fences a group whose ranges
        # disagree with its blocker.
        membership = _membership_widened(
            provider.pending_range_dep_ranges[
                (candidate_pkg, blocker_pkg, blocker_range)
            ],
            len(versions),
        )
        blocker_term = (
            membership
            if membership is not None and (blocker_range - membership).is_empty
            else blocker_range
        )
        provider.pending_clauses.append(
            Incompatibility(
                [
                    Term(candidate_pkg, range_union, positive=True),
                    Term(blocker_pkg, blocker_term, positive=True),
                ],
                cause=IncompatibilityCause.DEPENDENCY,
            )
        )
    provider.pending_range_blocks = defaultdict(list)
    provider.pending_range_dep_ranges = defaultdict(DepRangeUnion.zero)

    # Root- and metadata-blocks are diagnostic-only; drop them without
    # emitting clauses.
    provider.pending_root_blocks = defaultdict(list)
    provider.pending_metadata_blocks = defaultdict(dict)
