"""Tests for the decision scan's frozen view of listing arrival.

``choose_package_to_decide`` builds one sort key per undecided package out of
``prioritize`` and ``is_ready``, and both halves read whether a listing has
landed in the coordinator index.  The fetcher thread writes there while the
scan runs, so without a per-scan freeze two halves of one key, or two keys
compared against each other, can answer from different moments.

The index double below lands a listing on the first read that asks for it,
which is the race a traced resolve caught: the listing arrives between
``prioritize`` and ``is_ready`` for one package.

Freezing that view keeps one scan consistent with itself, but the scans
of two runs can still disagree: what had landed when each scan opened is
a fact about the HTTP cache.  ``decision-order = "stable"`` closes that.
Two classes cover the option: one varies only which listings were already
resident, the other runs the engine, where the config reaches the provider.

The scan is also where the resolver's two search counters reach the sort
key.  Seeding one counter at a time and reading back which package the
scan decides is what tells their two roles apart.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, NamedTuple

from nab_index.client import WheelFile
from nab_python._provider.priority import (
    _NO_LISTING_PRIOR,
    CONFLICT_THRESHOLD,
    CULPRIT_DEMOTE_THRESHOLD,
)
from nab_python._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.config import NabProjectConfig
from nab_python.fetch import DEFAULT_INDEX_NAME, InMemoryIndex
from nab_python.provider import BuildPolicy, DecisionOrder, Provider
from nab_python.resolve import resolve_with_coordinator
from nab_python.tags import PlatformSpec
from nab_python.target import Matrix
from nab_resolver import decide
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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

    def __init__(
        self,
        coordinator: FakeFetchPort,
        decision_order: DecisionOrder = DecisionOrder.ARRIVAL,
    ) -> None:
        super().__init__(coordinator, decision_order=decision_order)
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
) -> FakeFetchPort:
    """Coordinator whose index holds ``resident`` and queues ``arrivals``."""
    coordinator = make_coordinator(None)
    index = _ArrivesOnReadIndex(arrivals)
    for package, files in (resident or {}).items():
        index.store_listing(package, files)
    coordinator.index = index
    return coordinator


class _LandsOnWait(threading.Event):
    """The fetcher's event, carrying the store it does just before setting it.

    A synchronous double cannot race its reader, so the arrival hangs off
    ``wait`` itself.  A caller that does not wait then sees no listing, which
    is what it would see against a fetcher that had not finished.
    """

    def __init__(
        self,
        index: InMemoryIndex,
        package: str,
        files: Sequence[WheelFile | SdistFile],
    ) -> None:
        super().__init__()
        self._index = index
        self._package = package
        self._files = files

    def wait(self, timeout: float | None = None) -> bool:
        self._index.store_listing(self._package, self._files)
        self._index.store_listing_index(self._package, DEFAULT_INDEX_NAME)
        self.set()
        return super().wait(timeout)


def _fetching_coordinator(
    pending: Mapping[str, Sequence[WheelFile | SdistFile]],
    *,
    metadata_by_url: Mapping[str, str | None] | None = None,
) -> FakeFetchPort:
    """Coordinator whose listings land only for a caller that waits.

    Only a caller that runs a whole resolve needs ``metadata_by_url``.
    """
    coordinator = make_coordinator(None, metadata_by_url=metadata_by_url)
    index = coordinator.index
    coordinator.override(
        "request_listing",
        lambda package, _speculative: _LandsOnWait(index, package, pending[package]),
    )
    return coordinator


def _two_package_resolver(
    *, resident: bool, decision_order: DecisionOrder
) -> tuple[Resolver[str, Version], _ScanRecordingProvider]:
    """Resolver over a three-version ``alpha`` and a one-version ``beta``.

    ``resident`` is the only thing that differs between two runs of this
    project against this index: whether ``beta``'s listing had already
    landed when the scan opened, or lands during it.
    """
    beta = [_wheel("beta")]
    resident_listings: dict[str, Sequence[WheelFile | SdistFile]] = {
        "alpha": [_wheel("alpha", version) for version in ("1.0", "2.0", "3.0")]
    }
    arrivals: dict[str, Sequence[WheelFile | SdistFile]] = {}
    if resident:
        resident_listings["beta"] = beta
    else:
        arrivals["beta"] = beta

    provider = _ScanRecordingProvider(
        _coordinator(arrivals, resident_listings), decision_order
    )
    resolver: Resolver[str, Version] = Resolver(
        provider, range_type=VersionRange, root_version="0"
    )
    for package in ("alpha", "beta"):
        resolver.solution.derive(
            package, VersionRange.full(), positive=True, cause=_cause()
        )
    return resolver, provider


def _scan_keys(*, resident: bool, decision_order: DecisionOrder) -> dict[str, object]:
    """Return one scan's ``package -> (matching, ready)`` map."""
    resolver, provider = _two_package_resolver(
        resident=resident, decision_order=decision_order
    )
    decide.choose_package_to_decide(resolver)
    return {
        package: (matching, ready) for package, matching, ready in provider.scan_reads
    }


