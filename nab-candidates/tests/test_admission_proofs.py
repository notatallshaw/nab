"""Admission and refusal with only the standalone candidate distribution installed."""

import pytest

from nab_candidates.preference import PreferenceScope, PreparationStatus, _Context
from nab_candidates.provider import CataloguePreference
from nab_resolver.errors import ResolutionInvariantError, ResolutionLimitError
from nab_resolver.ranges import Range

from .catalogue_fixtures import Catalogue, Choice, Data, Pin


def test_late_parent_pin_grounds_a_previously_waiting_release() -> None:
    helper = Choice("helper", 1, withdrawn=True)
    target = Choice("target", 1, withdrawn=True)
    grant = Choice("grant", 1)
    catalogue = Catalogue((helper, grant, target))
    roots = {"helper": Range[int].full(), "grant": Range[int].full()}
    policy = CataloguePreference(catalogue, roots, (), {})
    selected = {c.package: c for c in (helper, target, grant)}

    catalogue.facts[grant] = Data({"helper": Range.full()}, (Pin("helper", 1),))
    assert not policy.parents(target, selected)

    catalogue.facts[helper] = Data({"target": Range.full()}, (Pin("target", 1),))
    catalogue.facts[target] = Data({})
    assert policy.parents(target, selected) == {helper, grant}
    assert policy.explain(selected) == {
        "helper": ("grant==1 requires Pin(package='helper', version=1)",),
        "target": ("helper==1 requires Pin(package='target', version=1)",),
    }


def test_missing_facts_and_unrooted_files_cannot_explain_admission() -> None:
    app = Choice("app", 1)
    dep = Choice("dep", 1, withdrawn=True)
    catalogue = Catalogue((app, dep))
    search = CataloguePreference(catalogue, {"app": Range.full()}, (), {})

    with pytest.raises(ResolutionInvariantError, match="fixed parent app==1"):
        search.fixed_range(dep, frozenset({app}))
    with pytest.raises(ResolutionInvariantError, match="missing metadata for app"):
        search.explain({"app": app})

    catalogue.facts[app] = Data({"dep": Range.full()})
    with pytest.raises(ResolutionInvariantError, match="no selected candidate for dep"):
        search.explain({"app": app})
    catalogue.facts[dep] = Data({})
    with pytest.raises(ResolutionInvariantError, match="selection is not rooted"):
        search.explain({"app": app, "dep": dep})


def test_frozen_input_bounds_and_live_candidates_need_no_proof() -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 2, withdrawn=True)
    catalogue = Catalogue((live, withdrawn))
    search = CataloguePreference(catalogue, {"dep": Range.singleton(1)}, (), {})

    assert search.check(live, {}, PreferenceScope()) is PreparationStatus.ALLOWED
    assert (
        search.check(withdrawn, {}, PreferenceScope()) is PreparationStatus.IMPOSSIBLE
    )
    assert not catalogue.facts


def test_cyclic_proof_request_and_exhausted_budget_are_distinct() -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    search = CataloguePreference(
        Catalogue((live, withdrawn)), {"dep": Range.full()}, (), {}, max_steps=1
    )
    with pytest.raises(ResolutionInvariantError, match="cyclic query"):
        search.check(withdrawn, {}, PreferenceScope(live=frozenset({"dep"})))
    with pytest.raises(ResolutionLimitError):
        search.spend(2)


def test_nested_proof_requires_the_requested_package() -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    search = CataloguePreference(
        Catalogue((live, withdrawn)), {"dep": Range.full()}, (), {}
    )

    def resolve(scope: PreferenceScope[Choice]) -> dict[str, Choice]:
        if scope.target:
            return {}

        search.check(withdrawn, {"dep": withdrawn}, scope)
        return {"dep": withdrawn}

    with pytest.raises(ResolutionInvariantError, match="target disappeared"):
        search.run(resolve)


def test_missing_listing_cannot_refute_an_assignment() -> None:
    catalogue = Catalogue(())
    catalogue.missing_listings.add("dep")
    search = CataloguePreference(catalogue, {"dep": Range.full()}, (), {})
    context = _Context(PreferenceScope(), frozenset())

    search.check_context(context)

    assert context not in search.refuted
    assert search.possible[context] == 0
