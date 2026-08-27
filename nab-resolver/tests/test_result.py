"""Tests for the solution a resolve returns: its pins, roots, and edges."""

from __future__ import annotations

import copy
import pickle
from itertools import permutations
from typing import Any

import pytest

import nab_resolver.result as result_module
from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolutionError, Resolver, Solution
from nab_resolver.result import build_solution_data
from nab_resolver.root import ROOT
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RootRequirement,
    Term,
)

from .test_resolver import DictProvider

Packages = dict[str, dict[int, dict[str, Range]]]


def solve(packages: Packages, **requirements: Range) -> Solution[str, int]:
    return Resolver(DictProvider(packages)).solve(dict(requirements))


def _order_not_preserved_by_set(packages: tuple[str, ...]) -> tuple[str, ...]:
    """Choose an order that this interpreter's set iteration changes."""
    return next(order for order in permutations(packages) if tuple(set(order)) != order)


class TestPins:
    def test_resolve_returns_the_pins_of_solve(self) -> None:
        packages: Packages = {
            "root": {1: {"foo": Range.full()}},
            "foo": {2: {"bar": Range.full()}, 1: {}},
            "bar": {1: {}},
        }
        pins = Resolver(DictProvider(packages)).resolve({"root": Range.singleton(1)})

        assert pins == solve(packages, root=Range.singleton(1)).pins
        assert pins == {"root": 1, "foo": 2, "bar": 1}

    def test_solve_raises_when_no_solution_exists(self) -> None:
        packages: Packages = {"foo": {1: {"bar": Range.singleton(9)}}, "bar": {1: {}}}

        with pytest.raises(ResolutionError):
            Resolver(DictProvider(packages)).solve({"foo": Range.full()})


class TestPublicSolution:
    def test_solution_uses_the_promised_module_and_pickle_path(self) -> None:
        solution = solve({"a": {1: {}}}, a=Range.full())
        serialized = pickle.dumps(solution, protocol=0)
        restored = pickle.loads(serialized)  # noqa: S301

        assert type(solution) is Solution
        assert Solution.__module__ == "nab_resolver.resolver"
        assert not hasattr(result_module, "Solution")
        assert b"cnab_resolver.resolver\nSolution\n" in serialized
        assert restored == solution

    def test_a_solution_survives_copying_and_the_default_pickle(self) -> None:
        solution = solve({"a": {1: {"b": Range.full()}}, "b": {1: {}}}, a=Range.full())

        assert copy.copy(solution) == solution
        assert copy.deepcopy(solution) == solution
        assert pickle.loads(pickle.dumps(solution)) == solution  # noqa: S301

    def test_equality_reads_every_field_and_declines_other_types(self) -> None:
        fields: dict[str, Any] = {
            "pins": {"a": 1},
            "edges": (("a", "b"),),
            "roots": ("a",),
        }
        others: dict[str, Any] = {"pins": {"a": 2}, "edges": (), "roots": ("b",)}
        solution: Solution[str, int] = Solution(**fields)

        assert solution == Solution(**fields)
        for name, other in others.items():
            assert solution != Solution(**{**fields, name: other}), name

        assert solution.__eq__("a") is NotImplemented

    def test_repr_names_the_class_and_every_field(self) -> None:
        solution = Solution(pins={"a": 1}, edges=(("a", "b"),), roots=("a",))

        assert repr(solution) == (
            "Solution(pins={'a': 1}, edges=(('a', 'b'),), roots=('a',))"
        )

    def test_hash_is_declared_but_the_pins_dict_defeats_it(self) -> None:
        solution = Solution(pins={"a": 1}, edges=(), roots=("a",))

        assert Solution.__hash__ is not None
        with pytest.raises(TypeError):
            hash(solution)

    def test_fields_cannot_be_reassigned_or_deleted(self) -> None:
        solution = solve({"a": {1: {}}}, a=Range.full())

        with pytest.raises(AttributeError, match="cannot assign to field 'pins'"):
            solution.pins = {}
        with pytest.raises(AttributeError, match="cannot delete field 'pins'"):
            del solution.pins

    def test_pattern_matching_reads_the_three_fields_positionally(self) -> None:
        match Solution(pins={"a": 1}, edges=(("a", "b"),), roots=("a",)):
            case Solution(pins, edges, roots):
                assert (pins, edges, roots) == ({"a": 1}, (("a", "b"),), ("a",))


