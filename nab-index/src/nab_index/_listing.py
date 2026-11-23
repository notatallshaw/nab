"""File availability from each package's last completed index lookup."""

from __future__ import annotations

__lazy_modules__ = ("packaging.utils",)

from typing import TYPE_CHECKING, NamedTuple

from packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from packaging.utils import NormalizedName

    from nab_provider.records import SdistFile, WheelFile


class ListingDiagnostics(NamedTuple):
    """File availability reported by one completed index lookup."""

    unreadable_only: bool = False
    unreachable_only: bool = False
    all_yanked: bool = False
    no_usable_file: bool = False
    zip_sdists: frozenset[str] = frozenset()


_EMPTY_DIAGNOSTICS = ListingDiagnostics()


class ListingResult(NamedTuple):
    """The records and diagnostics from one index listing."""

    files: list[WheelFile | SdistFile]
    diagnostics: ListingDiagnostics = _EMPTY_DIAGNOSTICS


class ListingReports:
    """Expose file diagnostics without retaining every returned listing."""

    def __init__(self) -> None:
        """Start without a report for any package."""
        self._diagnostics: dict[NormalizedName, ListingDiagnostics] = {}

    def _publish_listing(
        self, canonical: NormalizedName, listing: ListingResult
    ) -> list[WheelFile | SdistFile]:
        """Publish the report only once the lookup is ready to return its files."""
        self._diagnostics[canonical] = listing.diagnostics
        return listing.files

    def _last_listing(self, package: str) -> ListingDiagnostics:
        """Return the latest report for this package."""
        return self._diagnostics.get(canonicalize_name(package), _EMPTY_DIAGNOSTICS)

    def served_unreadable_only(self, package: str) -> bool:
        """Whether the last listing for ``package`` held only files nab cannot read."""
        return self._last_listing(package).unreadable_only

    def served_unreachable_only(self, package: str) -> bool:
        """Whether the last listing for ``package`` held only links nab cannot reach."""
        return self._last_listing(package).unreachable_only

    def served_no_usable_file(self, package: str) -> bool:
        """Whether the last listing for ``package`` named files and nab kept none."""
        return self._last_listing(package).no_usable_file

    def served_all_yanked(self, package: str) -> bool:
        """Whether the last listing had files and every one was yanked."""
        return self._last_listing(package).all_yanked

    def served_zip_sdists(self, package: str) -> frozenset[str]:
        """Versions ``package`` was served as a ``.zip`` sdist."""
        return self._last_listing(package).zip_sdists
