"""Solver-round budgets survive arbitrary host interruptions and new sessions."""

from __future__ import annotations

import pytest

from nab_provider._compat import override
from nab_provider.preference import CataloguePreference
from nab_provider.preference_provider import PreferenceProvider
from nab_provider.prepared_resolution import PreparedResolution
from nab_resolver.errors import ResolutionInvariantError, ResolutionLimitError
from nab_resolver.ranges import Range
from tests.test_preference_core import Catalogue, Choice, Data


class DomainChangedError(Exception):
    """A host observed a different candidate domain."""


class InterruptedCatalogue(Catalogue):
    """Interrupt one real metadata read, then serve the same immutable fact."""

    def __init__(self, candidate: Choice, error: Exception) -> None:
        super().__init__((candidate,))
        self.error = error
        self.calls = 0

    @override
    def prepare(self, candidate: Choice) -> Data | None:
        self.calls += 1
        if self.calls == 1:
            raise self.error
        return super().prepare(candidate)


@pytest.mark.parametrize("preferred", [False, True])
def test_arbitrary_host_abort_charges_completed_rounds_and_preserves_identity(
    preferred: bool,
) -> None:
    app = Choice("app", 1)
    error = DomainChangedError("new candidate identity")
    catalogue = InterruptedCatalogue(app, error)
    preference = CataloguePreference(catalogue, {"app": Range[int].full()}, (), {})
    initial = preference.remaining
    session = PreparedResolution(
        PreferenceProvider(preference),
        preferred_candidate={"app": app}.get if preferred else None,
    )

    with pytest.raises(DomainChangedError) as caught:
        session.solve()

    assert caught.value is error
    assert catalogue.calls == 1
    assert session.stats.rounds > 0
    assert session.remaining_rounds == initial - session.stats.rounds

    # Carry the remaining resource into a fresh candidate-domain session.
    first_rounds = session.stats.rounds
    restarted = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        max_rounds=session.remaining_rounds,
        preferred_candidate={"app": app}.get if preferred else None,
    )
    assert restarted.stats.rounds == 0
    assert restarted.solve().candidates == {"app": app}
    assert restarted.remaining_rounds == initial - first_rounds - restarted.stats.rounds
    assert session.stats.rounds == first_rounds


@pytest.mark.parametrize("preferred", [False, True])
def test_zero_budget_stops_before_host_callbacks(preferred: bool) -> None:
    app = Choice("app", 1)
    catalogue = InterruptedCatalogue(app, AssertionError("metadata was queried"))

    def read_preferred(_package: str) -> Choice:
        pytest.fail("preferred candidates were queried with no budget")

    session = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        max_rounds=0,
        preferred_candidate=read_preferred if preferred else None,
    )
    with pytest.raises(ResolutionLimitError):
        session.solve()

    assert session.remaining_rounds == 0
    assert session.stats.rounds == 0
    assert catalogue.calls == 0


def test_domain_abort_on_exact_final_round_reports_the_terminal_limit() -> None:
    app = Choice("app", 1)
    first = PreparedResolution.from_catalogue(
        InterruptedCatalogue(app, DomainChangedError()),
        {"app": Range[int].full()},
        (),
        {},
    )
    with pytest.raises(DomainChangedError):
        first.solve()
    boundary = first.stats.rounds
    assert boundary > 0

    error = DomainChangedError("last permitted round")
    catalogue = InterruptedCatalogue(app, error)
    session = PreparedResolution.from_catalogue(
        catalogue, {"app": Range[int].full()}, (), {}, max_rounds=boundary
    )
    with pytest.raises(ResolutionLimitError) as caught:
        session.solve()

    assert caught.value.__context__ is error
    assert catalogue.calls == 1
    assert session.stats.rounds == boundary
    assert session.remaining_rounds == 0


@pytest.mark.parametrize("preferred", [False, True])
def test_terminal_metadata_error_is_not_retried_or_replaced(preferred: bool) -> None:
    app = Choice("app", 1)
    error = ResolutionInvariantError("invalid prepared fact")
    catalogue = InterruptedCatalogue(app, error)
    session = PreparedResolution.from_catalogue(
        catalogue,
        {"app": Range[int].full()},
        (),
        {},
        preferred_candidate={"app": app}.get if preferred else None,
    )
    initial = session.remaining_rounds

    with pytest.raises(ResolutionInvariantError) as caught:
        session.solve()

    assert caught.value is error
    assert catalogue.calls == 1
    assert session.remaining_rounds == initial - session.stats.rounds


def test_wrapping_a_provider_preserves_its_existing_resource_and_stats() -> None:
    app = Choice("app", 1)
    preference = CataloguePreference(
        Catalogue((app,)), {"app": Range[int].full()}, (), {}
    )
    preference.spend(3)
    preference.stats.rounds = 3
    provider = PreferenceProvider(preference)
    session = PreparedResolution(provider)

    assert session.remaining_rounds == preference.remaining
    assert session.stats is preference.stats
    assert session.stats.rounds == 3


def test_listing_abort_before_any_solver_round_spends_nothing() -> None:
    app = Choice("app", 1)
    error = DomainChangedError("listing changed")

    class ListingChanged(Catalogue):
        @override
        def choices(self, package: str) -> tuple[Choice, ...]:
            raise error

    catalogue = ListingChanged((app,))
    session = PreparedResolution.from_catalogue(
        catalogue, {"app": Range[int].full()}, (), {}
    )
    initial = session.remaining_rounds
    with pytest.raises(DomainChangedError) as caught:
        session.solve()

    assert caught.value is error
    assert session.remaining_rounds == initial
    assert session.stats.rounds == 0


def test_success_on_the_final_permitted_round_is_not_exhaustion_failure() -> None:
    app = Choice("app", 1)
    control = PreparedResolution.from_catalogue(
        Catalogue((app,)), {"app": Range[int].full()}, (), {}
    )
    assert control.solve().candidates == {"app": app}
    needed = control.stats.rounds
    session = PreparedResolution.from_catalogue(
        Catalogue((app,)), {"app": Range[int].full()}, (), {}, max_rounds=needed
    )

    assert session.solve().candidates == {"app": app}
    assert session.stats.rounds == needed
    assert session.remaining_rounds == 0
