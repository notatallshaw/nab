"""Tests that the configured decision order reaches the search.

``nab-provider/tests/test_decision_scan.py`` covers the scan itself,
``DecisionOrder.STABLE`` included; the config that selects it is only
exercised by a whole resolve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.resolve import resolve_with_coordinator
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider.provider import BuildPolicy, DecisionOrder
from nab_provider.records import WheelFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix
from nab_provider.testing import make_metadata, make_wheel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider.records import SdistFile


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
    dependencies = () if requires is None else (requires,)
    text = make_metadata(package, version, *dependencies)
    return f"{make_wheel(version, package=package).url}.metadata", text


def _engine_counters(decision_order: DecisionOrder) -> _Counters:
    """Resolve ``alpha`` and ``beta`` off a cold index under ``decision_order``.

    ``beta`` pins ``alpha`` to its oldest version, so a scan that ranks
    ``alpha`` while ``beta``'s listing is in flight decides ``alpha`` on its
    own five versions and has to take that decision back.
    """
    pending: dict[str, Sequence[WheelFile | SdistFile]] = {
        "alpha": [make_wheel(version, package="alpha") for version in _ALPHA_VERSIONS],
        "beta": [make_wheel(package="beta")],
    }

    sidecars = [_sidecar("alpha", version) for version in _ALPHA_VERSIONS]
    sidecars.append(_sidecar("beta", "1.0", requires="alpha==1.0"))

    result = resolve_with_coordinator(
        make_coordinator(pending_listings=pending, metadata_by_url=dict(sidecars)),
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
