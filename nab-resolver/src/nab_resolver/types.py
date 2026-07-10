"""Core types for the PubGrub resolver.

Defines Term and Incompatibility, the two main data structures
in the PubGrub algorithm.

A Term is a statement about a package's allowed version range.
An Incompatibility is a set of terms that cannot all be true at once.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#definitions
"""

from __future__ import annotations

import enum
from typing import Generic, Protocol, TypeVar

from typing_extensions import Self, override

__all__ = [
    "Incompatibility",
    "IncompatibilityCause",
    "IncompatibilityState",
    "PackageType",
    "RangeProtocol",
    "SetRelation",
    "Term",
    "VersionType",
]


PackageType = TypeVar("PackageType")
VersionType = TypeVar("VersionType")

# Contravariant so packaging.ranges.VersionRange (accepts Version | str)
# can satisfy RangeProtocol[Version].
VersionType_contra = TypeVar("VersionType_contra", contravariant=True)


class RangeProtocol(Protocol[VersionType_contra]):
    """Contract for version range types used by the resolver.

    Both :class:`nab_resolver.ranges.Range` and
    :class:`packaging.ranges.VersionRange` satisfy this protocol.  Mixing
    range types within a single resolution is unsupported.
    """

    @classmethod
    def empty(cls) -> Self:
        """Create a range containing no versions."""
        ...

    @classmethod
    def full(cls) -> Self:
        """Create a range containing all versions."""
        ...

    @classmethod
    def singleton(cls, version: VersionType_contra) -> Self:
        """Create a range containing exactly one version."""
        ...

    @property
    def is_empty(self) -> bool:
        """``True`` if this range contains no versions."""
        ...

    def __contains__(self, version: VersionType_contra, /) -> bool:
        """Test version membership."""
        ...

    def __and__(self, other: object) -> Self:
        """Intersect two ranges."""
        ...

    def __or__(self, other: object) -> Self:
        """Union two ranges."""
        ...

    def __invert__(self) -> Self:
        """Complement the range."""
        ...

    def __sub__(self, other: object) -> Self:
        """Set difference: versions in self but not in other."""
        ...

    def is_subset(self, other: Self) -> bool:
        """Return whether every version in self is also in other."""
        ...

    def is_superset(self, other: Self) -> bool:
        """Return whether every version in other is also in self."""
        ...

    def is_disjoint(self, other: Self) -> bool:
        """Return whether self and other share no version."""
        ...

    # __eq__ and __hash__ come from object; redeclaring them in the
    # Protocol adds no constraint and mypy and zuban disagree on @override.


class Term(Generic[PackageType, VersionType]):
    """A statement about a package's version constraint.

    Either "package must be in range" (positive)
    or "package must NOT be in range" (negative).

    In PubGrub, terms are the building blocks of incompatibilities.
    A positive term ``foo [2, 5)`` means "foo must be version 2, 3, or 4".
    A negative term ``not foo [2, 5)`` means "foo must NOT be 2, 3, or 4".

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """

    __slots__ = ("_positive", "constraint", "package")

    def __init__(
        self,
        package: PackageType,
        constraint: RangeProtocol[VersionType],
        *,
        positive: bool = True,
    ) -> None:
        """Create a term constraining a package to a version range."""
        self.package = package
        self.constraint = constraint
        self._positive = positive

    def is_positive(self) -> bool:
        """Return True if this is a positive (required) term."""
        return self._positive

    def negate(self) -> Term[PackageType, VersionType]:
        """Return a term with the opposite polarity."""
        return Term(self.package, self.constraint, positive=not self._positive)

    def satisfies(self, assignment: RangeProtocol[VersionType]) -> bool:
        """Check whether this term is satisfied by the given assignment range.

        Positive: satisfied when assignment is a subset of constraint
        (every version in the assignment is also in the constraint).

        Negative: satisfied when assignment is disjoint from constraint
        (no version in the assignment is in the constraint).
        """
        if self._positive:
            return assignment.is_subset(self.constraint)
        return assignment.is_disjoint(self.constraint)

    def intersect(
        self, other: Term[PackageType, VersionType]
    ) -> Term[PackageType, VersionType] | None:
        """Intersect two terms for the same package.

        Returns None if the packages differ.
        """
        if self.package != other.package:
            return None

        if self._positive and other._positive:
            return Term(
                self.package,
                self.constraint & other.constraint,
                positive=True,
            )

        if not self._positive and not other._positive:
            # not(A) AND not(B) = not(A | B)
            return Term(
                self.package,
                self.constraint | other.constraint,
                positive=False,
            )

        # positive AND not(negative) = positive minus negative.
        positive_term = self if self._positive else other
        negative_term = other if self._positive else self
        difference = positive_term.constraint - negative_term.constraint
        return Term(self.package, difference, positive=True)

    @override
    def __repr__(self) -> str:
        """Return a debug representation of the term."""
        sign = "" if self._positive else "not "
        return f"Term({sign}{self.package!r}, {self.constraint})"


class SetRelation(enum.Enum):
    """How the partial solution relates to a term.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """

    SATISFIED = enum.auto()
    CONTRADICTED = enum.auto()
    UNDETERMINED = enum.auto()


class IncompatibilityState(enum.Enum):
    """Result of evaluating an incompatibility against the partial solution."""

    CONFLICT = enum.auto()


class IncompatibilityCause(enum.Enum):
    """Why an incompatibility exists.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#incompatibility
    """

    ROOT = enum.auto()
    """A root package requirement (user-specified)."""

    DEPENDENCY = enum.auto()
    """Package X version V depends on package Y in range R."""

    NO_VERSIONS = enum.auto()
    """No versions of the package exist in the given range."""

    CONSTRAINT = enum.auto()
    """A user-supplied constraint (restricts range only if the package is used)."""

    DERIVED = enum.auto()
    """Derived from two other incompatibilities via resolution.
    See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
    """


class Incompatibility(Generic[PackageType, VersionType]):
    """A set of terms that cannot all be true simultaneously.

    For example, ``{foo >= 2, bar < 1}`` means "it's not possible for
    foo to be >= 2 AND bar to be < 1 at the same time".

    Incompatibilities come from two sources:
    1. External facts (dependencies, missing versions, root requirements)
    2. Derived from two existing incompatibilities during conflict resolution

    The ``cause_left`` and ``cause_right`` fields form a derivation tree
    (a DAG) that can be walked to produce human-readable error messages.

    ``constraint_range`` holds the user's constraint for a ``CONSTRAINT``
    clause. The clause's term carries the requirement range that backtracking
    needs, so the user's constraint is kept here for the message.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#incompatibility
    """

    __slots__ = ("cause", "cause_left", "cause_right", "constraint_range", "terms")

    def __init__(
        self,
        terms: list[Term[PackageType, VersionType]],
        cause: IncompatibilityCause,
        cause_left: Incompatibility[PackageType, VersionType] | None = None,
        cause_right: Incompatibility[PackageType, VersionType] | None = None,
        constraint_range: RangeProtocol[VersionType] | None = None,
    ) -> None:
        """Create an incompatibility with terms and a cause."""
        self.terms = terms
        self.cause = cause
        self.cause_left = cause_left
        self.cause_right = cause_right
        self.constraint_range = constraint_range

    @override
    def __repr__(self) -> str:
        """Return a debug representation."""
        terms_str = ", ".join(repr(term) for term in self.terms)
        return f"Incompatibility([{terms_str}], {self.cause.name.lower()})"
