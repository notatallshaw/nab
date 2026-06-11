"""Graph-level oracles shared by the proof and metamorphic property tests.

``solve`` wraps a :class:`FuzzProvider` resolution and normalizes the
outcome; ``proof_leaves`` and ``check_leaf_against_universe`` walk an
unsat proof's derivation DAG and validate every external clause against
the generating graph.  The strategies here produce graph shapes the
shared strategies module does not: self-dependencies and non-contiguous
version sets.
"""

from __future__ import annotations

from hypothesis import strategies as st

from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolutionError, Resolver
from nab_resolver.root import ROOT
from nab_resolver.types import Incompatibility, IncompatibilityCause

from .providers import FuzzProvider
from .strategies import PACKAGE_NAMES, non_empty_ranges, version_ranges

Graph = dict[str, dict[int, dict[str, Range[int]]]]


def solve(
    graph: Graph,
    requirements: dict[str, Range[int]] | None = None,
    constraints: dict[str, Range[int]] | None = None,
) -> tuple[dict[str, int] | None, ResolutionError | None]:
    """Resolve ``graph`` and return ``(solution, error)``.

    Requirements default to the graph's root dependencies, resolved
    directly (no virtual root pin).  Iteration-limit blowups return
    ``(None, None)``: they carry no proof to check, so callers treat
    them as inconclusive.
    """
    provider = FuzzProvider(graph)
    resolver: Resolver[str, int] = Resolver(provider, max_iterations=2000)
    reqs = requirements if requirements is not None else graph["root"][1]
    try:
        return resolver.resolve(reqs, constraints=constraints), None
    except ResolutionError as error:
        if "exceeded" in str(error):
            return None, None
        return None, error


def proof_leaves(
    incompatibility: Incompatibility[str, int],
) -> list[Incompatibility[str, int]]:
    """All non-DERIVED clauses in the derivation DAG."""
    leaves: list[Incompatibility[str, int]] = []
    seen: set[int] = set()
    stack = [incompatibility]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node.cause is IncompatibilityCause.DERIVED:
            assert node.cause_left is not None, "DERIVED node missing cause_left"
            assert node.cause_right is not None, "DERIVED node missing cause_right"
            stack.append(node.cause_left)
            stack.append(node.cause_right)
        else:
            leaves.append(node)
    return leaves


def check_leaf_against_universe(
    leaf: Incompatibility[str, int],
    graph: Graph,
    requirements: dict[str, Range[int]],
    constraints: dict[str, Range[int]] | None = None,
) -> None:
    """Assert one external clause of an unsat proof is true in the universe.

    ROOT: matches a stated requirement.
    DEPENDENCY: for every real version of the parent inside the parent
    term, the universe dep range is a subset of the clause's dep range.
    A single-term clause is a merged self-dependency ({v} & ~range);
    it is true when the version's declared self-range excludes it.
    NO_VERSIONS: no real version of the package lies in the claimed range.
    CONSTRAINT: no real version lies in claimed-range & user-constraint.
    """
    constraints = constraints or {}
    if leaf.cause is IncompatibilityCause.ROOT:
        non_root = [t for t in leaf.terms if t.package is not ROOT]
        assert len(non_root) == 1, f"ROOT clause shape unexpected: {leaf!r}"
        term = non_root[0]
        assert not term.is_positive(), f"ROOT dep term should be negative: {leaf!r}"
        assert term.package in requirements, (
            f"ROOT clause names {term.package!r} which was never required"
        )
        assert term.constraint == requirements[term.package], (
            f"ROOT clause range {term.constraint} != stated "
            f"requirement {requirements[term.package]}"
        )
        return

    if leaf.cause is IncompatibilityCause.DEPENDENCY:
        if len(leaf.terms) == 1:
            (term,) = leaf.terms
            assert term.is_positive(), f"self-dep term should be positive: {leaf!r}"
            parent = term.package
            real_versions = [v for v in graph.get(parent, {}) if v in term.constraint]
            assert real_versions, (
                f"DEPENDENCY clause for {parent!r} covers no real version: {leaf!r}"
            )
            for v in real_versions:
                actual = graph[parent][v].get(parent)
                assert actual is not None, (
                    f"{parent!r}@{v} has no self-dep but proof claims it: {leaf!r}"
                )
                assert v not in actual, (
                    f"{parent!r}@{v} self-range {actual} contains {v}, the "
                    f"clause should have been vacuous: {leaf!r}"
                )
            return

        assert len(leaf.terms) == 2, f"DEPENDENCY clause shape: {leaf!r}"
        parent_term, dep_term = leaf.terms
        assert parent_term.is_positive()
        assert not dep_term.is_positive()
        parent = parent_term.package
        dep = dep_term.package
        real_versions = [
            v for v in graph.get(parent, {}) if v in parent_term.constraint
        ]
        assert real_versions, (
            f"DEPENDENCY clause for {parent!r} covers no real version: {leaf!r}"
        )
        for v in real_versions:
            actual = graph[parent][v].get(dep)
            assert actual is not None, (
                f"{parent!r}@{v} has no dep on {dep!r} but proof claims it: {leaf!r}"
            )
            assert (actual & dep_term.constraint) == actual, (
                f"{parent!r}@{v} dep range {actual} not a subset of "
                f"claimed {dep_term.constraint}: {leaf!r}"
            )
        return

    if leaf.cause is IncompatibilityCause.NO_VERSIONS:
        assert len(leaf.terms) == 1, f"NO_VERSIONS clause shape: {leaf!r}"
        term = leaf.terms[0]
        assert term.is_positive()
        in_range = [v for v in graph.get(term.package, {}) if v in term.constraint]
        assert not in_range, (
            f"NO_VERSIONS claims no {term.package!r} in {term.constraint} "
            f"but versions {in_range} exist"
        )
        return

    if leaf.cause is IncompatibilityCause.CONSTRAINT:
        assert len(leaf.terms) == 1, f"CONSTRAINT clause shape: {leaf!r}"
        term = leaf.terms[0]
        assert term.is_positive()
        constraint = constraints.get(term.package)
        assert constraint is not None, (
            f"CONSTRAINT clause for unconstrained package {term.package!r}"
        )
        narrowed = term.constraint & constraint
        in_range = [v for v in graph.get(term.package, {}) if v in narrowed]
        assert not in_range, (
            f"CONSTRAINT claims no {term.package!r} in "
            f"{term.constraint} & {constraint} but versions {in_range} exist"
        )
        return

    message = f"Unexpected leaf cause {leaf.cause!r} in proof: {leaf!r}"
    raise AssertionError(message)


