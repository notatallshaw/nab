"""Resolution when dependency metadata discovers additional candidate sources."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from nab_resolver._compat import override
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver, ResolverProvider
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)

_IMPORT_PROBE = """
import importlib.util
import sys
from types import ModuleType

package = ModuleType("tests")
package.__path__ = []
sys.modules["tests"] = package
spec = importlib.util.spec_from_file_location("tests.test_deferred_availability", sys.argv[1])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
"""


def test_import_with_another_tests_package() -> None:
    """Mixed-workspace collection may reserve the tests package for another suite."""
    subprocess.run(  # noqa: S603 - the interpreter, probe and test-module path are fixed
        [sys.executable, "-c", _IMPORT_PROBE, str(Path(__file__).resolve())],
        check=True,
    )


class DiscoveringProvider(BaseProvider[str, int]):
    """An in-memory candidate index extended by selected dependency metadata."""

    def __init__(self, *, reveal_root: bool = False) -> None:
        self.versions = {"app": [2, 1], "dep": [1], "reveal": [1]}
        self.reveal_root = reveal_root
        self.generation = 0
        self.queries: list[tuple[str, int | None]] = []
        self.decisions: Mapping[str, int] = {}

    def availability_generation(self) -> int:
        return self.generation

    def _eligible_versions(self, package: str) -> list[int]:
        """Offer the extra source only while its introducing parent is selected."""
        introduced = self.decisions.get("reveal") == 1 or (
            self.decisions.get("app") == 1 and not self.reveal_root
        )
        if package == "dep" and not introduced:
            return [1]
        return self.versions[package]

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        self.decisions = decisions

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        chosen = next(
            (
                version
                for version in self._eligible_versions(package)
                if version in version_range
            ),
            None,
        )
        self.queries.append((package, chosen))
        return chosen

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(
            version in version_range for version in self._eligible_versions(package)
        )

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        if package == "app" and version == 2:
            return {"dep": Range.at_least(2)}
        if package == "reveal" or (
            package == "app" and version == 1 and not self.reveal_root
        ):
            if 3 not in self.versions["dep"]:
                self.versions["dep"].insert(0, 3)
                self.generation += 1
            return {"dep": Range.singleton(3)}
        return {}

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return {"app": 0, "dep": 1, "reveal": 2}[package]

    def widen_decision(self, package: str, version: int) -> None:
        return None


@pytest.mark.parametrize("reveal_root", [False, True])
def test_static_absence_cannot_describe_discovered_sources(reveal_root: bool) -> None:
    provider = DiscoveringProvider(reveal_root=reveal_root)
    roots = {"app": Range.full()}
    if reveal_root:
        roots["app"] = Range.singleton(2)
        roots["reveal"] = Range.full()
    with pytest.raises(ResolutionError):
        Resolver(provider).resolve(roots)


@pytest.mark.parametrize("reveal_root", [False, True])
def test_deferred_absence_allows_later_source_discovery(reveal_root: bool) -> None:
    provider = DiscoveringProvider(reveal_root=reveal_root)
    roots = {"app": Range.full()}
    if reveal_root:
        roots["app"] = Range.singleton(2)
        roots["reveal"] = Range.full()
    result = Resolver(
        provider, availability_generation=provider.availability_generation
    ).resolve(roots)
    expected = {"app": 1, "dep": 3}
    if reveal_root:
        expected.update(app=2, reveal=1)
    assert result == expected
    assert ("dep", None) in provider.queries


class NeverAvailableProvider(DiscoveringProvider):
    """Keep the dependency unavailable under every application choice."""

    @override
    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        if package == "app":
            return {"dep": Range.at_least(2)}
        return {}


@pytest.mark.parametrize("with_parent", [False, True])
def test_exhausted_deferred_queries_terminate(with_parent: bool) -> None:
    provider = NeverAvailableProvider()
    roots = {"app": Range.full()} if with_parent else {"dep": Range.at_least(2)}
    with pytest.raises(ResolutionError) as caught:
        Resolver(
            provider, availability_generation=provider.availability_generation
        ).resolve(roots)
    assert "dep" in str(caught.value)
    if with_parent:
        assert "selected" in str(caught.value)


def test_deferred_constraint_failure_names_the_constraint() -> None:
    provider = DiscoveringProvider()
    with pytest.raises(ResolutionError) as caught:
        Resolver(
            provider, availability_generation=provider.availability_generation
        ).resolve({"app": Range.singleton(1)}, {"dep": Range.less_than(1)})
    assert "user constrained dep" in str(caught.value)


def test_deferred_solver_reuse_resets_unavailable_queries() -> None:
    provider = DiscoveringProvider()
    resolver = Resolver(
        provider, availability_generation=provider.availability_generation
    )
    with pytest.raises(ResolutionError):
        resolver.resolve({"app": Range.singleton(2)})
    assert resolver.resolve({"app": Range.singleton(1)}) == {"app": 1, "dep": 3}


def test_existing_incompatibility_cause_values_remain_stable() -> None:
    assert IncompatibilityCause.CONSTRAINT.value == 4
    assert IncompatibilityCause.DERIVED.value == 5


class ScanDiscoveringProvider(DiscoveringProvider):
    """Publish metadata started by a failed query at the next scan boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.pending = False

    @override
    def _eligible_versions(self, package: str) -> list[int]:
        return self.versions[package]

    @override
    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        version = super().choose_version(package, version_range)
        if package == "dep" and version is None:
            self.pending = True
        return version

    @override
    def begin_decision_scan(self) -> None:
        if self.pending:
            self.pending = False
            self.versions["dep"].insert(0, 3)
            self.generation += 1


