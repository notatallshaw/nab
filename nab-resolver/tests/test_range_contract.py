"""The range-algebra contract conflict resolution depends on.

Resolving a clause against its cause only shrinks the clause while
``x.is_subset((x - y) | y)`` holds for the range type.  ``full()`` is where a
range type breaks it: ``packaging.ranges.VersionRange.full()`` also admits
arbitrary ``===`` strings and loses them to any ``y`` that removes versions,
so the clause resolves into itself and the loop spins.

``nab_resolver.ranges.Range`` carries no such flag, so these tests use the
stub below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeRelation,
    Term,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_resolver.types import RangeProtocol


class FlaggedRange:
    """``Range[int]`` plus VersionRange's arbitrary-string admission flag.

    ``full()`` sets the flag, ``empty()`` clears it, and the four operations
    below carry it by VersionRange's rules.  As there, the flag admits strings
    only at full bounds, so a full-bounded flagged range is a strict superset
    of its unflagged twin and ``is_subset`` gates on that.  ``===`` literals
    and pre-release regions are left out; the identity above turns on neither.
    """

    __slots__ = ("arbitrary", "base")

    def __init__(self, base: Range[int], *, arbitrary: bool = False) -> None:
        self.base = base
        self.arbitrary = arbitrary

    @classmethod
    def empty(cls) -> FlaggedRange:
        return cls(Range.empty())

    @classmethod
    def full(cls) -> FlaggedRange:
        return cls(Range.full(), arbitrary=True)

    @classmethod
    def singleton(cls, version: int) -> FlaggedRange:
        return cls(Range.singleton(version))

    @property
    def is_empty(self) -> bool:
        return self.base.is_empty

    def arbitrary_active(self) -> bool:
        """Whether the flag actually admits strings, as VersionRange gates it."""
        return self.arbitrary and self.base == Range.full()

    def __contains__(self, version: int) -> bool:
        """Test membership; the flag admits no integer."""
        return version in self.base

    def __and__(self, other: Any) -> FlaggedRange:
        """Intersect; the flag needs both sides and a result with versions."""
        new_base = self.base & other.base
        return FlaggedRange(
            new_base,
            arbitrary=self.arbitrary and other.arbitrary and not new_base.is_empty,
        )

    def __or__(self, other: Any) -> FlaggedRange:
        """Union; only an operand that has versions passes the flag on.

        An empty operand carries the flag inertly, so ``~~full()`` is
        ``full()`` again while re-widening bounds does not revive the
        admission.
        """
        new_base = self.base | other.base
        if new_base.is_empty:
            arbitrary = self.arbitrary or other.arbitrary
        else:
            arbitrary = (self.arbitrary and not self.base.is_empty) or (
                other.arbitrary and not other.base.is_empty
            )
        return FlaggedRange(new_base, arbitrary=arbitrary)

    def __invert__(self) -> FlaggedRange:
        """Complement, carrying the flag through."""
        return FlaggedRange(~self.base, arbitrary=self.arbitrary)

    def __sub__(self, other: Any) -> FlaggedRange:
        """Difference, dropping the flag unless nothing was removed."""
        new_base = self.base - other.base
        return FlaggedRange(
            new_base, arbitrary=self.arbitrary and new_base == self.base
        )

    def is_subset(self, other: FlaggedRange) -> bool:
        """Subset on versions; a live admission needs another live one."""
        if self.arbitrary_active() and not other.arbitrary_active():
            return False
        return self.base.is_subset(other.base)

    def is_superset(self, other: FlaggedRange) -> bool:
        return other.is_subset(self)

    def is_disjoint(self, other: FlaggedRange) -> bool:
        """Disjoint when the intersection holds no version and no admission."""
        combined = self & other
        return combined.base.is_empty and not combined.arbitrary_active()

    def relation(self, other: FlaggedRange) -> RangeRelation:
        """Classify against ``other``, so the flag reaches term classification."""
        subset = self.is_subset(other)
        disjoint = self.is_disjoint(other)
        if subset:
            return RangeRelation.EMPTY if disjoint else RangeRelation.SUBSET
        return RangeRelation.DISJOINT if disjoint else RangeRelation.OVERLAPPING

    def __eq__(self, other: object) -> bool:
        """Equal when the interval sets and the flags both match."""
        return (
            isinstance(other, FlaggedRange)
            and self.base == other.base
            and self.arbitrary == other.arbitrary
        )

    def __hash__(self) -> int:
        """Hash the interval set together with the flag."""
        return hash((self.base, self.arbitrary))

    def __repr__(self) -> str:
        """Render the interval set, naming the flag when it is set."""
        suffix = ", arbitrary" if self.arbitrary else ""
        return f"FlaggedRange({self.base!r}{suffix})"


DEPENDENCY = FlaggedRange(Range.singleton(9))
GRAPH: dict[str, list[int]] = {"a": [3, 2, 1], "b": []}


class FlaggedProvider:
    """``a`` has three versions, each needing a ``b`` that does not exist."""

    graph: ClassVar[dict[str, list[int]]] = GRAPH

    def __init__(self, dependency: FlaggedRange = DEPENDENCY) -> None:
        self.dependency = dependency

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        for version in self.graph.get(package, []):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(v in version_range for v in self.graph.get(package, []))

    def get_dependencies(self, package: str, version: int) -> dict[str, FlaggedRange]:
        return {"b": self.dependency} if package == "a" else {}

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> object:
        return sum(1 for v in self.graph.get(package, []) if v in version_range)

    def is_ready(self, package: str) -> bool:
        """Everything is decidable immediately."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: this provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal."""
        return []

    def widen_decision(self, package: str, version: int) -> FlaggedRange | None:
        """No widening."""
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        return constraint


