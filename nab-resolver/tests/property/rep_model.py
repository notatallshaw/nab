"""Shared model machinery for partial-solution differential tests.

Constraints are modeled as exact finite-or-cofinite sets of integers
("reps"): a pair ``(frozenset, cofinite_flag)``.  The algebra of finite
unions of singletons and their complements is closed under union,
intersection, and complement, so a rep models the corresponding
``nab_resolver.ranges.Range`` exactly (no finite-pool blind spots) and
covers cofinite shapes the interval-based strategies never produce.

``ModelPS`` is an independent reimplementation of the documented
:class:`PartialSolution` semantics: a chronological trail replayed from
scratch on every query, with no incremental caching.  Differential
testing against the real class catches stored-field, cache,
binary-search, and backtrack-rebuild bugs.

Operations are restricted to resolver-reachable states:

- decide only an undecided package with a positive constraint, choosing
  a version inside the current effective range;
- derive never targets an already-decided package (a decided package's
  terms are never UNDETERMINED, so unit propagation cannot derive on it);
- backtrack targets are at most the current decision level.
"""

from __future__ import annotations

from hypothesis import strategies as st

from nab_resolver.partial_solution import PartialSolution
from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, IncompatibilityCause

POOL = tuple(range(1, 7))
OUTSIDE_PROBES = (0, 100)
ALL_PROBES = POOL + OUTSIDE_PROBES
PACKAGES = ("a", "b", "c")
PROBE_PACKAGES = (*PACKAGES, "z")
CANDIDATE_VERSIONS = (*POOL, 100)

Rep = tuple[frozenset[int], bool]

EMPTY_REP: Rep = (frozenset(), False)


def rep_not(a: Rep) -> Rep:
    """Complement of a rep."""
    return (a[0], not a[1])


def rep_and(a: Rep, b: Rep) -> Rep:
    """Intersection of two reps."""
    sa, ca = a
    sb, cb = b
    if not ca and not cb:
        return (sa & sb, False)
    if not ca and cb:
        return (sa - sb, False)
    if ca and not cb:
        return (sb - sa, False)
    return (sa | sb, True)


def rep_or(a: Rep, b: Rep) -> Rep:
    """Union of two reps."""
    return rep_not(rep_and(rep_not(a), rep_not(b)))


def rep_member(v: int, a: Rep) -> bool:
    """Membership test for a rep."""
    return (v in a[0]) != a[1]


def rep_is_empty(a: Rep) -> bool:
    """Whether a rep denotes the empty set."""
    return not a[1] and not a[0]


def rep_subset(a: Rep, b: Rep) -> bool:
    """Whether ``a`` is a subset of ``b``."""
    return rep_is_empty(rep_and(a, rep_not(b)))


def rep_disjoint(a: Rep, b: Rep) -> bool:
    """Whether ``a`` and ``b`` are disjoint."""
    return rep_is_empty(rep_and(a, b))


def rep_to_range(rep: Rep) -> Range[int]:
    """Convert a rep to the equivalent ``Range[int]``."""
    s, cofinite = rep
    r: Range[int] = Range.empty()
    for v in sorted(s):
        r = r | Range.singleton(v)
    return ~r if cofinite else r


@st.composite
def rep_constraints(draw: st.DrawFn) -> Rep:
    """Generate a random finite-or-cofinite rep over the version pool."""
    base = frozenset(draw(st.sets(st.sampled_from(POOL), max_size=4)))
    cofinite = draw(st.booleans())
    return (base, cofinite)


def root_cause() -> Incompatibility[str, int]:
    """A throwaway ROOT-cause incompatibility for derive calls."""
    return Incompatibility([], cause=IncompatibilityCause.ROOT)


