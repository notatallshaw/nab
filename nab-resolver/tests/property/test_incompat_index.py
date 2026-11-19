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
from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.incompat_index import add_incompatibility
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
def clauses(draw: st.DrawFn) -> Incompatibility[str, int]:
    """Generate both mergeable dependencies and clauses that must stay separate."""
    cause = draw(
        st.sampled_from(
            [IncompatibilityCause.DEPENDENCY] * 4
            + [IncompatibilityCause.NO_VERSIONS, IncompatibilityCause.DERIVED]
        )
    )
    parent_package = draw(st.sampled_from(_PKGS))
    parent_range = rep_to_range(draw(rep_constraints()))
    if cause is IncompatibilityCause.NO_VERSIONS:
        return Incompatibility(
            [Term(parent_package, parent_range, positive=True)], cause
        )

    parent = Term(
        parent_package,
        parent_range,
        positive=draw(st.sampled_from([True, True, True, False])),
    )
    dependency = Term(
        draw(st.sampled_from(_PKGS)),
        rep_to_range(draw(rep_constraints())),
        positive=draw(st.sampled_from([False, False, True])),
    )
    return Incompatibility([parent, dependency], cause)


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


@given(raw=st.lists(clauses(), min_size=1, max_size=8))
@PROPERTY_SETTINGS
def test_merged_store_semantically_equivalent(
    raw: list[Incompatibility[str, int]],
) -> None:
    """The merged store forbids exactly the assignments the raw list does."""
    resolver = _fresh_resolver()
    # Keep the reference clauses independent if merging changes an input in place.
    for clause in deepcopy(raw):
        add_incompatibility(resolver, clause)

    versions: tuple[int | None, ...] = (None, *POOL)
    for combo in itertools.product(versions, repeat=len(_PKGS)):
        assignment: dict[str, int | None] = dict(zip(_PKGS, combo, strict=True))
        assert _violated(raw, assignment) == _violated(
            resolver.incompatibilities, assignment
        ), f"forbidden-set divergence at {assignment}"


@given(raw=st.lists(clauses(), min_size=1, max_size=8))
@PROPERTY_SETTINGS
def test_package_index_matches_full_scan(raw: list[Incompatibility[str, int]]) -> None:
    """The per-package index reaches exactly the clauses a full scan finds.

    The skip stamps stay at one entry per clause too.
    """
    resolver = _fresh_resolver()
    for clause in raw:
        add_incompatibility(resolver, clause)

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

    assert len(resolver.clause_contradicted_at) == len(resolver.incompatibilities)


@given(raw=st.lists(clauses(), min_size=1, max_size=8))
@PROPERTY_SETTINGS
def test_dependency_index_matches_stored_clauses(
    raw: list[Incompatibility[str, int]],
) -> None:
    """Each mergeable clause has exactly one entry in the dependency index."""
    resolver = _fresh_resolver()
    for clause in raw:
        add_incompatibility(resolver, clause)

    expected = {}
    for index, clause in enumerate(resolver.incompatibilities):
        if (
            clause.cause is not IncompatibilityCause.DEPENDENCY
            or len(clause.terms) != 2
        ):
            continue
        parent, dependency = clause.terms
        if parent.is_positive():
            key = (
                parent.package,
                dependency.package,
                dependency.constraint,
                dependency.is_positive(),
            )
            assert key not in expected
            expected[key] = index

    assert resolver.dependency_index == expected
