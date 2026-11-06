"""Range algebra over an unbounded key domain with finite exceptions."""

from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._compat import override
from .types import RangeProtocol, RangeRelation
from .values import FrozenValue

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from typing_extensions import Self

__all__ = ["KeyedRange"]

KeyType = TypeVar("KeyType", bound=Hashable)
ValueType = TypeVar("ValueType", bound=Hashable)


class KeyedRange(FrozenValue, Generic[KeyType, ValueType]):
    """A default range and finite replacements for particular keys.

    Keys need equality and hashing, but need not be ordered or enumerable.
    Coordinate ranges must obey :class:`RangeProtocol` with one shared policy.
    A domain-specific subclass supplies its scalar factories and membership.
    Binary operations retain that subclass and call its constructor.
    """

    __slots__ = __match_args__ = ("default", "overrides", "_hash")

    default: RangeProtocol[ValueType]
    overrides: tuple[tuple[KeyType, RangeProtocol[ValueType]], ...]
    _hash: int

    def __init__(
        self,
        default: RangeProtocol[ValueType],
        overrides: Mapping[KeyType, RangeProtocol[ValueType]]
        | Iterable[tuple[KeyType, RangeProtocol[ValueType]]] = (),
    ) -> None:
        """Snapshot replacements, keeping the last range for repeated keys."""
        normalized = tuple(
            (key, value) for key, value in dict(overrides).items() if value != default
        )
        object.__setattr__(self, "default", default)
        object.__setattr__(self, "overrides", normalized)
        object.__setattr__(self, "_hash", hash((default, frozenset(normalized))))

    def for_key(self, key: KeyType) -> RangeProtocol[ValueType]:
        """Return the coordinate applying to a key, including an unseen key."""
        for stored, value in self.overrides:
            if stored == key:
                return value
        return self.default

    def contains(self, key: KeyType, value: ValueType) -> bool:
        """Test a value in its key's coordinate."""
        return value in self.for_key(key)

    @property
    def is_empty(self) -> bool:
        """Whether every coordinate is empty."""
        return self.default.is_empty and all(
            value.is_empty for _, value in self.overrides
        )

    def _combine(
        self,
        other: Self,
        operation: Callable[
            [RangeProtocol[ValueType], RangeProtocol[ValueType]],
            RangeProtocol[ValueType],
        ],
    ) -> Self:
        """Apply an operation to the default and each exceptional coordinate."""
        default = operation(self.default, other.default)

        # The default represents every key outside these finite exceptions.
        return type(self)(
            default,
            (
                (key, operation(left, right))
                for key, left, right in self._coordinate_pairs(other)
            ),
        )

    def _coordinate_pairs(
        self, other: Self
    ) -> Iterator[tuple[KeyType, RangeProtocol[ValueType], RangeProtocol[ValueType]]]:
        """Pair exceptional coordinates without repeated scans of either tuple."""
        left = dict(self.overrides)
        right = dict(other.overrides)
        for key in left.keys() | right.keys():
            yield key, left.get(key, self.default), right.get(key, other.default)

    def __and__(self, other: object) -> Self:
        """Intersect each key's range."""
        if not isinstance(other, type(self)):
            # Zuban does not allow the standard operator fallback for Self.
            return NotImplemented  # zuban: ignore[return-value]
        return self._combine(other, lambda left, right: left & right)

    def __or__(self, other: object) -> Self:
        """Union each key's range."""
        if not isinstance(other, type(self)):
            # Zuban does not allow the standard operator fallback for Self.
            return NotImplemented  # zuban: ignore[return-value]
        return self._combine(other, lambda left, right: left | right)

    def __sub__(self, other: object) -> Self:
        """Remove the other range at each key."""
        if not isinstance(other, type(self)):
            # Zuban does not allow the standard operator fallback for Self.
            return NotImplemented  # zuban: ignore[return-value]
        return self._combine(other, lambda left, right: left - right)

    def __invert__(self) -> Self:
        """Complement every coordinate, including keys not yet encountered."""
        return type(self)(
            ~self.default, ((key, ~value) for key, value in self.overrides)
        )

    def is_subset(self, other: Self) -> bool:
        """Whether every admitted keyed value is also admitted by the other."""
        return self.relation(other).is_subset

    def is_superset(self, other: Self) -> bool:
        """Whether every keyed value in the other range is admitted here."""
        return other.is_subset(self)

    def is_disjoint(self, other: Self) -> bool:
        """Whether the ranges share no keyed value."""
        return self.relation(other).is_disjoint

    def relation(self, other: Self) -> RangeRelation:
        """Combine the scalar relation over the default and finite exceptions."""
        relation = self.default.relation(other.default)
        subset = relation.is_subset
        disjoint = relation.is_disjoint
        for _, left, right in self._coordinate_pairs(other):
            relation = left.relation(right)
            subset &= relation.is_subset
            disjoint &= relation.is_disjoint

        if subset:
            return RangeRelation.EMPTY if disjoint else RangeRelation.SUBSET
        return RangeRelation.DISJOINT if disjoint else RangeRelation.OVERLAPPING

    @override
    def identity(self) -> tuple[object, ...]:
        """Compare coordinates independently of the cached hash."""
        return self.default, frozenset(self.overrides)

    @override
    def __eq__(self, other: object) -> bool:
        """Compare keyed coordinates without retaining a second lookup table."""
        if not isinstance(other, type(self)):
            return NotImplemented
        if self.default != other.default:
            return False
        if self.overrides == other.overrides:
            return True
        if len(self.overrides) != len(other.overrides):
            return False

        # Normalization excludes default-valued entries, so a missing key cannot match.
        right = dict(other.overrides)
        return all(
            value == right.get(key, self.default) for key, value in self.overrides
        )

    @override
    def __hash__(self) -> int:
        """Return the hash of the snapshotted coordinates."""
        return self._hash

    @override
    def __getstate__(
        self,
    ) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
        """Omit the process-specific hash when copying or serializing."""
        state = super().__getstate__()
        dictionary = state[0] if isinstance(state, tuple) else state
        del dictionary["_hash"]
        return state

    @override
    def __setstate__(
        self, state: dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Restore fields, then compute the hash in the receiving process."""
        super().__setstate__(state)
        object.__setattr__(self, "_hash", hash(self.identity()))

    @override
    def __str__(self) -> str:
        """Render the default and exceptional coordinates."""
        return f"{self.default!r}; keys={self.overrides!r}"
