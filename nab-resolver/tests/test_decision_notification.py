"""Deliver real decision events without requiring structural providers to listen."""

from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

from nab_resolver.ranges import Range
from nab_resolver.resolver import (
    BaseProvider,
    Resolver,
    ResolverObserver,
    ResolverProvider,
)
from nab_resolver.types import RangeProtocol


class CatalogueProvider(BaseProvider[str, int]):
    """Resolve a finite catalogue while recording metadata and callback order."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.catalogue: dict[str, dict[int, dict[str, Range[int]]]] = {"leaf": {1: {}}}
        self.dependency_error: Exception | None = None

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return next(
            (
                version
                for version in sorted(self.catalogue[package], reverse=True)
                if version in version_range
            ),
            None,
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return self.choose_version(package, version_range) is not None

    def get_dependencies(
        self, package: str, version: int
    ) -> Mapping[str, RangeProtocol[int]]:
        self.events.append(f"dependencies:{package}:{version}")
        if self.dependency_error is not None:
            raise self.dependency_error
        return self.catalogue[package][version]

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return list(self.catalogue).index(package)

    def widen_decision(self, package: str, version: int) -> None:
        return None


class ListeningProvider(CatalogueProvider):
    """Record notifications and optionally change priority or fail."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.notifications: list[tuple[str, int]] = []
        self.changed = False
        self.notification_error: Exception | None = None

    def receive_decision(self, package: str, version: int) -> bool:
        self.events.append(f"notification:{package}:{version}")
        self.notifications.append((package, version))
        if self.notification_error is not None:
            raise self.notification_error
        return self.changed


