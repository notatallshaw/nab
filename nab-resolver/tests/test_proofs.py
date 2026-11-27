"""Failure attribution preserves original requests and guarded absence domains."""

from nab_resolver.proofs import (
    dependency_terms,
    external_clauses,
    select_dependency_causes,
)
from nab_resolver.ranges import Range
from nab_resolver.report import format_error
from nab_resolver.root import ROOT
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


def request(
    parent: object,
    child: str,
    allowed: Range[int],
    *,
    cause: IncompatibilityCause = IncompatibilityCause.DEPENDENCY,
) -> Incompatibility[object, int]:
    """Build an original request with one selected parent version."""
    return Incompatibility[object, int](
        [
            Term(parent, Range.singleton(1), positive=True),
            Term(child, allowed, positive=False),
        ],
        cause=cause,
    )


def test_external_clauses_preserve_both_orders_and_visit_shared_nodes_once() -> None:
    left = request(ROOT, "left", Range.full(), cause=IncompatibilityCause.ROOT)
    right = request(ROOT, "right", Range.full(), cause=IncompatibilityCause.ROOT)
    shared = Incompatibility[object, int](
        [], cause=IncompatibilityCause.DERIVED, cause_left=left, cause_right=right
    )
    proof = Incompatibility[object, int](
        [], cause=IncompatibilityCause.DERIVED, cause_left=shared, cause_right=right
    )

    assert external_clauses(proof) == [left, right]
    assert external_clauses(proof, left_first=False) == [right, left]
    assert external_clauses(None) == []


def test_dependency_terms_recover_narrowed_and_self_dependencies() -> None:
    dependency = request("parent", "child", Range.singleton(2))
    assert dependency_terms(dependency) == tuple(dependency.terms)

    merged = Incompatibility[object, int](
        [Term("parent", Range.singleton(1), positive=True)],
        cause=IncompatibilityCause.DEPENDENCY,
        dependency_range=Range.singleton(2),
    )
    pair = dependency_terms(merged)
    assert pair is not None
    assert pair[0] is merged.terms[0]
    assert pair[1].package == "parent"
    assert pair[1].constraint == Range.singleton(2)
    assert not pair[1].is_positive()

    assert (
        dependency_terms(
            Incompatibility[object, int]([], cause=IncompatibilityCause.ROOT)
        )
        is None
    )
    assert (
        dependency_terms(
            Incompatibility[object, int]([], cause=IncompatibilityCause.DEPENDENCY)
        )
        is None
    )
    assert (
        dependency_terms(
            Incompatibility[object, int](
                merged.terms, cause=IncompatibilityCause.DEPENDENCY
            )
        )
        is None
    )


def test_absence_must_cover_a_requested_range_before_attribution() -> None:
    broad = request("one", "child", Range.from_versions((1, 2, 3)))
    narrow = request("two", "other", Range.singleton(1))
    missing = Incompatibility[object, int](
        [Term("child", Range.singleton(1), positive=True)],
        cause=IncompatibilityCause.NO_VERSIONS,
    )
    selected = select_dependency_causes([broad, narrow, missing])
    assert [cause.package for cause in selected] == ["child", "other"]

    missing_two = Incompatibility[object, int](
        [Term("child", Range.from_versions((2, 3)), positive=True)],
        cause=IncompatibilityCause.NO_VERSIONS,
    )
    selected = select_dependency_causes([broad, narrow, missing, missing_two])
    assert [(cause.parent, cause.package) for cause in selected] == [("one", "child")]


def test_contextual_absence_uses_its_first_package_and_keeps_guard_ranges() -> None:
    child = request("one", "child", Range.singleton(1))
    other = request("two", "other", Range.singleton(1))
    guarded = Incompatibility[object, int](
        [
            Term("child", Range.singleton(1), positive=True),
            Term("guard", Range.singleton(9), positive=True),
        ],
        cause=IncompatibilityCause.CONTEXTUAL_NO_VERSIONS,
    )
    selected = select_dependency_causes([child, other, guarded])
    assert [(cause.parent, cause.package) for cause in selected] == [("one", "child")]

    assert (
        format_error(guarded)
        == "because no versions of child 1 are available with guard 9 selected"
    )
    guarded.constraint_range = Range.singleton(2)
    assert (
        format_error(guarded)
        == "because the user constrained child 2 with guard 9 selected"
    )


def test_empty_dependencies_prefer_the_impossible_edge_to_its_upstream_request() -> (
    None
):
    root = request(ROOT, "parent", Range.singleton(1), cause=IncompatibilityCause.ROOT)
    broken = request("parent", "child", Range.empty())
    absent_parent = Incompatibility[object, int](
        [Term("parent", Range.full(), positive=True)],
        cause=IncompatibilityCause.NO_VERSIONS,
    )
    selected = select_dependency_causes([root, broken, absent_parent])
    assert [(cause.parent, cause.package) for cause in selected] == [
        ("parent", "child")
    ]


def test_constraints_and_repeated_requests_choose_the_responsible_package() -> None:
    root = request(ROOT, "child", Range.full(), cause=IncompatibilityCause.ROOT)
    parent = request("parent", "child", Range.singleton(1))
    unrelated = request("other", "other-child", Range.full())
    constraint = Incompatibility[object, int](
        [Term("child", Range.full(), positive=True)],
        cause=IncompatibilityCause.CONSTRAINT,
    )
    selected = select_dependency_causes([root, parent, unrelated, constraint])
    assert [(cause.parent, cause.package) for cause in selected] == [
        (None, "child"),
        ("parent", "child"),
    ]

    selected = select_dependency_causes([root, parent, unrelated])
    assert [cause.package for cause in selected] == ["child", "child"]


def test_impossible_parent_child_pair_keeps_each_original_request() -> None:
    broad = request("parent", "child", Range.full())
    impossible = request("parent", "child", Range.empty())
    selected = select_dependency_causes([broad, impossible])

    assert [cause.constraint for cause in selected] == [Range.full(), Range.empty()]


def test_merged_self_dependency_and_non_request_proofs() -> None:
    clause = Incompatibility[object, int](
        [Term("self", Range.singleton(1), positive=True)],
        cause=IncompatibilityCause.DEPENDENCY,
        dependency_range=Range.singleton(2),
    )
    selected = select_dependency_causes([clause])
    assert len(selected) == 1
    assert selected[0].parent == selected[0].package == "self"
    assert selected[0].constraint == Range.singleton(2)

    invalid = Incompatibility[object, int]([], cause=IncompatibilityCause.ROOT)
    guarded = Incompatibility[object, int](
        [
            Term("one", Range.full(), positive=True),
            Term("two", Range.full(), positive=True),
        ],
        cause=IncompatibilityCause.NO_VERSIONS,
    )
    assert select_dependency_causes([invalid, guarded]) == []
