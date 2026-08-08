"""Tests for the solution a resolve returns: its pins, roots, and edges."""

from __future__ import annotations

from typing import Any

import pytest

from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolutionError, Resolver, Solution
from nab_resolver.result import build_solution
from nab_resolver.root import ROOT
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .test_resolver import DictProvider

Packages = dict[str, dict[int, dict[str, Range]]]


def solve(packages: Packages, **requirements: Range) -> Solution[str, int]:
    return Resolver(DictProvider(packages)).solve(dict(requirements))


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


class TestRoots:
    def test_roots_are_the_requirements_in_the_order_given(self) -> None:
        packages: Packages = {"a": {1: {}}, "b": {1: {}}, "c": {1: {}}}

        solution = solve(packages, c=Range.full(), a=Range.full(), b=Range.full())

        assert solution.roots == ("c", "a", "b")

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


class TestBuildSolution:
    def test_the_root_sentinel_is_neither_pinned_nor_a_root(self) -> None:
        # Term[Any, int] sidesteps the invariant PackageType TypeVar so
        # ROOT and str entries can share a list.
        terms: list[Term[Any, int]] = [
            Term(ROOT, Range.singleton(1)),
            Term("a", Range.full(), positive=False),
        ]
        root_clause = Incompatibility(terms, cause=IncompatibilityCause.ROOT)

        solution = build_solution(
            {ROOT: 1, "a": 3},
            [root_clause],
            lambda package, version: {},
            root_sentinel=ROOT,
        )

        assert solution.pins == {"a": 3}
        assert solution.roots == ("a",)

    def test_clauses_that_are_not_root_causes_contribute_no_roots(self) -> None:
        terms: list[Term[Any, int]] = [
            Term("a", Range.singleton(1)),
            Term("b", Range.full(), positive=False),
        ]
        dependency_clause = Incompatibility(
            terms, cause=IncompatibilityCause.DEPENDENCY
        )

        solution = build_solution(
            {"a": 1, "b": 1},
            [dependency_clause],
            lambda package, version: {},
            root_sentinel=ROOT,
        )

        assert solution.pins == {}
        assert solution.roots == ()
        assert solution.edges == ()