@st.composite
def selfdep_graphs(draw: st.DrawFn) -> Graph:
    """Small graphs where at least one package depends on itself.

    The forced self-dependency's range may be empty, may exclude the
    carrying version, or may include it.
    """
    num_packages = draw(st.integers(min_value=2, max_value=4))
    packages = PACKAGE_NAMES[:num_packages]

    graph: Graph = {}
    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=3))
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            deps: dict[str, Range[int]] = {}
            num_deps = draw(st.integers(min_value=0, max_value=2))
            if num_deps:
                dep_packages = draw(
                    st.lists(
                        st.sampled_from(packages),  # self allowed
                        min_size=num_deps,
                        max_size=num_deps,
                        unique=True,
                    )
                )
                for dep_package in dep_packages:
                    deps[dep_package] = draw(version_ranges())
            package_versions[version] = deps
        graph[package] = package_versions

    target = draw(st.sampled_from(packages))
    target_version = draw(st.sampled_from(sorted(graph[target].keys())))
    graph[target][target_version][target] = draw(version_ranges())

    root_dep_count = draw(st.integers(min_value=1, max_value=min(2, len(packages))))
    root_dep_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_dep_count,
            max_size=root_dep_count,
            unique=True,
        )
    )
    graph["root"] = {1: {p: draw(non_empty_ranges()) for p in root_dep_packages}}
    return graph


@st.composite
def sparse_version_graphs(draw: st.DrawFn) -> Graph:
    """Small graphs whose version numbers are non-contiguous (e.g. {2, 7, 15}).

    The shared strategies always use 1..n; sparse sets exercise ranges
    whose interior contains no actual versions.
    """
    num_packages = draw(st.integers(min_value=2, max_value=3))
    packages = PACKAGE_NAMES[:num_packages]

    graph: Graph = {}
    for package in packages:
        versions = draw(
            st.lists(
                st.integers(min_value=1, max_value=20),
                min_size=1,
                max_size=3,
                unique=True,
            )
        )
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in sorted(versions):
            deps: dict[str, Range[int]] = {}
            num_deps = draw(st.integers(min_value=0, max_value=2))
            others = [p for p in packages if p != package]
            if num_deps and others:
                dep_packages = draw(
                    st.lists(
                        st.sampled_from(others),
                        min_size=min(num_deps, len(others)),
                        max_size=min(num_deps, len(others)),
                        unique=True,
                    )
                )
                for dep_package in dep_packages:
                    deps[dep_package] = draw(version_ranges())
            package_versions[version] = deps
        graph[package] = package_versions

    root_dep_count = draw(st.integers(min_value=1, max_value=min(2, len(packages))))
    root_dep_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_dep_count,
            max_size=root_dep_count,
            unique=True,
        )
    )
    graph["root"] = {1: {p: draw(version_ranges()) for p in root_dep_packages}}
    return graph
