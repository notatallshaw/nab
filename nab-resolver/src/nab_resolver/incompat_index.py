"""Incompatibility index and dependency-clause merging.

The resolver keeps three structures alongside ``incompatibilities``:
``package_to_incompatibilities`` (package -> list of clause indices) for
unit-propagation lookup, ``dependency_index`` (merge key -> index) for
collapsing many singleton dependency clauses into one
``pkg in {v1, v2, ...}`` clause (pubgrub-rs's ``merge_dependents``), and
``clause_contradicted_at`` (clause index -> epoch), unit propagation's
per-clause skip stamp.
"""

from __future__ import annotations

__lazy_modules__ = ("nab_resolver.types",)

from typing import TYPE_CHECKING, Any

from .types import Incompatibility, IncompatibilityCause, Term

if TYPE_CHECKING:
    from .resolver import Resolver
    from .types import PackageType, RangeProtocol, VersionType


__all__ = ["add_incompatibility", "append_incompatibility"]


# A dependency-style clause is exactly two terms (parent + dep).
_DEPENDENCY_CLAUSE_TERMS = 2

# Stamp for a clause with no term known to be contradicted.  Epochs start at
# zero, so this can never read as current.
_NOT_CONTRADICTED = -1


def add_incompatibility(
    resolver: Resolver[PackageType, VersionType],
    incompatibility: Incompatibility[PackageType, VersionType],
) -> int:
    """Return the index of the added or merged incompatibility."""
    key = _dependency_merge_key(incompatibility)
    if key is None:
        return append_incompatibility(resolver, incompatibility)

    existing = resolver.dependency_index.get(key)
    if existing is None:
        existing = append_incompatibility(resolver, incompatibility)
        resolver.dependency_index[key] = existing
    else:
        _merge_dependency(resolver, existing, incompatibility.terms[0])
    return existing


def append_incompatibility(
    resolver: Resolver[Any, Any],
    incompatibility: Incompatibility[PackageType, VersionType],
) -> int:
    """Store a clause and register each package for unit propagation."""
    index = len(resolver.incompatibilities)
    resolver.incompatibilities.append(incompatibility)
    resolver.clause_contradicted_at.append(_NOT_CONTRADICTED)
    for term in incompatibility.terms:
        resolver.package_to_incompatibilities[term.package].append(index)
    return index


def _dependency_merge_key(
    incompatibility: Incompatibility[PackageType, VersionType],
) -> tuple[PackageType, PackageType, RangeProtocol[VersionType], bool] | None:
    """Return the merge key for a two-term DEPENDENCY clause, else None.

    Every DEPENDENCY clause we emit is ``[package_term (positive), dep_term]``,
    so we trust call-site order: first term is the package, second is the dep.
    Clauses with matching ``(package, dep_package, dep_constraint, dep_positive)``
    tuples can be merged by unioning the package term's positive ranges.
    """
    if (
        incompatibility.cause is not IncompatibilityCause.DEPENDENCY
        or len(incompatibility.terms) != _DEPENDENCY_CLAUSE_TERMS
    ):
        return None

    pkg_term, dep_term = incompatibility.terms
    if not pkg_term.is_positive():
        return None

    return (
        pkg_term.package,
        dep_term.package,
        dep_term.constraint,
        dep_term.is_positive(),
    )


def _merge_dependency(
    resolver: Resolver[PackageType, VersionType],
    existing_index: int,
    parent: Term[PackageType, VersionType],
) -> None:
    """Extend the parent range of the indexed dependency clause."""
    existing = resolver.incompatibilities[existing_index]
    existing_pkg, existing_dep = existing.terms
    merged_constraint = existing_pkg.constraint | parent.constraint
    if merged_constraint == existing_pkg.constraint:
        return

    merged = Incompatibility(
        [
            Term(existing_pkg.package, merged_constraint, positive=True),
            existing_dep,
        ],
        cause=IncompatibilityCause.DEPENDENCY,
    )
    # Keep the old clause intact for derivations that still refer to it.
    resolver.incompatibilities[existing_index] = merged
    # The union widens the package term, which can lift a contradiction.
    resolver.clause_contradicted_at[existing_index] = _NOT_CONTRADICTED
