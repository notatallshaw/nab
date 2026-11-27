"""One selected graph traversal supports fetching and cache-only consumers."""

import pytest

from nab_resolver.ranges import Range
from nab_resolver.result import (
    IncompleteSelectionError,
    build_solution_data,
    walk_selected,
)
from nab_resolver.root import ROOT
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


@pytest.mark.parametrize("depth_first", [False, True])
def test_traversal_visits_selected_cycles_once_in_requested_order(
    *, depth_first: bool
) -> None:
    selected = {"root": 1, "left": 2, "right": 3, "leaf": 4, "unrelated": 5}
    dependencies = {
        "root": ("left", "right"),
        "left": ("leaf",),
        "right": ("leaf",),
        "leaf": ("root",),
    }
    reads: list[tuple[str, int]] = []

    def read(package: str, candidate: int) -> tuple[str, ...]:
        assert (package, candidate) not in reads
        reads.append((package, candidate))
        return dependencies[package]

    reached = list(walk_selected(("root",), selected, read, depth_first=depth_first))
    order = (
        ["root", "right", "leaf", "left"]
        if depth_first
        else ["root", "left", "right", "leaf"]
    )
    assert [package for package, _, _ in reached] == order
    assert reads == [(package, selected[package]) for package in order]


def test_missing_selection_or_unread_metadata_remains_incomplete() -> None:
    with pytest.raises(IncompleteSelectionError) as missing:
        list(walk_selected(("missing",), {}, lambda _package, _candidate: ()))
    assert missing.value.package == "missing"

    with pytest.raises(IncompleteSelectionError) as unread:
        list(walk_selected(("root",), {"root": 1}, lambda _package, _candidate: None))
    assert unread.value.package == "root"


def test_metadata_lookup_errors_propagate_without_becoming_incomplete() -> None:
    failure = ValueError("metadata lookup failed")
    selected: dict[str, int] = {"root": 1}

    def read(_package: str, _candidate: int) -> tuple[str, ...]:
        raise failure

    with pytest.raises(ValueError, match="metadata lookup failed") as caught:
        list(walk_selected(("root",), selected, read))
    assert caught.value is failure


def test_final_result_retains_missing_decision_diagnostic() -> None:
    root = Incompatibility[object, int](
        [
            Term(ROOT, Range.singleton(0), positive=True),
            Term("missing", Range.full(), positive=False),
        ],
        cause=IncompatibilityCause.ROOT,
    )
    with pytest.raises(
        RuntimeError, match="reachable package 'missing' has no decision"
    ):
        build_solution_data(
            {}, (root,), lambda _package, _candidate: {}, root_sentinel=ROOT
        )
