"""Decision-aware look-ahead for :class:`nab_provider.provider.Provider`.

Owns ``_look_ahead_ok`` and the pending-block tables that record why a
candidate was rejected.  ``flush_pending_blocks`` turns those into
incompatibilities at the end of ``choose_version``.  A decision or
positive-range rejection becomes a grouped binary incompatibility
(``{candidate range, blocker range}``); a candidate blocked by a
requirement on itself names one package on both sides, so the two terms
merge into one and the declared range is kept on the clause.
Version-derived terms are widened onto the listing's gaps, which leaves
the selectable versions they name unchanged.  The blocker term widens
further when every rejection in the group recorded a dependency range:
each fired because the blocker sat outside that range, so every blocker
version outside their union repeats the same rejections.  Groups queued
without ranges, such as the extras block path, keep the narrower term.

A root-requirement or metadata rejection has no blocker to name: nothing
later in the resolve undoes it, so its versions are banned outright.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from ..errors import MetadataError
from .listing_diagnosis import MetadataBlock

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from nab_provider._vendor.packaging.version import Version

    from ..provider import Provider


class DepRangeUnion(NamedTuple):
    """The blocker constraints recorded by one pending group.

    ``union`` accumulates the dependency range each rejected candidate
    declared on the blocker; ``covered`` counts the rejections that
    contributed one.  Widening needs the whole group, so a flush declines
    when ``covered`` falls short of the group's rejection count.

    ``declared`` keeps the distinct ranges in first-recorded order.  Ranges
    that disagree can union into a disjunction no specifier set spells, so a
    failure report states them one by one instead.
    """

    covered: int
    union: VersionRange
    declared: tuple[VersionRange, ...] = ()

    @classmethod
    def zero(cls) -> DepRangeUnion:
        """Return an accumulator with nothing recorded yet."""
        return cls(0, VersionRange.empty())

    def record(self, dep_range: VersionRange) -> DepRangeUnion:
        """Return this accumulator with one more rejection's range folded in."""
        if dep_range in self.declared:
            return DepRangeUnion(self.covered + 1, self.union, self.declared)
        return DepRangeUnion(
            self.covered + 1, self.union | dep_range, (*self.declared, dep_range)
        )


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
    if provider.split_and_normalize(package)[1] is not None:
        return True

    cache_key = (package, version)
    if cache_key not in provider.deps_cache:
        try:
            provider.get_dependencies(package, version)
        except MetadataError as exc:
            provider.pending_metadata_blocks[canonicalize_name(package)].setdefault(
                version, MetadataBlock(str(exc), exc.filtered_sdist_version)
            )
            return False

    deps = provider.deps_cache.get(cache_key, {})
    decisions = provider.solution_decisions if check_decisions else None

    for dep_name, dep_range in deps.items():
        dep_normalized = canonicalize_name(dep_name)

        if dep_normalized in provider.root_requirements:
            root_range = provider.root_requirements[dep_normalized]
            if dep_range.is_disjoint(root_range):
                provider.pending_root_blocks[
                    (package, dep_normalized, dep_range, root_range)
                ].append(version)
                return False

        if decisions is not None and _blocked_by_solution(
            provider, package, version, dep_normalized, dep_range, decisions
        ):
            return False

    return True


def _blocked_by_solution(
    provider: Provider,
    package: str,
    version: Version,
    dep_normalized: str,
    dep_range: VersionRange,
    decisions: Mapping[str, Version],
) -> bool:
    """Whether the partial solution already rules ``dep_range`` out.

    Queues the rejection it finds: against the decision that contradicts the
    range, against the positive range disjoint from it, or, when the candidate
    named itself, under the range it declared.
    """
    decided_version = decisions.get(dep_normalized)
    if decided_version is not None:
        if decided_version in dep_range:
            return False

        decision_key = (package, dep_normalized, decided_version)
        provider.pending_blocks[decision_key].append(version)
        provider.pending_decision_dep_ranges[decision_key] = (
            provider.pending_decision_dep_ranges[decision_key].record(dep_range)
        )
        return True

    # Positive-range disagreement: {candidate==v, dep in pos_range} is
    # impossible.  Sound across backjumps because the ``dep in pos_range``
    # term goes UNDETERMINED if the supporting derivation is reverted.
    pos_range = provider.solution_ranges.get(dep_normalized)
    if pos_range is None or not dep_range.is_disjoint(pos_range):
        return False

    # A self-dependency is grouped by the range it declared, so the merged
    # clause has one edge to name.
    if dep_normalized == package:
        provider.pending_self_blocks[(package, dep_range, pos_range)].append(version)
        return True

    range_key = (package, dep_normalized, pos_range)
    provider.pending_range_blocks[range_key].append(version)
    provider.pending_range_dep_ranges[range_key] = provider.pending_range_dep_ranges[
        range_key
    ].record(dep_range)
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