class ActingObserver(ResolverObserver[str, int]):
    """Record the observer event before an optional replacement or exception."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.action: Callable[[], None] | None = None

    def on_decision(self, package: str, version: int, level: int) -> None:
        self.events.append(f"observer:{package}:{version}")
        if self.action is not None:
            self.action()


@pytest.mark.parametrize("changed", [False, True])
def test_leaf_notification_follows_observer_and_precedes_metadata(
    *, changed: bool
) -> None:
    events: list[str] = []
    provider = ListeningProvider(events)
    provider.changed = changed
    resolver = Resolver(provider, ActingObserver(events))

    assert resolver.solve({"leaf": Range.full()}).pins == {"leaf": 1}

    assert events[:3] == [
        "observer:leaf:1",
        "notification:leaf:1",
        "dependencies:leaf:1",
    ]
    assert provider.notifications == [("leaf", 1)]
    assert resolver.priority_epoch == int(changed)


def test_immediate_conflict_notifies_the_discarded_decision() -> None:
    events: list[str] = []
    provider = ListeningProvider(events)
    provider.catalogue = {
        "parent": {2: {"blocker": Range.singleton(2)}, 1: {}},
        "blocker": {1: {}},
    }
    resolver = Resolver(provider, ActingObserver(events))

    result = resolver.solve({"parent": Range.full(), "blocker": Range.singleton(1)})

    assert result.pins == {"parent": 1, "blocker": 1}
    assert provider.notifications == [("parent", 2), ("parent", 1), ("blocker", 1)]
    assert events.index("notification:parent:2") < events.index("dependencies:parent:2")
    assert resolver.stats.conflicts > 0


def test_empty_solve_does_not_notify_the_virtual_root() -> None:
    provider = ListeningProvider([])

    assert Resolver(provider).solve({}).pins == {}
    assert provider.notifications == []


@pytest.mark.parametrize("stage", ["observer", "notification", "dependencies"])
def test_callback_and_metadata_error_order(stage: str) -> None:
    events: list[str] = []
    provider = ListeningProvider(events)
    observer = ActingObserver(events)
    provider.dependency_error = OSError("dependency failure")
    if stage in {"observer", "notification"}:
        provider.notification_error = ValueError("notification failure")
    if stage == "observer":

        def fail() -> None:
            raise RuntimeError("observer failure")

        observer.action = fail
    resolver = Resolver(provider, observer)
    expected, message = {
        "observer": (RuntimeError, "observer failure"),
        "notification": (ValueError, "notification failure"),
        "dependencies": (OSError, "dependency failure"),
    }[stage]

    with pytest.raises(expected, match=message):
        resolver.solve({"leaf": Range.full()})

    assert resolver.solution.decisions()["leaf"] == 1
    expected_events = ["observer:leaf:1"]
    if stage != "observer":
        expected_events.append("notification:leaf:1")
    if stage == "dependencies":
        expected_events.append("dependencies:leaf:1")
    assert events == expected_events


def test_observer_replacement_routes_notification_and_metadata_to_current_provider() -> (
    None
):
    events: list[str] = []
    original = ListeningProvider(events)
    original.dependency_error = OSError("metadata must use the replacement provider")
    replacement = ListeningProvider(events)
    replacement.changed = True
    observer = ActingObserver(events)
    resolver = Resolver(original, observer)

    def replace() -> None:
        resolver.provider = replacement

    observer.action = replace
    assert resolver.solve({"leaf": Range.full()}).pins == {"leaf": 1}
    assert original.notifications == []
    assert replacement.notifications == [("leaf", 1)]
    assert resolver.priority_epoch == 1


def test_observer_can_rebind_the_hook_on_the_same_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = ListeningProvider(events)
    observer = ActingObserver(events)
    resolver = Resolver(provider, observer)

    def rebound(package: str, version: int) -> bool:
        events.append(f"rebound:{package}:{version}")
        return True

    def rebind() -> None:
        monkeypatch.setattr(provider, "receive_decision", rebound)

    observer.action = rebind
    assert resolver.solve({"leaf": Range.full()}).pins == {"leaf": 1}
    assert provider.notifications == []
    assert events[:3] == ["observer:leaf:1", "rebound:leaf:1", "dependencies:leaf:1"]
    assert resolver.priority_epoch == 1


class StructuralProvider:
    """Delegate required operations while omitting decision notifications."""

    def __init__(self, inner: CatalogueProvider) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        """Expose the inner provider except for the optional decision hook."""
        if name == "receive_decision":
            raise AttributeError(name)
        return getattr(self.inner, name)


class StructuralListener(StructuralProvider):
    def receive_decision(self, package: str, version: int) -> bool:
        self.inner.events.append(f"structural:{package}:{version}")
        return True


@pytest.mark.parametrize("listens", [False, True])
def test_structural_notification_is_optional(*, listens: bool) -> None:
    inner = CatalogueProvider([])
    wrapper = StructuralListener(inner) if listens else StructuralProvider(inner)
    provider = cast("ResolverProvider[str, int]", wrapper)
    resolver = Resolver(provider)

    assert resolver.solve({"leaf": Range.full()}).pins == {"leaf": 1}
    assert ("structural:leaf:1" in inner.events) is listens
    assert resolver.priority_epoch == int(listens)


def test_base_provider_notification_is_a_noop() -> None:
    assert BaseProvider[str, int]().receive_decision("leaf", 1) is False


def test_a_tentative_choice_abandoned_by_force_backtracking_does_not_notify() -> None:
    class ForcingProvider(ListeningProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.catalogue = {name: {1: {}} for name in ["blocker", "middle", "leaf"]}
            self.targets: list[str] = []
            self.forced = False
            self.leaf_queries = 0

        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            if package == "leaf":
                self.leaf_queries += 1
                if not self.forced:
                    self.targets.append("blocker")
                    self.forced = True
            return super().choose_version(package, version_range)

        def consume_force_backtrack_targets(self) -> list[str]:
            targets, self.targets = self.targets, []
            return targets

    provider = ForcingProvider()
    resolver = Resolver(provider)
    result = resolver.solve({name: Range.full() for name in provider.catalogue})

    assert result.pins == {"blocker": 1, "middle": 1, "leaf": 1}
    assert resolver.stats.targeted_backtracks == 1
    assert provider.leaf_queries == 2
    assert provider.notifications.count(("leaf", 1)) == 1
    assert provider.notifications.count(("blocker", 1)) == 2
