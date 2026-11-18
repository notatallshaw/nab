"""Requests passed from the synchronous coordinator to its fetcher thread."""

from __future__ import annotations

from typing import Final

from nab_provider.value import Record


class Listing(Record):
    """Read the available distributions for a package."""

    __slots__ = __match_args__ = ("package",)
    operation = "listing"

    def __init__(self, package: str) -> None:
        self.package: Final = package


class _FileRequest(Record):
    """Identify a distribution by its package, version and URL."""

    __slots__ = __match_args__ = ("package", "version", "url")

    def __init__(self, package: str, version: str, url: str) -> None:
        self.package: Final = package
        self.version: Final = version
        self.url: Final = url


class DirectArchive(_FileRequest):
    """Read a source archive by its declared URL."""

    __slots__ = ()
    operation = "direct-archive"


class SidecarMetadata(_FileRequest):
    """Read a metadata sidecar and verify its advertised digest."""

    __slots__ = ("metadata_hash",)
    __match_args__ = (*_FileRequest.__match_args__, "metadata_hash")
    operation = "metadata"

    def __init__(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(package, version, url)
        self.metadata_hash: Final = metadata_hash


class _IndexedDistribution(_FileRequest):
    """An indexed wheel or sdist and the hashes published for it."""

    __slots__ = ("hashes",)
    __match_args__ = (*_FileRequest.__match_args__, "hashes")

    def __init__(
        self,
        package: str,
        version: str,
        url: str,
        hashes: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(package, version, url)
        self.hashes: Final = hashes


class RangeMetadata(_IndexedDistribution):
    """Read metadata from a wheel using HTTP range requests."""

    __slots__ = ()
    operation = "range-metadata"


class SdistMetadata(_IndexedDistribution):
    """Read metadata and project declarations from an indexed source archive."""

    __slots__ = ()
    operation = "sdist"


class SdistArchive(_IndexedDistribution):
    """Read the raw indexed source archive for a build."""

    __slots__ = ()
    operation = "sdist-archive"


VersionedRequest = (
    SidecarMetadata | RangeMetadata | SdistMetadata | SdistArchive | DirectArchive
)
FetchRequest = Listing | VersionedRequest