def _candidate_union(
    provider: Provider, package: str, versions: Iterable[Version]
) -> VersionRange:
    """Return the widened union of one group's rejected candidate versions."""
    union = VersionRange.empty()
    for version in versions:
        union = union | _widen_or_singleton(provider, package, version)
    return union


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


def reset_pending_blocks(provider: Provider) -> None:
    """Drop the rejections queued so far without flushing them into clauses."""
    provider.pending_blocks = defaultdict(list)
    provider.pending_decision_dep_ranges = defaultdict(DepRangeUnion.zero)
    provider.pending_range_blocks = defaultdict(list)
    provider.pending_range_dep_ranges = defaultdict(DepRangeUnion.zero)
    provider.pending_self_blocks = defaultdict(list)
    provider.pending_root_blocks = defaultdict(list)
    provider.pending_metadata_blocks = defaultdict(dict)


def flush_pending_blocks(provider: Provider) -> None:
    """Convert queued rejections into incompatibilities.

    For each ``(candidate_pkg, blocker_pkg, blocker_key)`` group we add
    ``{candidate_pkg in {v1,v2,...}, blocker_pkg in R}``, with each candidate
    version widened through ``widen_decision_gap``: a version's open neighbor
    gap holds no other listed version, so adjacent gaps coalesce without
    changing which versions the clause names.  ``R`` is the membership widening
    when the group recorded a range for every rejection and that widening still
    covers the blocker, otherwise the decided version's gap for decision-keyed
    groups and the captured positive range for range-keyed ones.  Sound across
    backjumps because the blocker term goes UNDETERMINED when the supporting
    decision is reverted, so the candidate range can be reconsidered.

    A candidate rejected by a requirement on itself has no separate blocker:
    both terms name it, so they merge to the rejected versions the declared
    range keeps out, and the clause carries that range for the report.  The
    ban is unconditional, which is sound: a version that requires itself to
    be elsewhere can never be selected.

    Root-requirement and metadata rejections have no such blocker: neither a
    root requirement nor unreadable metadata changes over the resolve, so each
    package's rejected versions go out as one single-term ``NO_VERSIONS``
    clause naming just those versions.  The resolver's own fallback bans the
    whole asked range, including the pre-releases the PEP 440 buffer kept out
    of the scan.
    """
    # Decision-keyed rejections: the blocker term covers the decided version.
    for (
        candidate_pkg,
        blocker_pkg,
        blocker_version,
    ), versions in provider.pending_blocks.items():
        range_union = _candidate_union(provider, candidate_pkg, versions)
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

    # Range-keyed rejections: the blocker term starts from the positive range.
    for (
        candidate_pkg,
        blocker_pkg,
        blocker_range,
    ), versions in provider.pending_range_blocks.items():
        range_union = _candidate_union(provider, candidate_pkg, versions)
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

    # Self-dependency rejections: the candidate is its own blocker, so the
    # two terms merge into one.
    for (
        candidate_pkg,
        dep_range,
        _pos_range,
    ), versions in provider.pending_self_blocks.items():
        rejected = _candidate_union(provider, candidate_pkg, versions)
        merged = Term(candidate_pkg, rejected & dep_range.complement(), positive=True)
        provider.pending_clauses.append(
            Incompatibility(
                [merged],
                cause=IncompatibilityCause.DEPENDENCY,
                dependency_range=dep_range,
            )
        )

    # Permanent rejections: a root requirement is fixed for the whole resolve,
    # and a version whose metadata will not read is unusable in every state.
    unusable: defaultdict[str, VersionRange] = defaultdict(VersionRange.empty)

    for (candidate_pkg, *_), versions in provider.pending_root_blocks.items():
        unusable[candidate_pkg] |= _candidate_union(provider, candidate_pkg, versions)

    for candidate_pkg, versions in provider.pending_metadata_blocks.items():
        unusable[candidate_pkg] |= _candidate_union(provider, candidate_pkg, versions)
        # The ban outlives this flush, so its reason has to as well.
        provider.record_metadata_ban(candidate_pkg, versions)

    for candidate_pkg, rejected in unusable.items():
        provider.pending_clauses.append(
            Incompatibility(
                [Term(candidate_pkg, rejected, positive=True)],
                cause=IncompatibilityCause.NO_VERSIONS,
            )
        )

    reset_pending_blocks(provider)
