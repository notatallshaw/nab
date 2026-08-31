"""Tests that the configured decision order reaches the search.

``nab-provider/tests/test_decision_scan.py`` covers the scan itself,
``DecisionOrder.STABLE`` included; the config that selects it is only
exercised by a whole resolve.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, NamedTuple

from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.resolve import resolve_with_coordinator
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider.provider import BuildPolicy, DecisionOrder
from nab_provider.records import DEFAULT_INDEX_NAME, WheelFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from nab_provider.records import SdistFile
    from nab_provider.store import InMemoryIndex
    from nab_provider.testing import FakeFetchPort


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


class _LandsOnWait(threading.Event):
    """The fetcher's event: it stores the listing just before setting itself.

    A synchronous double cannot race its reader, so the arrival hangs off
    ``wait``: a caller that does not wait sees no listing, as it would
    against an unfinished fetcher.
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
    metadata_by_url: Mapping[str, str | None],
) -> FakeFetchPort:
    """Coordinator whose listings land only for a caller that waits."""
    coordinator = make_coordinator(None, metadata_by_url=metadata_by_url)
    index = coordinator.index
    coordinator.override(
        "request_listing",
        lambda package, _speculative: _LandsOnWait(index, package, pending[package]),
    )
    return coordinator


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
        inputs=ResolveInputs(
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


class TestTheConfiguredOrderReachesTheSearch:
    """A resolve searches in the order the project config asked for."""

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
