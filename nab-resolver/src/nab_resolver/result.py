"""Build the final resolution result.

Per the PubGrub spec, a solution must not include packages that
aren't transitively reachable from the root.  This module owns the
BFS that walks the dependency graph from the root incompatibilities,
filters the partial solution's decisions down to that reachable set,
and keeps the edges the walk crossed.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#result
"""

from __future__ import annotations

__lazy_modules__ = (
    "collections",
    "nab_resolver.types",
)

from collections import deque
from typing import TYPE_CHECKING, Any, TypeVar

from .types import IncompatibilityCause, PackageType, VersionType

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator, Mapping

    from .types import Incompatibility, RangeProtocol

__all__ = ["IncompleteSelectionError", "build_solution_data", "walk_selected"]

SelectedCandidateType = TypeVar("SelectedCandidateType")


class IncompleteSelectionError(Exception):
    """A reachable package lacks a selected candidate or readable dependencies."""

    def __init__(self, package: object) -> None:
        """Retain the first incomplete package in traversal order."""
        self.package = package
        super().__init__(f"Incomplete selected metadata for {package!r}")


def walk_selected(
    roots: Iterable[PackageType],
    selected: Mapping[PackageType, SelectedCandidateType],
    dependency_names: Callable[
        [PackageType, SelectedCandidateType], Collection[PackageType] | None
    ],
    *,
    depth_first: bool = False,
) -> Iterator[tuple[PackageType, SelectedCandidateType, Collection[PackageType]]]:
    """Visit the selected reachable graph once using the caller's metadata policy.

    None from ``dependency_names`` means unread metadata; callback exceptions propagate.
    """
    seen: set[PackageType] = set()
    pending: deque[PackageType] = deque(roots)
    while pending:
        package = pending.pop() if depth_first else pending.popleft()
        if package in seen:
            continue

        candidate = selected.get(package)
        if candidate is None:
            raise IncompleteSelectionError(package)

        dependencies = dependency_names(package, candidate)
        if dependencies is None:
            raise IncompleteSelectionError(package)

        # Revisit suppression applies only after the caller has supplied metadata.
        seen.add(package)
        yield package, candidate, dependencies
        pending.extend(child for child in dependencies if child not in seen)


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
    try:
        for package, _, dependencies in walk_selected(
            root_required, all_decisions, get_dependencies
        ):
            reachable.add(package)
            edges.extend((package, child) for child in dependencies)
    except IncompleteSelectionError as error:
        unreachable = f"Bug: reachable package {error.package!r} has no decision"
        raise RuntimeError(unreachable) from error

    return (
        {
            package: version
            for package, version in all_decisions.items()
            if package in reachable
        },
        tuple(edges),
        tuple(root_required),
    )
