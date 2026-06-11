"""End-to-end property tests for :mod:`nab_python.universal.reresolve`.

Universe: an ``app`` whose listing baseline metadata depends on one
subset of a dependency pool while its chosen wheel's metadata
depends on another.  ``validate_lock`` must flag the divergence;
``reresolve_divergent_tuples`` must report exactly the dependency
swap as its diff; and re-running reresolve against the wheel-aware
result must be a no-op (fixed point).  A drifting fixed point means
repeated ``nab lock`` runs never converge.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.client import WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python.provider import BuildPolicy
from nab_python.universal.matrix import Matrix
from nab_python.universal.reresolve import (
    _collect_wheel_metadata_overrides,
    _override_metadata,
    reresolve_divergent_tuples,
)
from nab_python.universal.resolve import resolve_with_coordinator
from nab_python.universal.validate import validate_lock

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

DEP_POOL = ("depa", "depb", "depc", "depd")


def _wheel(package: str, version: str = "1.0") -> WheelFile:
    """Build a pure-Python wheel listing entry for ``package``."""
    fn = f"{package}-{version}-py3-none-any.whl"
    return WheelFile(
        filename=fn,
        url=f"https://example.com/{fn}",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=(("sha256", "a" * 64),),
    )


def _metadata(name: str, deps: list[str]) -> str:
    """Build METADATA text for ``name`` 1.0 with the given dependencies."""
    lines = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0"]
    lines += [f"Requires-Dist: {d}" for d in deps]
    return "\n".join(lines) + "\n\n"


def _build_universe(baseline_deps: list[str], wheel_deps: list[str]) -> MagicMock:
    """Build a coordinator whose app baseline and wheel metadata diverge."""
    listings = {"app": [_wheel("app")]}
    baseline = {"app": _metadata("app", baseline_deps)}
    per_wheel = {_wheel("app").filename: _metadata("app", wheel_deps)}
    for dep in DEP_POOL:
        listings[dep] = [_wheel(dep)]
        baseline[dep] = _metadata(dep, [])
        per_wheel[_wheel(dep).filename] = _metadata(dep, [])
    return make_coordinator(
        listings=listings,
        baseline_metadata=baseline,
        per_wheel_metadata=per_wheel,
    )


def _matrix() -> Matrix:
    """Build the single-tuple linux/3.11 matrix used by every test."""
    return Matrix(python="==3.11", platforms=("linux_x86_64",))


deps_sets = st.lists(st.sampled_from(DEP_POOL), unique=True, max_size=3).map(sorted)


class TestReresolveDiffIsExactlyTheDepSwap:
    """The reresolve diff against a divergent pin must report exactly
    the set difference between the wheel's dependencies and the
    baseline's: every added dep, every removed dep, no version
    changes, and an empty diff when the metadata agrees.
    """

    @given(baseline_deps=deps_sets, wheel_deps=deps_sets)
    @PROPERTY_SETTINGS
    def test_reresolve_diff_is_exactly_the_dep_swap(
        self, baseline_deps: list[str], wheel_deps: list[str]
    ) -> None:
        """The divergence diff equals the baseline/wheel dependency swap."""
        coord = _build_universe(baseline_deps, wheel_deps)
        result = resolve_with_coordinator(
            coord, _matrix(), ["app"], build_policy=BuildPolicy.NEVER
        )
        assert result.success, [tr.error for tr in result.tuple_results]
        report = validate_lock(result, coord)
        diffs = reresolve_divergent_tuples(coord, ["app"], result, report)
        if baseline_deps == wheel_deps:
            assert not any(f.status != "ok" for f in report.findings)
            assert diffs == {}
            return
        (diff,) = diffs.values()
        assert sorted(diff.added) == sorted(set(wheel_deps) - set(baseline_deps))
        assert sorted(diff.removed) == sorted(set(baseline_deps) - set(wheel_deps))
        assert diff.version_changed == {}


class TestReresolveFixedPoint:
    """Resolving with the divergent wheels' metadata substituted in
    must itself validate without a further diff: reresolve is a fixed
    point after one step on this universe.
    """

    @given(baseline_deps=deps_sets, wheel_deps=deps_sets)
    @PROPERTY_SETTINGS
    def test_reresolve_fixed_point(
        self, baseline_deps: list[str], wheel_deps: list[str]
    ) -> None:
        """Re-running reresolve against the wheel-aware result is a no-op."""
        coord = _build_universe(baseline_deps, wheel_deps)
        result = resolve_with_coordinator(
            coord, _matrix(), ["app"], build_policy=BuildPolicy.NEVER
        )
        assert result.success
        report = validate_lock(result, coord)
        divergent = [f for f in report.findings if f.status == "divergent"]
        if not divergent:
            return
        overrides = _collect_wheel_metadata_overrides(coord, divergent)
        with _override_metadata(coord, overrides):
            wheel_aware = resolve_with_coordinator(
                coord, _matrix(), ["app"], build_policy=BuildPolicy.NEVER
            )
        assert wheel_aware.success
        report2 = validate_lock(wheel_aware, coord)
        diffs2 = reresolve_divergent_tuples(coord, ["app"], wheel_aware, report2)
        for label, diff in diffs2.items():
            assert diff.added == {}, (label, diff)
            assert diff.removed == {}, (label, diff)
            assert diff.version_changed == {}, (label, diff)


class TestOverrideMetadataRestoresIndexState:
    """``_override_metadata`` is a context manager; on exit every
    metadata slot it touched must hold its original value, otherwise
    a reresolve poisons later phases of the same run.
    """

    @given(baseline_deps=deps_sets, wheel_deps=deps_sets)
    @PROPERTY_SETTINGS
    def test_override_metadata_restores_index_state(
        self, baseline_deps: list[str], wheel_deps: list[str]
    ) -> None:
        """The override context restores every metadata slot it touched."""
        coord = _build_universe(baseline_deps, wheel_deps)
        keys = [("app", "1.0"), *((d, "1.0") for d in DEP_POOL)]
        before = {k: coord.index.get_metadata(*k) for k in keys}
        overrides = {("app", "1.0"): _metadata("app", ["depz"])}
        with _override_metadata(coord, overrides):
            assert coord.index.get_metadata("app", "1.0") == _metadata("app", ["depz"])
        after = {k: coord.index.get_metadata(*k) for k in keys}
        assert before == after
