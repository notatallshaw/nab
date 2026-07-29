"""Tests for the decision scan's frozen view of listing arrival.

``choose_package_to_decide`` builds one sort key per undecided package out of
``prioritize`` and ``is_ready``, and both halves read whether a listing has
landed in the coordinator index.  The fetcher thread writes there while the
scan runs, so without a per-scan freeze two halves of one key, or two keys
compared against each other, can answer from different moments.

The index double below lands a listing on the first read that asks for it,
which is the race a traced resolve caught: the listing arrives between
``prioritize`` and ``is_ready`` for one package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_index.client import WheelFile
from nab_python._provider.priority import _NO_LISTING_PRIOR
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.version import Version
from nab_python.fetch import InMemoryIndex
from nab_python.provider import Provider
from nab_resolver import decide
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from unittest.mock import MagicMock

    from nab_index.client import SdistFile
    from nab_resolver.types import RangeProtocol


def _wheel(package: str, version: str = "1.0") -> WheelFile:
    """Build a WheelFile for ``package``."""
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.com/{package}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


class _ArrivesOnReadIndex(InMemoryIndex):
    """Index whose queued listings land on the first read that asks for them.

    Stands in for the fetcher thread publishing a listing in the middle of a
    decision scan: the read that first misses is the one that makes the next
    read hit.
    """

    def __init__(self, arrivals: Mapping[str, Sequence[WheelFile | SdistFile]]) -> None:
        super().__init__()
        self._arrivals = dict(arrivals)

    def get_listing(self, package: str) -> list[WheelFile | SdistFile] | None:
        listing = super().get_listing(package)
        queued = self._arrivals.pop(package, None)
        if queued is not None:
            self.store_listing(package, queued)
        return listing


class _ScanRecordingProvider(Provider):
    """Records both halves of every sort key a scan builds, in scan order."""

    def __init__(self, coordinator: MagicMock) -> None:
        super().__init__(coordinator)
        self.scan_reads: list[tuple[str, int, bool]] = []
        self._matching: dict[str, int] = {}

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int, bool]:
        priority = super().prioritize(
            package, version_range, conflict_counts, culprit_counts
        )
        self._matching[package] = priority[1]
        return priority

    def is_ready(self, package: str) -> bool:
        ready = super().is_ready(package)
        self.scan_reads.append((package, self._matching[package], ready))
        return ready


def _coordinator(
    arrivals: Mapping[str, Sequence[WheelFile | SdistFile]],
    resident: Mapping[str, Sequence[WheelFile | SdistFile]] | None = None,
) -> MagicMock:
    """Coordinator whose index holds ``resident`` and queues ``arrivals``."""
    coordinator = make_coordinator(None)
    index = _ArrivesOnReadIndex(arrivals)
    for package, files in (resident or {}).items():
        index.store_listing(package, files)
    coordinator.index = index
    return coordinator


def _cause() -> Incompatibility[str, Version]:
    return Incompatibility(
        [Term("root", VersionRange.full(), positive=True)],
        cause=IncompatibilityCause.DEPENDENCY,
    )


class TestOneKeyHoldsTogether:
    """A listing landing between the two halves of a key is not seen by either."""

    def test_priority_and_readiness_agree_within_a_scan(self) -> None:
        """The half that misses first makes the other half miss too."""
        coordinator = _coordinator({"foo": [_wheel("foo")]})
        provider = Provider(coordinator)
        rng = VersionRange.full()

        provider.begin_decision_scan()
        priority = provider.prioritize("foo", rng, {})

        assert priority[1] == _NO_LISTING_PRIOR
        assert provider.is_ready("foo") is False

    def test_readiness_holds_for_the_rest_of_the_scan(self) -> None:
        """A name seen in flight stays in flight however often it is read."""
        coordinator = _coordinator({"foo": [_wheel("foo")]})
        provider = Provider(coordinator)

        provider.begin_decision_scan()
        assert provider.is_ready("foo") is False
        assert provider.is_ready("foo") is False
        priority = provider.prioritize("foo", VersionRange.full(), {})

        assert priority[1] == _NO_LISTING_PRIOR

    def test_the_extras_proxy_reads_like_its_base(self) -> None:
        """A proxy and its base answer from the same frozen view."""
        coordinator = _coordinator({"foo": [_wheel("foo")]})
        provider = Provider(coordinator)
        rng = VersionRange.full()

        provider.begin_decision_scan()
        assert provider.prioritize("foo[socks]", rng, {})[1] == _NO_LISTING_PRIOR
        assert provider.is_ready("foo[socks]") is False
        assert provider.prioritize("foo", rng, {})[1] == _NO_LISTING_PRIOR
        assert provider.is_ready("foo") is False

    def test_the_next_scan_picks_the_arrival_up(self) -> None:
        """The freeze lasts one scan, so the listing is not lost."""
        coordinator = _coordinator({"foo": [_wheel("foo"), _wheel("foo", "2.0")]})
        provider = Provider(coordinator)
        rng = VersionRange.full()

        provider.begin_decision_scan()
        assert provider.is_ready("foo") is False

        provider.begin_decision_scan()
        assert provider.is_ready("foo") is True
        assert provider.prioritize("foo", rng, {})[1] == 2


class TestScanKeysShareOneView:
    """Every key a scan compares is built against one view of what has landed."""

    def _resolver(self) -> tuple[Resolver[str, Version], _ScanRecordingProvider]:
        coordinator = _coordinator(
            {"pending-a": [_wheel("pending-a")], "pending-b": [_wheel("pending-b")]},
            {"arriving": [_wheel("arriving")]},
        )
        provider = _ScanRecordingProvider(coordinator)
        provider.versions_cache["cached"] = [(Version("1.0"), _wheel("cached"))]
        resolver: Resolver[str, Version] = Resolver(
            provider, range_type=VersionRange, root_version="0"
        )
        for package in ("cached", "arriving", "pending-a", "pending-b"):
            resolver.solution.derive(
                package, VersionRange.full(), positive=True, cause=_cause()
            )
        return resolver, provider

    def test_every_key_agrees_with_itself(self) -> None:
        """No package is compared as ready while its matching count says in flight."""
        resolver, provider = self._resolver()

        assert decide.choose_package_to_decide(resolver) is not None

        assert len(provider.scan_reads) == 4
        for package, matching, ready in provider.scan_reads:
            assert ready is (matching != _NO_LISTING_PRIOR), package

    def test_a_package_that_lands_mid_scan_waits_for_the_next_one(self) -> None:
        """The scan's view does not move under it, and the next scan sees the rest."""
        resolver, provider = self._resolver()

        decide.choose_package_to_decide(resolver)
        in_flight = {name for name, _, ready in provider.scan_reads if not ready}
        assert in_flight == {"pending-a", "pending-b"}
        assert all(provider.is_ready(name) is False for name in in_flight)

        provider.scan_reads.clear()
        decide.choose_package_to_decide(resolver)

        assert all(ready for _, _, ready in provider.scan_reads)
