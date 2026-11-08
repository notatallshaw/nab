"""Preference proofs accept a non-Python version catalogue through the public API."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import pytest

from nab_provider._compat import override
from nab_provider.preference import (
    CataloguePreference,
    IncompletePreferenceError,
    PreferenceScope,
    PreferenceSearch,
    PreparationStatus,
    _Context,
    _KnownFacts,
)
from nab_provider.preference_provider import (
    MetadataProxy,
    Package,
    PermissionProxy,
    PreferenceProvider,
)
from nab_provider.prepared_resolution import PreparedResolution
from nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver, ResolverProvider, ResolverStats
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

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
        self.unavailable: set[Choice] = set()
        self.unavailable_reason = "metadata is unavailable"
        self.metadata: dict[Choice, Data | None] = {}

    def prepare(self, candidate: Choice) -> Data | None:
        """Cache the declared metadata when the shared provider grants admission."""
        if candidate in self.unavailable:
            return None
        if candidate not in self.facts:
            self.facts[candidate] = self.metadata.get(candidate, Data({}))
        return self.facts[candidate]

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


def test_proxy_projects_original_dependency_choices_and_skips_permission_facts() -> (
    None
):
    parent = Choice("parent", 1)
    child = Choice("child", 2)
    catalogue = Catalogue((parent, child))
    catalogue.facts[parent] = Data({"child": Range.singleton(2)})
    proxy = PreferenceProvider.from_catalogue(catalogue, {}, (), {})
    parent_token = proxy.identify(parent)
    child_token = proxy.identify(child)
    dependency = Incompatibility[Package, int](
        [
            Term(MetadataProxy(parent_token), Range.singleton(0), positive=True),
            Term("child", Range.singleton(child_token), positive=False),
        ],
        cause=IncompatibilityCause.DEPENDENCY,
    )
    projected = proxy.project_requirements(
        dependency, version_of=lambda candidate: candidate.version, range_type=Range
    )

    assert len(projected) == 1
    assert [term.package for term in projected[0].terms] == ["parent", "child"]
    assert projected[0].terms[0].constraint == Range.singleton(1)
    assert projected[0].terms[1].constraint == Range.singleton(2)
    assert not projected[0].terms[1].is_positive()

    permission = Incompatibility[Package, int](
        [Term(PermissionProxy(parent_token), Range.full(), positive=True)],
        cause=IncompatibilityCause.DEPENDENCY,
    )
    absent = Incompatibility[Package, int](
        [Term("child", Range.full(), positive=True)],
        cause=IncompatibilityCause.NO_VERSIONS,
    )
    assert (
        proxy.project_requirements(
            permission, version_of=lambda candidate: candidate.version, range_type=Range
        )
        == []
    )
    assert (
        proxy.project_requirements(
            absent, version_of=lambda candidate: candidate.version, range_type=Range
        )
        == []
    )

    assert (
        proxy.project_requirements(
            Incompatibility([], cause=IncompatibilityCause.ROOT),
            version_of=lambda candidate: candidate.version,
            range_type=Range,
        )
        == []
    )

    assert list(proxy.empty_requirements(absent)) == []
    assert list(proxy.empty_requirements(dependency)) == []
    dependency.terms[1] = Term("child", Range.empty(), positive=False)
    assert list(proxy.empty_requirements(dependency)) == [("child", Range.singleton(2))]


def test_proxy_failure_preserves_empty_original_root_constraints() -> None:
    proxy = PreferenceProvider.from_catalogue(
        Catalogue(()), {"child": Range.singleton(2)}, (), {"child": Range.singleton(1)}
    )
    with pytest.raises(ResolutionError) as caught:
        proxy.select()

    proof = caught.value.incompatibility
    assert proof is not None
    requests = [
        request for node in proof.walk() for request in proxy.empty_requirements(node)
    ]
    assert requests == [("child", Range.empty())]

    projected = proxy.project_requirements(
        proof, version_of=lambda candidate: candidate.version, range_type=Range
    )
    assert len(projected) == 1
    assert projected[0].terms[1].package == "child"
    assert projected[0].terms[1].constraint.is_empty


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


def test_shared_provider_resolves_a_grounded_transitive_pin() -> None:
    parent = Choice("app", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    live = Choice("dep", 2)
    catalogue = Catalogue((parent, live, withdrawn))
    catalogue.metadata[parent] = Data({"dep": Range.singleton(1)}, (Pin("dep", 1),))
    search = CataloguePreference(catalogue, {"app": Range.full()}, (), {})
    provider = PreferenceProvider(search)

    selected = search.run(provider.run_query)

    assert selected == {"app": parent, "dep": withdrawn}
    assert search.explain(selected) == {
        "dep": ("app==1 requires Pin(package='dep', version=1)",)
    }
    assert search.stats.rounds > 0
    assert search.remaining < 100_000


def test_shared_provider_does_not_prepare_a_self_authorizing_yank() -> None:
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((withdrawn,))
    catalogue.metadata[withdrawn] = Data({}, (Pin("dep", 1),))
    search = CataloguePreference(catalogue, {"dep": Range.full()}, (), {})

    with pytest.raises(ResolutionError):
        search.run(PreferenceProvider(search).run_query)

    assert withdrawn not in catalogue.facts


def test_shared_provider_keeps_host_plan_policy_in_scoped_queries() -> None:
    high = Choice("app", 2)
    low = Choice("app", 1)
    catalogue = Catalogue((high, low))
    search = CataloguePreference(catalogue, {"app": Range.full()}, (), {})
    checked = []

    def admitted(candidate: Choice, selected: Mapping[str, Choice]) -> bool:
        checked.append((candidate, dict(selected)))
        return candidate == low

    provider = PreferenceProvider(search, metadata_is_admitted=admitted)
    selected = search.run(provider.run_query)

    assert selected == {"app": low}
    assert (high, {"app": high}) in checked
    assert (low, {"app": low}) in checked


def test_shared_provider_requires_a_complete_failed_query() -> None:
    catalogue = Catalogue(())
    search = CataloguePreference(catalogue, {"dep": Range.full()}, (), {})
    provider = PreferenceProvider(search, query_is_complete=lambda _scope: False)

    with pytest.raises(IncompletePreferenceError):
        provider.run_query(PreferenceScope())


def test_shared_provider_keeps_missing_metadata_incomplete() -> None:
    candidate = Choice("dep", 1)
    catalogue = Catalogue((candidate,))
    catalogue.unavailable.add(candidate)
    search = CataloguePreference(catalogue, {"dep": Range.full()}, (), {})

    with pytest.raises(IncompletePreferenceError, match="metadata is unavailable"):
        PreferenceProvider(search).run_query(PreferenceScope())


def test_shared_provider_uses_the_host_resolver_factory() -> None:
    candidate = Choice("app", 1)
    catalogue = Catalogue((candidate,))
    budgets = []

    def make_solver(
        provider: ResolverProvider[Package, int], limit: int
    ) -> Resolver[Package, int]:
        budgets.append(limit)
        return Resolver(provider, max_iterations=limit)

    provider = PreferenceProvider.from_catalogue(
        catalogue, {"app": Range.full()}, (), {}, resolver_factory=make_solver
    )

    assert provider.select() == {"app": candidate}
    assert budgets == [100_000]


def test_incomplete_refresh_keeps_yanked_metadata_unprepared() -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((live, withdrawn))
    catalogue.unavailable.add(live)
    provider = PreferenceProvider.from_catalogue(
        catalogue,
        {"dep": Range.singleton(1)},
        [Pin("dep", 1)],
        {},
        refresh_incomplete=True,
    )

    with pytest.raises(IncompletePreferenceError):
        provider.select()

    assert withdrawn not in catalogue.facts
    assert provider.preference.outcomes
    assert set(provider.preference.outcomes.values()) == {PreparationStatus.INCOMPLETE}


class FileChoice(Choice):
    """Distinguish two opaque file identities at the same package version."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        """Compare opaque file identity without equating version labels."""
        return self is other

    __hash__ = object.__hash__


class ExactCatalogue(Catalogue):
    """Refuse child enumeration while retaining its genuinely known identity."""

    def __init__(
        self,
        choices: tuple[Choice, ...],
        *,
        forbidden: frozenset[str] = frozenset({"child"}),
    ) -> None:
        super().__init__(choices)
        self.listed: list[str] = []
        self.bases: dict[Choice, Choice] = {}
        self.forbidden = forbidden

    def choices(self, package: str) -> tuple[Choice, ...]:
        self.listed.append(package)
        if package in self.forbidden:
            pytest.fail("exact child triggered an unrelated candidate listing")
        return super().choices(package)

    def base_candidate(self, candidate: Choice) -> Choice:
        return self.bases.get(candidate, Catalogue.base_candidate(candidate))


@pytest.mark.parametrize("preferred_child", ["none", "same", "different"])
def test_exact_child_is_selected_without_listing_in_trials_and_fallback(
    preferred_child: str,
) -> None:
    parent = Choice("parent", 1)
    child = FileChoice("child", 1)
    catalogue = ExactCatalogue((parent,))
    catalogue.metadata[parent] = Data({"child": Range[int].full()})
    preferred = {"parent": parent}
    observed: list[Choice] = []
    if preferred_child != "none":
        preferred["child"] = (
            child if preferred_child == "same" else FileChoice("child", 1)
        )

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (),
        {},
        preferred_candidate=preferred.get,
        on_candidate_considered=observed.append,
        exact_dependencies=lambda candidate: (child,) if candidate is parent else (),
    ).solve()

    assert result.candidates == {"parent": parent, "child": child}
    assert result.candidates["child"] is child
    assert result.edges == (("parent", "child"),)
    assert catalogue.facts[child] == Data({})
    assert any(candidate is child for candidate in observed)
    assert "child" not in catalogue.listed


@pytest.mark.parametrize("unavailable", [False, True])
def test_exact_child_still_requires_available_valid_metadata(unavailable: bool) -> None:
    parent = Choice("parent", 1)
    child = FileChoice("child", 1)
    catalogue = ExactCatalogue((parent,))
    catalogue.metadata[parent] = Data({"child": Range[int].full()})
    if unavailable:
        catalogue.unavailable.add(child)
    else:
        catalogue.metadata[child] = None
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (),
        {},
        exact_dependencies=lambda candidate: (child,) if candidate is parent else (),
    )

    error = IncompletePreferenceError if unavailable else ResolutionError
    with pytest.raises(error):
        session.solve()

    assert "child" not in catalogue.listed


@pytest.mark.parametrize("preferred", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_exact_callback_error_preserves_classification_and_round_budget(
    preferred: bool, terminal: bool
) -> None:
    parent = Choice("parent", 1)
    catalogue = ExactCatalogue((parent,))
    error = ResolutionInvariantError("invalid exact edge") if terminal else ValueError()

    def exact_dependencies(_candidate: Choice) -> tuple[Choice, ...]:
        raise error

    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (),
        {},
        preferred_candidate={"parent": parent}.get if preferred else None,
        exact_dependencies=exact_dependencies,
    )
    initial = session.remaining_rounds
    with pytest.raises(type(error)) as caught:
        session.solve()

    assert caught.value is error
    assert session.stats.rounds > 0
    assert session.remaining_rounds == initial - session.stats.rounds


@pytest.mark.parametrize("constraint", [False, True])
def test_exact_child_keeps_original_edge_and_input_bounds(constraint: bool) -> None:
    parent = Choice("parent", 1)
    child = FileChoice("child", 1)
    catalogue = ExactCatalogue((parent,))
    catalogue.metadata[parent] = Data(
        {
            "child": Range[int].full() if constraint else Range[int].singleton(2),
        }
    )
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (),
        {"child": Range[int].singleton(2)} if constraint else {},
        exact_dependencies=lambda candidate: (child,) if candidate is parent else (),
    )

    with pytest.raises(ResolutionError):
        session.solve()

    assert child not in catalogue.facts
    assert "child" not in catalogue.listed


@pytest.mark.parametrize("duplicate", [False, True])
def test_exact_dependencies_are_conjunctive_at_equal_versions(duplicate: bool) -> None:
    parent = Choice("parent", 1)
    first = FileChoice("child", 1)
    second = first if duplicate else FileChoice("child", 1)
    catalogue = ExactCatalogue((parent,))
    catalogue.metadata[parent] = Data({"child": Range[int].full()})
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (),
        {},
        exact_dependencies=lambda candidate: (
            (first, second) if candidate is parent else ()
        ),
    )

    if duplicate:
        assert session.solve().candidates == {"parent": parent, "child": first}
    else:
        with pytest.raises(ResolutionError):
            session.solve()
    assert "child" not in catalogue.listed


def test_different_parents_cannot_choose_distinct_files_at_one_version() -> None:
    first = Choice("first", 1)
    second = Choice("second", 1)
    child = FileChoice("child", 1)
    other = FileChoice("child", 1)
    catalogue = ExactCatalogue((first, second))
    catalogue.metadata[first] = Data({"child": Range[int].full()})
    catalogue.metadata[second] = Data({"child": Range[int].full()})
    exact = {first: (child,), second: (other,)}
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"first": Range[int].full(), "second": Range[int].full()},
        (),
        {},
        exact_dependencies=lambda candidate: exact.get(candidate, ()),
    )

    with pytest.raises(ResolutionError):
        session.solve()

    assert "child" not in catalogue.listed


def test_exact_dependency_requires_an_original_prepared_edge() -> None:
    parent = Choice("parent", 1)
    child = FileChoice("child", 1)
    catalogue = ExactCatalogue((parent,))
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (),
        {},
        exact_dependencies=lambda candidate: (child,) if candidate is parent else (),
    )

    with pytest.raises(ResolutionInvariantError, match="no prepared dependency edge"):
        session.solve()

    assert child not in catalogue.facts
    assert "child" not in catalogue.listed


@pytest.mark.parametrize("live", [False, True])
def test_scoped_queries_preserve_exact_identity_and_live_restrictions(
    live: bool,
) -> None:
    parent = Choice("parent", 1)
    child = FileChoice("child", 1, withdrawn=live)
    other = FileChoice("child", 1)
    catalogue = ExactCatalogue((parent,))
    catalogue.metadata[parent] = Data({"child": Range[int].full()})
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (Pin("child", 1),),
        {},
        exact_dependencies=lambda candidate: (child,) if candidate is parent else (),
    )
    scope = (
        PreferenceScope[Choice](live=frozenset({"child"}))
        if live
        else PreferenceScope(fixed=frozenset({other}))
    )

    with pytest.raises(ResolutionError):
        session.provider.run_query(scope)

    assert child not in catalogue.facts
    assert "child" not in catalogue.listed


@pytest.mark.parametrize("compatible", [False, True])
@pytest.mark.parametrize("forbid_listing", [False, True])
def test_known_fact_proofs_retain_exact_identity_without_preparation(
    compatible: bool,
    forbid_listing: bool,
) -> None:
    parent = Choice("parent", 1)
    child = FileChoice("child", 1)
    other = child if compatible else FileChoice("child", 1)
    catalogue = (
        ExactCatalogue((parent,))
        if forbid_listing
        else Catalogue((parent, child, other))
    )
    catalogue.facts[parent] = Data({"child": Range[int].full()})
    catalogue.facts[child] = Data({})
    catalogue.facts[other] = Data({})
    search = CataloguePreference(
        catalogue,
        {"parent": Range[int].full()},
        (),
        {},
        exact_dependencies=lambda candidate: (child,) if candidate is parent else (),
    )
    context = _Context(PreferenceScope[Choice](), frozenset({parent, other}))
    before = dict(catalogue.facts)
    search.check_context(context)

    assert (context in search.refuted) is not compatible
    assert (context in search.possible) is compatible
    assert catalogue.facts == before
    if isinstance(catalogue, ExactCatalogue):
        assert "child" not in catalogue.listed


@pytest.mark.parametrize("pin_source", ["none", "input", "parent"])
def test_exact_withdrawn_child_still_needs_original_pin_permission(
    pin_source: str,
) -> None:
    parent = Choice("parent", 1)
    child = FileChoice("child", 1, withdrawn=True)
    catalogue = ExactCatalogue((parent,))
    catalogue.metadata[parent] = Data(
        {"child": Range[int].full()},
        (Pin("child", 1),) if pin_source == "parent" else (),
    )
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (Pin("child", 1),) if pin_source == "input" else (),
        {},
        exact_dependencies=lambda candidate: (child,) if candidate is parent else (),
    )

    if pin_source == "none":
        with pytest.raises(ResolutionError):
            session.solve()
        assert child not in catalogue.facts
    else:
        result = session.solve()
        assert result.candidates == {"parent": parent, "child": child}
        assert result.admission_sources["child"]
        assert session.provider.preference.outcomes
        assert session.provider.preference.possible
    assert "child" not in catalogue.listed


@pytest.mark.parametrize("compatible", [False, True])
def test_frozen_extra_base_coupling_preserves_exact_identity(compatible: bool) -> None:
    extra = FileChoice("child[feature]", 1)
    child = FileChoice("child", 1)
    other = child if compatible else FileChoice("child", 1)
    catalogue = ExactCatalogue((extra,))
    catalogue.bases[extra] = child
    catalogue.facts[extra] = Data({"child": Range[int].full()})
    catalogue.facts[child] = Data({})
    search = CataloguePreference(
        catalogue,
        {extra.package: Range[int].full()},
        (),
        {},
        exact_dependencies=lambda candidate: (other,) if candidate is extra else (),
    )
    context = _Context(PreferenceScope[Choice](), frozenset({extra, child}))
    search.check_context(context)

    assert (context in search.refuted) is not compatible
    assert (context in search.possible) is compatible
    assert "child" not in catalogue.listed


@pytest.mark.parametrize("with_extra", [False, True])
def test_unrelated_exact_edge_does_not_suppress_a_working_live_alternative(
    with_extra: bool,
) -> None:
    parent = Choice("parent", 1)
    withdrawn = FileChoice("child", 1, withdrawn=True)
    live = FileChoice("child", 1)
    base = FileChoice("auxiliary", 1)
    auxiliary = FileChoice("auxiliary[feature]", 1) if with_extra else base
    catalogue = ExactCatalogue(
        (parent, withdrawn, live),
        forbidden=frozenset({base.package, auxiliary.package}),
    )
    catalogue.metadata[parent] = Data(
        {"child": Range[int].full(), auxiliary.package: Range[int].full()}
    )
    exact = {parent: (auxiliary,)}
    if with_extra:
        catalogue.bases[auxiliary] = base
        catalogue.metadata[auxiliary] = Data({base.package: Range[int].full()})
        exact[auxiliary] = (base,)
    result = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (Pin("child", 1),),
        {},
        exact_dependencies=lambda candidate: exact.get(candidate, ()),
    ).solve()

    expected = {"parent": parent, "child": live, auxiliary.package: auxiliary}
    if with_extra:
        expected[base.package] = base
    assert result.candidates == expected
    assert withdrawn not in catalogue.facts
    assert "child" in catalogue.listed
    assert "auxiliary" not in catalogue.listed


def test_frozen_proof_keeps_normal_order_for_an_unrefined_dependency() -> None:
    first = FileChoice("parent", 2)
    second = FileChoice("parent", 1)
    catalogue = Catalogue((first, second))
    catalogue.facts[first] = Data({})
    catalogue.facts[second] = Data({})
    context = _Context(PreferenceScope[Choice](), frozenset())
    provider = _KnownFacts(
        catalogue, {}, context, exact_dependencies=lambda _candidate: ()
    )
    allowed = provider.encode("parent", Range[int].full())
    before = dict(catalogue.facts)

    result = Resolver(provider).resolve({"parent": allowed})

    assert provider.items[result["parent"]] is first
    assert catalogue.facts == before


@pytest.mark.parametrize("pinned", [False, True])
def test_exact_withdrawn_extra_keeps_base_identity_and_pin_permission(
    pinned: bool,
) -> None:
    parent = Choice("parent", 1)
    extra = FileChoice("child[feature]", 1, withdrawn=True)
    child = FileChoice("child", 1, withdrawn=True)
    catalogue = ExactCatalogue(
        (parent,), forbidden=frozenset({"child", "child[feature]"})
    )
    catalogue.bases[extra] = child
    catalogue.metadata[parent] = Data({extra.package: Range[int].full()})
    catalogue.metadata[extra] = Data({"child": Range[int].singleton(1)})
    exact = {parent: (extra,), extra: (child,)}
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (Pin("child", 1),) if pinned else (),
        {},
        exact_dependencies=lambda candidate: exact.get(candidate, ()),
    )

    if pinned:
        result = session.solve()
        assert result.candidates == {
            "parent": parent,
            extra.package: extra,
            "child": child,
        }
        assert result.admission_sources["child"]
    else:
        with pytest.raises(ResolutionError):
            session.solve()
        assert extra not in catalogue.facts
        assert child not in catalogue.facts
    assert not {"child", extra.package}.intersection(catalogue.listed)


def test_exact_extra_cannot_require_a_different_base_file() -> None:
    parent = Choice("parent", 1)
    extra = FileChoice("child[feature]", 1, withdrawn=True)
    child = FileChoice("child", 1, withdrawn=True)
    other = FileChoice("child", 1, withdrawn=True)
    catalogue = ExactCatalogue(
        (parent,), forbidden=frozenset({"child", "child[feature]"})
    )
    catalogue.bases[extra] = child
    catalogue.metadata[parent] = Data({extra.package: Range[int].full()})
    catalogue.metadata[extra] = Data({"child": Range[int].full()})
    exact = {parent: (extra,), extra: (other,)}
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full()},
        (Pin("child", 1),),
        {},
        exact_dependencies=lambda candidate: exact.get(candidate, ()),
    )

    with pytest.raises(ResolutionError):
        session.solve()

    assert child not in catalogue.facts
    assert other not in catalogue.facts
    assert not {"child", extra.package}.intersection(catalogue.listed)


def test_new_metadata_revises_permission_for_unchanged_choices() -> None:
    app = Choice("app", 1)
    dep = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((app, dep))
    catalogue.metadata[app] = Data({"dep": Range.singleton(1)}, (Pin("dep", 1),))
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"app": Range.full()}, (), {}
    )
    app_token, dep_token = provider.identify(app), provider.identify(dep)
    decisions = {"app": app_token, "dep": dep_token}

    provider.receive_decision_scan_hint({}, decisions)
    assert not provider.is_ready(MetadataProxy(dep_token))

    catalogue.prepare(app)
    provider.receive_decision_scan_hint({}, decisions)
    assert provider.is_ready(MetadataProxy(dep_token))


def test_changed_parent_revokes_permission_with_unchanged_metadata() -> None:
    old, new = Choice("app", 1), Choice("app", 2)
    dep = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((new, old, dep))
    catalogue.metadata[new] = Data({"dep": Range.singleton(1)}, (Pin("dep", 1),))
    catalogue.metadata[old] = Data({"dep": Range.singleton(1)})
    catalogue.prepare(new)
    catalogue.prepare(old)
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"app": Range.full()}, (), {}
    )
    old_token, new_token, dep_token = map(provider.identify, (old, new, dep))
    decisions = {"app": new_token, "dep": dep_token}

    provider.receive_decision_scan_hint({}, decisions)
    assert provider.is_ready(MetadataProxy(dep_token))

    decisions["app"] = old_token
    provider.receive_decision_scan_hint({}, decisions)
    assert not provider.is_ready(MetadataProxy(dep_token))


def excludes_assignment(
    clauses: list[Incompatibility[Package, int]], assignments: Mapping[Package, int]
) -> bool:
    return any(
        all(
            term.package in assignments
            and term.satisfies(Range.singleton(assignments[term.package]))
            for term in clause.terms
        )
        for clause in clauses
    )


def test_final_metadata_rejection_does_not_depend_on_unrelated_choices() -> None:
    bad, old, new = Choice("bad", 1), Choice("other", 1), Choice("other", 2)
    catalogue = Catalogue((bad, old, new))
    catalogue.metadata[bad] = None
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"bad": Range.full(), "other": Range.full()}, (), {}
    )
    bad_token, old_token, new_token = map(provider.identify, (bad, old, new))
    provider.receive_decision_scan_hint({}, {"bad": bad_token, "other": old_token})
    metadata = MetadataProxy(bad_token)
    assert provider.choose_version(metadata, Range.full()) is None
    clauses = provider.consume_pending_clauses()

    for other in (old_token, new_token):
        assert excludes_assignment(
            clauses, {"bad": bad_token, "other": other, metadata: 0}
        )


def test_unavailable_metadata_has_no_permanent_rejection_proof() -> None:
    bad = Choice("bad", 1)
    catalogue = Catalogue((bad,))
    catalogue.unavailable.add(bad)
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"bad": Range.full()}, (), {}
    )
    token = provider.identify(bad)
    provider.receive_decision_scan_hint({}, {"bad": token})
    metadata = MetadataProxy(token)

    assert provider.choose_version(metadata, Range.full()) is None
    assert provider.incomplete
    assert not excludes_assignment(
        provider.consume_pending_clauses(), {"bad": token, metadata: 0}
    )


def test_contextual_metadata_refusal_keeps_its_selected_parent_guard() -> None:
    bad, old, new = Choice("bad", 1), Choice("other", 1), Choice("other", 2)
    catalogue = Catalogue((bad, old, new))
    provider = PreferenceProvider.from_catalogue(
        catalogue,
        {"bad": Range.full(), "other": Range.full()},
        (),
        {},
        metadata_is_admitted=lambda candidate, selected: selected["other"].version == 2,
    )
    bad_token, old_token, new_token = map(provider.identify, (bad, old, new))
    provider.receive_decision_scan_hint({}, {"bad": bad_token, "other": old_token})
    metadata = MetadataProxy(bad_token)
    assert provider.choose_version(metadata, Range.full()) is None
    clauses = provider.consume_pending_clauses()

    assert excludes_assignment(
        clauses, {"bad": bad_token, "other": old_token, metadata: 0}
    )
    assert not excludes_assignment(
        clauses, {"bad": bad_token, "other": new_token, metadata: 0}
    )


def test_live_grounding_keeps_cycle_support_and_excludes_orphans() -> None:
    app, middle, leaf, orphan = (
        Choice(name, 1) for name in ("app", "middle", "leaf", "orphan")
    )
    catalogue = Catalogue((app, middle, leaf, orphan))
    catalogue.metadata[app] = Data({"middle": Range.singleton(1)})
    catalogue.metadata[middle] = Data(
        {"leaf": Range.singleton(1), "app": Range.singleton(1)}
    )
    catalogue.metadata[orphan] = Data({"leaf": Range.singleton(1)})
    for candidate in (app, middle, leaf, orphan):
        catalogue.prepare(candidate)
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"app": Range.full()}, (), {}
    )
    app_token, middle_token, leaf_token, orphan_token = map(
        provider.identify, (app, middle, leaf, orphan)
    )
    provider.receive_decision_scan_hint(
        {},
        {
            "app": app_token,
            "middle": middle_token,
            "leaf": leaf_token,
            "orphan": orphan_token,
        },
    )

    assert provider.supports == {
        app_token: frozenset(),
        middle_token: frozenset({app_token}),
        leaf_token: frozenset({app_token, middle_token}),
    }
    assert not provider.is_ready(MetadataProxy(orphan_token))


def test_cached_rejections_skip_choices_with_one_permanent_clause() -> None:
    bad, older, good = (Choice("dep", version) for version in (3, 2, 1))
    catalogue = Catalogue((bad, older, good))
    catalogue.facts.update({bad: None, older: None})
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"dep": Range.full()}, (), {}
    )
    domain = provider.encode_range("dep", Range.full())

    assert provider.choose_version("dep", domain) == provider.identify(good)
    clauses = provider.consume_pending_clauses()
    assert len(clauses) == 1
    for candidate in (bad, older):
        assert excludes_assignment(clauses, {"dep": provider.identify(candidate)})
    assert not excludes_assignment(clauses, {"dep": provider.identify(good)})


def test_cached_exact_rejection_retains_its_permanent_cause() -> None:
    bad = Choice("dep", 1)
    catalogue = Catalogue((bad,))
    catalogue.facts[bad] = None
    provider = PreferenceProvider.from_catalogue(
        catalogue,
        {"dep": Range.full()},
        (),
        {},
        exact_dependencies=lambda candidate: (),
    )
    token = provider.identify(bad)

    assert provider.choose_version("dep", Range.singleton(token)) is None
    assert excludes_assignment(provider.consume_pending_clauses(), {"dep": token})


def test_unknown_metadata_keeps_its_candidate_available() -> None:
    unknown, old = Choice("dep", 2), Choice("dep", 1)
    catalogue = Catalogue((unknown, old))
    catalogue.unavailable.add(unknown)
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"dep": Range.full()}, (), {}
    )
    domain = provider.encode_range("dep", Range.full())

    assert provider.choose_version("dep", domain) == provider.identify(unknown)
    assert not provider.consume_pending_clauses()


@pytest.mark.parametrize("versions", [(), (1,), (1, 2), (1, 2, 3)])
def test_choice_priority_preserves_empty_forced_and_wide_domains(
    versions: tuple[int, ...],
) -> None:
    choices = tuple(Choice("dep", version) for version in range(1, 5))
    catalogue = Catalogue(choices)
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"dep": Range.full()}, (), {}
    )
    tokens = {choice.version: provider.identify(choice) for choice in choices}
    allowed = Range[int].from_versions(tokens[version] for version in versions)

    assert provider.prioritize("dep", allowed, {})[2] == min(len(versions), 2)


def test_choice_priority_stops_after_two_matching_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = tuple(Choice("dep", version) for version in range(1, 49))
    catalogue = Catalogue(choices)
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"dep": Range.full()}, (), {}
    )
    for choice in choices:
        provider.identify(choice)
    allowed = Range[int].full()
    contains = Range.__contains__
    calls = 0

    def counted(domain: Range[int], token: object) -> bool:
        nonlocal calls
        if domain is allowed:
            calls += 1
        return contains(domain, token)

    monkeypatch.setattr(Range, "__contains__", counted)
    assert provider.prioritize("dep", allowed, {})[2] == 2
    assert calls == 2


def test_metadata_dependency_cache_keeps_maps_independent_and_scopes_fresh() -> None:
    app, live, withdrawn = (
        Choice("app", 1),
        Choice("dep", 1),
        Choice("dep", 2, withdrawn=True),
    )

    class ObservedCatalogue(Catalogue):
        def __init__(self) -> None:
            super().__init__((app, live, withdrawn))
            self.order_reads = 0

        @override
        def ordered(self, package: str, allowed: Range[int]) -> list[Choice]:
            self.order_reads += 1
            return super().ordered(package, allowed)

    catalogue = ObservedCatalogue()
    catalogue.metadata[app] = Data({"dep": Range.full()})
    catalogue.prepare(app)
    provider = PreferenceProvider.from_catalogue(
        catalogue, {"app": Range.full()}, (), {}
    )
    metadata = MetadataProxy(provider.identify(app))
    first = provider.get_dependencies(metadata, 0)
    expected = dict(first)
    first.clear()

    assert provider.get_dependencies(metadata, 0) == expected
    assert catalogue.order_reads == 1

    scoped = provider.new_query(PreferenceScope(live=frozenset({"dep"})))
    restricted = scoped.get_dependencies(metadata, 0)
    assert provider.identify(live) in restricted["dep"]
    assert provider.identify(withdrawn) not in restricted["dep"]
    assert catalogue.order_reads == 2


def test_empty_metadata_dependencies_are_cached_without_exposing_the_cache() -> None:
    app = Choice("app", 1)
    catalogue = Catalogue((app,))
    catalogue.prepare(app)
    calls = 0

    def exact(candidate: Choice) -> tuple[Choice, ...]:
        nonlocal calls
        assert candidate == app
        calls += 1
        return ()

    provider = PreferenceProvider.from_catalogue(
        catalogue, {"app": Range.full()}, (), {}, exact_dependencies=exact
    )
    metadata = MetadataProxy(provider.identify(app))
    first = provider.get_dependencies(metadata, 0)
    first["injected"] = Range.full()

    assert provider.get_dependencies(metadata, 0) == {}
    assert calls == 1
