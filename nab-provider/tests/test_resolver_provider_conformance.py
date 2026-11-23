"""Run the generic provider contracts through real wheel metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from packaging.version import Version

from nab_provider.provider import Provider
from nab_provider.ranges import VersionRange
from nab_provider.testing import make_coordinator, make_metadata, make_wheel
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.testing import ProviderScenario, check_provider_scenarios

if TYPE_CHECKING:
    from nab_provider.records import WheelFile
    from nab_resolver.types import RangeProtocol

_PACKAGES = ("contractfirst", "contractsecond", "contractthird")
_VERSIONS = (Version("1"), Version("2"))
_SPECIFIERS = {
    (False, False): "<1",
    (True, False): "==1",
    (False, True): "==2",
    (True, True): "",
}


def _requirement(package: str, allowed: RangeProtocol[Version]) -> str:
    """Express membership in the two-version fixture as a standard requirement."""
    membership = (_VERSIONS[0] in allowed, _VERSIONS[1] in allowed)
    return package + _SPECIFIERS[membership]


def _provider(scenario: ProviderScenario[str, Version]) -> Provider:
    """Serve each independently supplied graph as wheel records and Core Metadata."""
    listings: dict[str, list[WheelFile]] = {package: [] for package in _PACKAGES}
    metadata: dict[str, str] = {}

    # Encode fixture facts before the provider interprets them.
    for package, versions in scenario.graph.items():
        for version, dependencies in versions.items():
            wheel = make_wheel(str(version), package=package)
            listings[package].append(wheel)
            requires = [
                _requirement(child, allowed) for child, allowed in dependencies.items()
            ]
            metadata[wheel.url + ".metadata"] = make_metadata(
                package, str(version), *requires
            )

    # Each solver receives its own decision state and fetch store.
    return Provider(make_coordinator(listings=listings, metadata_by_url=metadata))


def test_wheel_provider_satisfies_generic_contracts() -> None:
    check_provider_scenarios(
        _provider,
        packages=_PACKAGES,
        versions=_VERSIONS,
        range_type=VersionRange,
    )


def test_generic_range_reports_the_required_python_backend() -> None:
    provider = Provider(make_coordinator([make_wheel()], auto_metadata=True))
    foreign: Range[Version] = Range.full()
    with pytest.raises(TypeError) as caught:
        Resolver(provider).resolve({"pkg": foreign})
    message = str(caught.value)
    assert "'pkg'" in message
    assert "nab_resolver.ranges.Range" in message
    assert "nab_provider.ranges.VersionRange" in message
    assert "range_type=VersionRange" in message

    resolver = Resolver(provider, range_type=VersionRange, root_version="0")
    assert resolver.resolve({"pkg": VersionRange.full()}) == {"pkg": Version("1.0")}


def test_foreign_display_range_keeps_its_own_rendering() -> None:
    provider = Provider(make_coordinator([make_wheel()], auto_metadata=True))
    provider.fetch_versions("pkg")
    foreign = Range.singleton(Version("2"))
    assert provider.narrow_for_display("pkg", foreign) is foreign
    assert provider.format_range(foreign) == str(foreign)
