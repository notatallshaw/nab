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

from typing import TYPE_CHECKING, Any

from .types import Incompatibility, IncompatibilityCause, Term

if TYPE_CHECKING:
    from .resolver import Resolver


__all__ = [
    "add_incompatibility",
    "dependency_merge_key",
    "index_dependency",
    "maybe_merge_dependency",
]


# A dependency-style clause is exactly two terms (parent + dep).
_DEPENDENCY_CLAUSE_TERMS = 2

# Stamp for a clause with no term known to be contradicted.  Epochs start at
# zero, so this can never read as current.
_NOT_CONTRADICTED = -1


def add_incompatibility(
    resolver: Resolver[Any, Any], incompatibility: Incompatibility[Any, Any]
) -> None:
    """Add an incompatibility, merging into an existing clause if possible."""
    if maybe_merge_dependency(resolver, incompatibility):
        return

    index = len(resolver.incompatibilities)
    resolver.incompatibilities.append(incompatibility)
    resolver.clause_contradicted_at.append(_NOT_CONTRADICTED)
    for term in incompatibility.terms:
        resolver.package_to_incompatibilities[term.package].append(index)
    index_dependency(resolver, incompatibility, index)


def dependency_merge_key(
    incompatibility: Incompatibility[Any, Any],
) -> tuple[Any, Any, Any, bool] | None:
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


def index_dependency(
    resolver: Resolver[Any, Any],
    incompatibility: Incompatibility[Any, Any],
    index: int,
) -> None:
    """Record this incompatibility as the canonical clause for its key."""
    key = dependency_merge_key(incompatibility)
    if key is not None:
        resolver.dependency_index[key] = index


def maybe_merge_dependency(
    resolver: Resolver[Any, Any], incompatibility: Incompatibility[Any, Any]
) -> bool:
    """Try to merge ``incompatibility`` into an existing clause.

    Returns ``True`` if a merge happened (caller should not also append).
    Replaces the existing package term with the union; the dep term stays
    unchanged so semantics are preserved.
    """
    key = dependency_merge_key(incompatibility)
    if key is None:
        return False

    existing_index = resolver.dependency_index.get(key)
    if existing_index is None:
        return False

    existing = resolver.incompatibilities[existing_index]
    existing_pkg, existing_dep = existing.terms
    new_pkg, _ = incompatibility.terms
    merged_constraint = existing_pkg.constraint | new_pkg.constraint
    if merged_constraint == existing_pkg.constraint:
        return True

    merged = Incompatibility(
        [
            Term(existing_pkg.package, merged_constraint, positive=True),
            existing_dep,
        ],
        cause=IncompatibilityCause.DEPENDENCY,
    )
    # Safe to replace in place: DEPENDENCY clauses have no cause_left/right
    # references that would break.
    resolver.incompatibilities[existing_index] = merged
    # The union widens the package term, which can lift a contradiction.
    resolver.clause_contradicted_at[existing_index] = _NOT_CONTRADICTED
    return True
