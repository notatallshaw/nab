"""Real index records and metadata for yanking infrastructure and provider tests."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from nab_project._testing.coordinator_fake import make_coordinator
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.pep508 import parse_requirement
from nab_provider.records import WheelFile
from nab_provider.yanking import mark_yanked

if TYPE_CHECKING:
    from nab_provider.testing import FakeFetchPort
ReleaseRow = tuple[str, str, bool, Sequence[str]]
Extras = Mapping[tuple[str, str, bool], tuple[str, ...]]


def graph_port(
    releases: Sequence[ReleaseRow], provided_extras: Extras | None = None
) -> FakeFetchPort:
    """Serve metadata by artifact URL, including withdrawals and declared extras."""
    listings = defaultdict(list)
    metadata = {}
    for raw_name, version, yanked, dependencies in releases:
        name = canonicalize_name(raw_name)
        distribution = name.replace("-", "_")
        file = WheelFile(
            filename=f"{distribution}-{version}-py3-none-any.whl",
            url=f"https://example.test/{name}-{version}-{yanked}.whl",
            version=version,
            requires_python=None,
            has_metadata=True,
            upload_time=None,
            hashes=(("sha256", "a" * 64),),
        )
        record = mark_yanked(file, reason="withdrawn") if yanked else file
        listings[name].append(record)
        metadata[file.metadata_url] = (
            f"Metadata-Version: 2.2\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {dep}\n" for dep in dependencies)
            + "".join(
                f"Provides-Extra: {extra}\n"
                for extra in (provided_extras or {}).get((name, version, yanked), ())
            )
            + "\n"
        )
    for _name, _version, _yanked, dependencies in releases:
        for dependency in dependencies:
            listings.setdefault(
                canonicalize_name(parse_requirement(dependency).name), []
            )
    return make_coordinator(listings=listings, metadata_by_url=metadata)
