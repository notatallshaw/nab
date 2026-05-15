"""Build the final resolution result.

Per the PubGrub spec, a solution must not include packages that
aren't transitively reachable from the root.  This module owns the
BFS that walks the dependency graph from the root incompatibilities
and filters the partial solution's decisions down to that reachable
set.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#result
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .types import IncompatibilityCause, PackageType, VersionType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from .types import Incompatibility, RangeProtocol

__all__ = [
    "build_reachable_decisions",
]


def build_reachable_decisions(
    decisions: Mapping[PackageType, VersionType],
    incompatibilities: Iterable[Incompatibility[PackageType, VersionType]],
    get_dependencies: Callable[
        [PackageType, VersionType], Mapping[PackageType, RangeProtocol[VersionType]]
    ],
    *,
    root_sentinel: Any,
) -> dict[PackageType, VersionType]:
    """Filter ``decisions`` to packages transitively reachable from root.

    ``incompatibilities`` is scanned for clauses with cause ``ROOT``
    to recover the user-specified root requirements.  ``get_dependencies``
    is the provider's ``get_dependencies(package, version)`` method,
    which is used to traverse the dependency graph.
    """
    all_decisions = dict(decisions)
    all_decisions.pop(root_sentinel, None)

    # Recover the user-specified roots from ROOT-cause clauses.
    root_required: set[PackageType] = set()
    for incompatibility in incompatibilities:
        if incompatibility.cause != IncompatibilityCause.ROOT:
            continue
        for term in incompatibility.terms:
            if term.package is not root_sentinel:
                root_required.add(term.package)

    # BFS through the decided graph to find transitively reachable packages.
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

        dependencies = get_dependencies(package, version)
        queue.extend(
            dep_package for dep_package in dependencies if dep_package not in reachable
        )

    return {
        package: version
        for package, version in all_decisions.items()
        if package in reachable
    }
