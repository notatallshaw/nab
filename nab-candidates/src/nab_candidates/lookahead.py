"""Check whether a release's dependencies fit the current package choices."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from packaging.ranges import VersionRange
    from packaging.version import Version

__all__ = [
    "LOOKAHEAD_ABORT_THRESHOLD",
    "MAX_FORCE_BACKTRACKS_PER_PACKAGE",
    "DecisionConflict",
    "RangeConflict",
    "find_dependency_conflict",
    "queue_blocker",
]

# Reconsider a blocker only when every rejection in a scan points to its
# same decision. Repeated backtracks help on long histories but can oscillate.
LOOKAHEAD_ABORT_THRESHOLD = 4
MAX_FORCE_BACKTRACKS_PER_PACKAGE = 3


class DecisionConflict(NamedTuple):
    """A selected version outside a release's declared dependency range."""

    package: str
    required: VersionRange
    version: Version


class RangeConflict(NamedTuple):
    """A root or derived bound disjoint from the release's dependency."""

    package: str
    required: VersionRange
    allowed: VersionRange
    root: bool


def find_dependency_conflict(
    dependencies: Mapping[str, VersionRange],
    roots: Mapping[str, VersionRange],
    decisions: Mapping[str, Version] | None,
    ranges: Mapping[str, VersionRange],
    *,
    normalize: Callable[[str], str] | None = None,
) -> DecisionConflict | RangeConflict | None:
    """Return the first conflicted declaration, checking root bounds first.

    Passing no decisions limits the scan to root requirements. A concrete
    decision takes precedence over the wider range from which it was chosen.
    """
    for name, required in dependencies.items():
        package = normalize(name) if normalize is not None else name
        root = roots.get(package)
        if root is not None and required.is_disjoint(root):
            return RangeConflict(package, required, root, root=True)

        if decisions is None:
            continue

        version = decisions.get(package)
        if version is not None:
            if version not in required:
                return DecisionConflict(package, required, version)
            continue

        allowed = ranges.get(package)
        if allowed is not None and required.is_disjoint(allowed):
            return RangeConflict(package, required, allowed, root=False)

    return None


def queue_blocker(
    package: str,
    counts: dict[str, int],
    pending: list[str],
    *,
    limit: int = MAX_FORCE_BACKTRACKS_PER_PACKAGE,
) -> None:
    """Queue a repeated blocker for reconsideration within its resolution budget."""
    count = counts.get(package, 0)
    if package not in pending and count < limit:
        pending.append(package)
        counts[package] = count + 1
