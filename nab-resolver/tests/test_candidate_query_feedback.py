"""Exercise contextual feedback on a finite dependency conflict with unrelated leaves."""

from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import cast

import pytest

from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import RangeProtocol


class Package(Enum):
    APP = "app"
    PARQUET = "fastparquet"
    FILES = "fsspec"
    API = "opentelemetry-api"
    COMMON = "opentelemetry-exporter-otlp-proto-common"
    GRPC = "opentelemetry-exporter-otlp-proto-grpc"
    HTTP = "opentelemetry-exporter-otlp-proto-http"
    SDK = "opentelemetry-sdk"
    PACKAGING = "packaging"
    SODA = "soda-core"
    DUCK = "soda-core-duckdb"


class Dependency:
    """Retain a dependency's constraint and its host ordering category."""

    __slots__ = ("constraint", "package", "priority")

    def __init__(
        self, package: Package, constraint: Range[int], *, priority: int
    ) -> None:
        self.package = package
        self.constraint = constraint
        self.priority = priority

    def bind(self) -> CandidateRequirement[Package, int]:
        """Keep the host's ordering category beside the solver restriction."""
        return CandidateRequirement(self.package, self.constraint, self)


def pin(package: Package, version: int) -> Dependency:
    """Give exact requirements the first host priority category."""
    return Dependency(package, Range.singleton(version), priority=0)


def bounded(package: Package, low: int, high: int) -> Dependency:
    """Represent an upper-bounded requirement without making it an exact pin."""
    return Dependency(package, Range.at_least(low) & Range.less_than(high), priority=1)


def free(package: Package) -> Dependency:
    """Leave a leaf's versions unconstrained and last in host order."""
    return Dependency(package, Range.full(), priority=2)


class GraphHost:
    """Prepare newest candidates from active requests while retaining real dependency records."""

    def __init__(self, *, shared_dependency: bool) -> None:
        app = [pin(Package.PARQUET, 1), bounded(Package.DUCK, 1, 2)]
        if shared_dependency:
            app.append(bounded(Package.GRPC, 116, 200))
        app.append(bounded(Package.HTTP, 116, 200))
        self.graph = {
            Package.APP: {1: app},
            Package.PARQUET: {1: [free(Package.FILES), free(Package.PACKAGING)]},
            Package.FILES: {version: [] for version in (1, 2, 3)},
            Package.PACKAGING: {version: [] for version in (1, 2, 3)},
            Package.DUCK: {1: [pin(Package.SODA, 1)]},
            Package.SODA: {
                1: [bounded(Package.API, 116, 123), bounded(Package.HTTP, 116, 123)]
            },
            Package.API: {version: [] for version in (122, 134)},
            Package.SDK: {
                version: [pin(Package.API, version)] for version in (122, 134)
            },
        }
        for package in (
            (Package.HTTP, Package.GRPC) if shared_dependency else (Package.HTTP,)
        ):
            self.graph[package] = {}
            for version in (122, 134):
                dependencies = [bounded(Package.API, 115, 200)]
                if shared_dependency:
                    dependencies.append(pin(Package.COMMON, version))
                dependencies.append(bounded(Package.SDK, version, version + 1))
                self.graph[package][version] = dependencies
        if shared_dependency:
            self.graph[Package.COMMON] = {version: [] for version in (122, 134)}

    def iter_candidates(
        self,
        package: Package,
        allowed: RangeProtocol[int],
        requirements: Mapping[Package, Sequence[CandidateRequirement[Package, int]]],
    ) -> Iterable[PreparedCandidate[int]]:
        """Yield catalog candidates admitted by the active original requests."""
        if package not in requirements:
            return
        for version in sorted(self.graph[package], reverse=True):
            if all(
                version in requirement.constraint
                for requirement in requirements[package]
            ):
                yield PreparedCandidate(version, (package, version))

    def get_dependencies(
        self, candidate: PreparedCandidate[int]
    ) -> Iterable[CandidateRequirement[Package, int]]:
        """Bind the selected catalog entry's dependency declarations."""
        package, version = cast("tuple[Package, int]", candidate.origin)
        return (dependency.bind() for dependency in self.graph[package][version])

    def priority(
        self,
        package: Package,
        requirements: Mapping[Package, Sequence[CandidateRequirement[Package, int]]],
    ) -> tuple[int, str]:
        """Prefer pins and upper bounds, then use the package's stable label."""
        priorities = (
            cast("Dependency", requirement.origin).priority
            for requirement in requirements.get(package, ())
        )
        return min(priorities, default=2), package.value


@pytest.mark.parametrize("shared_dependency", [False, True])
def test_feedback_limits_unrelated_version_enumeration(shared_dependency: bool) -> None:
    host = GraphHost(shared_dependency=shared_dependency)
    roots = [pin(Package.APP, 1).bind()]
    ordinary = CandidateProvider(host, roots)
    guided = CandidateProvider(host, roots, query_feedback=True)
    baseline = Resolver(ordinary, availability_generation=lambda: 0)
    resolver = Resolver(guided, availability_generation=lambda: 0)

    expected = {package: max(versions) for package, versions in host.graph.items()}
    for package in (
        Package.API,
        Package.SDK,
        Package.HTTP,
        Package.COMMON,
        Package.GRPC,
    ):
        if package in expected:
            expected[package] = 122
    assert baseline.solve(ordinary.root_requirements()).pins == expected
    assert resolver.solve(guided.root_requirements()).pins == expected
    assert resolver.stats.decisions <= 128
    assert resolver.stats.decisions < baseline.stats.decisions

    first_decisions = resolver.stats.decisions
    assert resolver.solve(guided.root_requirements()).pins == expected
    assert resolver.stats.decisions == first_decisions
