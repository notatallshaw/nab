"""Decision-aware look-ahead for :class:`nab_python.provider.Provider`.

Owns ``_look_ahead_ok`` and the pending-block tables that record
"this candidate is incompatible with this decision/positive range"
rejections.  Each rejection becomes a grouped binary
incompatibility (``{candidate range, blocker range}``) when
``flush_pending_blocks`` runs at the end of ``choose_version``.
Version-derived terms are widened onto the listing's gaps, which
leaves the selectable versions they name unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from .._vendor.packaging.version import Version
    from ..provider import Provider


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
                provider.pending_blocks[
                    (package, dep_normalized, decided_version)
                ].append(version)
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
                provider.pending_range_dep_ranges[range_key] |= dep_range
                return False

    return True


def _widen_or_singleton(
    provider: Provider, package: str, version: Version
) -> VersionRange:
    """Return ``version``'s widened neighbor gap, or its singleton without one.

    Look-ahead needs the gap contract specifically: the gap contains
    ``version`` and no other listed version, so unions and merge keys name
    exactly the versions the scan rejected.
    """
    widened = provider.widen_decision_gap(package, version)
    return VersionRange.singleton(version) if widened is None else widened


def flush_pending_blocks(provider: Provider) -> None:
    """Convert queued rejections into grouped binary incompatibilities.

    For each ``(candidate_pkg, blocker_pkg, blocker_version)`` group we add
    ``{candidate_pkg in {v1,v2,...}, blocker_pkg==w}``, with each version
    widened through ``widen_decision_gap``: a version's open neighbor gap
    holds no other listed version, so adjacent gaps coalesce without changing
    which versions the clause names.  Sound across backjumps because the blocker
    term goes UNDETERMINED when the supporting decision is reverted, so the
    candidate range can be reconsidered.
    """
    # Decision-keyed rejections: the blocker term names one selectable version.
    for (
        candidate_pkg,
        blocker_pkg,
        blocker_version,
    ), versions in provider.pending_blocks.items():
        range_union = VersionRange.empty()
        for v in versions:
            range_union = range_union | _widen_or_singleton(provider, candidate_pkg, v)
        provider.pending_clauses.append(
            Incompatibility(
                [
                    Term(candidate_pkg, range_union, positive=True),
                    Term(
                        blocker_pkg,
                        _widen_or_singleton(provider, blocker_pkg, blocker_version),
                        positive=True,
                    ),
                ],
                cause=IncompatibilityCause.DEPENDENCY,
            )
        )
    provider.pending_blocks = defaultdict(list)

    # Range-keyed rejections: the blocker term uses the positive range directly.
    for (
        candidate_pkg,
        blocker_pkg,
        blocker_range,
    ), versions in provider.pending_range_blocks.items():
        range_union = VersionRange.empty()
        for v in versions:
            range_union = range_union | _widen_or_singleton(provider, candidate_pkg, v)
        provider.pending_clauses.append(
            Incompatibility(
                [
                    Term(candidate_pkg, range_union, positive=True),
                    Term(blocker_pkg, blocker_range, positive=True),
                ],
                cause=IncompatibilityCause.DEPENDENCY,
            )
        )
    provider.pending_range_blocks = defaultdict(list)
    provider.pending_range_dep_ranges = defaultdict(VersionRange.empty)

    # Root- and metadata-blocks are diagnostic-only; drop them without
    # emitting clauses.
    provider.pending_root_blocks = defaultdict(list)
    provider.pending_metadata_blocks = defaultdict(dict)
