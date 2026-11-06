"""Recover original dependency requests from a PubGrub failure proof."""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Sequence
from typing import Generic, TypeVar

from .root import ROOT
from .types import Incompatibility, IncompatibilityCause, RangeProtocol, Term

__all__ = [
    "DependencyCause",
    "dependency_terms",
    "external_clauses",
    "select_dependency_causes",
]

PackageType = TypeVar("PackageType", bound=Hashable)
VersionType = TypeVar("VersionType", bound=Hashable)


class DependencyCause(Generic[PackageType, VersionType]):
    """An original request and the parent versions responsible for it."""

    parent: PackageType | None
    package: PackageType
    parent_range: RangeProtocol[VersionType] | None
    constraint: RangeProtocol[VersionType]

    __slots__ = ("constraint", "package", "parent", "parent_range")

    def __init__(
        self,
        parent: PackageType | None,
        package: PackageType,
        parent_range: RangeProtocol[VersionType] | None,
        constraint: RangeProtocol[VersionType],
    ) -> None:
        """Retain one original request without changing its ranges."""
        self.parent = parent
        self.package = package
        self.parent_range = parent_range
        self.constraint = constraint


_DEPENDENCY_TERMS = 2


def external_clauses(
    derivation: Incompatibility[PackageType, VersionType] | None,
    *,
    left_first: bool = True,
) -> list[Incompatibility[PackageType, VersionType]]:
    """Return original facts once in the caller's chosen proof order."""
    if derivation is None:
        return []

    return [
        clause
        for clause in derivation.walk(left_first=left_first)
        if clause.cause is not IncompatibilityCause.DERIVED
    ]


def dependency_terms(
    clause: Incompatibility[PackageType, VersionType],
    terms: Sequence[Term[PackageType, VersionType]] | None = None,
) -> tuple[Term[PackageType, VersionType], Term[PackageType, VersionType]] | None:
    """Recover dependency sides, including merged self-dependency terms."""
    if clause.cause is not IncompatibilityCause.DEPENDENCY:
        return None

    terms = clause.terms if terms is None else terms
    if len(terms) == _DEPENDENCY_TERMS:
        return terms[0], terms[1]

    if len(terms) != 1 or clause.dependency_range is None:
        return None

    parent = terms[0]
    return parent, Term(parent.package, clause.dependency_range, positive=False)


def _requested_cause(
    clause: Incompatibility[PackageType, VersionType],
) -> DependencyCause[PackageType, VersionType] | None:
    """Read a root or dependency clause without treating guards as requests."""
    positive = [term for term in clause.terms if term.is_positive()]
    negative = [term for term in clause.terms if not term.is_positive()]

    # A merged self-dependency keeps its requested range on the clause.
    if len(positive) == 1 and clause.dependency_range is not None:
        parent = positive[0]
        return DependencyCause(
            parent.package, parent.package, parent.constraint, clause.dependency_range
        )

    if len(positive) != 1 or len(negative) != 1:
        return None

    parent, requested = positive[0], negative[0]
    return DependencyCause(
        None if parent.package is ROOT else parent.package,
        requested.package,
        None if parent.package is ROOT else parent.constraint,
        requested.constraint,
    )


def _unavailable_ranges(
    clauses: Sequence[Incompatibility[PackageType, VersionType]],
) -> tuple[set[PackageType], dict[PackageType, RangeProtocol[VersionType]]]:
    """Collect blamed packages and proven absence domains."""
    unavailable: dict[PackageType, RangeProtocol[VersionType]] = {}
    blamed: set[PackageType] = set()
    for clause in clauses:
        # Contextual absence names its unavailable package before its guards.
        if clause.cause is IncompatibilityCause.CONTEXTUAL_NO_VERSIONS:
            package = clause.terms[0].package
        else:
            positive = [term for term in clause.terms if term.is_positive()]
            if len(positive) != 1:
                continue
            package = positive[0].package

        if clause.cause is IncompatibilityCause.CONSTRAINT:
            blamed.add(package)
        elif clause.cause in {
            IncompatibilityCause.NO_VERSIONS,
            IncompatibilityCause.CONTEXTUAL_NO_VERSIONS,
        }:
            bounds = clause.terms[0].constraint
            previous = unavailable.get(package)
            unavailable[package] = bounds if previous is None else previous | bounds

    return blamed, unavailable


def select_dependency_causes(
    clauses: Sequence[Incompatibility[PackageType, VersionType]],
) -> list[DependencyCause[PackageType, VersionType]]:
    """Attribute absence and contradictory requests to original dependency edges."""
    blamed, unavailable = _unavailable_ranges(clauses)
    requested: list[DependencyCause[PackageType, VersionType]] = []
    impossible: set[
        tuple[PackageType | None, PackageType, RangeProtocol[VersionType] | None]
    ] = set()

    for clause in clauses:
        if clause.cause not in {
            IncompatibilityCause.ROOT,
            IncompatibilityCause.DEPENDENCY,
        }:
            continue
        cause = _requested_cause(clause)
        if cause is None:
            continue
        requested.append(cause)
        if clause.cause is IncompatibilityCause.DEPENDENCY and any(
            not term.is_positive() and term.constraint.is_empty for term in clause.terms
        ):
            impossible.add((cause.parent, cause.package, cause.parent_range))

        absent = unavailable.get(cause.package)
        if absent is not None and cause.constraint.is_subset(absent):
            blamed.add(cause.package)

    # An impossible dependency already explains why its parent cannot be selected.
    upstream = {parent for parent, package, _ in impossible if parent != package}
    selected = [
        cause
        for cause in requested
        if (cause.parent, cause.package, cause.parent_range) in impossible
        or (cause.package in blamed and cause.package not in upstream)
    ]
    if selected or not requested:
        return selected

    counts = Counter(cause.package for cause in requested)
    most_requested = max(counts.values())
    return [cause for cause in requested if counts[cause.package] == most_requested]
