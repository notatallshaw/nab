"""Listing guesses respect declared Python support without restricting demand."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_project.fetch import FetchCoordinator
from nab_provider.fetch_port import PrefetchSettings
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata, make_wheel, pkg_override

if TYPE_CHECKING:
    from collections.abc import Generator


class CustomProvider(Provider):
    """Keep a subclass's established metadata guesses."""


@contextmanager
def python_index(
    bound: str | None = ">=3.11",
) -> Generator[dict[str, respx.Route], None, None]:
    """Serve actual metadata and a Simple listing with per-file Python bounds."""
    routes = {}
    files = []
    with respx.mock(assert_all_called=False) as router:
        for version, requirement in (("1.0", None), ("2.0", bound)):
            wheel = make_wheel(version)
            entry = {
                "filename": wheel.filename,
                "url": wheel.url,
                "core-metadata": True,
            }
            if requirement is not None:
                entry["requires-python"] = requirement
            files.append(entry)
            routes[version] = router.get(wheel.url + ".metadata").mock(
                return_value=httpx.Response(200, text=make_metadata("pkg", version))
            )
        router.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json={"files": files})
        )
        yield routes


@pytest.mark.parametrize(
    ("python", "expected", "counts"),
    [
        ("3.10", "1.0", {"1.0": 1, "2.0": 0}),
        ("3.11", "2.0", {"1.0": 0, "2.0": 1}),
        (None, "2.0", {"1.0": 0, "2.0": 1}),
    ],
)
def test_declared_python_skips_only_unusable_speculative_sidecar(
    python: str | None, expected: str, counts: dict[str, int]
) -> None:
    target = (
        None
        if python is None
        else ResolveTarget.for_declared(
            python_version=python, spec=PlatformSpec("linux_x86_64")
        )
    )
    with (
        python_index() as routes,
        FetchCoordinator(HttpxAsyncTransport()) as coordinator,
    ):
        provider = Provider(coordinator, target=target)
        selected = provider.choose_version("pkg", SpecifierSet("").to_range())
        assert selected is not None
        assert selected == Version(expected)
        provider.get_dependencies("pkg", selected)

    assert {version: route.call_count for version, route in routes.items()} == counts


@pytest.mark.parametrize(
    ("settings", "provider_type"),
    [(PrefetchSettings(metadata_versions=1), Provider), (None, CustomProvider)],
)
def test_explicit_windows_and_custom_providers_keep_python_guesses(
    settings: PrefetchSettings | None, provider_type: type[Provider]
) -> None:
    with (
        python_index() as routes,
        FetchCoordinator(
            HttpxAsyncTransport(), prefetch_settings=settings
        ) as coordinator,
    ):
        provider = provider_type(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.10", spec=PlatformSpec("linux_x86_64")
            ),
        )
        selected = provider.choose_version("pkg", SpecifierSet("").to_range())
        assert selected == Version("1.0")
        provider.get_dependencies("pkg", Version("1.0"))

    assert {version: route.call_count for version, route in routes.items()} == {
        "1.0": 1,
        "2.0": 1,
    }


def test_override_can_admit_the_newest_python_bound() -> None:
    with (
        python_index() as routes,
        FetchCoordinator(HttpxAsyncTransport()) as coordinator,
    ):
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.10", spec=PlatformSpec("linux_x86_64")
            ),
            package_overrides=(pkg_override("pkg", requires_python=">=3.8"),),
        )
        selected = provider.choose_version("pkg", SpecifierSet("").to_range())
        assert selected == Version("2.0")
        provider.get_dependencies("pkg", Version("2.0"))

    assert routes["2.0"].call_count == 1
    assert routes["1.0"].call_count == 0


@pytest.mark.parametrize("bound", [None, "", "invalid"])
def test_unknown_python_bounds_keep_the_default_guess(bound: str | None) -> None:
    with (
        python_index(bound) as routes,
        FetchCoordinator(HttpxAsyncTransport()) as coordinator,
    ):
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.10", spec=PlatformSpec("linux_x86_64")
            ),
        )
        assert provider.choose_version("pkg", SpecifierSet("").to_range()) == Version(
            "2.0"
        )

    assert routes["2.0"].call_count == 1


def test_declined_group_does_not_expand_the_existing_window(
    monkeypatch: pytest.MonkeyPatch,
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

    coordinator = FetchCoordinator(HttpxAsyncTransport(), start_on_demand=True)
    try:
        Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.10", spec=PlatformSpec("linux_x86_64")
            ),
        )
        monkeypatch.setattr(coordinator, "request_metadata", request)
        coordinator._run_listing_tail(
            "pkg", [make_wheel("1.0"), make_wheel("2.0", requires_python=">=3.11")]
        )
        assert requested == []

    finally:
        coordinator.shutdown()


def test_required_metadata_is_not_restricted_by_automatic_admission() -> None:
    with (
        python_index() as routes,
        FetchCoordinator(HttpxAsyncTransport()) as coordinator,
    ):
        Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.10", spec=PlatformSpec("linux_x86_64")
            ),
        )
        wheel = make_wheel("2.0", requires_python=">=3.11")
        coordinator._run_listing_tail("pkg", [make_wheel("1.0"), wheel])
        url = wheel.metadata_url
        assert url is not None

        # A later target can still require a sidecar this guess declined.
        event = coordinator.request_metadata("pkg", "2.0", url)
        assert event.wait(5)

    assert routes["2.0"].call_count == 1
    assert routes["1.0"].call_count == 0
