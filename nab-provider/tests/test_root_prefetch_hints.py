"""Root version hints retain range semantics without retaining a provider."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable, Mapping

import pytest

from nab_provider._provider.metadata_resolver import pick_metadata_wheel
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.store import InMemoryIndex
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import FakeFetchPort, make_coordinator, make_wheel


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


class ArtifactHintPort(FakeFetchPort):
    """Support artifact hints independently of root-version hints."""

    def __init__(self) -> None:
        super().__init__(
            InMemoryIndex(),
            serve_metadata=_no_result,
            serve_sdist=_no_result,
            serve_range=_no_result,
            serve_archive=_no_result,
        )
        self.selector: Callable[[list[WheelFile]], WheelFile] | None = None

    def set_metadata_prefetch_selector(
        self, selector: Callable[[list[WheelFile]], WheelFile]
    ) -> None:
        self.selector = selector


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


def test_artifact_selector_keeps_target_context_without_retaining_provider() -> None:
    port = ArtifactHintPort()
    wheels = [
        make_wheel(tag="cp310-none-any"),
        make_wheel(tag="cp312-none-any"),
    ]
    first = Provider(
        port,
        ResolveTarget.for_declared(
            python_version="3.10", spec=PlatformSpec("linux_x86_64")
        ),
    )
    earlier_selector = port.selector
    assert earlier_selector is not None
    assert earlier_selector(wheels) is wheels[0]

    reference = weakref.ref(first)
    del first
    gc.collect()
    assert reference() is None

    Provider(
        port,
        ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        ),
    )
    assert port.selector is not None
    assert port.selector(wheels) is wheels[1]
    assert earlier_selector(wheels) is wheels[0]


def test_metadata_wheel_selection_requires_candidates() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        pick_metadata_wheel([], None)
    wheel = make_wheel()
    assert pick_metadata_wheel([wheel], None) is wheel
