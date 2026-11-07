from collections.abc import Mapping

import pytest

from nab_provider.preference import (
    CataloguePreference,
    IncompletePreferenceError,
    PreferenceScope,
)
from nab_provider.preference_provider import (
    MetadataProxy,
    PermissionProxy,
    PreferenceProvider,
)
from nab_provider.prepared_resolution import (
    PreparedResolution,
    _PreferredProvider,
    _PreferredTokens,
    _SelectedAdmission,
)
from nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
)
from nab_resolver.ranges import Range
from tests.test_preference_core import Catalogue, Choice, Data, Pin


class ObservedCatalogue(Catalogue):
    """Record whether a preferred trial queried the full candidate listing."""

    def __init__(self, choices: tuple[Choice, ...]) -> None:
        super().__init__(choices)
        self.listed: list[str] = []

    def choices(self, package: str) -> tuple[Choice, ...]:
        self.listed.append(package)
        return super().choices(package)


def test_preferred_graph_needs_no_full_listing() -> None:
    app = Choice("app", 1)
    dep = Choice("dep", 1)
    catalogue = ObservedCatalogue((app, dep))
    catalogue.metadata[app] = Data({"dep": Range.singleton(1)})
    preferred = {"app": app, "dep": dep}

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range.full()},
        (),
        {},
        preferred_candidate=preferred.get,
    ).solve()

    assert result.candidates == preferred
    assert result.edges == (("app", "dep"),)
    assert catalogue.listed == []
    assert set(catalogue.facts) == {app, dep}


def test_preferred_refusal_keeps_full_domain_for_ordinary_search() -> None:
    app = Choice("app", 1)
    newer = Choice("app", 2)
    dep = Choice("dep", 1)
    replacement = Choice("dep", 2)
    catalogue = ObservedCatalogue((app, newer, dep, replacement))
    catalogue.metadata[app] = Data({"dep": Range.singleton(1)})
    catalogue.metadata[newer] = Data({"dep": Range.singleton(2)})

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range.full(), "dep": Range.singleton(2)},
        (),
        {},
        preferred_candidate={"app": app, "dep": dep}.get,
    ).solve()

    assert result.candidates == {"app": newer, "dep": replacement}
    assert catalogue.listed
    assert set(catalogue.choices("dep")) == {dep, replacement}


def test_rejected_preference_does_not_make_ordinary_resolution_impossible() -> None:
    app = Choice("app", 1)
    other = Choice("app", 2)
    catalogue = ObservedCatalogue((app, other))
    catalogue.metadata[app] = None

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range.full()},
        (),
        {},
        preferred_candidate={"app": app}.get,
    ).solve()

    assert result.candidates == {"app": other}
    assert catalogue.facts[app] is None
    assert catalogue.listed


def test_preferred_trial_never_grants_withdrawal_permission() -> None:
    withdrawn = Choice("dep", 1, withdrawn=True)
    live = Choice("dep", 1)
    catalogue = ObservedCatalogue((withdrawn, live))

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"dep": Range.full()},
        (Pin("dep", 1),),
        {},
        preferred_candidate={"dep": withdrawn}.get,
    ).solve()

    assert result.candidates == {"dep": live}
    assert withdrawn not in catalogue.facts


def test_actual_unavailable_preference_cannot_authorize_a_withdrawn_file() -> None:
    live = Choice("dep", 1)
    withdrawn = Choice("dep", 1, withdrawn=True)
    catalogue = ObservedCatalogue((live, withdrawn))
    catalogue.unavailable.add(live)

    session = PreparedResolution.from_catalogue(
        catalogue,
        {"dep": Range.full()},
        (Pin("dep", 1),),
        {},
        preferred_candidate={"dep": live}.get,
    )
    with pytest.raises(IncompletePreferenceError):
        session.solve()

    assert withdrawn not in catalogue.facts


def test_preferred_trial_spends_the_shared_budget_before_ordinary_search() -> None:
    app = Choice("app", 1)
    catalogue = ObservedCatalogue((app,))
    preference = CataloguePreference(
        catalogue, {"app": Range[int].full()}, (), {}, max_steps=1
    )
    session = PreparedResolution(
        PreferenceProvider(preference), preferred_candidate={"app": app}.get
    )

    with pytest.raises(ResolutionLimitError):
        session.solve()

    assert preference.remaining == 0
    assert session.stats.rounds == 1
    assert catalogue.listed == []