def _decided_first(*, resident: bool, decision_order: DecisionOrder) -> str | None:
    """Return the package the scan would decide next."""
    resolver, _ = _two_package_resolver(
        resident=resident, decision_order=decision_order
    )
    return decide.choose_package_to_decide(resolver)


def _counter_resolver() -> Resolver[str, Version]:
    """The same two packages with both listings already resident.

    Nothing is in flight, so both are ready and the sort key comes down to
    the tier the search counters give a package, then its candidate count.
    """
    resolver, _ = _two_package_resolver(
        resident=True, decision_order=DecisionOrder.ARRIVAL
    )
    return resolver


def _cause() -> Incompatibility[str, Version]:
    return Incompatibility(
        [Term("root", VersionRange.full(), positive=True)],
        cause=IncompatibilityCause.DEPENDENCY,
    )


class _Counters(NamedTuple):
    """One target's pins and the search counters behind them."""

    pins: dict[str, str]
    decisions: int
    rounds: int
    conflicts: int
    backjumps: int


_ALPHA_VERSIONS = ("1.0", "2.0", "3.0", "4.0", "5.0")


def _sidecar(
    package: str, version: str, requires: str | None = None
) -> tuple[str, str]:
    """Map one wheel's METADATA URL to its text."""
    text = f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n"
    if requires is not None:
        text += f"Requires-Dist: {requires}\n"
    return f"{_wheel(package, version).url}.metadata", text


