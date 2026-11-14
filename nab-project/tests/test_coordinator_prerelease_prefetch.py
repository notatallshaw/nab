"""Deferred guesses use actual positive ranges without changing selection."""

from __future__ import annotations

import gc
import threading
import weakref
from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_project.fetch import FetchCoordinator
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.fetch_port import PrefetchSettings
from nab_provider.provider import Provider
from nab_provider.testing import make_metadata, make_wheel

if TYPE_CHECKING:
    from collections.abc import Generator


class CustomProvider(Provider):
    """Keep the speculative hooks of existing provider subclasses."""


@contextmanager
def metadata_index(
    versions: list[str],
) -> Generator[dict[str, respx.Route], None, None]:
    """Serve real Core Metadata for each wheel in a controlled Simple listing."""
    files = []
    routes = {}
    with respx.mock(assert_all_called=False) as router:
        for version in versions:
            wheel = make_wheel(version)
            files.append(
                {"filename": wheel.filename, "url": wheel.url, "core-metadata": True}
            )
            routes[version] = router.get(wheel.url + ".metadata").mock(
                return_value=httpx.Response(
                    200, text=make_metadata("pkg", version, provides_extra=("feature",))
                )
            )
        router.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json={"files": files})
        )
        yield routes


def resolve_metadata(
    versions: list[str],
    specifier: str,
    *,
    prereleases: bool | None = None,
    settings: PrefetchSettings | None = None,
    provider_type: type[Provider] = Provider,
) -> tuple[Version, dict[str, int], list[str]]:
    """Read actual sidecars and retain requests queued before dependencies."""
    requested: list[str] = []
    with (
        metadata_index(versions) as routes,
        FetchCoordinator(
            HttpxAsyncTransport(), prefetch_settings=settings
        ) as coordinator,
    ):
        original = coordinator.request_metadata

        def request(
            package: str,
            version: str,
            url: str,
            metadata_hash: tuple[str, str] | None = None,
        ) -> threading.Event:
            requested.append(version)
            return original(package, version, url, metadata_hash)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(coordinator, "request_metadata", request)
            provider = provider_type(coordinator)
            selected = provider.choose_version(
                "pkg", SpecifierSet(specifier, prereleases=prereleases).to_range()
            )
            prefetched = list(requested)
            assert selected is not None
            provider.get_dependencies("pkg", selected)

    return (
        selected,
        {version: route.call_count for version, route in routes.items()},
        prefetched,
    )


def test_explicit_prefetch_window_retains_prerelease_guess() -> None:
    selected, requests, _ = resolve_metadata(
        ["1.0", "2.0b1"], "", settings=PrefetchSettings(metadata_versions=1)
    )
    assert selected == Version("1.0")
    assert requests == {"1.0": 1, "2.0b1": 1}


def test_custom_provider_keeps_existing_guesses() -> None:
    selected, requests, _ = resolve_metadata(
        ["1.0", "2.0b1"], "", provider_type=CustomProvider
    )
    assert selected == Version("1.0")
    assert requests == {"1.0": 1, "2.0b1": 1}


def test_deferral_callback_does_not_retain_provider() -> None:
    with FetchCoordinator(HttpxAsyncTransport(), start_on_demand=True) as coordinator:
        provider = Provider(coordinator)
        defer = coordinator._metadata_prefetch_defer
        assert defer is not None
        reference = weakref.ref(provider)
        del provider
        gc.collect()

        assert reference() is None
        assert not defer("1.0")
        assert defer("2.0rc1")


@pytest.mark.parametrize(
    ("latest", "window", "expected"),
    [("2.0", None, ["2.0"]), ("2.0b1", None, []), ("2.0b1", 1, ["2.0b1"])],
)
def test_listing_tail_respects_provider_deferral_without_expanding_window(
    monkeypatch: pytest.MonkeyPatch,
    latest: str,
    window: int | None,
    expected: list[str],
) -> None:
    requested: list[str] = []

    def request(
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> threading.Event:
        requested.append(version)
        return threading.Event()

    coordinator = FetchCoordinator(
        HttpxAsyncTransport(),
        prefetch_settings=PrefetchSettings(metadata_versions=window),
        start_on_demand=True,
    )
    try:
        Provider(coordinator)
        monkeypatch.setattr(coordinator, "request_metadata", request)

        # No root requirement filters this transitive listing before deferral.
        coordinator._run_listing_tail("pkg", [make_wheel("1.0"), make_wheel(latest)])
    finally:
        coordinator.shutdown()

    assert requested == expected


@pytest.mark.parametrize(("specifier", "expected"), [("", "1.0"), (">=2.0b1", "2.0b1")])
def test_coordinator_prefetches_the_admitted_version(
    specifier: str, expected: str
) -> None:
    selected, requests, prefetched = resolve_metadata(["1.0", "2.0b1"], specifier)
    assert selected == Version(expected)
    assert expected in prefetched
    assert requests == {version: int(version == expected) for version in requests}