class WideningProvider(FlaggedProvider):
    """A provider that widens every decision to the admitting top."""

    def widen_decision(self, package: str, version: int) -> FlaggedRange:
        """Widen the decision to ``full()``, whatever was chosen."""
        return FlaggedRange.full()


class PendingClauseProvider(FlaggedProvider):
    """A provider that queues ``a==3``'s edge as a clause it built itself.

    Those terms reach the formula through ``consume_pending_clauses`` rather
    than as ranges the resolver turns into terms.  ``c`` rules ``a==3`` out,
    so the queued term has to shrink for ``a==2`` to be reached.
    """

    graph: ClassVar[dict[str, list[int]]] = {"a": [3, 2], "c": [1]}

    def __init__(self, dependency: FlaggedRange) -> None:
        """Queue ``a==3``'s edge on ``c`` as ``dependency``."""
        super().__init__(dependency)
        self.queued: list[Incompatibility[str, int]] = []

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick as the base provider does, queueing ``a==3``'s edge on the way."""
        chosen = super().choose_version(package, version_range)
        if package == "a" and chosen == 3:
            self.queued.append(
                Incompatibility(
                    [
                        Term("a", FlaggedRange.singleton(chosen), positive=True),
                        Term("c", self.dependency, positive=False),
                    ],
                    cause=IncompatibilityCause.DEPENDENCY,
                )
            )
        return chosen

    def get_dependencies(self, package: str, version: int) -> dict[str, FlaggedRange]:
        """``c`` needs an ``a`` that does not exist; ``a``'s own edge is queued."""
        return {"a": DEPENDENCY} if package == "c" else {}

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """Hand over whatever choosing a version queued."""
        queued, self.queued = self.queued, []
        return queued


STALL_TIMEOUT_SECONDS = 60


def build_resolver(provider: FlaggedProvider) -> Resolver[str, int]:
    """Build a resolver over the provider's graph."""
    return Resolver(
        provider,
        range_type=FlaggedRange,
        root_version=0,
        max_iterations=5000,
    )