class TestRoots:
    def test_roots_are_the_requirements_in_the_order_given(self) -> None:
        packages: Packages = {"a": {1: {}}, "b": {1: {}}, "c": {1: {}}}
        root_order = _order_not_preserved_by_set(tuple(packages))
        requirements = [
            RootRequirement[str, int](package, Range.full()) for package in root_order
        ]

        solution = Resolver(DictProvider(packages)).solve(requirements)

        assert solution.roots == root_order

    def test_a_root_that_is_also_a_dependency_is_still_a_root(self) -> None:
        packages: Packages = {"a": {1: {"b": Range.full()}}, "b": {1: {}}}

        solution = solve(packages, a=Range.full(), b=Range.full())

        assert solution.roots == ("a", "b")
        assert solution.edges == (("a", "b"),)


class TestEdges:
    def test_edges_are_breadth_first_from_the_roots(self) -> None:
        packages: Packages = {
            "a": {1: {"b": Range.full(), "c": Range.full()}},
            "b": {1: {"d": Range.full()}},
            "c": {1: {"d": Range.full()}},
            "d": {1: {}},
        }

        solution = solve(packages, a=Range.full())

        assert solution.edges == (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))

    def test_a_shared_dependency_is_recorded_once_per_dependent(self) -> None:
        packages: Packages = {
            "a": {1: {"shared": Range.full()}},
            "b": {1: {"shared": Range.full()}},
            "shared": {1: {}},
        }

        solution = solve(packages, a=Range.full(), b=Range.full())

        assert solution.edges == (("a", "shared"), ("b", "shared"))

    def test_a_dependency_cycle_terminates(self) -> None:
        packages: Packages = {
            "a": {1: {"b": Range.full()}},
            "b": {1: {"a": Range.full()}},
        }

        solution = solve(packages, a=Range.full())

        assert solution.pins == {"a": 1, "b": 1}
        assert solution.edges == (("a", "b"), ("b", "a"))

    def test_a_leaf_resolve_has_no_edges(self) -> None:
        solution = solve({"a": {1: {}}}, a=Range.full())

        assert solution.pins == {"a": 1}
        assert solution.edges == ()

    def test_every_edge_endpoint_is_pinned(self) -> None:
        packages: Packages = {
            "a": {1: {"b": Range.full()}},
            "b": {2: {"c": Range.full()}, 1: {}},
            "c": {1: {}},
            "unused": {1: {}},
        }

        solution = solve(packages, a=Range.full())

        assert "unused" not in solution.pins
        assert all(
            parent in solution.pins and child in solution.pins
            for parent, child in solution.edges
        )


class TestBuildSolutionData:
    def test_the_root_sentinel_is_neither_pinned_nor_a_root(self) -> None:
        # Term[Any, int] sidesteps the invariant PackageType TypeVar so
        # ROOT and str entries can share a list.
        terms: list[Term[Any, int]] = [
            Term(ROOT, Range.singleton(1)),
            Term("a", Range.full(), positive=False),
        ]
        root_clause = Incompatibility(terms, cause=IncompatibilityCause.ROOT)

        pins, edges, roots = build_solution_data(
            {ROOT: 1, "a": 3},
            [root_clause],
            lambda package, version: {},
            root_sentinel=ROOT,
        )

        assert pins == {"a": 3}
        assert edges == ()
        assert roots == ("a",)

    def test_clauses_that_are_not_root_causes_contribute_no_roots(self) -> None:
        terms: list[Term[Any, int]] = [
            Term("a", Range.singleton(1)),
            Term("b", Range.full(), positive=False),
        ]
        dependency_clause = Incompatibility(
            terms, cause=IncompatibilityCause.DEPENDENCY
        )

        pins, edges, roots = build_solution_data(
            {"a": 1, "b": 1},
            [dependency_clause],
            lambda package, version: {},
            root_sentinel=ROOT,
        )

        assert pins == {}
        assert edges == ()
        assert roots == ()
