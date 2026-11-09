"""Preference proofs accept a non-Python version catalogue through the public API."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from typing import TypeVar

import pytest

from nab_provider._compat import override
from nab_provider.preference import (
    IncompletePreferenceError,
    PreferenceScope,
    PreferenceSearch,
    PreparationStatus,
    _Context,
    _KnownFacts,
)
from nab_resolver.errors import ResolutionError, ResolutionLimitError
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver, ResolverStats

PackageType = TypeVar("PackageType", bound=Hashable)


@dataclass(frozen=True, slots=True)
class Choice:
    """One immutable package/version/file-group identity."""

    package: str
    version: int
    withdrawn: bool = False

    @property
    def base(self) -> str:
        return self.package.partition("[")[0]

    @property
    def label(self) -> str:
        return str(self.version)


@dataclass(frozen=True, slots=True)
class Pin:
    package: str
    version: int


@dataclass(slots=True)
class Data:
    dependencies: dict[str, Range[int]]
    pins: tuple[Pin, ...] = ()


class Catalogue:
    """A fixed integer domain with exact-pin admission and cached dependency facts."""

    def __init__(self, choices: tuple[Choice, ...]) -> None:
        self.items = choices
        self.facts: dict[Choice, Data | None] = {}
        self.missing_listings: set[str] = set()

    def choices(self, package: str) -> tuple[Choice, ...]:
        return tuple(
            candidate for candidate in self.items if candidate.package == package
        )

    def ordered(self, package: str, allowed: Range[int]) -> list[Choice]:
        return [
            candidate
            for candidate in self.choices(package)
            if candidate.version in allowed
        ]

    @staticmethod
    def candidate_package(candidate: Choice) -> str:
        return candidate.package

    @staticmethod
    def candidate_base(candidate: Choice) -> str:
        return candidate.base

    @staticmethod
    def candidate_label(candidate: Choice) -> str:
        return candidate.label

    @staticmethod
    def candidate_withdrawn(candidate: Choice) -> bool:
        return candidate.withdrawn

    @staticmethod
    def full_range() -> Range[int]:
        return Range[int].full()

    @staticmethod
    def intersect_ranges(left: Range[int], right: Range[int]) -> Range[int]:
        return left & right

    @staticmethod
    def range_contains(candidate: Choice, allowed: Range[int]) -> bool:
        return candidate.version in allowed

    @staticmethod
    def pin_allows(pin: Pin, candidate: Choice) -> bool:
        return pin.package == candidate.base and pin.version == candidate.version

    @staticmethod
    def pin_project(pin: Pin) -> str:
        return pin.package

    @staticmethod
    def base_name(package: str) -> str:
        return package.partition("[")[0]

    @staticmethod
    def base_candidate(candidate: Choice) -> Choice:
        return Choice(candidate.base, candidate.version, candidate.withdrawn)


class Search(PreferenceSearch[Choice, Range[int], Pin]):
    """Keep the shared proof scheduler's actual solver-work count."""

    def __init__(
        self,
        catalogue: Catalogue,
        *,
        refresh: bool = False,
        max_steps: int = 100_000,
    ) -> None:
        super().__init__(
            {"dep": Range[int].full()},
            [Pin("dep", 1)],
            {},
            refresh_incomplete=refresh,
            max_steps=max_steps,
        )
        self._catalogue = catalogue
        self.rounds = 0

    @property
    @override
    def catalogue(self) -> Catalogue:
        return self._catalogue

    @override
    def record_solver_stats(self, stats: ResolverStats[PackageType]) -> None:
        self.rounds += stats.rounds


def test_parameterized_scope_has_immutable_hash_keys() -> None:
    candidate = Choice("dep", 1)
    scope = PreferenceScope[Choice](frozenset({candidate}), frozenset({"dep"}), "dep")
    cached = {scope: "proved"}

    assert (
        cached[PreferenceScope(frozenset({candidate}), frozenset({"dep"}), "dep")]
        == "proved"
    )
    assert PreferenceScope[Choice]() not in cached
    assert scope != PreferenceScope(frozenset({candidate}), frozenset({"dep"}), "other")
    assert scope != object()

    for attribute in ("fixed", "live", "target"):
        with pytest.raises(AttributeError):
            setattr(scope, attribute, None)


def test_actual_known_fact_solve_rejects_the_conflicting_assignment() -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((live, withdrawn))
    catalogue.facts[withdrawn] = Data({"missing": Range.singleton(1)})
    search = Search(catalogue)
    selected = {"dep": withdrawn}
    statuses = []

    def attempt(scope: PreferenceScope[Choice]) -> dict[str, Choice]:
        if scope.target:
            raise ResolutionError("the live metadata rejected its candidate")
        statuses.append(search.check(withdrawn, selected, scope))
        return selected

    assert search.run(attempt) == selected
    assert statuses == [PreparationStatus.IMPOSSIBLE]
    assert search.rounds > 0