class ModelPS:
    """Replay-from-scratch model of the documented PartialSolution."""

    def __init__(self) -> None:
        self.level = 0
        # Entries are (package, kind, payload, level) with kind one of
        # decide, pos, neg.
        self.trail: list[tuple[str, str, object, int]] = []

    def decide(self, pkg: str, version: int) -> None:
        """Record a decision, raising the level by one."""
        self.level += 1
        self.trail.append((pkg, "decide", version, self.level))

    def derive(self, pkg: str, rep: Rep, *, positive: bool) -> None:
        """Record a derivation at the current level."""
        self.trail.append((pkg, "pos" if positive else "neg", rep, self.level))

    def backtrack(self, target: int) -> None:
        """Drop every trail entry above the target level."""
        self.trail = [e for e in self.trail if e[3] <= target]
        self.level = target

    def state(self, pkg: str) -> tuple[Rep | None, Rep, int | None, bool]:
        """Return (pos, neg, decided_version, neg_recorded) by replay."""
        pos: Rep | None = None
        neg: Rep = EMPTY_REP
        decided: int | None = None
        neg_recorded = False
        for p, kind, payload, _ in self.trail:
            if p != pkg:
                continue
            if kind == "decide":
                assert isinstance(payload, int)
                pos = (frozenset({payload}), False)
                decided = payload
            elif kind == "pos":
                assert isinstance(payload, tuple)
                pos = payload if pos is None else rep_and(pos, payload)
            else:
                assert isinstance(payload, tuple)
                neg = rep_or(neg, payload)
                neg_recorded = True
        return pos, neg, decided, neg_recorded

    def decided_map(self) -> dict[str, int]:
        """Return {package: decided version} by replay."""
        out: dict[str, int] = {}
        for pkg in PACKAGES:
            decided = self.state(pkg)[2]
            if decided is not None:
                out[pkg] = decided
        return out

    def undecided(self) -> set[str]:
        """Packages with a positive constraint but no decision."""
        out: set[str] = set()
        for pkg in PACKAGES:
            pos, _, decided, _ = self.state(pkg)
            if pos is not None and decided is None:
                out.add(pkg)
        return out

    def prefix_states(self, pkg: str) -> list[tuple[Rep | None, Rep, bool]]:
        """Cumulative (pos, neg, is_decision_entry) after each pkg entry."""
        out: list[tuple[Rep | None, Rep, bool]] = []
        pos: Rep | None = None
        neg: Rep = EMPTY_REP
        for p, kind, payload, _ in self.trail:
            if p != pkg:
                continue
            if kind == "decide":
                assert isinstance(payload, int)
                pos = (frozenset({payload}), False)
            elif kind == "pos":
                assert isinstance(payload, tuple)
                pos = payload if pos is None else rep_and(pos, payload)
            else:
                assert isinstance(payload, tuple)
                neg = rep_or(neg, payload)
            out.append((pos, neg, kind == "decide"))
        return out

    @staticmethod
    def _satisfied_at(pos: Rep | None, neg: Rep, c: Rep, *, positive: bool) -> bool:
        if positive and pos is None:
            return False
        eff = rep_and(pos, rep_not(neg)) if pos is not None else rep_not(neg)
        return rep_subset(eff, c) if positive else rep_disjoint(eff, c)

    def satisfier_index(self, pkg: str, c: Rep, *, positive: bool) -> int | None:
        """Index of the earliest pkg entry whose prefix satisfies the term."""
        for i, (pos, neg, _) in enumerate(self.prefix_states(pkg)):
            if self._satisfied_at(pos, neg, c, positive=positive):
                return i
        return None


_operations = st.one_of(
    st.tuples(
        st.just("derive"),
        st.sampled_from(PACKAGES),
        rep_constraints(),
        st.booleans(),
    ),
    st.tuples(st.just("decide"), st.sampled_from(PACKAGES)),
    st.tuples(st.just("backtrack"), st.integers(min_value=0, max_value=10)),
)


def operations() -> st.SearchStrategy[tuple[object, ...]]:
    """Strategy for derive/decide/backtrack operations."""
    return _operations


def probe_terms() -> st.SearchStrategy[tuple[str, Rep, bool]]:
    """Strategy for (package, constraint rep, positive) probe terms."""
    return st.tuples(st.sampled_from(PROBE_PACKAGES), rep_constraints(), st.booleans())


def apply_op(
    ps: PartialSolution[str, int], model: ModelPS, op: tuple[object, ...]
) -> None:
    """Apply one generated op to the real PartialSolution and model in lockstep."""
    kind = op[0]
    if kind == "derive":
        _, pkg, rep, positive = op
        assert isinstance(pkg, str)
        assert isinstance(rep, tuple)
        assert isinstance(positive, bool)
        if model.state(pkg)[2] is not None:
            return
        ps.derive(pkg, rep_to_range(rep), positive=positive, cause=root_cause())
        model.derive(pkg, rep, positive=positive)
    elif kind == "decide":
        _, pkg = op
        assert isinstance(pkg, str)
        pos, neg, decided, _ = model.state(pkg)
        if decided is not None or pos is None:
            return
        eff = rep_and(pos, rep_not(neg))
        version = next((v for v in CANDIDATE_VERSIONS if rep_member(v, eff)), None)
        if version is None:
            return
        ps.decide(pkg, version)
        model.decide(pkg, version)
    else:
        _, raw = op
        assert isinstance(raw, int)
        target = raw % (model.level + 1)
        ps.backtrack(target)
        model.backtrack(target)
