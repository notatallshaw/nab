"""Property tests for unsat-proof soundness.

When resolution fails, ``error.incompatibility`` is the root of a
derivation DAG whose external (non-DERIVED) clauses must each be a true
fact of the generating universe.  The resolver tests elsewhere only
check that the proof exists; here every leaf is validated against the
graph: ROOT clauses match stated requirements, DEPENDENCY clauses cover
real versions whose actual dep ranges are subsets of the claimed range,
and NO_VERSIONS / CONSTRAINT clauses name ranges containing no real
version.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.ranges import Range

from .graph_oracles import (
    Graph,
    check_leaf_against_universe,
    proof_leaves,
    selfdep_graphs,
    solve,
    sparse_version_graphs,
)
from .strategies import (
    PROPERTY_SETTINGS,
    empty_dep_graphs,
    graph_and_constraints,
    small_exhaustive_graphs,
)

pytestmark = pytest.mark.property

PROOF_TIMEOUT_SECONDS = 120


def _check_proof(
    graph: Graph,
    requirements: dict[str, Range[int]],
    constraints: dict[str, Range[int]] | None = None,
) -> None:
    """Resolve and, on failure, validate every proof leaf."""
    _, error = solve(graph, requirements=requirements, constraints=constraints)
    if error is None:
        return
    assert error.incompatibility is not None
    leaves = proof_leaves(error.incompatibility)
    assert leaves, "unsat proof has no external clauses"
    for leaf in leaves:
        check_leaf_against_universe(leaf, graph, requirements, constraints)


class TestProofLeavesAreTrueFacts:
    """Every external clause in an unsat proof is true in the universe."""

    @pytest.mark.timeout(PROOF_TIMEOUT_SECONDS)
    @given(graph=small_exhaustive_graphs())
    @PROPERTY_SETTINGS
    def test_plain_graphs(self, graph: Graph) -> None:
        """Plain small graphs yield sound proofs."""
        _check_proof(graph, graph["root"][1])

    @pytest.mark.timeout(PROOF_TIMEOUT_SECONDS)
    @given(graph=empty_dep_graphs())
    @PROPERTY_SETTINGS
    def test_empty_dep_graphs(self, graph: Graph) -> None:
        """Graphs with empty-range dependencies yield sound proofs."""
        _check_proof(graph, graph["root"][1])

    @pytest.mark.timeout(PROOF_TIMEOUT_SECONDS)
    @given(graph=selfdep_graphs())
    @PROPERTY_SETTINGS
    def test_selfdep_graphs(self, graph: Graph) -> None:
        """Graphs with self-dependencies yield sound proofs when they fail."""
        _check_proof(graph, graph["root"][1])

    @pytest.mark.timeout(PROOF_TIMEOUT_SECONDS)
    @given(graph=sparse_version_graphs())
    @PROPERTY_SETTINGS
    def test_sparse_graphs(self, graph: Graph) -> None:
        """Graphs with non-contiguous version numbers yield sound proofs."""
        _check_proof(graph, graph["root"][1])

    @pytest.mark.timeout(PROOF_TIMEOUT_SECONDS)
    @given(case=graph_and_constraints())
    @PROPERTY_SETTINGS
    def test_constrained_graphs(
        self, case: tuple[Graph, dict[str, Range[int]]]
    ) -> None:
        """Constraint-driven failures carry sound CONSTRAINT leaves."""
        graph, constraints = case
        _check_proof(graph, graph["root"][1], constraints)


class TestDegenerateRequirements:
    """Degenerate requirements fail with a proof whose leaves are sound."""

    @pytest.mark.timeout(PROOF_TIMEOUT_SECONDS)
    @given(graph=small_exhaustive_graphs())
    @PROPERTY_SETTINGS
    def test_unknown_package_requirement(self, graph: Graph) -> None:
        """Requiring a nonexistent package fails with a sound proof."""
        requirements = dict(graph["root"][1])
        requirements["ghost-package"] = Range.full()
        solution, error = solve(graph, requirements=requirements)
        if solution is None and error is None:
            return
        assert solution is None, "resolved despite requiring a nonexistent package"
        assert error is not None
        assert error.incompatibility is not None
        for leaf in proof_leaves(error.incompatibility):
            check_leaf_against_universe(leaf, graph, requirements)

    @pytest.mark.timeout(PROOF_TIMEOUT_SECONDS)
    @given(
        graph=small_exhaustive_graphs(),
        pkg_index=st.integers(min_value=0, max_value=3),
    )
    @PROPERTY_SETTINGS
    def test_empty_range_requirement(self, graph: Graph, pkg_index: int) -> None:
        """An empty-range requirement fails with a sound proof."""
        packages = sorted(p for p in graph if p != "root")
        target = packages[pkg_index % len(packages)]
        requirements = dict(graph["root"][1])
        requirements[target] = Range.empty()
        solution, error = solve(graph, requirements=requirements)
        if solution is None and error is None:
            return
        assert solution is None, "resolved despite an empty-range requirement"
        assert error is not None
        assert error.incompatibility is not None
        for leaf in proof_leaves(error.incompatibility):
            check_leaf_against_universe(leaf, graph, requirements)