def test_known_facts_preserve_restrictions_rejections_and_extra_identity() -> None:
    one = Choice("dep", 1)
    two = Choice("dep", 2)
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((one, two, withdrawn))
    catalogue.facts[two] = None
    context = _Context(PreferenceScope(live=frozenset({"dep"})), frozenset())
    facts = _KnownFacts(catalogue, {"dep": Range.singleton(1)}, context)

    allowed = facts.encode("dep", Range[int].full())
    assert facts.choose_version("dep", allowed) == facts.identify(one)
    assert facts.has_satisfying_version("dep", allowed)
    assert not facts.has_satisfying_version("dep", Range[int].empty())
    assert facts.identify(withdrawn) not in allowed
    assert facts.prioritize("dep", allowed, {}) == len(catalogue.items)

    extra = Choice("dep[feature]", 1)
    dependencies = facts.get_dependencies(extra.package, facts.identify(extra))
    assert dependencies == {"dep": Range.singleton(facts.identify(one))}
    assert one not in catalogue.facts


def test_known_rejected_metadata_refutes_the_assignment() -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((live, withdrawn))
    catalogue.facts[withdrawn] = None
    search = Search(catalogue)
    selected = {"dep": withdrawn}
    statuses = []

    def attempt(scope: PreferenceScope[Choice]) -> dict[str, Choice]:
        if scope.target:
            raise ResolutionError("the live metadata rejected its candidate")
        statuses.append(search.check(withdrawn, selected, scope))
        return selected

    assert search.run(attempt) == selected
    assert statuses == [PreparationStatus.IMPOSSIBLE]


@pytest.mark.parametrize("live_only", [False, True])
def test_known_fact_encoding_preserves_sparse_ids(live_only: bool) -> None:
    one, two, three = (Choice("dep", version) for version in (1, 2, 3))
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((one, two, three, withdrawn))
    catalogue.facts[two] = None
    context = _Context(
        PreferenceScope(live=frozenset({"dep"}) if live_only else frozenset()),
        frozenset(),
    )
    facts = _KnownFacts(catalogue, {}, context)
    candidates = (one, two, three, withdrawn)
    assert [facts.identify(candidate) for candidate in candidates] == [1, 2, 3, 4]

    encoded = facts.encode("dep", Range[int].full())

    assert {token for token in range(6) if token in encoded} == (
        {1, 3} if live_only else {1, 3, 4}
    )
    assert facts.encode("dep", Range[int].empty()).is_empty
    assert catalogue.facts == {two: None}


def test_context_limit_preserves_the_unproven_context() -> None:
    search = Search(Catalogue((Choice("dep", 1),)), max_steps=0)
    context = _Context(PreferenceScope(), frozenset())

    with pytest.raises(ResolutionLimitError, match="exceeded 0 iterations"):
        search.check_context(context)
    assert not search.refuted
    assert not search.possible


def test_proofless_context_error_remains_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = Search(Catalogue(()))
    context = _Context(PreferenceScope(), frozenset())

    def stop(*_args: object, **_kwargs: object) -> None:
        raise ResolutionError("context interrupted")

    monkeypatch.setattr(Resolver, "resolve", stop)
    with pytest.raises(IncompletePreferenceError, match="context check stopped"):
        search.check_context(context)
    assert not search.refuted
    assert not search.possible


@pytest.mark.parametrize("refresh", [False, True])
def test_metadata_growth_refreshes_only_the_requested_policy(refresh: bool) -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((live, withdrawn))
    search = Search(catalogue, refresh=refresh)
    query = PreferenceScope[Choice](live=frozenset({"dep"}), target="dep")
    search.record_failure(query, IncompletePreferenceError("metadata unavailable"))
    catalogue.facts[live] = Data({})
    visits = []

    def attempt(scope: PreferenceScope[Choice]) -> dict[str, Choice]:
        visits.append(scope)
        if scope.target:
            return {"dep": live}
        assert search.check(withdrawn, {}, scope) is PreparationStatus.INCOMPLETE
        return {}

    result = search.run(attempt)
    assert result == ({"dep": live} if refresh else {})
    assert len(visits) == (2 if refresh else 1)


def test_terminal_query_error_preserves_its_budget_and_classification() -> None:
    catalogue = Catalogue((Choice("dep", 1), Choice("dep", 1, withdrawn=True)))
    search = Search(catalogue)
    selected = {"dep": catalogue.items[1]}

    def attempt(scope: PreferenceScope[Choice]) -> dict[str, Choice]:
        if scope.target:
            raise ResolutionLimitError("owned query budget exhausted")
        search.check(selected["dep"], selected, scope)
        return selected

    with pytest.raises(ResolutionLimitError, match="owned query budget exhausted"):
        search.run(attempt)
    assert not search.outcomes


@pytest.mark.parametrize("base_version", [1, 2])
def test_an_extra_keeps_its_exact_base_identity_in_a_known_fact_proof(
    base_version: int,
) -> None:
    base = Choice("dep", base_version, withdrawn=True)
    extra = Choice("dep[feature]", 1, withdrawn=True)
    live = Choice("dep", 1)
    catalogue = Catalogue((base, extra, live))
    catalogue.facts[extra] = Data({})
    search = Search(catalogue)
    search.roots = {"dep[feature]": Range.singleton(1)}
    selected = {"dep[feature]": extra, "dep": base}
    statuses = []

    def attempt(scope: PreferenceScope[Choice]) -> dict[str, Choice]:
        if scope.target:
            raise ResolutionError("live extra metadata was rejected")
        statuses.append(search.check(extra, selected, scope))
        return selected

    assert search.run(attempt) == selected
    expected = (
        PreparationStatus.ALLOWED if base_version == 1 else PreparationStatus.IMPOSSIBLE
    )
    assert statuses == [expected]
    assert search.rounds > 0
