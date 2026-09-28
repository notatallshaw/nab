"""Standard and canary runners preserve original pins for production admission."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, TypeVar

import pytest

from nab_project._testing.coordinator_fake import make_coordinator
from nab_project._testing.yanking import graph_port
from nab_provider.records import WheelFile
from nab_provider.yanked_resolution import YankProxyProvider
from nab_provider.yanking import mark_yanked
from nab_resolver.resolver import Resolver

from .test_benchmark_parse_requirements import _harness

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from nab_provider._vendor.packaging.version import Version
    from nab_provider.provider import Provider
    from nab_provider.testing import FakeFetchPort
    from nab_resolver.types import RangeProtocol, RootRequirement

pytestmark = [pytest.mark.benchmark, pytest.mark.usefixtures("benchmark_import_path")]

PackageT = TypeVar("PackageT")
VersionT = TypeVar("VersionT")
SOLVER_COUNTERS = (
    "decisions",
    "conflicts",
    "backjumps",
    "restarts",
    "incompatibilities_learned",
)


@pytest.mark.parametrize("runner", ["scenarios", "canary"])
@pytest.mark.parametrize(
    ("dependency", "constraint", "success"),
    [
        ("dep==1", None, True),
        ("dep>=1", "dep==1", True),
        ("dep>=1", None, False),
        ("dep==1.*", None, False),
        ("dep>=1,<=1", None, False),
    ],
)
def test_runners_admit_only_original_exact_pins(
    runner: str,
    dependency: str,
    constraint: str | None,
    success: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness(runner)
    proxy_decisions: list[int] = []
    original_resolve = YankProxyProvider.resolve

    def counted_resolve(self: YankProxyProvider) -> tuple[dict[str, Version], Provider]:
        """Record decisions from the real admission solve, including failures."""
        try:
            return original_resolve(self)
        finally:
            proxy_decisions.append(self.stats.decisions)

    monkeypatch.setattr(YankProxyProvider, "resolve", counted_resolve)
    files = {}
    metadata = {}
    for name, declarations in (("app", (dependency,)), ("dep", ())):
        wheel = WheelFile(
            filename=f"{name}-1-py3-none-any.whl",
            url=f"https://index.test/{name}-1.whl",
            version="1",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        files[name] = [
            mark_yanked(wheel, reason="withdrawn") if name == "dep" else wheel
        ]
        metadata[wheel.metadata_url] = (
            f"Metadata-Version: 2.2\nName: {name}\nVersion: 1\n"
            + "".join(f"Requires-Dist: {req}\n" for req in declarations)
            + "\n"
        )
    coordinator = make_coordinator(listings=files, metadata_by_url=metadata)

    @contextmanager
    def opened_coordinator(
        *_args: object, **_kwargs: object
    ) -> Iterator[FakeFetchPort]:
        yield coordinator

    monkeypatch.setattr(module, "FetchCoordinator", opened_coordinator)
    monkeypatch.setattr(module, "HttpxAsyncTransport", object)
    host = module.BenchmarkHost.current(None)
    config = module.build_benchmark_config(
        indexes=[], trust_unverified_sdist_deps=False
    )
    requirements = module.parse_requirements(["dep>=1", "app"])
    constraints = module.parse_requirements([constraint]) if constraint else None
    execute = module.resolve_scenario if runner == "scenarios" else module.run_one
    result = execute(
        requirements, constraints, config=config, target=host.target, host=host
    )
    outcome = result["result"] if runner == "scenarios" else result
    stats = result["stats"] if runner == "scenarios" else result
    assert outcome["success"] is success, outcome.get("error")
    assert stats["yanked_proxy_rounds"] > 0
    assert len(proxy_decisions) == 1
    assert stats["decisions"] >= proxy_decisions[0]
    if not success:
        assert "YankAdmissionRequiredError" not in outcome["error"]
        assert all(
            "dep-1.whl" not in str(call)
            for call in coordinator.calls_to("request_metadata")
        )


@pytest.mark.parametrize("runner", ["scenarios", "canary"])
def test_retry_counters_include_every_actual_solve(
    runner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _harness(runner)
    observed: list[dict[str, int]] = []
    original_resolve = Resolver.resolve

    def counted_resolve(
        self: Resolver[PackageT, VersionT],
        requirements: Mapping[PackageT, RangeProtocol[VersionT]]
        | Sequence[RootRequirement[PackageT, VersionT]],
        constraints: Mapping[PackageT, RangeProtocol[VersionT]] | None = None,
    ) -> dict[PackageT, VersionT]:
        """Snapshot actual counters before the benchmark aggregates into them."""
        try:
            return original_resolve(self, requirements, constraints)
        finally:
            observed.append(
                {name: getattr(self.stats, name) for name in SOLVER_COUNTERS}
            )

    monkeypatch.setattr(Resolver, "resolve", counted_resolve)
    releases = [
        (f"noise{i}", str(version), False, []) for i in range(5) for version in (1, 2)
    ]
    releases.extend(
        [
            ("app", "1", False, ["left", "right"]),
            ("left", "1", False, ["leaf==1"]),
            ("right", "1", False, ["leaf==2"]),
            ("leaf", "1", False, []),
            ("leaf", "2", False, []),
            ("withdrawn", "1", True, []),
        ]
    )
    coordinator = graph_port(releases)

    @contextmanager
    def opened_coordinator(
        *_args: object, **_kwargs: object
    ) -> Iterator[FakeFetchPort]:
        yield coordinator

    monkeypatch.setattr(module, "FetchCoordinator", opened_coordinator)
    monkeypatch.setattr(module, "HttpxAsyncTransport", object)
    host = module.BenchmarkHost.current(None)
    config = module.build_benchmark_config(
        indexes=[], trust_unverified_sdist_deps=False
    )
    requirements = module.parse_requirements(
        [*(f"noise{i}" for i in range(5)), "app", "withdrawn==1"]
    )
    execute = module.resolve_scenario if runner == "scenarios" else module.run_one
    result = execute(requirements, None, config=config, target=host.target, host=host)
    outcome = result["result"] if runner == "scenarios" else result
    stats = result["stats"] if runner == "scenarios" else result

    assert not outcome["success"]
    assert len(observed) > 1
    assert sum(row["decisions"] for row in observed[1:]) > 0
    for name in SOLVER_COUNTERS:
        assert stats[name] == sum(row[name] for row in observed), (
            name,
            stats,
            observed,
        )
