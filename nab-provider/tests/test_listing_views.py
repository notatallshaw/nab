"""Lazy listing views preserve selection and listing order."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from nab_provider._provider import metadata_resolver
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import IndexAccessError
from nab_provider.provider import Provider
from nab_provider.records import SdistFile, WheelFile
from nab_provider.tags import TagSet
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator


def wheel(version: str, build: int = 0) -> WheelFile:
    """Return a wheel whose build number distinguishes siblings."""
    filename = f"foo-{version}-{build}-py3-none-any.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


@pytest.fixture
def provider_listing() -> tuple[Provider, list[tuple[Version, WheelFile | SdistFile]]]:
    """Build an owned listing with singleton and sibling releases."""
    files = [wheel("3.0"), wheel("3.0", 1), wheel("2.0"), wheel("2.0", 1), wheel("1.0")]
    provider = Provider(make_coordinator(files, package="foo"))
    return provider, provider.fetch_versions("foo")


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_equal_keys_and_out_of_order_reads(
    provider_listing: tuple[Provider, list[tuple[Version, WheelFile | SdistFile]]],
    order: tuple[int, ...],
) -> None:
    provider, listing = provider_listing
    versions = list(dict.fromkeys(version for version, _ in listing))
    lazy = metadata_resolver.version_dists(provider, "foo", listing)
    eager = metadata_resolver.version_dists(provider, "foo", list(listing))

    for offset in order:
        key = Version(str(versions[offset]))
        assert lazy.picked[key] is eager.picked[key]
        assert lazy.sibling_wheels.get(key) == eager.sibling_wheels.get(key)

    assert list(lazy.picked) == versions
    assert list(lazy.sibling_wheels) == [Version("3.0"), Version("2.0")]
    assert len(lazy.sibling_wheels) == 2


def test_external_unsorted_listing_groups_nonadjacent_versions(
    provider_listing: tuple[Provider, list[tuple[Version, WheelFile | SdistFile]]],
) -> None:
    provider, listing = provider_listing
    external = [listing[2], listing[0], listing[4], listing[3], listing[1]]
    indexed = metadata_resolver.version_dists(provider, "foo", external)

    assert indexed.picked[Version("3.0")] is listing[0][1]
    assert indexed.picked[Version("2.0")] is listing[2][1]
    assert indexed.sibling_wheels[Version("3.0")] == [listing[0][1], listing[1][1]]
    assert indexed.sibling_wheels[Version("2.0")] == [listing[2][1], listing[3][1]]


def test_completed_sibling_count_does_not_hash_versions(
    provider_listing: tuple[Provider, list[tuple[Version, WheelFile | SdistFile]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, listing = provider_listing
    indexed = metadata_resolver.version_dists(provider, "foo", listing)
    assert len(indexed.sibling_wheels) == 2
    calls = 0
    original_hash = Version.__hash__

    def count_hash(version: Version) -> int:
        nonlocal calls
        calls += 1
        return original_hash(version)

    monkeypatch.setattr(Version, "__hash__", count_hash)
    assert len(indexed.sibling_wheels) == 2
    assert calls == 0


def test_empty_views_and_missing_keys() -> None:
    provider = Provider(make_coordinator([], package="foo"))
    listing = provider.fetch_versions("foo")
    indexed = metadata_resolver.version_dists(provider, "foo", listing)

    assert not indexed.picked
    assert not indexed.sibling_wheels
    assert list(indexed.picked) == []
    assert list(indexed.sibling_wheels) == []
    assert indexed.picked.get(Version("1")) is None
    assert indexed.sibling_wheels.get(Version("1")) is None


@pytest.mark.parametrize("as_sdist", [False, True])
def test_singleton_needs_no_selection(
    monkeypatch: pytest.MonkeyPatch, *, as_sdist: bool
) -> None:
    dist = (
        SdistFile(
            "foo-1.0.tar.gz", "https://example.com/foo-1.0.tar.gz", "1.0", None, None
        )
        if as_sdist
        else wheel("1.0")
    )
    provider = Provider(make_coordinator([dist], package="foo"))
    listing = provider.fetch_versions("foo")
    calls = 0
    original_pick = metadata_resolver.pick_dist

    def count_pick(
        dists: Sequence[WheelFile | SdistFile],
        tags: TagSet | None,
        target: ResolveTarget | None = None,
        wheels: list[WheelFile] | None = None,
    ) -> WheelFile | SdistFile:
        nonlocal calls
        calls += 1
        return original_pick(dists, tags, target, wheels)

    monkeypatch.setattr(metadata_resolver, "pick_dist", count_pick)
    indexed = metadata_resolver.version_dists(provider, "foo", listing)
    assert indexed.picked[Version("1.0")] is listing[0][1]
    assert len(indexed.sibling_wheels) == 0
    assert calls == 0


def test_listing_request_must_publish_a_result() -> None:
    provider = Provider(make_coordinator())
    with pytest.raises(IndexAccessError, match="Listing request for 'some-pkg'"):
        provider.fetch_versions("Some_Pkg")

    error = IndexAccessError("index unavailable")
    provider.coordinator.index.store_listing_error("some-pkg", error)
    with pytest.raises(IndexAccessError) as caught:
        provider.fetch_versions("Some_Pkg")
    assert caught.value is error

    provider.coordinator.index.store_listing("some-pkg", [])
    assert provider.fetch_versions("Some_Pkg") == []