def test_final_scan_observes_metadata_published_after_a_failed_query() -> None:
    provider = ScanDiscoveringProvider()
    result = Resolver(
        provider, availability_generation=provider.availability_generation
    ).resolve({"app": Range.singleton(2)})
    assert result == {"app": 2, "dep": 3}
    assert provider.queries == [("app", 2), ("dep", None), ("dep", 3)]


def test_contextual_error_identifies_the_missing_package() -> None:
    provider = NeverAvailableProvider()
    with pytest.raises(ResolutionError) as caught:
        Resolver(
            provider, availability_generation=provider.availability_generation
        ).resolve({"app": Range.full()})
    clauses = [caught.value.incompatibility]
    packages: set[str | None] = set()
    while clauses:
        clause = clauses.pop()
        if clause is None:
            continue
        packages.add(clause.unavailable_package)
        clauses.extend([clause.cause_left, clause.cause_right])
    assert packages == {None, "dep"}


class ActionProvider(BaseProvider[str, int]):
    """Offer a global absence fact or request a backjump after a failed query."""

    def __init__(self, action: str) -> None:
        self.dependencies: dict[str, dict[str, Range[int]]] = {
            "app": {"mid": Range.full()},
            "mid": {"missing": Range.full()},
            "trigger": {},
        }
        self.action = action
        self.emitted = False
        self.decisions: Mapping[str, int] = {}
        self.targets: list[str] = []
        self.clauses: list[Incompatibility[str, int]] = []

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return {"app": 0, "mid": 1, "missing": 2, "trigger": 3}[package]

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        self.decisions = decisions

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        if package == "trigger" and "mid" in self.decisions and not self.emitted:
            self.emitted = True
            if self.action == "backjump":
                self.targets = ["mid"]
            else:
                self.clauses = [
                    Incompatibility(
                        [Term("missing", Range.full(), positive=True)],
                        cause=IncompatibilityCause.NO_VERSIONS,
                    )
                ]
            return None
        return 1 if self.has_satisfying_version(package, version_range) else None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return package in self.dependencies and 1 in version_range

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        return self.dependencies[package]

    def widen_decision(self, package: str, version: int) -> None:
        return None

    @override
    def consume_force_backtrack_targets(self) -> list[str]:
        targets, self.targets = self.targets, []
        return targets

    @override
    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        clauses, self.clauses = self.clauses, []
        return clauses


class ActionTraceResolver(Resolver[str, int]):
    """Record the deferred sweep immediately around a provider action."""

    def __init__(self, provider: ActionProvider) -> None:
        super().__init__(provider, availability_generation=lambda: 0)
        self.action_provider = provider
        self.sweeps: list[tuple[list[str], list[str]]] = []

    @override
    def _decide_next(
        self,
        next_package: str,
        *,
        hinted_provider: ResolverProvider[str, int] | None = None,
    ) -> str:
        assert self.deferred is not None
        before = list(self.deferred.packages)
        emitted = self.action_provider.emitted
        result = super()._decide_next(next_package, hinted_provider=hinted_provider)
        if self.action_provider.emitted and not emitted:
            self.sweeps.append((before, list(self.deferred.packages)))
        return result


@pytest.mark.parametrize("action", ["backjump", "clause"])
def test_provider_actions_start_a_fresh_deferred_sweep(action: str) -> None:
    resolver = ActionTraceResolver(ActionProvider(action))
    with pytest.raises(ResolutionError):
        resolver.resolve({"app": Range.full(), "trigger": Range.full()})
    assert resolver.sweeps == [(["missing"], [])]
