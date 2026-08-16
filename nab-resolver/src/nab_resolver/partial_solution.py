"""Partial solution: ordered assignment list with decision levels.

The partial solution is a chronological trail of assignments.  Each
assignment constrains a package's allowed versions and is either a
decision (the resolver picks a specific version to try) or a
derivation (a constraint deduced by unit propagation).

Each decision opens a new "decision level".  Backtracking removes
all assignments above a target level, which is cheaper than copying
the entire state on every decision.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#partial-solution
"""

from __future__ import annotations

import operator
from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar, cast, overload
from weakref import ref

from ._compat import override
from .ranges import Range
from .types import PackageType, RangeProtocol, VersionType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from collections.abc import Set as AbstractSet
    from typing import TypeAlias

    from .types import Incompatibility, Term

    _RangeOp: TypeAlias = Callable[[RangeProtocol[Any], RangeProtocol[Any]], Any]


__all__ = [
    "Assignment",
    "PartialSolution",
]


# An entry in the range-algebra memo pins both of its operands, so this cap
# bounds what the memo retains, not just how large it gets.  It sits below
# what a large resolve fills, trading a little hit rate for a memo that clears
# instead of holding every operand to the end.
RANGE_OP_MEMO_MAX = 2_048

# Marks a missing map entry, where None can itself be the stored value.
_UNSET = object()

# Marks a package that the live map did not hold when a snapshot was taken.
_ABSENT = object()

_ValueType = TypeVar("_ValueType")
_DefaultType = TypeVar("_DefaultType")


class _Snapshot(Mapping[PackageType, _ValueType]):
    """A read-only view of one of the solution's maps, pinned to one moment.

    Reads fall through to the live map, so nothing is copied up front.
    Before the solution changes a package's entry it calls :meth:`freeze`,
    which records the value this view has to keep.
    """

    __slots__ = ("__weakref__", "_live", "_shadow")

    def __init__(self, live: dict[PackageType, _ValueType]) -> None:
        self._live = live
        # A value or _ABSENT. Any rather than that union, which no checker
        # narrows the sentinel back out of.
        self._shadow: dict[PackageType, Any] = {}

    def freeze(self, package: PackageType) -> None:
        """Keep the package's current value before the live map moves on."""
        if package not in self._shadow:
            self._shadow[package] = self._live.get(package, _ABSENT)

    def detach(self) -> None:
        """Take a copy of the live map, so later changes need no freezing.

        Backtracking rewrites most of the map at once, where one copy beats
        recording each package on its way out.
        """
        frozen = dict(self._live)
        for package, value in self._shadow.items():
            if value is _ABSENT:
                frozen.pop(package, None)
            else:
                frozen[package] = value
        self._live = frozen
        self._shadow = {}

    @override
    def __getitem__(self, package: PackageType) -> _ValueType:
        frozen = self._shadow.get(package, _UNSET)
        if frozen is _UNSET:
            return self._live[package]
        if frozen is _ABSENT:
            raise KeyError(package)
        return cast("_ValueType", frozen)

    # ``object`` rather than ``PackageType``: narrowing the key would not
    # substitute for ``Mapping.get``.
    @overload
    def get(self, package: object, /) -> _ValueType | None: ...
    @overload
    def get(
        self, package: object, /, default: _ValueType | _DefaultType
    ) -> _ValueType | _DefaultType: ...
    @override
    def get(
        self, package: Any, /, default: _ValueType | _DefaultType | None = None
    ) -> _ValueType | _DefaultType | None:
        """Return the package's value as of the snapshot, else ``default``.

        Defined rather than inherited: ``Mapping.get`` routes every read
        through ``__getitem__``, and every miss through a raised ``KeyError``.
        """
        frozen = self._shadow.get(package, _UNSET)
        if frozen is _UNSET:
            return self._live.get(package, default)
        if frozen is _ABSENT:
            return default
        return cast("_ValueType", frozen)

    @override
    def __contains__(self, package: object) -> bool:
        frozen = self._shadow.get(cast("PackageType", package), _UNSET)
        if frozen is _UNSET:
            return package in self._live
        return frozen is not _ABSENT

    @override
    def __iter__(self) -> Iterator[PackageType]:
        """Yield the snapshot's packages, in the order the live map holds them.

        Freezing leaves a package where it is, and the only path that removes
        one is ``backtrack``, which detaches first, so the live map still holds
        every key of the snapshot in the order it arrived.
        """
        shadow = self._shadow
        for package in self._live:
            if shadow.get(package, _UNSET) is not _ABSENT:
                yield package

    @override
    def __len__(self) -> int:
        return sum(1 for _ in self)


