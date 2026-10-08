"""Stable resolution reports override conflicts in decision order."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider.errors import OverrideConflictError
from nab_provider.overrides import IndexOverride
from nab_provider.policy import DecisionOrder, DistPolicy
from nab_provider.provider import Provider
from nab_provider.records import DEFAULT_INDEX_NAME, WheelFile
from nab_provider.testing import make_coordinator, pkg_override
from nab_resolver.resolver import Resolver

_PROBE = """
import sys

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider.errors import OverrideConflictError
from nab_provider.overrides import IndexOverride
from nab_provider.policy import DecisionOrder, DistPolicy
from nab_provider.provider import Provider
from nab_provider.records import DEFAULT_INDEX_NAME, WheelFile
from nab_provider.testing import make_coordinator, pkg_override
from nab_resolver.resolver import Resolver

names = sys.argv[1:]
requirements = {name: VersionRange.full() for name in names}
listings = {
    name: [WheelFile(
        filename=f"{name}-1.0-py3-none-any.whl",
        version="1.0",
        url=f"https://example.com/{name}-1.0-py3-none-any.whl",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )]
    for name in names
}
provider = Provider(
    make_coordinator(listings=listings, auto_metadata=True),
    root_requirements=requirements,
    decision_order=DecisionOrder.STABLE,
    package_overrides=tuple(
        pkg_override(name, dist_policy=DistPolicy.WHEEL_ONLY) for name in names
    ),
    index_overrides={
        DEFAULT_INDEX_NAME: IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
    },
)
resolver = Resolver(provider, range_type=VersionRange, root_version="0")
try:
    resolver.resolve(requirements)
except OverrideConflictError as exc:
    print(f"{type(exc).__name__}: {exc}")
else:
    raise AssertionError("conflicting overrides were accepted")
"""


def _wheel(package: str, version: str = "1.0") -> WheelFile:
    """Build a wheel record for resolver integration tests."""
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        version=version,
        url=f"https://example.com/{package}-{version}-py3-none-any.whl",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _provider(decision_order: DecisionOrder) -> Provider:
    """Build a provider whose resident wheel has conflicting distribution policies."""
    return Provider(
        make_coordinator(listings={"alpha": [_wheel("alpha")]}),
        decision_order=decision_order,
        package_overrides=(pkg_override("alpha", dist_policy=DistPolicy.WHEEL_ONLY),),
        index_overrides={
            DEFAULT_INDEX_NAME: IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
        },
    )


@pytest.mark.parametrize("names", [("alpha", "bravo"), ("bravo", "alpha")])
def test_stable_override_error_ignores_hash_seed(names: tuple[str, str]) -> None:
    errors = []
    for seed in (0, 4):
        result = subprocess.run(  # noqa: S603 - fixed interpreter and inline probe
            [sys.executable, "-B", "-c", _PROBE, *names],
            env=dict(
                os.environ,
                PYTHONHASHSEED=str(seed),
                PYTHONPATH=os.pathsep.join(sys.path),
            ),
            capture_output=True,
            check=True,
            timeout=30,
        )
        errors.append(result.stdout)

    assert errors[0] == errors[1]
    assert errors[0].startswith(
        f"OverrideConflictError: override conflict for {names[0]}==1.0 ".encode()
    )


@pytest.mark.parametrize("decision_order", list(DecisionOrder))
@pytest.mark.parametrize("package", ["alpha", "alpha[feature]"])
def test_override_conflict_remains_fatal(
    decision_order: DecisionOrder, package: str
) -> None:
    provider = _provider(decision_order)
    provider.begin_decision_scan()

    if decision_order is DecisionOrder.STABLE:
        priority = provider.prioritize(
            package, VersionRange.full(), {"alpha": 10}, {"alpha": 10}
        )
        assert priority < (0, 0, False)
        assert provider.is_ready(package)
        assert "alpha" not in provider.versions_cache
    else:
        with pytest.raises(OverrideConflictError, match="override conflict for alpha"):
            provider.prioritize(package, VersionRange.full(), {})

    with pytest.raises(OverrideConflictError, match="override conflict for alpha"):
        provider.choose_version(package, VersionRange.full())


@pytest.mark.parametrize("scenario", ["unavailable-root", "backtracking"])
@pytest.mark.parametrize("package", ["bravo", "bravo[feature]"])
def test_override_conflict_preempts_no_versions(scenario: str, package: str) -> None:
    parent_new = _wheel("parent", "2.0")
    parent_old = _wheel("parent")
    bravo = _wheel("bravo")
    requirements = (
        {"parent": VersionRange.full()}
        if scenario == "backtracking"
        else {"alpha": VersionRange.full(), package: VersionRange.full()}
    )

    coordinator = make_coordinator(
        listings={"parent": [parent_new, parent_old], "alpha": [], "bravo": [bravo]},
        metadata_by_url={
            f"{parent_new.url}.metadata": (
                "Metadata-Version: 2.1\nName: parent\nVersion: 2.0\n"
                f"Requires-Dist: alpha\nRequires-Dist: {package}\n\n"
            ),
            f"{parent_old.url}.metadata": (
                "Metadata-Version: 2.1\nName: parent\nVersion: 1.0\n\n"
            ),
            f"{bravo.url}.metadata": (
                "Metadata-Version: 2.1\nName: bravo\nVersion: 1.0\n"
                "Provides-Extra: feature\n\n"
            ),
        },
    )

    provider = Provider(
        coordinator,
        root_requirements=requirements,
        decision_order=DecisionOrder.STABLE,
        package_overrides=(pkg_override("bravo", dist_policy=DistPolicy.WHEEL_ONLY),),
        index_overrides={
            DEFAULT_INDEX_NAME: IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
        },
    )
    resolver = Resolver(provider, range_type=VersionRange, root_version="0")

    with pytest.raises(OverrideConflictError, match="override conflict for bravo==1.0"):
        resolver.resolve(requirements)
