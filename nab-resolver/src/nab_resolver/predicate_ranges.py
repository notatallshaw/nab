"""Boolean restrictions over host predicates and exact candidate identities."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from typing import Generic, TypeAlias, TypeVar

from ._compat import override
from .types import RangeRelation

__all__ = ["PredicateAlgebra", "PredicateRange"]

CandidateType = TypeVar("CandidateType", bound=Hashable)
PredicateNode: TypeAlias = "bool | Decision[CandidateType]"


class _Predicate(Generic[CandidateType]):
    """One stable host predicate in an operation's Boolean ordering."""

    __slots__ = ("matches", "order")

    def __init__(self, order: int, matches: Callable[[CandidateType], bool]) -> None:
        """Retain one predicate and its stable order."""
        self.order = order
        self.matches = matches


class Decision(Generic[CandidateType]):
    """A shared Boolean decision with distinct false and true continuations."""

    __slots__ = ("high", "low", "predicate")

    def __init__(
        self,
        predicate: _Predicate[CandidateType],
        low: PredicateNode[CandidateType],
        high: PredicateNode[CandidateType],
    ) -> None:
        """Retain the predicate and its two continuations."""
        self.predicate = predicate
        self.low = low
        self.high = high


def matches(node: PredicateNode[CandidateType], candidate: CandidateType) -> bool:
    """Evaluate a Boolean path against one real host candidate."""
    while isinstance(node, Decision):
        node = node.high if node.predicate.matches(candidate) else node.low
    return node


class PredicateAlgebra(Generic[CandidateType]):
    """Own canonical Boolean nodes and operation memoization for one solve."""

    def __init__(self) -> None:
        """Start operation-owned node and calculation caches."""
        self.predicates: list[_Predicate[CandidateType]] = []
        self.nodes: dict[
            tuple[
                _Predicate[CandidateType],
                PredicateNode[CandidateType],
                PredicateNode[CandidateType],
            ],
            Decision[CandidateType],
        ] = {}
        self.negations: dict[
            PredicateNode[CandidateType], PredicateNode[CandidateType]
        ] = {True: False, False: True}
        self.operations: dict[
            tuple[str, PredicateNode[CandidateType], PredicateNode[CandidateType]],
            PredicateNode[CandidateType],
        ] = {}

    def node(
        self,
        predicate: _Predicate[CandidateType],
        low: PredicateNode[CandidateType],
        high: PredicateNode[CandidateType],
    ) -> PredicateNode[CandidateType]:
        """Reuse the canonical node for two distinct continuations."""
        if low is high:
            return low

        key = predicate, low, high
        if key not in self.nodes:
            self.nodes[key] = Decision(predicate, low, high)
        return self.nodes[key]

    def combine(
        self,
        operation: str,
        left: PredicateNode[CandidateType],
        right: PredicateNode[CandidateType],
    ) -> PredicateNode[CandidateType]:
        """Apply a Boolean operation in predicate order."""
        initial = operation, left, right
        pending = [(left, right)]
        while pending:
            left, right = pending[-1]
            key = operation, left, right
            if key in self.operations:
                pending.pop()
                continue
            if left is right:
                self.operations[key] = left
            elif isinstance(left, bool):
                self.operations[key] = right if left == (operation == "and") else left
            elif isinstance(right, bool):
                self.operations[key] = left if right == (operation == "and") else right
            else:
                predicate = min(
                    left.predicate, right.predicate, key=lambda item: item.order
                )
                low = (
                    left.low if left.predicate is predicate else left,
                    right.low if right.predicate is predicate else right,
                )
                high = (
                    left.high if left.predicate is predicate else left,
                    right.high if right.predicate is predicate else right,
                )
                low_key = operation, *low
                high_key = operation, *high
                if low_key not in self.operations:
                    pending.append(low)
                elif high_key not in self.operations:
                    pending.append(high)
                else:
                    self.operations[key] = self.node(
                        predicate, self.operations[low_key], self.operations[high_key]
                    )
        return self.operations[initial]

    def negate(
        self, node: PredicateNode[CandidateType]
    ) -> PredicateNode[CandidateType]:
        """Complement a shared formula without using the interpreter call stack."""
        pending = [node]
        while pending:
            current = pending[-1]
            if current in self.negations:
                pending.pop()
                continue
            assert isinstance(current, Decision)
            if current.low not in self.negations:
                pending.append(current.low)
            elif current.high not in self.negations:
                pending.append(current.high)
            else:
                self.negations[current] = self.node(
                    current.predicate,
                    self.negations[current.low],
                    self.negations[current.high],
                )
        return self.negations[node]

    def restriction(
        self, matches: Callable[[CandidateType], bool]
    ) -> PredicateRange[CandidateType]:
        """Bind a native predicate in this operation's stable variable order."""
        predicate = _Predicate(len(self.predicates), matches)
        self.predicates.append(predicate)
        return PredicateRange(self.node(predicate, low=False, high=True), algebra=self)