def _engine_counters(decision_order: DecisionOrder) -> _Counters:
    """Resolve ``alpha`` and ``beta`` off a cold index under ``decision_order``.

    ``beta`` pins ``alpha`` to its oldest version, so a scan that ranks
    ``alpha`` while ``beta``'s listing is in flight decides ``alpha`` on its
    own five versions and has to take that decision back.
    """
    pending: dict[str, Sequence[WheelFile | SdistFile]] = {
        "alpha": [_wheel("alpha", version) for version in _ALPHA_VERSIONS],
        "beta": [_wheel("beta")],
    }

    sidecars = [_sidecar("alpha", version) for version in _ALPHA_VERSIONS]
    sidecars.append(_sidecar("beta", "1.0", requires="alpha==1.0"))

    result = resolve_with_coordinator(
        _fetching_coordinator(pending, metadata_by_url=dict(sidecars)),
        Matrix(python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)).expand(),
        [Requirement("alpha"), Requirement("beta")],
        config=NabProjectConfig(
            build_policy=BuildPolicy.NEVER, decision_order=decision_order
        ),
    )

    target = result.target_results[0]
    return _Counters(
        pins={name: str(version) for name, version in target.pins.items()},
        decisions=target.decisions,
        rounds=target.rounds,
        conflicts=target.conflicts,
        backjumps=target.backjumps,
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


class TestStableOrderIgnoresArrival:
    """Under ``stable`` the scan settles a listing rather than ranking its absence."""

    def test_a_listing_that_lands_mid_scan_is_counted_now(self) -> None:
        """The count is the real one, not the in-flight sentinel."""
        provider = Provider(
            _coordinator({"foo": [_wheel("foo")]}),
            decision_order=DecisionOrder.STABLE,
        )

        provider.begin_decision_scan()

        assert provider.prioritize("foo", VersionRange.full(), {})[1] == 1
        assert provider.is_ready("foo") is True

    def test_settling_waits_for_the_fetch(self) -> None:
        """The count comes from the wait, not from a second look at the index."""
        provider = Provider(
            _fetching_coordinator({"foo": [_wheel("foo")]}),
            decision_order=DecisionOrder.STABLE,
        )

        provider.begin_decision_scan()

        assert provider.prioritize("foo", VersionRange.full(), {})[1] == 1
        assert provider.is_ready("foo") is True

    def test_a_resident_listing_is_read_without_a_request(self) -> None:
        """Settling costs nothing once the listing is in the index."""
        coordinator = _coordinator({}, {"foo": [_wheel("foo")]})
        provider = Provider(coordinator, decision_order=DecisionOrder.STABLE)

        listing = provider.settled_listing("foo")

        assert listing is not None
        assert [file.filename for file in listing] == ["foo-1.0-py3-none-any.whl"]
        assert not coordinator.calls_to("request_listing")

    def test_a_failed_listing_is_not_requested_again(self) -> None:
        """A fetch that already failed has settled; asking again would not help."""
        coordinator = _coordinator({})
        coordinator.index.store_listing_error("foo", RuntimeError("index is down"))
        provider = Provider(coordinator, decision_order=DecisionOrder.STABLE)

        provider.begin_decision_scan()

        assert (
            provider.prioritize("foo", VersionRange.full(), {})[1] == _NO_LISTING_PRIOR
        )
        assert provider.is_ready("foo") is False
        assert not coordinator.calls_to("request_listing")

    def test_a_listing_that_never_lands_leaves_the_package_in_flight(self) -> None:
        """One wait, then the sentinel: the scan must not spin on a dead fetch."""
        provider = Provider(_coordinator({}), decision_order=DecisionOrder.STABLE)

        provider.begin_decision_scan()

        assert (
            provider.prioritize("foo", VersionRange.full(), {})[1] == _NO_LISTING_PRIOR
        )
        assert provider.is_ready("foo") is False

    def test_the_scan_builds_the_same_keys_whatever_had_landed(self) -> None:
        """Cache warmth is invisible to the sort key."""
        warm = _scan_keys(resident=True, decision_order=DecisionOrder.STABLE)
        cold = _scan_keys(resident=False, decision_order=DecisionOrder.STABLE)

        assert warm == cold == {"alpha": (3, True), "beta": (1, True)}

    def test_the_same_package_is_decided_whatever_had_landed(self) -> None:
        """The package with fewer candidates decides first either way."""
        warm = _decided_first(resident=True, decision_order=DecisionOrder.STABLE)
        cold = _decided_first(resident=False, decision_order=DecisionOrder.STABLE)

        assert warm == cold == "beta"

    def test_the_default_decides_on_what_happened_to_have_landed(self) -> None:
        """The default decides on cache warmth, which is what the option closes."""
        warm = _decided_first(resident=True, decision_order=DecisionOrder.ARRIVAL)
        cold = _decided_first(resident=False, decision_order=DecisionOrder.ARRIVAL)

        assert warm == "beta"
        assert cold == "alpha"


class TestTheConfiguredOrderReachesTheSearch:
    """A resolve searches in the order the project config asked for.

    The config parsing ``stable`` and a provider built with it settling
    listings are both covered elsewhere.  Only a whole resolve shows the
    config reaching the provider, and the evidence is how the search went.
    """

    def test_stable_settles_the_listing_before_deciding(self) -> None:
        """Waiting for ``beta`` pins both packages without backtracking."""
        assert _engine_counters(DecisionOrder.STABLE) == _Counters(
            pins={"alpha": "1.0", "beta": "1.0"},
            decisions=3,
            rounds=3,
            conflicts=0,
            backjumps=0,
        )

    def test_the_default_decides_before_the_listing_lands(self) -> None:
        """The same input under ``arrival`` reaches the same pins by backtracking."""
        assert _engine_counters(DecisionOrder.ARRIVAL) == _Counters(
            pins={"alpha": "1.0", "beta": "1.0"},
            decisions=4,
            rounds=6,
            conflicts=1,
            backjumps=1,
        )


class TestTheSearchCountersReachTheSortKey:
    """The scan ranks on both search counters, each in its own role.

    They pull opposite ways: a package whose own decisions keep being
    discarded is decided first, one blamed for discarding another's is
    decided last.  The scan passes them positionally, so only a scan shows
    each one landing in the role named for it.
    """

    def test_the_candidate_count_decides_with_no_history(self) -> None:
        """Baseline: nothing has conflicted, so fewer candidates wins."""
        assert decide.choose_package_to_decide(_counter_resolver()) == "beta"

    def test_a_runaway_culprit_decides_last(self) -> None:
        """``beta`` blamed for the conflicts drops behind the wider ``alpha``."""
        resolver = _counter_resolver()
        resolver.stats.package_culprit_counts["beta"] = CULPRIT_DEMOTE_THRESHOLD

        assert decide.choose_package_to_decide(resolver) == "alpha"

    def test_a_repeatedly_discarded_package_decides_first(self) -> None:
        """``alpha``'s own discarded decisions lift it past ``beta``."""
        resolver = _counter_resolver()
        resolver.stats.package_conflict_counts["alpha"] = CONFLICT_THRESHOLD

        assert decide.choose_package_to_decide(resolver) == "alpha"
