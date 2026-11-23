"""Real index records and metadata for yanking infrastructure and provider tests."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from packaging.utils import canonicalize_name

from nab_provider.pep508 import parse_requirement
from nab_provider.testing import make_metadata, make_wheel
from nab_provider.yanking import mark_yanked
from nab_test_support.coordinator import make_coordinator

if TYPE_CHECKING:
    from nab_provider.testing import FakeFetchPort


ReleaseRow = tuple[str, str, bool, Sequence[str]]
ReleaseExtras = Mapping[tuple[str, str, bool], tuple[str, ...]]


def make_release_coordinator(
    releases: Sequence[ReleaseRow], provided_extras: ReleaseExtras | None = None
) -> FakeFetchPort:
    """Serve metadata by artifact URL, including withdrawals and declared extras."""
    listings = defaultdict(list)
    metadata = {}
    for raw_name, version, yanked, dependencies in releases:
        name = canonicalize_name(raw_name)
        file = make_wheel(
            version,
            package=name,
            url=f"https://example.test/{name}-{version}-{yanked}.whl",
            hashes=(("sha256", "a" * 64),),
        )
        record = mark_yanked(file, reason="withdrawn") if yanked else file
        listings[name].append(record)
        metadata[file.metadata_url] = make_metadata(
            name,
            version,
            *dependencies,
            metadata_version="2.2",
            provides_extra=(provided_extras or {}).get((name, version, yanked), ()),
        )

    # A dependency with no releases has an empty index listing.
    for _name, _version, _yanked, dependencies in releases:
        for dependency in dependencies:
            listings.setdefault(
                canonicalize_name(parse_requirement(dependency).name), []
            )
    return make_coordinator(listings=listings, metadata_by_url=metadata)
