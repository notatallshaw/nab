"""Listing guesses use provider ranking without expanding their version window."""

from __future__ import annotations

import hashlib
import threading

import httpx
import pytest
import respx

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_project.fetch import FetchCoordinator
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.fetch_port import PrefetchSettings
from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata, make_sdist, make_wheel
from nab_resolver.resolver import Resolver


def test_prefetch_matches_the_targets_required_artifact() -> None:
    target = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )
    files = []
    routes = []
    with respx.mock(assert_all_called=False) as router:
        for tag, dependencies in (
            ("cp310-cp310-macosx_10_9_x86_64", ("unused",)),
            ("cp312-cp312-manylinux_2_17_x86_64", ()),
        ):
            wheel = make_wheel("1", tag=tag)
            metadata = make_metadata("pkg", "1", *dependencies).encode()
            files.append(
                {
                    "filename": wheel.filename,
                    "url": wheel.url,
                    "hashes": {"sha256": "a" * 64},
                    "core-metadata": {"sha256": hashlib.sha256(metadata).hexdigest()},
                }
            )
            routes.append(
                router.get(wheel.url + ".metadata").mock(
                    return_value=httpx.Response(200, content=metadata)
                )
            )
        router.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json={"files": files})
        )

        roots = {"pkg": SpecifierSet("==1").to_range()}
        with FetchCoordinator(HttpxAsyncTransport()) as coordinator:
            provider = Provider(
                coordinator,
                target,
                root_requirements=roots,
                assume_consistent_metadata=True,
            )
            resolver = Resolver(
                provider, range_type=VersionRange, root_version=Version("0")
            )
            assert resolver.solve(roots).pins == {"pkg": Version("1")}

    assert not routes[0].called
    assert routes[1].call_count == 1


@pytest.mark.parametrize("allowed", [False, True])
def test_selector_only_sees_admitted_versions_in_the_existing_window(
    monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    first = make_wheel("2", tag="cp310-none-any")
    second = make_wheel("2", tag="cp312-none-any")
    files = [
        make_wheel("1"),
        first,
        second,
        make_wheel("3", has_metadata=False),
        make_sdist("4"),
    ]
    seen = []
    requested = []

    def select(wheels: list[WheelFile]) -> WheelFile:
        seen.append(tuple(wheels))
        return wheels[-1]

    def request(
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> threading.Event:
        requested.append((package, version, url, metadata_hash))
        return threading.Event()

    coordinator = FetchCoordinator(
        HttpxAsyncTransport(),
        prefetch_settings=PrefetchSettings(metadata_versions=1),
        start_on_demand=True,
    )
    coordinator.set_metadata_prefetch_selector(select)
    coordinator.set_metadata_prefetch_filters({"pkg": lambda _: allowed})
    monkeypatch.setattr(coordinator, "request_metadata", request)
    coordinator._prefetch_metadata_after_listing("pkg", files)

    assert seen == ([(first, second)] if allowed else [])
    assert requested == ([("pkg", "2", second.metadata_url, None)] if allowed else [])
