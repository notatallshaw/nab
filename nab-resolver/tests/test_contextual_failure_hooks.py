"""Check optional failure notifications against the real decision queue and solve lifecycle."""

from collections.abc import Mapping
from typing import Any

import pytest

from nab_resolver.decide import choose_package_to_decide, record_contextual_no_versions
from nab_resolver.errors import ResolutionError
from nab_resolver.propagate import unit_propagation
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.root import ROOT
from nab_resolver.types import RangeProtocol, RootRequirement


class OrderingProvider(BaseProvider[str, int]):
    """Change package order only when a failure notification requests it."""

    def __init__(self, *, change_priority: bool = True) -> None:
        self.change_priority = change_priority
        self.failures: list[str] = []
        self.beginnings = 0

    def begin_resolution(self) -> None:
        self.beginnings += 1
        self.failures.clear()

    def receive_contextual_failure(self, package: str) -> bool:
        self.failures.append(package)
        return self.change_priority

    def choose_version(self, package: str, version_range: RangeProtocol[int]) -> int | None:
        return 1 if package != "missing" and 1 in version_range else None

    def has_satisfying_version(self, package: str, version_range: RangeProtocol[int]) -> bool:
        return package != "missing" and 1 in version_range

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        return {"missing": Range.full()} if package == "app" else {}

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        prefer_second = self.change_priority and bool(self.failures)
        return int(package != ("second" if prefer_second else "first"))

    def widen_decision(self, package: str, version: int) -> None:
        return None


@pytest.mark.parametrize("change_priority", [False, True])
def test_notification_invalidates_cached_order_only_when_requested(
    change_priority: bool,
) -> None:
    provider = OrderingProvider(change_priority=change_priority)
    resolver = Resolver(provider)
    resolver._reset(None)
    resolver._add_root_requirements(
        [RootRequirement(name, Range.full()) for name in ("guard", "first", "second")]
    )
    assert unit_propagation(resolver, ROOT) is None
    resolver.solution.decide("guard", 1)
    assert choose_package_to_decide(resolver) == "first"
    before = resolver.priority_epoch

    record_contextual_no_versions(resolver, "missing")

    assert provider.failures == ["missing"]
    assert resolver.priority_epoch == before + int(change_priority)
    assert choose_package_to_decide(resolver) == (
        "second" if change_priority else "first"
    )


def test_notifications_use_the_replacement_provider() -> None:
    original = OrderingProvider()
    replacement = OrderingProvider()
    resolver = Resolver(original)
    resolver.provider = replacement
    resolver._reset(None)
    resolver._add_root_requirements([RootRequirement("guard", Range.full())])
    resolver.solution.decide("guard", 1)

    record_contextual_no_versions(resolver, "missing")

    assert original.beginnings == 0
    assert original.failures == []
    assert replacement.beginnings == 1
    assert replacement.failures == ["missing"]


class LegacyProvider:
    """Expose the original provider protocol without either optional notification."""

    def __init__(self) -> None:
        self.inner = OrderingProvider()

    def __getattr__(self, name: str) -> Any:
        if name in {"begin_resolution", "receive_contextual_failure"}:
            raise AttributeError(name)
        return getattr(self.inner, name)


def test_structural_provider_can_omit_notifications() -> None:
    provider = LegacyProvider()
    resolver = Resolver(provider, availability_generation=lambda: 0)
    with pytest.raises(ResolutionError):
        resolver.solve({"app": Range.full()})
    assert provider.inner.beginnings == 0
    assert provider.inner.failures == []


def test_unguarded_absence_sends_no_contextual_notification() -> None:
    provider = OrderingProvider()
    resolver = Resolver(provider, availability_generation=lambda: 0)
    with pytest.raises(ResolutionError):
        resolver.solve({"missing": Range.full()})
    assert provider.beginnings == 1
    assert provider.failures == []


def test_backjump_does_not_begin_another_resolution() -> None:
    provider = OrderingProvider()
    resolver = Resolver(provider, availability_generation=lambda: 0)
    for count in (1, 2):
        with pytest.raises(ResolutionError):
            resolver.solve({"app": Range.full()})
        assert resolver.stats.conflicts > 0
        assert provider.beginnings == count
        assert provider.failures == ["missing"]