def _take_snapshot(
    live: dict[PackageType, _ValueType],
    holders: list[ref[_Snapshot[PackageType, _ValueType]]],
) -> _Snapshot[PackageType, _ValueType]:
    """Snapshot ``live`` and hold a weak reference to it in ``holders``.

    Entries whose snapshot the caller has since released are dropped on the way.
    """
    snapshot = _Snapshot(live)
    holders[:] = [holder for holder in holders if holder() is not None]
    holders.append(ref(snapshot))
    return snapshot


def _detach_snapshots(
    holders: list[ref[_Snapshot[PackageType, _ValueType]]],
) -> None:
    """Detach every outstanding snapshot from the map it reads through."""
    for holder in holders:
        snapshot = holder()
        if snapshot is not None:
            snapshot.detach()


# Every field in declaration order.  ``__slots__`` holds the same names sorted,
# so equality, the repr and ``__match_args__`` read this one instead.
_ASSIGNMENT_FIELDS: Final = (
    "package",
    "accumulated_range",
    "decision_level",
    "is_decision",
    "trail_index",
    "version",
    "cause",
    "positive",
    "cum_positive",
    "cum_negative",
)


class Assignment(Generic[PackageType, VersionType]):
    """A single entry in the partial solution trail."""

    __slots__ = (
        "accumulated_range",
        "cause",
        "cum_negative",
        "cum_positive",
        "decision_level",
        "is_decision",
        "package",
        "positive",
        "trail_index",
        "version",
    )

    __match_args__ = _ASSIGNMENT_FIELDS

    package: PackageType
    """Which package this assignment constrains."""

    accumulated_range: RangeProtocol[VersionType]
    """The cumulative range for this package at the time of assignment."""

    decision_level: int
    """The decision depth when this assignment was made."""

    is_decision: bool
    """True if this is a version choice; False if derived by propagation."""

    trail_index: int
    """Chronological position in the assignment trail."""

    version: VersionType | None
    """The chosen version (only set for decisions)."""

    cause: Incompatibility[PackageType, VersionType] | None
    """The incompatibility that forced this derivation (only for derivations)."""

    positive: bool
    """Whether this constrains the package positively or negatively."""

    cum_positive: RangeProtocol[VersionType] | None
    """Latest positive accumulated range for the package as of this entry."""

    cum_negative: RangeProtocol[VersionType] | None
    """Latest negative accumulated range for the package as of this entry."""

    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per field
        self,
        package: PackageType,
        accumulated_range: RangeProtocol[VersionType],
        decision_level: int,
        is_decision: bool,  # noqa: FBT001
        trail_index: int = 0,
        version: VersionType | None = None,
        cause: Incompatibility[PackageType, VersionType] | None = None,
        positive: bool = True,  # noqa: FBT001, FBT002
        cum_positive: RangeProtocol[VersionType] | None = None,
        cum_negative: RangeProtocol[VersionType] | None = None,
    ) -> None:
        """Record one trail entry."""
        self.package = package
        self.accumulated_range = accumulated_range
        self.decision_level = decision_level
        self.is_decision = is_decision

        self.trail_index = trail_index
        self.version = version
        self.cause = cause
        self.positive = positive
        self.cum_positive = cum_positive
        self.cum_negative = cum_negative

    @override
    def __eq__(self, other: object) -> bool:
        """Compare every field."""
        if not isinstance(other, Assignment):
            return NotImplemented
        return tuple(getattr(self, name) for name in _ASSIGNMENT_FIELDS) == tuple(
            getattr(other, name) for name in _ASSIGNMENT_FIELDS
        )

    __hash__ = None  # type: ignore[assignment]

    @override
    def __repr__(self) -> str:
        """Return a debug representation, fields in declaration order."""
        fields = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in _ASSIGNMENT_FIELDS
        )
        return f"{type(self).__qualname__}({fields})"


