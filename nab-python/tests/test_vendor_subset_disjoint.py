"""Tests for the vendored patch's bounds-only subset and disjointness walks.

``is_subset`` and ``is_disjoint`` take a two-pointer walk over the interval
lists whenever both operands are plain, so every answer is checked against the
set-algebra oracle it replaces (``(a - b).is_empty`` and ``(a & b).is_empty``)
and against pointwise membership over a version pool. The population runs the
algebra over specifier-built ranges first, which reaches interval shapes no
specifier writes.
"""

from __future__ import annotations

import functools
import itertools
from typing import TYPE_CHECKING

from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Iterator

# Finals, every pre-release phase, dev, post, local, and a second epoch.
POOL = [
    Version(text)
    for text in (
        "0",
        "0.dev0",
        "1.0.dev1",
        "1.0a1",
        "1.0b2",
        "1.0rc1",
        "1.0",
        "1.0+local",
        "1.0.post1",
        "1.5",
        "2.0a1",
        "2.0",
        "3.0",
        "1!0.5",
        "1!1.0",
    )
]

SPECIFIERS = [
    "",
    ">=1.0",
    "<2.0",
    ">=1.0,<2.0",
    "==1.0",
    "!=1.0",
    "~=1.0.1",
    ">=1.0,<3.0,!=1.5",
    "==1.0.*",
    ">1.0",
    "<=1.0",
    ">=1.0.dev0,<2.0a1",
    "!=1.0,!=1.5,!=2.0",
    ">=1!0.5",
    "===frobnitz",
    "===1.0",
    "==1.0+local",
]


def _seed_ranges() -> Iterator[VersionRange]:
    for text in SPECIFIERS:
        for prereleases in (None, True, False):
            yield SpecifierSet(text, prereleases=prereleases).to_range()
    yield VersionRange.full()
    yield VersionRange.full(admit_arbitrary=False)
    yield VersionRange.empty()
    yield VersionRange.singleton(Version("1.0"))
    yield VersionRange.from_bounds(Version("1.0"), Version("2.0"))
    yield VersionRange.from_bounds(None, Version("2.0"), include_upper=True)
    yield VersionRange.from_bounds(Version("1.0"), None)


@functools.lru_cache(maxsize=1)
def _population() -> tuple[VersionRange, ...]:
    """Seeds plus complements and pairwise algebra over a sample of them."""
    seeds = list(_seed_ranges())
    built = list(seeds)
    built.extend(~r for r in seeds)
    sample = seeds[::4]
    for a, b in itertools.product(sample, sample):
        if a._prereleases_configured is not b._prereleases_configured:
            continue
        built.extend((a & b, a | b, a - b, ~(a - b)))
    return tuple(built)


def _same_policy_pairs() -> Iterator[tuple[VersionRange, VersionRange]]:
    population = _population()
    for a, b in itertools.product(population, population):
        if a._prereleases_configured is b._prereleases_configured:
            yield a, b


class TestSubsetAndDisjointMatchTheAlgebra:
    def test_population_covers_the_required_shapes(self) -> None:
        population = _population()
        interval_counts = {len(r._bounds) for r in population}
        assert len(population) > 400
        assert {0, 1, 2} <= interval_counts
        assert max(interval_counts) >= 4
        assert any(r._admit or r._reject for r in population)
        assert any(r._arbitrary_active() for r in population)
        assert any(r.is_empty for r in population)
        assert VersionRange.full() in population

    def test_subset_matches_the_difference_algebra(self) -> None:
        for a, b in _same_policy_pairs():
            if a._arbitrary_active() and not b._arbitrary_active():
                # Carved out below: a live arbitrary admission holds non-version
                # strings, which the interval difference does not model.
                continue
            assert a.is_subset(b) == (a - b).is_empty, (a, b)

    def test_a_live_arbitrary_admission_is_never_a_subset_of_a_dead_one(self) -> None:
        live, dead = VersionRange.full(), VersionRange.full(admit_arbitrary=False)
        assert (live - dead).is_empty
        assert not live.is_subset(dead)
        assert dead.is_subset(live)
        witnessed = 0
        for a, b in _same_policy_pairs():
            if a._arbitrary_active() and not b._arbitrary_active():
                assert not a.is_subset(b), (a, b)
                witnessed += 1
        assert witnessed > 0

    def test_disjoint_matches_the_intersection_algebra(self) -> None:
        for a, b in _same_policy_pairs():
            assert a.is_disjoint(b) == (a & b).is_empty, (a, b)

    def test_superset_mirrors_subset(self) -> None:
        for a, b in _same_policy_pairs():
            assert a.is_superset(b) == b.is_subset(a), (a, b)

    def test_subset_implies_pointwise_membership(self) -> None:
        for a, b in _same_policy_pairs():
            if not a.is_subset(b):
                continue
            for version in POOL:
                assert version not in a or version in b, (a, b, version)

    def test_disjoint_implies_no_shared_member(self) -> None:
        for a, b in _same_policy_pairs():
            if not a.is_disjoint(b):
                continue
            for version in POOL:
                assert version not in a or version not in b, (a, b, version)

    def test_overlap_exhibits_a_shared_member_or_a_gap(self) -> None:
        witnessed = 0
        for a, b in _same_policy_pairs():
            if a.is_subset(b) or a.is_disjoint(b):
                continue
            shared = [v for v in POOL if v in a and v in b]
            only_a = [v for v in POOL if v in a and v not in b]
            if shared and only_a:
                witnessed += 1
        assert witnessed > 0

    def test_empty_is_a_subset_of_and_disjoint_from_everything(self) -> None:
        empty = VersionRange.empty()
        for other in _population():
            if other._prereleases_configured is not empty._prereleases_configured:
                continue
            assert empty.is_subset(other)
            assert empty.is_disjoint(other)

    def test_a_punctured_range_is_a_subset_of_its_span(self) -> None:
        punctured = SpecifierSet(">=1.0,<2.0,!=1.5").to_range()
        span = SpecifierSet(">=1.0,<2.0").to_range()
        assert punctured.is_subset(span)
        assert not span.is_subset(punctured)

    def test_a_pin_on_an_excluded_version_is_disjoint(self) -> None:
        pin = SpecifierSet("==1.5").to_range()
        punctured = SpecifierSet(">=1.0,<2.0,!=1.5").to_range()
        assert pin.is_disjoint(punctured)
        assert punctured.is_disjoint(pin)

    def test_an_interval_running_past_the_right_list_is_not_a_subset(self) -> None:
        # The walk exhausts the right list before the left one, which is the
        # branch a left interval above every right interval takes.
        assert (
            not SpecifierSet(">=3.0")
            .to_range()
            .is_subset(SpecifierSet(">=1.0,<2.0").to_range())
        )
