"""Provider-owned deferral follows admission without widening speculation."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.policy import ResolutionStrategy
from nab_provider.provider import Provider
from nab_provider.store import InMemoryIndex
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)


def no_result(*_args: str) -> None:
    """These cases read only wheel sidecars."""


def serve_metadata(package: str, version: str, _url: str) -> str:
    """Serve real Core Metadata through the native fake fetch boundary."""
    return make_metadata(package, version, provides_extra=("feature",))


class DeferringPort(FakeFetchPort):
    """Offer automatic deferral while retaining the native fetch implementation."""

    def __init__(self, versions: list[str]) -> None:
        index = InMemoryIndex()
        index.store_listing("pkg", [make_wheel(version) for version in versions])
        super().__init__(
            index,
            serve_metadata=serve_metadata,
            serve_sdist=no_result,
            serve_range=no_result,
            serve_archive=no_result,
        )
        index.store_listing_index("pkg", self.indexes[0].name)
        self.defer: Callable[[str], bool] | None = None

    def set_metadata_prefetch_deferral(
        self, defer: Callable[[str], bool] | None
    ) -> bool:
        self.defer = defer
        return defer is not None


@pytest.mark.parametrize(
    ("versions", "specifier", "prereleases", "expected"),
    [
        (["1.0", "2.0b1"], "", None, "1.0"),
        (["1.0", "2.0b1"], ">=2.0b1", None, "2.0b1"),
        (["1.0", "2.0b1"], "", True, "2.0b1"),
        (["2.0b1"], "", None, "2.0b1"),
        (["1.0", "2.0b1"], "", False, "1.0"),
    ],
)
def test_positive_range_prefetches_the_admitted_version(
    versions: list[str],
    specifier: str,
    prereleases: bool | None,
    expected: str,
) -> None:
    port = DeferringPort(versions)
    provider = Provider(port)
    selected = provider.choose_version(
        "pkg", SpecifierSet(specifier, prereleases=prereleases).to_range()
    )
    assert selected == Version(expected)
    assert [call[1] for call in port.calls_to("request_metadata")] == [expected]

    provider.get_dependencies("pkg", selected)
    assert [call[1] for call in port.calls_to("request_metadata")] == [expected]


def test_explicit_refusal_does_not_guess_metadata() -> None:
    port = DeferringPort(["2b1"])
    provider = Provider(port)
    assert (
        provider.choose_version("pkg", SpecifierSet("", prereleases=False).to_range())
        is None
    )
    assert port.calls_to("request_metadata") == []


@pytest.mark.parametrize(("specifier", "expected"), [("", "1"), (">=0.9a1", "0.9a1")])
def test_lowest_uses_the_demand_admission_order(specifier: str, expected: str) -> None:
    port = DeferringPort(["0.9a1", "1", "2b1"])
    provider = Provider(port, resolution_strategy=ResolutionStrategy.LOWEST)
    selected = provider.choose_version("pkg", SpecifierSet(specifier).to_range())
    assert selected == Version(expected)
    assert [call[1] for call in port.calls_to("request_metadata")] == [expected]


def test_tightening_constraints_keeps_one_initial_guess() -> None:
    port = DeferringPort(["1", "2", "3", "4b1"])
    provider = Provider(port)
    versions = provider.fetch_versions("pkg")
    assert port.calls_to("request_metadata") == []

    provider.prefetch_in_range("pkg", versions, SpecifierSet(">=1").to_range())
    provider.prefetch_in_range("pkg", versions, SpecifierSet("<3").to_range())
    assert [call[1] for call in port.calls_to("request_metadata")] == ["3"]

    selected = provider.choose_version("pkg", SpecifierSet("<2").to_range())
    assert selected == Version("1")
    provider.get_dependencies("pkg", selected)
    assert [call[1] for call in port.calls_to("request_metadata")] == ["3", "1"]


def test_extras_proxy_obeys_base_prerelease_admission() -> None:
    port = DeferringPort(["1", "2b1"])
    provider = Provider(port)
    provider.receive_partial_solution_hint(
        {"pkg": SpecifierSet(">=2b1").to_range()}, {}
    )
    extra_range = SpecifierSet("").to_range()
    provider.prioritize("pkg[feature]", extra_range, {})
    assert provider.choose_version("pkg[feature]", extra_range) == Version("2b1")
    assert [call[1] for call in port.calls_to("request_metadata")] == ["2b1"]


def test_legacy_port_keeps_existing_guesses() -> None:
    port = make_coordinator(
        [make_wheel("1.0"), make_wheel("2.0b1")], auto_metadata=True
    )
    provider = Provider(port)
    selected = provider.choose_version("pkg", SpecifierSet("").to_range())
    assert selected == Version("1.0")
    provider.get_dependencies("pkg", selected)
    assert [call[1] for call in port.calls_to("request_metadata")] == ["2.0b1", "1.0"]


def test_default_deferral_declines_prereleases_and_invalid_versions() -> None:
    port = DeferringPort([])
    Provider(port)
    assert port.defer is not None
    assert not port.defer("1.0")
    assert port.defer("2.0rc1")
    assert port.defer("not-a-version")
