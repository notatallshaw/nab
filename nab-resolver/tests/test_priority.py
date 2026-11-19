"""Exercise conflict-driven priority through real resolver decisions."""

from __future__ import annotations

import pytest

from nab_resolver.errors import ResolutionError
from nab_resolver.priority import (
    CONFLICT_THRESHOLD,
    CULPRIT_DEMOTE_THRESHOLD,
    TIER_AFFECTED,
    TIER_CULPRIT,
    TIER_NORMAL,
    compute_tier,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver

from .property.providers import FuzzProvider, PromotingFuzzProvider


def test_dependency_conflict_credits_the_parent() -> None:
    graph = {"parent": {1: {"child": Range.singleton(2)}}, "child": {1: {}}}
    resolver = Resolver(FuzzProvider(graph))
    with pytest.raises(ResolutionError):
        resolver.resolve({"parent": Range.full()})

    assert resolver.stats.package_conflict_counts["parent"] > 0
    assert resolver.stats.package_conflict_counts.get("child", 0) == 0


def test_conflict_promotion_changes_which_valid_solution_is_selected() -> None:
    graph = {
        "base": {1: {}, 2: {}},
        "consumer": {
            1: {},
            2: {"missing": Range.singleton(3)},
            3: {"base": Range.singleton(1)},
        },
    }
    roots = {"base": Range.full(), "consumer": Range.full()}

    # Choosing base 2 first rules out consumer 3. Promoting consumer after
    # its conflict lets it choose 3 and require base 1 on the next attempt.
    assert Resolver(FuzzProvider(graph)).resolve(roots) == {"base": 2, "consumer": 1}
    provider = PromotingFuzzProvider(graph, conflict_threshold=1)
    assert Resolver(provider).resolve(roots) == {"base": 1, "consumer": 3}


def test_conflict_tiers_for_opaque_package_keys() -> None:
    package, peer = object(), object()
    assert compute_tier(package, 0, 0, None) == TIER_NORMAL

    count = CULPRIT_DEMOTE_THRESHOLD + 1
    assert compute_tier(package, 0, count, {package: count, peer: 1}) == TIER_CULPRIT
    assert compute_tier(package, 0, count, {package: count, peer: 2}) == TIER_NORMAL


def test_promotion_precedes_forced_culprit_demotion() -> None:
    assert compute_tier("pkg", 0, 0, None, force_backtracked=True) == TIER_CULPRIT
    assert (
        compute_tier("pkg", CONFLICT_THRESHOLD, 0, None, force_backtracked=True)
        == TIER_AFFECTED
    )
