"""Conflict priority works with package identities independent of Python names."""

from __future__ import annotations

import pytest

from nab_resolver.priority import (
    TIER_AFFECTED,
    TIER_CULPRIT,
    TIER_NORMAL,
    compute_tier,
)


@pytest.mark.parametrize(
    ("affected", "culprit", "counts", "forced", "expected"),
    [
        pytest.param(0, 0, None, False, TIER_NORMAL, id="no-history"),
        pytest.param(4, 4, {1: 4}, False, TIER_NORMAL, id="below-thresholds"),
        pytest.param(5, 20, {1: 20}, True, TIER_AFFECTED, id="conflict-first"),
        pytest.param(0, 0, None, True, TIER_CULPRIT, id="forced-backtrack"),
        pytest.param(0, 5, {1: 5}, False, TIER_CULPRIT, id="only-culprit"),
        pytest.param(0, 8, {1: 8, 2: 4}, False, TIER_NORMAL, id="small-lead"),
        pytest.param(0, 9, {1: 9, 2: 4}, False, TIER_CULPRIT, id="large-lead"),
        pytest.param(0, 5, {1: 5, 2: 5}, False, TIER_NORMAL, id="equal-culprits"),
        pytest.param(0, 6, None, False, TIER_NORMAL, id="missing-culprit-map"),
    ],
)
def test_conflict_tiers(
    affected: int,
    culprit: int,
    counts: dict[int, int] | None,
    *,
    forced: bool,
    expected: int,
) -> None:
    assert (
        compute_tier(1, affected, culprit, counts, force_backtracked=forced) == expected
    )
