"""Operation settings override backend advice without changing foreign clients."""

from __future__ import annotations

import pytest

from nab_index.transport import HttpResponse
from nab_project.fetch import FetchCoordinator, FetchKind, FetchRequest
from nab_project.resolve import resolve_with_coordinator
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider.fetch_port import PrefetchSettings
from nab_provider.provider import Provider
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata, make_wheel


class BareTransport:
    """A custom transport with no performance recommendations."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        msg = "this test must not reach the network"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        pass


class TunedTransport(BareTransport):
    """Custom advice with the same interface as a built-in transport."""

    def __init__(self, settings: PrefetchSettings) -> None:
        self.prefetch_settings = settings


def test_foreign_transport_keeps_existing_defaults() -> None:
    coord = FetchCoordinator(BareTransport())

    assert coord.prefetch_settings.metadata_versions is None
    assert coord.prefetch_settings.lookahead_versions is None
    assert coord._max_concurrency == 50


@pytest.mark.parametrize("override_metadata", [None, 0, 4])
@pytest.mark.parametrize("override_lookahead", [None, 0, 8])
def test_caller_overrides_each_backend_window_and_snapshots_it(
    override_metadata: int | None, override_lookahead: int | None
) -> None:
    backend = PrefetchSettings(metadata_versions=2, lookahead_versions=3)
    override = PrefetchSettings(
        metadata_versions=override_metadata, lookahead_versions=override_lookahead
    )
    coord = FetchCoordinator(
        TunedTransport(backend), prefetch_settings=override, max_concurrency=7
    )
    expected_metadata = 2 if override_metadata is None else override_metadata
    expected_lookahead = 3 if override_lookahead is None else override_lookahead

    assert coord.prefetch_settings.metadata_versions == expected_metadata
    assert coord.prefetch_settings.lookahead_versions == expected_lookahead
    assert coord._max_concurrency == 7

    backend.metadata_versions = 99
    backend.lookahead_versions = 99
    override.metadata_versions = 99
    override.lookahead_versions = 99

    assert coord.prefetch_settings.metadata_versions == expected_metadata
    assert coord.prefetch_settings.lookahead_versions == expected_lookahead


def test_backend_settings_without_an_override_are_snapshotted() -> None:
    settings = PrefetchSettings(metadata_versions=0, lookahead_versions=12)
    coord = FetchCoordinator(TunedTransport(settings))
    settings.lookahead_versions = 99

    assert coord.prefetch_settings.metadata_versions == 0
    assert coord.prefetch_settings.lookahead_versions == 12


@pytest.mark.parametrize("window", [None, 0, 2])
def test_listing_tail_obeys_the_operation_metadata_window(
    monkeypatch: pytest.MonkeyPatch, window: int | None
) -> None:
    coord = FetchCoordinator(
        BareTransport(), prefetch_settings=PrefetchSettings(metadata_versions=window)
    )
    queued: list[object] = []
    monkeypatch.setattr(coord, "_submit", queued.append)
    coord._prefetch_metadata_after_listing(
        "pkg", [make_wheel(str(i)) for i in range(1, 5)]
    )
    versions = {item.version for item in queued if isinstance(item, FetchRequest)}

    assert versions == (
        {"4"} if window is None else {str(i) for i in range(5 - window, 5)}
    )


@pytest.mark.parametrize("override", [None, 0])
def test_real_resolve_forwards_backend_advice_and_explicit_zero(
    monkeypatch: pytest.MonkeyPatch, override: int | None
) -> None:
    hint = 3 * Provider.PREFETCH_BATCH
    versions = hint + 3
    coord = FetchCoordinator(TunedTransport(PrefetchSettings(lookahead_versions=hint)))
    coord.index.store_listing(
        "pkg", [make_wheel(str(i)) for i in range(1, versions + 1)]
    )
    coord.index.store_listing("dep", [make_wheel("2", package="dep")])
    for package in ("pkg", "dep"):
        coord.index.store_listing_index(package, coord.indexes[0].name)
    requested: list[tuple[str, str]] = []

    def serve(item: FetchRequest | list[FetchRequest] | None) -> None:
        """Fulfill metadata queue items through the real shared store."""
        assert item is not None
        requests = item if isinstance(item, list) else [item]
        for request in requests:
            assert request.kind is FetchKind.METADATA
            assert request.version is not None
            requested.append((request.package, request.version))
            deps = (
                ()
                if request.package == "dep"
                else ("dep==1" if request.version == str(versions) else "dep==2",)
            )
            coord.index.store_metadata(
                request.package,
                request.version,
                make_metadata(request.package, request.version, *deps),
                metadata_url=request.url,
            )

    monkeypatch.setattr(coord, "_submit", serve)
    result = resolve_with_coordinator(
        coord,
        [ResolveTarget.for_host_python("3.12.0")],
        [Requirement("pkg"), Requirement("dep==2")],
        lookahead_versions=override,
    )
    result.raise_for_failure()

    assert str(result.target_results[0].pins["pkg"]) == str(versions - 1)

    expected = hint if override is None else 1 + 2 * Provider.PREFETCH_BATCH

    assert sum(name == "pkg" for name, _ in requested) == expected