class PartialSolution(Generic[PackageType, VersionType]):
    """Tracks the resolver's current partial solution as a decision trail.

    This is the PubGrub equivalent of a SAT solver's assignment trail.
    See: https://en.wikipedia.org/wiki/Conflict-driven_clause_learning#Organization
    """

    def __init__(
        self,
        range_type: type[RangeProtocol[Any]] = Range,
        *,
        contradiction_epoch: int = 0,
    ) -> None:
        """Initialize an empty partial solution.

        ``contradiction_epoch`` carries on from a solution this one replaces,
        so a stamp taken against the old trail cannot look current against the
        new one.
        """
        self._contradiction_epoch = contradiction_epoch
        self._range_type = range_type
        self._assignments: list[Assignment[PackageType, VersionType]] = []
        self._decision_level = 0
        self._positive_ranges: dict[PackageType, RangeProtocol[VersionType]] = {}
        self._negative_ranges: dict[PackageType, RangeProtocol[VersionType]] = {}
        self._decided_versions: dict[PackageType, VersionType] = {}

        # Incrementally maintained as set(positive) - set(decided).
        self._undecided: set[PackageType] = set()

        # Packages whose effective range or decided state moved since the last
        # drain.
        self._changed: set[PackageType] = set()

        # Memoises positive - negative per package.
        self._effective_range_cache: dict[
            PackageType, RangeProtocol[VersionType] | None
        ] = {}

        # Per-package index of trail entries; lets satisfier() avoid the full scan.
        self._assignments_by_package: defaultdict[
            PackageType, list[Assignment[PackageType, VersionType]]
        ] = defaultdict(list)

        # Weak references to the snapshots still outstanding.
        self._range_snapshots: list[
            ref[_Snapshot[PackageType, RangeProtocol[VersionType]]]
        ] = []
        self._decision_snapshots: list[ref[_Snapshot[PackageType, VersionType]]] = []

        # Results of the range algebra the trail replays, keyed by operand
        # identity.  A key is only sound while both its operands are alive, so
        # the list beside the memo holds them and the two clear together.
        self._range_ops: dict[
            tuple[int, int, _RangeOp], RangeProtocol[VersionType]
        ] = {}
        self._range_op_operands: list[RangeProtocol[VersionType]] = []

    @property
    def decision_level(self) -> int:
        """Return the current decision depth."""
        return self._decision_level

    @property
    def contradiction_epoch(self) -> int:
        """Return the epoch a contradicted term stays contradicted for.

        It advances on a rollback, and on any assignment that empties a
        package's range: an empty range is both a subset of and disjoint from
        every constraint, so terms on that package stop reading as
        contradicted.
        """
        return self._contradiction_epoch

    @property
    def trail_length(self) -> int:
        """Return the number of assignments currently on the trail."""
        return len(self._assignments)

    def assignments_for(
        self, package: PackageType
    ) -> Sequence[Assignment[PackageType, VersionType]]:
        """Return the chronological assignment trail for ``package``.

        Read-only view; callers must not mutate the result.
        """
        entries = self._assignments_by_package.get(package)
        if entries is None:
            return ()
        return entries

    def _combine(
        self,
        op: _RangeOp,
        left: RangeProtocol[VersionType],
        right: RangeProtocol[VersionType],
    ) -> RangeProtocol[VersionType]:
        """Apply ``op`` to two ranges, reusing the result for a repeated pair.

        Backtracking replays the trail from the same stored range objects, so
        the same operands recur and the memo returns the object the first pass
        built.  Reuse preserves the value because :class:`RangeProtocol`
        requires immutable ranges.
        """
        key = (id(left), id(right), op)
        memo = self._range_ops
        hit = memo.get(key)
        if hit is not None:
            return hit

        result: RangeProtocol[VersionType] = op(left, right)
        if len(memo) >= RANGE_OP_MEMO_MAX:
            memo.clear()
            self._range_op_operands.clear()
        memo[key] = result
        self._range_op_operands.append(left)
        self._range_op_operands.append(right)
        return result

    def get(self, package: PackageType) -> RangeProtocol[VersionType] | None:
        """Get the combined allowed range for a package, or None if unassigned.

        Computes ``positive - negative``, cached per package.
        """
        # ``None`` is a cached answer, so membership decides the hit.
        cache = self._effective_range_cache
        if package in cache:
            return cache[package]

        positive = self._positive_ranges.get(package)
        negative = self._negative_ranges.get(package)

        if positive is None and negative is None:
            result: RangeProtocol[VersionType] | None = None
        elif positive is None:
            # No requirement to subtract from. An exclusion-only package is
            # never offered for selection, so the plain complement is enough.
            assert negative is not None
            result = ~negative
        elif negative is None:
            result = positive
        else:
            result = self._combine(operator.sub, positive, negative)

        cache[package] = result
        return result

    def _refresh_effective_range(self, package: PackageType) -> None:
        """Recompute the package's range, advancing the epoch if it emptied."""
        self._effective_range_cache.pop(package, None)
        self._changed.add(package)
        effective = self.get(package)
        assert effective is not None
        if effective.is_empty:
            self._contradiction_epoch += 1

    def decide(self, package: PackageType, version: VersionType) -> None:
        """Record a decision: pick a specific version for a package."""
        self._decision_level += 1
        exact_range = self._range_type.singleton(version)

        self._freeze_ranges(package)
        self._freeze_decisions(package)
        self._positive_ranges[package] = exact_range
        self._decided_versions[package] = version
        self._refresh_effective_range(package)
        self._undecided.discard(package)

        assignment = Assignment(
            package=package,
            accumulated_range=exact_range,
            decision_level=self._decision_level,
            is_decision=True,
            trail_index=len(self._assignments),
            version=version,
            positive=True,
            cum_positive=exact_range,
            cum_negative=self._negative_ranges.get(package),
        )
        self._assignments.append(assignment)
        self._assignments_by_package[package].append(assignment)

    def derive(
        self,
        package: PackageType,
        constraint: RangeProtocol[VersionType],
        *,
        positive: bool,
        cause: Incompatibility[PackageType, VersionType],
    ) -> None:
        """Record a derivation from unit propagation.

        A package's first derivation of a sign has nothing to fold into, so it
        records ``constraint`` itself.

        See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
        """
        if positive:
            # Positive derivation narrows the package's allowed range.
            current = self._positive_ranges.get(package)
            new_range = (
                constraint
                if current is None
                else self._combine(operator.and_, current, constraint)
            )
            self._freeze_ranges(package)
            self._positive_ranges[package] = new_range
            if package not in self._decided_versions:
                self._undecided.add(package)
        else:
            # Negative derivation accumulates excluded versions.
            excluded = self._negative_ranges.get(package)
            new_range = (
                constraint
                if excluded is None
                else self._combine(operator.or_, excluded, constraint)
            )
            self._negative_ranges[package] = new_range

        self._refresh_effective_range(package)

        assignment = Assignment(
            package=package,
            accumulated_range=new_range,
            decision_level=self._decision_level,
            is_decision=False,
            trail_index=len(self._assignments),
            cause=cause,
            positive=positive,
            cum_positive=self._positive_ranges.get(package),
            cum_negative=self._negative_ranges.get(package),
        )
        self._assignments.append(assignment)
        self._assignments_by_package[package].append(assignment)

    def backtrack(self, target_level: int) -> None:
        """Remove all assignments above target_level.

        Non-chronological backjumping: skips past irrelevant decision levels
        directly to the cause of the conflict.  A package that loses entries is
        rebuilt from its last survivor, which already carries that package's
        ``cum_positive`` and ``cum_negative``.
        See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
        """
        self._contradiction_epoch += 1
        _detach_snapshots(self._range_snapshots)
        _detach_snapshots(self._decision_snapshots)

        # Trail levels never decrease, so this pops exactly the assignments above
        # target_level; every other package keeps the positive and negative ranges
        # its cached effective range was derived from.
        while self._assignments and self._assignments[-1].decision_level > target_level:
            package = self._assignments.pop().package
            self._effective_range_cache.pop(package, None)
            self._changed.add(package)

        self._decision_level = target_level

        empty_packages: list[PackageType] = []
        for package, entries in self._assignments_by_package.items():
            popped = decision_popped = False
            while entries and entries[-1].decision_level > target_level:
                popped = True
                if entries.pop().is_decision:
                    decision_popped = True

            if not entries:
                empty_packages.append(package)
                self._positive_ranges.pop(package, None)
                self._negative_ranges.pop(package, None)
                self._decided_versions.pop(package, None)
                self._undecided.discard(package)
            # A package that kept every entry already holds what the rebuild
            # would restore.
            elif popped:
                self._update_package_state_after_backtrack(
                    package, entries, decision_popped=decision_popped
                )

        for package in empty_packages:
            del self._assignments_by_package[package]

    def _update_package_state_after_backtrack(
        self,
        package: PackageType,
        entries: list[Assignment[PackageType, VersionType]],
        *,
        decision_popped: bool,
    ) -> None:
        """Restore a package's state from its last surviving entry.

        ``decide`` and ``derive`` stamp each entry with the package's positive
        and negative ranges as of that entry, and a backtrack keeps a prefix of
        the entries, so the last survivor already carries both.  A package holds
        at most one decision at a time, so its decided version stands unless the
        pop reached the decision itself.
        """
        tail = entries[-1]
        last_pos = tail.cum_positive
        last_neg = tail.cum_negative

        if last_pos is None:
            self._positive_ranges.pop(package, None)
        else:
            self._positive_ranges[package] = last_pos

        if last_neg is None:
            self._negative_ranges.pop(package, None)
        else:
            self._negative_ranges[package] = last_neg

        if decision_popped:
            self._decided_versions.pop(package, None)

        if last_pos is not None and package not in self._decided_versions:
            self._undecided.add(package)
        else:
            self._undecided.discard(package)

    def decisions(self) -> Mapping[PackageType, VersionType]:
        """Return the decision map ``{package: version}``.

        Read-only, and pinned: later decisions and backtracking do not reach it.
        """
        return _take_snapshot(self._decided_versions, self._decision_snapshots)

    def _freeze_decisions(self, package: PackageType) -> None:
        """Preserve the package's decided version in outstanding snapshots."""
        for holder in self._decision_snapshots:
            snapshot = holder()
            if snapshot is not None:
                snapshot.freeze(package)

    def _freeze_ranges(self, package: PackageType) -> None:
        """Preserve the package's positive range in outstanding snapshots."""
        for holder in self._range_snapshots:
            snapshot = holder()
            if snapshot is not None:
                snapshot.freeze(package)

    def take_changed_packages(self) -> set[PackageType]:
        """Return the packages whose state moved since the last call, and reset.

        Every path that moves a package's effective range or its decided state
        records it here, backtracking included.
        """
        changed = self._changed
        self._changed = set()
        return changed

    def undecided_packages(self) -> AbstractSet[PackageType]:
        """Return packages with positive constraints but no decision yet.

        Packages with only negative derivations (learned exclusions) are not
        yet known to be required.  This is the live set, not a copy: callers
        must not mutate it, and the solution must not change while one iterates
        it.
        """
        return self._undecided

    def has_positive_constraint(self, package: PackageType) -> bool:
        """Return True if the package has a positive constraint or decision."""
        return package in self._positive_ranges or package in self._decided_versions

    def positive_ranges(self) -> Mapping[PackageType, RangeProtocol[VersionType]]:
        """Return each package's positive range.

        Read-only, and pinned: later derivations and backtracking do not reach it.
        """
        return _take_snapshot(self._positive_ranges, self._range_snapshots)

    def positive_range(self, package: PackageType) -> RangeProtocol[VersionType] | None:
        """Return the package's accumulated positive range, or None if unset."""
        return self._positive_ranges.get(package)

    def _satisfied_at(
        self,
        assignment: Assignment[PackageType, VersionType],
        term: Term[PackageType, VersionType],
        *,
        is_positive: bool,
    ) -> bool:
        """Whether the trail up to and including ``assignment`` satisfies term.

        Positive terms need a positive assignment first; negatives alone
        only exclude versions.
        """
        cum_positive = assignment.cum_positive
        if is_positive and cum_positive is None:
            return False

        if cum_positive is None:
            assert assignment.cum_negative is not None
            effective = ~assignment.cum_negative
        elif assignment.cum_negative is None:
            effective = cum_positive
        else:
            effective = self._combine(
                operator.sub, cum_positive, assignment.cum_negative
            )

        return term.satisfies(effective)

    def satisfier(
        self, term: Term[PackageType, VersionType]
    ) -> Assignment[PackageType, VersionType] | None:
        """Find the earliest assignment that causes the term to be satisfied.

        The effective range only narrows along the trail, so ``term.satisfies``
        is monotonic: once an entry satisfies the term, every later one does
        too.  That lets a binary search replace the linear scan.
        See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
        """
        entries = self._assignments_by_package.get(term.package, ())
        count = len(entries)
        if count == 0:
            return None

        is_positive = term.is_positive()
        if not self._satisfied_at(entries[count - 1], term, is_positive=is_positive):
            return None

        low, high = 0, count - 1
        while low < high:
            mid = (low + high) // 2
            if self._satisfied_at(entries[mid], term, is_positive=is_positive):
                high = mid
            else:
                low = mid + 1
        return entries[low]
