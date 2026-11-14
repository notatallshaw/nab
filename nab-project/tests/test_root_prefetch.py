"""Known root constraints filter guesses while required requests remain available."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import httpx
import respx

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_project.fetch import FetchCoordinator
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.testing import make_metadata
from nab_resolver.resolver import Resolver


def _routes(router: respx.Router) -> tuple[respx.Route, respx.Route]:
    """Serve two valid releases, keeping their distinct sidecars observable."""
    files = []
    routes = []
    for version in ("1.0", "2.0"):
        url = f"https://files.example/pkg-{version}-py3-none-any.whl"
        metadata = make_metadata("pkg", version).encode()
        files.append(
            {
                "filename": f"pkg-{version}-py3-none-any.whl",
                "url": url,
                "hashes": {"sha256": "a" * 64},
                "core-metadata": {"sha256": hashlib.sha256(metadata).hexdigest()},
            }
        )
        routes.append(
            router.get(url + ".metadata").mock(
                return_value=httpx.Response(200, content=metadata)
            )
        )
    router.get("https://pypi.org/simple/pkg/").mock(
        return_value=httpx.Response(200, json={"files": files})
    )
    return routes[0], routes[1]


def test_root_pin_does_not_prefetch_an_excluded_release() -> None:
    with respx.mock(assert_all_called=False) as router:
        needed, excluded = _routes(router)
        roots = {"pkg": SpecifierSet("==1.0").to_range()}
        with FetchCoordinator(HttpxAsyncTransport()) as coordinator:
            provider = Provider(coordinator, root_requirements=roots)
            resolver = Resolver(
                provider, range_type=VersionRange, root_version=Version("0")
            )
            result = resolver.solve(roots)
            assert result.pins == {"pkg": Version("1.0")}

    assert needed.call_count == 1
    assert not excluded.called


def test_speculative_filters_are_snapshots_and_do_not_filter_demand() -> None:
    with respx.mock(assert_all_called=False) as router:
        needed, excluded = _routes(router)
        filters: dict[str, Callable[[str], bool]] = {"pkg": lambda _: False}
        with FetchCoordinator(HttpxAsyncTransport()) as coordinator:
            coordinator.set_metadata_prefetch_filters(filters)
            filters.clear()
            assert coordinator.request_listing("pkg").wait(timeout=5)
            url = "https://files.example/pkg-1.0-py3-none-any.whl.metadata"
            assert coordinator.request_metadata("pkg", "1.0", url).wait(timeout=5)
            assert coordinator.index.has_metadata("pkg", "1.0", url)

    assert needed.call_count == 1
    assert not excluded.called