class PredicateRange(Generic[CandidateType]):
    """A Boolean predicate with finite exact-candidate exceptions."""

    __slots__ = ("_hash", "algebra", "default", "overrides")

    def __init__(
        self,
        default: PredicateNode[CandidateType],
        overrides: Iterable[tuple[CandidateType, bool]] = (),
        *,
        algebra: PredicateAlgebra[CandidateType] | None = None,
    ) -> None:
        """Snapshot the formula and exact candidate exceptions."""
        self.default = default
        self.overrides = frozenset(
            (candidate, accepted)
            for candidate, accepted in dict(overrides).items()
            if accepted != matches(default, candidate)
        )
        self.algebra = algebra
        self._hash = hash((default, self.overrides))

    @classmethod
    def empty(cls) -> PredicateRange[CandidateType]:
        """Exclude every predicate assignment and candidate."""
        return cls(default=False)

    @classmethod
    def full(cls) -> PredicateRange[CandidateType]:
        """Admit every predicate assignment and candidate."""
        return cls(default=True)

    @classmethod
    def singleton(cls, version: CandidateType) -> PredicateRange[CandidateType]:
        """Admit one exact candidate identity."""
        return cls(default=False, overrides=((version, True),))

    @property
    def is_empty(self) -> bool:
        """Report an empty formula with no admitted exceptions."""
        return self.default is False and not any(value for _, value in self.overrides)

    def __contains__(self, candidate: CandidateType) -> bool:
        """Evaluate the exact exception or native predicate formula."""
        for known, accepted in self.overrides:
            if candidate == known:
                return accepted
        return matches(self.default, candidate)

    def _combine(
        self, other: PredicateRange[CandidateType], operation: str
    ) -> PredicateRange[CandidateType]:
        algebra = self.algebra or other.algebra
        if (
            self.algebra is not None
            and other.algebra is not None
            and self.algebra is not other.algebra
        ):
            message = "predicate ranges belong to different operations"
            raise ValueError(message)

        default: PredicateNode[CandidateType]
        if algebra is None:
            assert isinstance(self.default, bool)
            assert isinstance(other.default, bool)
            default = (
                self.default and other.default
                if operation == "and"
                else self.default or other.default
            )
        else:
            default = algebra.combine(operation, self.default, other.default)

        candidates = {candidate for candidate, _ in self.overrides | other.overrides}
        overrides = (
            (
                candidate,
                (candidate in self and candidate in other)
                if operation == "and"
                else (candidate in self or candidate in other),
            )
            for candidate in candidates
        )
        return PredicateRange(default, overrides, algebra=algebra)

    def __and__(self, other: object) -> PredicateRange[CandidateType]:
        """Intersect two symbolic candidate restrictions."""
        if not isinstance(other, PredicateRange):
            return NotImplemented
        return self._combine(other, "and")

    def __or__(self, other: object) -> PredicateRange[CandidateType]:
        """Unite two symbolic candidate restrictions."""
        if not isinstance(other, PredicateRange):
            return NotImplemented
        return self._combine(other, "or")

    def __invert__(self) -> PredicateRange[CandidateType]:
        """Complement the formula and candidate exceptions."""
        default: PredicateNode[CandidateType]
        if self.algebra is None:
            assert isinstance(self.default, bool)
            default = not self.default
        else:
            default = self.algebra.negate(self.default)
        return PredicateRange(
            default,
            ((candidate, not accepted) for candidate, accepted in self.overrides),
            algebra=self.algebra,
        )

    def __sub__(self, other: object) -> PredicateRange[CandidateType]:
        """Exclude the other symbolic restriction."""
        if not isinstance(other, PredicateRange):
            return NotImplemented
        return self & ~other

    def is_subset(self, other: PredicateRange[CandidateType]) -> bool:
        """Prove symbolic containment in the other restriction."""
        return (self - other).is_empty

    def is_superset(self, other: PredicateRange[CandidateType]) -> bool:
        """Prove symbolic containment of the other restriction."""
        return other.is_subset(self)

    def is_disjoint(self, other: PredicateRange[CandidateType]) -> bool:
        """Prove that two symbolic restrictions cannot overlap."""
        return (self & other).is_empty

    def relation(self, other: PredicateRange[CandidateType]) -> RangeRelation:
        """Return the resolver subset and disjoint conclusions."""
        subset, disjoint = self.is_subset(other), self.is_disjoint(other)
        if subset:
            return RangeRelation.EMPTY if disjoint else RangeRelation.SUBSET
        return RangeRelation.DISJOINT if disjoint else RangeRelation.OVERLAPPING

    @override
    def __eq__(self, other: object) -> bool:
        """Compare canonical formulas and exact exceptions."""
        if not isinstance(other, PredicateRange):
            return NotImplemented
        return self.default is other.default and self.overrides == other.overrides

    @override
    def __hash__(self) -> int:
        """Return the snapshot hash."""
        return self._hash

    @override
    def __str__(self) -> str:
        """Label the opaque restriction for generic diagnostics."""
        return "host candidate restriction"