def constraints_for(
    resolver: Resolver[str, int],
    package: str,
    cause: IncompatibilityCause,
    *,
    positive: bool,
) -> list[RangeProtocol[int]]:
    """Collect the constraints ``package`` carries in clauses of ``cause``.

    ``positive`` picks the side of the clause: a dependency edge holds the
    depending package positively and the depended-on package negatively.
    """
    return [
        term.constraint
        for incompatibility in resolver.incompatibilities
        if incompatibility.cause is cause
        for term in incompatibility.terms
        if term.package == package and term.is_positive() is positive
    ]


class TestFlaggedRangeFidelity:
    """The stub carries the flag the way the range type it stands in for does."""

    def test_flagged_top_breaks_the_subset_identity(self) -> None:
        """``full()`` fails the identity; the term top holds it."""
        flagged = FlaggedRange.full()
        plain = ~FlaggedRange.empty()
        assert not flagged.is_subset((flagged - DEPENDENCY) | DEPENDENCY)
        assert plain.is_subset((plain - DEPENDENCY) | DEPENDENCY)
        assert plain.is_subset(flagged)
        assert not flagged.is_subset(plain)

    def test_flag_survives_only_where_versionrange_keeps_it(self) -> None:
        """A union cannot revive the admission, and an empty result drops it."""
        flagged = FlaggedRange.full()
        plain = ~FlaggedRange.empty()
        assert not (~flagged | plain).arbitrary_active()
        assert flagged & ~flagged == FlaggedRange.empty()


class TestTermTopSubstitution:
    """A supplied range equal to ``full()`` becomes the term top instead."""

    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_root_range_equal_to_full_still_explains_the_conflict(self) -> None:
        """A root seeded with ``full()`` reports why, rather than stalling."""
        resolver = build_resolver(FlaggedProvider())
        with pytest.raises(ResolutionError) as excinfo:
            resolver.resolve({"a": FlaggedRange.full()})
        message = str(excinfo.value)
        assert "no versions of b" in message
        assert "your project depends on a" in message
        assert "resolver bug" not in message

    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_root_clause_records_the_term_top(self) -> None:
        """The recorded root clause carries the term top, not ``full()``."""
        resolver = build_resolver(FlaggedProvider())
        with pytest.raises(ResolutionError):
            resolver.resolve({"a": FlaggedRange.full()})
        assert constraints_for(
            resolver, "a", IncompatibilityCause.ROOT, positive=False
        ) == [resolver.term_top]

    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_dependency_clause_records_the_term_top(self) -> None:
        """A provider handing back ``full()`` gets the same substitution."""
        resolver = build_resolver(FlaggedProvider(FlaggedRange.full()))
        with pytest.raises(ResolutionError):
            resolver.resolve({"a": ~FlaggedRange.empty()})
        recorded = constraints_for(
            resolver, "b", IncompatibilityCause.DEPENDENCY, positive=False
        )
        assert recorded
        assert all(constraint == resolver.term_top for constraint in recorded)

    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_widened_decision_records_the_term_top(self) -> None:
        """A decision widened to ``full()`` is substituted in the parent term."""
        resolver = build_resolver(WideningProvider())
        with pytest.raises(ResolutionError):
            resolver.resolve({"a": ~FlaggedRange.empty()})
        recorded = constraints_for(
            resolver, "a", IncompatibilityCause.DEPENDENCY, positive=True
        )
        assert recorded
        assert all(constraint == resolver.term_top for constraint in recorded)

    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_pending_clause_records_the_term_top(self) -> None:
        """A term the provider built itself is substituted as it is absorbed."""
        resolver = build_resolver(PendingClauseProvider(FlaggedRange.full()))
        assert resolver.resolve({"a": ~FlaggedRange.empty()}) == {"a": 2}
        recorded = constraints_for(
            resolver, "c", IncompatibilityCause.DEPENDENCY, positive=False
        )
        assert recorded
        assert all(constraint == resolver.term_top for constraint in recorded)
