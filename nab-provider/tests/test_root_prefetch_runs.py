"""Root prefetch keeps file selection while reusing pure version-range queries."""

from __future__ import annotations

import pytest

from nab_provider._provider.listing import prefetch_root_batch
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.policy import ResolutionStrategy
from nab_provider.provider import Provider
from nab_provider.records import DistFile
from nab_provider.testing import make_coordinator, make_sdist, make_wheel


@pytest.mark.parametrize(
    "strategy", [ResolutionStrategy.HIGHEST, ResolutionStrategy.LOWEST]
)
def test_repeated_versions_share_queries_without_deduplicating_files(
    monkeypatch: pytest.MonkeyPatch, strategy: ResolutionStrategy
) -> None:
    versions = {text: Version(text) for text in ("1", "2", "3")}
    wheels = [
        make_wheel("2", package="demo"),
        make_wheel("2", package="demo", tag="cp312-cp312-win_amd64"),
    ]
    rows: list[tuple[Version, DistFile]] = [
        (versions["3"], make_wheel("3", package="demo")),
        (versions["3"], make_sdist("3", package="demo")),
        (versions["2"], wheels[0]),
        (versions["2"], make_sdist("2", package="demo")),
        (versions["2"], wheels[1]),
        (versions["1"], make_wheel("1", package="demo")),
        (versions["1"], make_sdist("1", package="demo")),
    ]
    port = make_coordinator(
        [dist for _, dist in rows], auto_metadata=True, package="demo"
    )
    provider = Provider(port, resolution_strategy=strategy)
    root_range = SpecifierSet("==2").to_range()
    queries: list[Version] = []
    original = VersionRange.__contains__

    def contains(self: VersionRange, version: Version) -> bool:
        if self is root_range:
            queries.append(version)
        return original(self, version)

    monkeypatch.setattr(VersionRange, "__contains__", contains)
    prefetch_root_batch(provider, "demo", rows, root_range)
    expected_versions = [versions[text] for text in ("3", "2", "1")]
    expected_wheels = wheels
    if strategy is ResolutionStrategy.LOWEST:
        expected_versions.reverse()
        expected_wheels = list(reversed(wheels))

    assert queries == expected_versions
    assert port.calls_to("request_metadata_batch") == [
        (
            [
                ("demo", wheel.version, wheel.metadata_url, wheel.metadata_hash)
                for wheel in expected_wheels
            ],
        )
    ]


def test_nonconsecutive_and_distinct_equal_versions_are_rechecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = [Version(text) for text in ("1", "2", "1", "1.0")]
    rows = [(version, make_wheel(str(version), package="demo")) for version in versions]
    port = make_coordinator(
        [dist for _, dist in rows], auto_metadata=True, package="demo"
    )
    provider = Provider(port)
    root_range = SpecifierSet("<1").to_range()
    queries: list[Version] = []
    original = VersionRange.__contains__

    def contains(self: VersionRange, version: Version) -> bool:
        if self is root_range:
            queries.append(version)
        return original(self, version)

    monkeypatch.setattr(VersionRange, "__contains__", contains)
    prefetch_root_batch(provider, "demo", rows, root_range)

    assert len(queries) == len(versions)
    assert all(
        actual is expected for actual, expected in zip(queries, versions, strict=True)
    )
    assert port.calls_to("request_metadata_batch") == []


def test_equal_versions_can_have_distinct_literal_membership() -> None:
    wheels = [make_wheel(text, package="demo") for text in ("1", "1.0")]
    rows = [(Version(wheel.version), wheel) for wheel in wheels]
    port = make_coordinator(wheels, auto_metadata=True, package="demo")
    provider = Provider(port)
    root_range = SpecifierSet("===1.0").to_range()
    prefetch_root_batch(provider, "demo", rows, root_range)
    wanted = wheels[1]

    assert port.calls_to("request_metadata_batch") == [
        ([("demo", "1.0", wanted.metadata_url, wanted.metadata_hash)],)
    ]