def test_preferred_metadata_terminal_error_is_not_a_refused_trial() -> None:
    error = ResolutionInvariantError("invalid prepared metadata")

    class FaultCatalogue(ObservedCatalogue):
        def prepare(self, candidate: Choice) -> Data | None:
            raise error

    app = Choice("app", 1)
    catalogue = FaultCatalogue((app,))
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": app}.get,
    )

    with pytest.raises(ResolutionInvariantError) as caught:
        session.solve()

    assert caught.value is error
    assert catalogue.listed == []


def test_original_constraints_and_selected_policy_can_refuse_preferences() -> None:
    first = Choice("app", 1)
    second = Choice("app", 2)
    third = Choice("app", 3)
    catalogue = ObservedCatalogue((first, second, third))
    observed: list[dict[str, Choice]] = []

    def selection_is_admitted(selected: Mapping[str, Choice]) -> bool:
        observed.append(dict(selected))
        return selected["app"] == third

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {"app": Range[int].singleton(2) | Range[int].singleton(3)},
        preferred_candidate={"app": second}.get,
        selection_policy=selection_is_admitted,
    ).solve()

    assert result.candidates == {"app": third}
    assert {"app": second} in observed
    assert {"app": third} in observed
    assert {"app": first} not in observed


def test_live_metadata_and_eligible_incumbent_precede_unrelated_choices() -> None:
    parent = Choice("parent", 1)
    installed = Choice("app", 1)
    upgrade = Choice("app", 2)
    older = Choice("other", 1)
    newer = Choice("other", 2)
    catalogue = ObservedCatalogue((parent, newer, older, installed, upgrade))
    catalogue.metadata[parent] = Data({"app": Range[int].full()})
    catalogue.metadata[installed] = Data({"other": Range[int].singleton(1)})
    catalogue.metadata[upgrade] = Data({"other": Range[int].singleton(2)})

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"parent": Range[int].full(), "other": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": installed}.get,
    ).solve()

    assert result.candidates == {"parent": parent, "app": installed, "other": older}
    assert upgrade not in catalogue.facts


def test_refreshable_ordinary_session_uses_real_fact_growth() -> None:
    app = Choice("app", 1)
    catalogue = ObservedCatalogue((app,))
    result = PreparedResolution.from_catalogue(
        catalogue, {"app": Range[int].full()}, (), {}, refresh_incomplete=True
    ).solve()
    assert result.candidates == {"app": app}
    assert catalogue.facts[app] == Data({})


def test_nonterminal_trial_error_does_not_prove_global_absence() -> None:
    app = Choice("app", 1)

    class RefusingCatalogue(ObservedCatalogue):
        first_read = True

        def prepare(self, candidate: Choice) -> Data | None:
            if self.first_read:
                self.first_read = False
                raise ResolutionError("preferred trial refused")
            return super().prepare(candidate)

    catalogue = RefusingCatalogue((app,))
    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": app}.get,
    ).solve()
    assert result.candidates == {"app": app}
    assert catalogue.listed


def test_selected_graph_requires_actual_prepared_records() -> None:
    app = Choice("app", 1)
    catalogue = ObservedCatalogue((app,))

    class UnpreparedProvider(PreferenceProvider[Choice, Range[int], Pin]):
        def select(self) -> dict[str, Choice]:
            return {"app": app}

    preference = CataloguePreference(catalogue, {"app": Range[int].full()}, (), {})
    with pytest.raises(ResolutionInvariantError, match="no prepared metadata"):
        PreparedResolution(UnpreparedProvider(preference)).solve()


@pytest.mark.parametrize("admitted_version", [None, 2])
def test_optional_selection_callback_controls_the_selected_model(
    admitted_version: int | None,
) -> None:
    first = Choice("app", 1)
    second = Choice("app", 2)
    catalogue = ObservedCatalogue((first, second))
    observed: list[Mapping[str, Choice]] = []

    def selection_is_admitted(selected: Mapping[str, Choice]) -> bool:
        observed.append(selected)
        return selected["app"].version == admitted_version

    result = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        selection_policy=None if admitted_version is None else selection_is_admitted,
    ).solve()

    if admitted_version is None:
        assert result.candidates == {"app": first}
        assert observed == []
    else:
        assert result.candidates == {"app": second}
        assert {"app": first} in observed
        assert {"app": second} in observed


