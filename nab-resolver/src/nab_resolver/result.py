"""Build the final resolution result.

Per the PubGrub spec, a solution must not include packages that
aren't transitively reachable from the root.  This module owns the
BFS that walks the dependency graph from the root incompatibilities,
filters the partial solution's decisions down to that reachable set,
and keeps the edges the walk crossed.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#result
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from .types import IncompatibilityCause, PackageType, VersionType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from .types import Incompatibility, RangeProtocol

__all__ = ["build_solution_data"]


def build_solution_data(
    decisions: Mapping[PackageType, VersionType],
    incompatibilities: Iterable[Incompatibility[PackageType, VersionType]],
    get_dependencies: Callable[
        [PackageType, VersionType], Mapping[PackageType, RangeProtocol[VersionType]]
    ],
    *,
    root_sentinel: Any,
) -> tuple[
    dict[PackageType, VersionType],
    tuple[tuple[PackageType, PackageType], ...],
    tuple[PackageType, ...],
]:
    """Return pins, edges, and roots for decisions reachable from the root.

    ``incompatibilities`` is scanned for clauses with cause ``ROOT``
    to recover the user-specified root requirements.  ``get_dependencies``
    is the provider's ``get_dependencies(package, version)`` method,
    which is used to traverse the dependency graph.  Every dependency it
    reports for a reachable package becomes an edge.
    """
    all_decisions = dict(decisions)
    all_decisions.pop(root_sentinel, None)

    # Keep each root's first appearance so traversal follows the caller's order.
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
    queue: deque[PackageType] = deque(root_required)
    while queue:
        package = queue.popleft()
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

    return (
        {
            package: version
            for package, version in all_decisions.items()
            if package in reachable
        },
        tuple(edges),
        tuple(root_required),
    )
