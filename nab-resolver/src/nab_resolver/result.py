"""Build the final resolution result.

Per the PubGrub spec, a solution must not include packages that
aren't transitively reachable from the root.  This module owns the
BFS that walks the dependency graph from the root incompatibilities,
filters the partial solution's decisions down to that reachable set,
and keeps the edges the walk crossed.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#result
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic

from .types import IncompatibilityCause, PackageType, VersionType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from .types import Incompatibility, RangeProtocol

__all__ = [
    "Solution",
    "build_solution",
]


@dataclass(frozen=True)
class Solution(Generic[PackageType, VersionType]):
    """A finished resolution: what was chosen, and how it was reached.

    ``pins`` maps every transitively reachable package to its decided
    version.  ``roots`` are the packages the caller required directly, in
    the order they were required.  ``edges`` are ``(parent, child)`` pairs,
    one per dependency the reachability walk crossed, in breadth-first
    order from ``roots``; a pair appears once, and both ends of every edge
    are keys of ``pins``.

    An edge records only that one pinned package depends on another, with
    no version range and no requirement text, so reading the graph needs
    nothing this package cannot express.  A caller that installs what it
    resolves has to put a dependency on disk before its dependent, and
    that ordering is a property of the graph rather than of the pins.  A
    caller wanting one rooted graph joins ``roots`` to a root node of its
    own.
    """

    pins: dict[PackageType, VersionType]
    edges: tuple[tuple[PackageType, PackageType], ...]
    roots: tuple[PackageType, ...]


def build_solution(
    decisions: Mapping[PackageType, VersionType],
    incompatibilities: Iterable[Incompatibility[PackageType, VersionType]],
    get_dependencies: Callable[
        [PackageType, VersionType], Mapping[PackageType, RangeProtocol[VersionType]]
    ],
    *,
    root_sentinel: Any,
) -> Solution[PackageType, VersionType]:
    """Filter ``decisions`` to packages transitively reachable from root.

    ``incompatibilities`` is scanned for clauses with cause ``ROOT``
    to recover the user-specified root requirements.  ``get_dependencies``
    is the provider's ``get_dependencies(package, version)`` method,
    which is used to traverse the dependency graph.  Every dependency it
    reports for a reachable package becomes an edge: the walk asks for
    them to find the reachable set, so the graph costs nothing more.
    """
    all_decisions = dict(decisions)
    all_decisions.pop(root_sentinel, None)

    # Recover the user-specified roots from ROOT-cause clauses.  Insertion
    # ordered rather than a set, so the walk that starts here, and the edge
    # order that comes out of it, do not move with the hash seed.
    root_required: dict[PackageType, None] = {}
    for incompatibility in incompatibilities:
        if incompatibility.cause != IncompatibilityCause.ROOT:
            continue
        for term in incompatibility.terms:
            if term.package is not root_sentinel:
                root_required[term.package] = None

    # BFS through the decided graph to find transitively reachable packages,
    # recording each dependency crossed on the way.
    edges: list[tuple[PackageType, PackageType]] = []
    reachable: set[PackageType] = set()
    queue: list[PackageType] = list(root_required)
    while queue:
        package = queue.pop(0)
        if package in reachable:
            continue
        reachable.add(package)

        version = all_decisions.get(package)
        if version is None:  # pragma: no cover
            unreachable = f"Bug: reachable package {package!r} has no decision"
            raise RuntimeError(unreachable)

        for dep_package in get_dependencies(package, version):
            edges.append((package, dep_package))
            if dep_package not in reachable:
                queue.append(dep_package)

    return Solution(
        pins={
            package: version
            for package, version in all_decisions.items()
            if package in reachable
        },
        edges=tuple(edges),
        roots=tuple(root_required),
    )
