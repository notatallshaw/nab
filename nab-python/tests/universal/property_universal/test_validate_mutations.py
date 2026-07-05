"""Mutation-style property tests for :mod:`nab_python.universal.validate`.

Each test builds a small valid universal lock via the coordinator
fake, corrupts exactly one thing, and requires ``validate_lock`` to
flag it.  A corruption that yields an "ok" report means the validate
phase would wave a broken lock through; the unmutated control checks
the inverse (no false positives).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.client import WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.version import Version
from nab_python.universal.matrix import Matrix
from nab_python.universal.resolve import TupleResult, UniversalResult
from nab_python.universal.validate import validate_lock

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

DEP_POOL = ("requests", "numpy", "click", "attrs", "idna")


def _wheel(filename: str) -> WheelFile:
    """Build a ``WheelFile`` whose version is parsed from the filename."""
    parts = filename.split("-")
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=parts[1],
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _matrix() -> Matrix:
    """Build the single-tuple linux/3.11 matrix used by every test."""
    return Matrix(python=">=3.11,<3.12", platforms=("linux_x86_64",))


def _metadata(deps: list[str], version: str = "1.0") -> str:
    """Build METADATA text for pkg with the given dependencies."""
    lines = ["Metadata-Version: 2.1", "Name: pkg", f"Version: {version}"]
    lines += [f"Requires-Dist: {d}" for d in deps]
    return "\n".join(lines) + "\n\n"


def _result(pins: dict[str, Version]) -> UniversalResult:
    """Wrap ``pins`` as a successful one-tuple ``UniversalResult``."""
    matrix = _matrix()
    return UniversalResult(
        matrix=matrix,
        tuple_results=[TupleResult(tuple_=matrix.expand()[0], success=True, pins=pins)],
    )


def _coordinator(deps: list[str], wheel_deps: list[str]) -> MagicMock:
    """Build a coordinator whose baseline and per-wheel metadata may differ."""
    fn = "pkg-1.0-py3-none-any.whl"
    return make_coordinator(
        wheels=[_wheel(fn)],
        baseline_metadata={"pkg": _metadata(deps)},
        per_wheel_metadata={fn: _metadata(wheel_deps)},
    )


class TestUnmutatedLockIsOk:
    """Control: when the chosen wheel's metadata equals the baseline
    the resolver saw, the report must be clean.  A false positive
    here would make every real lock fail validation.
    """

    @given(deps=st.lists(st.sampled_from(DEP_POOL), unique=True, max_size=4))
    @PROPERTY_SETTINGS
    def test_unmutated_lock_is_ok(self, deps: list[str]) -> None:
        """Identical baseline and wheel metadata validates clean."""
        coord = _coordinator(deps, deps)
        report = validate_lock(_result({"pkg": Version("1.0")}), coord)
        assert report.pins_checked == 1
        assert report.pins_ok == 1
        assert not report.fatal_findings(build_allowed=False)


class TestDroppedDepInWheelIsFlagged:
    """Chosen wheel metadata missing one baseline dep must surface as
    a divergent finding naming the dropped dep; an "ok" report would
    silently under-install it.
    """

    @given(
        deps=st.lists(st.sampled_from(DEP_POOL), unique=True, min_size=1, max_size=4),
        data=st.data(),
    )
    @PROPERTY_SETTINGS
    def test_dropped_dep_in_wheel_is_flagged(
        self, deps: list[str], data: st.DataObject
    ) -> None:
        """A baseline dep absent from the wheel shows in ``missing_deps``."""
        drop = data.draw(st.sampled_from(deps))
        wheel_deps = [d for d in deps if d != drop]
        coord = _coordinator(deps, wheel_deps)
        report = validate_lock(_result({"pkg": Version("1.0")}), coord)
        assert report.pins_ok == 0
        (finding,) = report.findings
        assert finding.status == "divergent"
        assert drop in finding.missing_deps


class TestExtraDepInWheelIsFlagged:
    """Chosen wheel metadata with one dep added must surface as a
    divergent finding naming the added dep; an "ok" report would
    silently over-install it.
    """

    @given(
        deps=st.lists(st.sampled_from(DEP_POOL), unique=True, max_size=3),
        extra_dep=st.sampled_from(DEP_POOL),
    )
    @PROPERTY_SETTINGS
    def test_extra_dep_in_wheel_is_flagged(
        self, deps: list[str], extra_dep: str
    ) -> None:
        """A wheel-only dep shows in ``extra_deps``."""
        if extra_dep in deps:
            deps = [d for d in deps if d != extra_dep]
        coord = _coordinator(deps, [*deps, extra_dep])
        report = validate_lock(_result({"pkg": Version("1.0")}), coord)
        assert report.pins_ok == 0
        (finding,) = report.findings
        assert finding.status == "divergent"
        assert extra_dep in finding.extra_deps


class TestFlippedMarkerInWheelIsFlagged:
    """Dependency divergence can hide behind markers: a dep the
    baseline gates on the tuple's platform but the wheel gates on a
    different one evaluates to a dropped dep for that tuple and must
    be flagged.
    """

    @given(dep=st.sampled_from(DEP_POOL))
    @PROPERTY_SETTINGS
    def test_flipped_marker_in_wheel_is_flagged(self, dep: str) -> None:
        """Baseline dep gated on linux; wheel flips the marker to win32."""
        fn = "pkg-1.0-py3-none-any.whl"
        base = _metadata([f'{dep}; sys_platform == "linux"'])
        wheel = _metadata([f'{dep}; sys_platform == "win32"'])
        coord = make_coordinator(
            wheels=[_wheel(fn)],
            baseline_metadata={"pkg": base},
            per_wheel_metadata={fn: wheel},
        )
        report = validate_lock(_result({"pkg": Version("1.0")}), coord)
        (finding,) = report.findings
        assert finding.status == "divergent"
        assert dep in finding.missing_deps


class TestWheelForWrongPlatformIsFatal:
    """A pin whose only artifact targets a different platform cannot
    install on this tuple; the finding must be fatal even when
    building is allowed (there is no sdist to fall back to).
    """

    def test_wheel_for_wrong_platform_is_always_fatal(self) -> None:
        """Only a win32 wheel exists; the linux tuple pin must be fatal."""
        fn = "pkg-1.0-cp311-cp311-win_amd64.whl"
        coord = make_coordinator(
            wheels=[_wheel(fn)],
            baseline_metadata={"pkg": _metadata(["requests"])},
        )
        report = validate_lock(_result({"pkg": Version("1.0")}), coord)
        (finding,) = report.findings
        assert finding.status == "no_compatible_wheel"
        assert report.fatal_findings(build_allowed=True)
