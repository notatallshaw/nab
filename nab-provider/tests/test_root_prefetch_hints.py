"""Root version hints retain range semantics without retaining a provider."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable, Mapping

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider.provider import Provider
from nab_provider.store import InMemoryIndex
from nab_provider.testing import FakeFetchPort, make_coordinator


def _no_result(*_args: str) -> None:
    """These probes request listings but never ask the port to serve metadata."""


class HintPort(FakeFetchPort):
    """Record the optional hint capability while retaining real fake-port behavior."""

    def __init__(self) -> None:
        super().__init__(
            InMemoryIndex(),
            serve_metadata=_no_result,
            serve_sdist=_no_result,
            serve_range=_no_result,
            serve_archive=_no_result,
        )
        self.filters: dict[str, Callable[[str], bool]] = {}

    def set_metadata_prefetch_filters(
        self, filters: Mapping[str, Callable[[str], bool]]
    ) -> None:
        self.filters = dict(filters)


def test_normalized_roots_combine_constraints_without_retaining_provider() -> None:
    port = HintPort()
    provider = Provider(
        port,
        root_requirements={
            "Foo": SpecifierSet(">=1").to_range(),
            "foo[speed]": SpecifierSet("<2").to_range(),
        },
    )
    assert set(port.filters) == {"foo"}
    allowed = port.filters["foo"]
    assert allowed("1.5")
    assert allowed("1.5+vendor")
    assert allowed("1.6rc1")
    assert not allowed("0.5")
    assert not allowed("2.0")
    assert not allowed("not-a-version")

    reference = weakref.ref(provider)
    del provider
    gc.collect()
    assert reference() is None
    assert allowed("1.5")


def test_new_provider_replaces_previous_root_filters() -> None:
    port = HintPort()
    Provider(port, root_requirements={"first": SpecifierSet("==1").to_range()})
    Provider(port, root_requirements={"second": VersionRange.full()})
    assert set(port.filters) == {"second"}
    assert port.filters["second"]("3.0")


@pytest.mark.parametrize("prereleases", [None, False, True])
def test_hints_preserve_configured_prerelease_policy(prereleases: bool | None) -> None:
    port = HintPort()
    version_range = SpecifierSet(">=1,<2", prereleases=prereleases).to_range()
    Provider(port, root_requirements={"foo": version_range})
    assert port.filters["foo"]("1.5")
    assert port.filters["foo"]("1.6rc1") is (prereleases is not False)


def test_host_without_optional_hints_keeps_requesting_root_listings() -> None:
    port = make_coordinator([])
    Provider(port, root_requirements={"foo": SpecifierSet("==1").to_range()})
    assert port.calls_to("request_listing")