def test_selection_callback_is_cache_only_and_ignores_unreachable_metadata() -> None:
    app = Choice("app", 1)
    dep = Choice("dep", 1)
    other = Choice("other", 1)
    catalogue = ObservedCatalogue((app, dep, other))
    observed: list[Mapping[str, Choice]] = []

    def selection_is_admitted(selected: Mapping[str, Choice]) -> bool:
        observed.append(selected)
        return False

    check = _SelectedAdmission(
        catalogue, {"app": Range[int].full()}, selection_is_admitted
    )
    assert check(app, {"app": app}) is True
    assert catalogue.facts == {}
    assert observed == []

    catalogue.facts[app] = Data({"dep": Range[int].full()})
    assert check(app, {"app": app}) is True
    assert check(app, {"app": app, "dep": dep}) is True
    assert catalogue.facts == {app: Data({"dep": Range[int].full()})}
    assert observed == []

    catalogue.facts[dep] = Data({})
    assert check(other, {"app": app, "dep": dep, "other": other}) is True
    assert observed == []
    assert check(app, {"app": app, "dep": dep, "other": other}) is False
    assert observed == [{"app": app, "dep": dep}]


def test_preferred_token_view_is_a_consistent_readonly_mapping() -> None:
    app = Choice("app", 1)
    catalogue = ObservedCatalogue((app,))
    preference = CataloguePreference(catalogue, {"app": Range[int].full()}, (), {})
    trial = _PreferredProvider(preference, {"app": app}.get, metadata_is_admitted=None)
    assert trial.preferred("app") == app
    assert trial.preferred("missing") is None
    assert trial.choose_version("missing", Range[int].full()) is None
    token = trial.identify(app)
    view = _PreferredTokens(trial)

    assert dict(view) == {"app": token, MetadataProxy(token): 0}
    assert len(view) == 2
    with pytest.raises(KeyError):
        view["missing"]
    with pytest.raises(KeyError):
        view[MetadataProxy(token + 1)]
    with pytest.raises(KeyError):
        view[PermissionProxy(token)]
    assert catalogue.listed == []


def test_priority_is_cache_only_and_a_decision_does_not_imply_a_forced_choice() -> None:
    first = Choice("app", 1)
    second = Choice("app", 2)
    catalogue = ObservedCatalogue((first, second))
    session = PreparedResolution.from_catalogue(
        catalogue, {"app": Range[int].full()}, (), {}
    )
    provider = session.provider
    first_token = provider.identify(first)
    second_token = provider.identify(second)
    choices = Range[int].singleton(first_token) | Range[int].singleton(second_token)
    provider.prioritize("app", choices, {})
    assert catalogue.listed == []
    assert catalogue.facts == {}

    assert provider.choose_version("app", choices) == first_token
    provider.receive_decision_scan_hint(
        {"app": Range[int].singleton(first_token)}, {"app": first_token}
    )
    ordinary = provider.prioritize(
        MetadataProxy(first_token), Range[int].singleton(0), {}
    )
    assert ordinary[0] > 0

    assert (
        provider.choose_version("app", Range[int].singleton(first_token)) == first_token
    )
    forced = provider.prioritize(
        MetadataProxy(first_token), Range[int].singleton(0), {}
    )
    assert forced < ordinary

    assert provider.choose_version("app", choices) == first_token
    assert (
        provider.prioritize(MetadataProxy(first_token), Range[int].singleton(0), {})
        == ordinary
    )
    assert catalogue.facts == {}


def test_scoped_query_preserves_eligible_preferred_identity_without_listing() -> None:
    first = Choice("app", 1)
    second = Choice("app", 2)
    other = Choice("other", 1)
    alternative = Choice("other", 2)
    catalogue = ObservedCatalogue((first, second, other, alternative))
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": first}.get,
    )
    provider = session.provider
    app_tokens = [provider.identify(first), provider.identify(second)]
    other_tokens = [provider.identify(other), provider.identify(alternative)]
    query = provider.new_query(PreferenceScope())

    assert query.prioritize(
        "app", Range[int].from_versions(app_tokens), {}
    ) < query.prioritize("other", Range[int].from_versions(other_tokens), {})
    assert catalogue.listed == []
    assert catalogue.facts == {}
