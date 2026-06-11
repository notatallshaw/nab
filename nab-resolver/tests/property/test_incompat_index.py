"""Brute-force equivalence for incompat_index clause merging and indexing.

Invariants:

1. Semantic equivalence: the merged clause store forbids exactly the
   same full assignments (each package absent or pinned to a version)
   as the raw un-merged clause list.  Merging unions the parent term of
   matching DEPENDENCY clauses, which must preserve the forbidden set.
2. Index exactness: ``package_to_incompatibilities[p]`` reaches exactly
   the clauses a full scan over ``incompatibilities`` would find for p.
3. ``dependency_index`` validity: every key maps to a live clause whose
   merge key still equals that key (in-place merges keep keys stable).

The clause generator includes shapes the resolver tests never produce:
self-dependency clauses (parent == dep package), negative parent terms
(no-merge path), positive dep terms, non-DEPENDENCY causes mixed in,
and cofinite ranges.
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.incompat_index import add_incompatibility, dependency_merge_key
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .providers import FuzzProvider
from .rep_model import POOL, rep_constraints, rep_to_range
from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

_PKGS = ("p", "q", "r")


def _fresh_resolver() -> Resolver[str, int]:
    """A resolver used only as the clause-store state carrier."""
    return Resolver(FuzzProvider({}))


@st.composite
def clause_specs(draw: st.DrawFn) -> tuple[object, ...]:
    """Generate a clause spec; building twice yields independent copies."""
    cause = draw(
        st.sampled_from(
            [IncompatibilityCause.DEPENDENCY] * 4
            + [IncompatibilityCause.NO_VERSIONS, IncompatibilityCause.DERIVED]
        )
    )
    parent = draw(st.sampled_from(_PKGS))
    if cause is IncompatibilityCause.NO_VERSIONS:
        return ("one", cause, parent, draw(rep_constraints()))
    dep = draw(st.sampled_from(_PKGS))  # parent == dep allowed (self-dep shape)
    parent_rep = draw(rep_constraints())
    parent_positive = draw(st.sampled_from([True, True, True, False]))
    dep_rep = draw(rep_constraints())
    dep_positive = draw(st.sampled_from([False, False, True]))
    return (
        "two",
        cause,
        parent,
        parent_rep,
        parent_positive,
        dep,
        dep_rep,
        dep_positive,
    )


def _build_clause(spec: tuple[object, ...]) -> Incompatibility[str, int]:
    """Materialize a clause spec into an Incompatibility."""
    if spec[0] == "one":
        _, cause, parent, rep = spec
        assert isinstance(cause, IncompatibilityCause)
        assert isinstance(parent, str)
        assert isinstance(rep, tuple)
        return Incompatibility(
            [Term(parent, rep_to_range(rep), positive=True)], cause=cause
        )
    _, cause, parent, parent_rep, parent_positive, dep, dep_rep, dep_positive = spec
    assert isinstance(cause, IncompatibilityCause)
    assert isinstance(parent, str)
    assert isinstance(parent_rep, tuple)
    assert isinstance(parent_positive, bool)
    assert isinstance(dep, str)
    assert isinstance(dep_rep, tuple)
    assert isinstance(dep_positive, bool)
    return Incompatibility(
        [
            Term(parent, rep_to_range(parent_rep), positive=parent_positive),
            Term(dep, rep_to_range(dep_rep), positive=dep_positive),
        ],
        cause=cause,
    )


def _term_true(term: Term[str, int], assignment: dict[str, int | None]) -> bool:
    """Whether a term holds under a full assignment (None means absent)."""
    version = assignment[term.package]
    if term.is_positive():
        return version is not None and version in term.constraint
    return version is None or version not in term.constraint


def _violated(
    clauses: list[Incompatibility[str, int]], assignment: dict[str, int | None]
) -> bool:
    """Whether any clause has all terms true under the assignment."""
    return any(
        all(_term_true(t, assignment) for t in clause.terms) for clause in clauses
    )


@given(specs=st.lists(clause_specs(), min_size=1, max_size=8))
@PROPERTY_SETTINGS
def test_merged_store_semantically_equivalent(
    specs: list[tuple[object, ...]],
) -> None:
    """The merged store forbids exactly the assignments the raw list does."""
    resolver = _fresh_resolver()
    raw: list[Incompatibility[str, int]] = []
    for spec in specs:
        raw.append(_build_clause(spec))  # independent copy for the raw store
        add_incompatibility(resolver, _build_clause(spec))

    versions: tuple[int | None, ...] = (None, *POOL)
    for combo in itertools.product(versions, repeat=len(_PKGS)):
        assignment: dict[str, int | None] = dict(zip(_PKGS, combo, strict=True))
        assert _violated(raw, assignment) == _violated(
            resolver.incompatibilities, assignment
        ), f"forbidden-set divergence at {assignment}"


@given(specs=st.lists(clause_specs(), min_size=1, max_size=8))
@PROPERTY_SETTINGS
def test_package_index_matches_full_scan(specs: list[tuple[object, ...]]) -> None:
    """The per-package index reaches exactly the clauses a full scan finds."""
    resolver = _fresh_resolver()
    for spec in specs:
        add_incompatibility(resolver, _build_clause(spec))

    full_scan: dict[str, set[int]] = {pkg: set() for pkg in _PKGS}
    for i, clause in enumerate(resolver.incompatibilities):
        for term in clause.terms:
            full_scan[term.package].add(i)

    for pkg in _PKGS:
        indexed = set(resolver.package_to_incompatibilities.get(pkg, []))
        assert indexed == full_scan[pkg], (
            f"index for {pkg!r}: {sorted(indexed)} != {sorted(full_scan[pkg])}"
        )
    for indices in resolver.package_to_incompatibilities.values():
        assert all(0 <= i < len(resolver.incompatibilities) for i in indices)


@given(specs=st.lists(clause_specs(), min_size=1, max_size=8))
@PROPERTY_SETTINGS
def test_dependency_index_keys_stay_canonical(
    specs: list[tuple[object, ...]],
) -> None:
    """Every dependency_index key still matches its clause's merge key."""
    resolver = _fresh_resolver()
    for spec in specs:
        add_incompatibility(resolver, _build_clause(spec))

    for key, index in resolver.dependency_index.items():
        assert 0 <= index < len(resolver.incompatibilities)
        clause = resolver.incompatibilities[index]
        assert dependency_merge_key(clause) == key
