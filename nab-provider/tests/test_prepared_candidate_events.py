"""Prepared searches report opaque candidate choices without preparing metadata."""

from __future__ import annotations

import pytest

from nab_provider.preference import PreferenceScope
from nab_provider.preference_provider import MetadataProxy, PermissionProxy
from nab_provider.prepared_resolution import PreparedResolution
from nab_resolver.errors import ResolutionInvariantError, ResolutionLimitError
from nab_resolver.ranges import Range
from tests.test_preference_core import Catalogue, Choice, Data, Pin


@pytest.mark.parametrize("preferred", [False, True])
def test_considered_candidates_retain_identity_and_lazy_metadata(
    preferred: bool,
) -> None:
    app = Choice("app", 1)
    dep = Choice("dep", 2)
    unused = Choice("unused", 3)
    catalogue = Catalogue((app, dep, unused))
    catalogue.metadata[app] = Data({"dep": Range[int].full()})
    observed: list[Choice] = []
    prepared_before_choice: list[frozenset[Choice]] = []

    def considered(candidate: Choice) -> None:
        observed.append(candidate)
        prepared_before_choice.append(frozenset(catalogue.facts))

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": app, "dep": dep}.get if preferred else None,
        on_candidate_considered=considered,
    ).solve()

    assert result.candidates == {"app": app, "dep": dep}
    assert set(observed) == {app, dep}
    assert all(candidate is app or candidate is dep for candidate in observed)

    assert prepared_before_choice[observed.index(app)] == frozenset()
    assert dep not in prepared_before_choice[observed.index(dep)]
    assert unused not in catalogue.facts


def test_refused_preference_is_reported_before_ordinary_fallback() -> None:
    first = Choice("app", 1)
    second = Choice("app", 2)
    catalogue = Catalogue((first, second))
    catalogue.metadata[first] = None
    observed: list[Choice] = []

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": first}.get,
        on_candidate_considered=observed.append,
    ).solve()

    assert result.candidates == {"app": second}
    assert list(dict.fromkeys(observed)) == [first, second]
    assert observed[0] is first
    assert observed[-1] is second


@pytest.mark.parametrize("preferred", [False, True])
def test_optional_callback_preserves_selection_metadata_and_work(
    preferred: bool,
) -> None:
    app = Choice("app", 1)
    control_catalogue = Catalogue((app,))
    observed_catalogue = Catalogue((app,))
    observed: list[Choice] = []
    preferred_candidate = {"app": app}.get if preferred else None
    control = PreparedResolution.from_catalogue(
        control_catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate=preferred_candidate,
    )
    session = PreparedResolution.from_catalogue(
        observed_catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate=preferred_candidate,
        on_candidate_considered=observed.append,
    )

    assert session.solve().candidates == control.solve().candidates
    assert observed_catalogue.facts == control_catalogue.facts
    assert observed
    assert session.stats == control.stats
    assert session.remaining_rounds == control.remaining_rounds


def test_scoped_queries_report_repeated_real_choices_and_skip_proxy_events() -> None:
    app = Choice("app", 1, withdrawn=True)
    catalogue = Catalogue((app,))
    observed: list[Choice] = []
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (Pin("app", 1),),
        {},
        refresh_incomplete=True,
        on_candidate_considered=observed.append,
    )
    provider = session.provider

    assert provider.run_query(PreferenceScope()) == {"app": app}
    first_count = len(observed)
    assert first_count > 0

    scope = PreferenceScope(fixed=frozenset({app}))
    assert provider.run_query(scope) == {"app": app}
    assert len(observed) > first_count
    assert all(candidate is app for candidate in observed)

    query = provider.new_query(scope)
    token = query.identify(app)
    query.receive_decision_scan_hint({}, {"app": token})
    before = observed.copy()
    assert query.choose_version("app", Range[int].empty()) is None
    assert query.choose_version(PermissionProxy(token), Range[int].full()) is not None
    assert query.choose_version(MetadataProxy(token), Range[int].full()) == 0
    assert observed == before


@pytest.mark.parametrize("preferred", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_candidate_callback_errors_preserve_identity_and_charge_rounds(
    preferred: bool, terminal: bool
) -> None:
    app = Choice("app", 1)
    catalogue = Catalogue((app,))
    error = ResolutionInvariantError("invalid candidate") if terminal else ValueError()
    observed: list[Choice] = []

    def considered(candidate: Choice) -> None:
        observed.append(candidate)
        raise error

    session = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": app}.get if preferred else None,
        on_candidate_considered=considered,
    )
    initial = session.remaining_rounds
    with pytest.raises(type(error)) as caught:
        session.solve()

    assert caught.value is error
    assert observed == [app]
    assert catalogue.facts == {}
    assert session.stats.rounds > 0
    assert session.remaining_rounds == initial - session.stats.rounds


@pytest.mark.parametrize("preferred", [False, True])
def test_exhausted_session_does_not_report_candidates(preferred: bool) -> None:
    app = Choice("app", 1)
    observed: list[Choice] = []
    session = PreparedResolution.from_catalogue(
        Catalogue((app,)),
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": app}.get if preferred else None,
        on_candidate_considered=observed.append,
        max_rounds=0,
    )

    with pytest.raises(ResolutionLimitError):
        session.solve()

    assert observed == []
    assert session.stats.rounds == 0
