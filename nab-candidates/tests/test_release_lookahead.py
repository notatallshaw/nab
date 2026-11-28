from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from nab_candidates.lookahead import (
    DecisionConflict,
    RangeConflict,
    find_dependency_conflict,
    queue_blocker,
)


def test_root_requirement_can_reject_a_release_before_a_decision() -> None:
    required = SpecifierSet(">=2").to_range()
    allowed = SpecifierSet("<2").to_range()

    conflict = find_dependency_conflict(
        {"Render_Lib": required},
        {"render-lib": allowed},
        {"render-lib": Version("1")},
        {},
        normalize=canonicalize_name,
    )

    assert conflict == RangeConflict("render-lib", required, allowed, root=True)


def test_lookahead_can_check_only_roots_or_also_the_partial_solution() -> None:
    required = SpecifierSet(">=2").to_range()
    compatible = SpecifierSet(">=3").to_range()
    incompatible = SpecifierSet("<2").to_range()
    dependencies = {"renderer": required}

    assert (
        find_dependency_conflict(dependencies, {}, None, {"renderer": incompatible})
        is None
    )
    assert find_dependency_conflict(
        dependencies, {}, {}, {"renderer": incompatible}
    ) == RangeConflict("renderer", required, incompatible, root=False)
    assert find_dependency_conflict(
        dependencies, {}, {"renderer": Version("1")}, {}
    ) == DecisionConflict("renderer", required, Version("1"))
    assert (
        find_dependency_conflict(dependencies, {}, {"renderer": Version("2")}, {})
        is None
    )
    assert (
        find_dependency_conflict(dependencies, {}, {}, {"renderer": compatible}) is None
    )
    assert find_dependency_conflict(dependencies, {}, {}, {}) is None


def test_repeated_blocker_is_queued_once_until_consumed_and_bounded_afterwards() -> (
    None
):
    pending: list[str] = []
    counts: dict[str, int] = {}
    queue_blocker("renderer", counts, pending, limit=2)
    queue_blocker("renderer", counts, pending, limit=2)
    assert pending == ["renderer"]
    assert counts == {"renderer": 1}

    pending.clear()
    queue_blocker("renderer", counts, pending, limit=2)
    pending.clear()
    queue_blocker("renderer", counts, pending, limit=2)
    assert pending == []
    assert counts == {"renderer": 2}
