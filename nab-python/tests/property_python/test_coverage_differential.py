"""Differential property tests for :mod:`nab_python._lockfile.coverage`.

`PEP 751`_ consumers refuse a lock unless one of its declared
``environments`` markers matches the installing interpreter, so a lock
that declares no row for an interpreter its own resolve produced pins for
is silently unusable there.  ``validate_marker_coverage`` guards that class
through the marker algebra: for each target the resolve ran it asks whether
the union of the emitted rows admits the whole range the target stands for,
and names an uncovered point when one exists.  These tests drive the real
emit pipeline and check every witness against direct marker evaluation.

.. _PEP 751: https://peps.python.org/pep-0751/#packages
"""

from __future__ import annotations

import random
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._lockfile.coverage import CoverageError, validate_marker_coverage
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.markersets import IntractableMarkerSet
from nab_python._vendor.packaging.version import Version
from nab_python.tags import PlatformSpec
from nab_python.target import (
    ResolveTarget,
    declared_range_marker,
    environment_declaration,
    micro_boundary_points,
    slices_from_points,
)

from .strategies import PROPERTY_SETTINGS

MINORS = ("3.11", "3.12", "3.13")
PLATFORMS = ("linux_x86_64", "windows_amd64", "macos_arm64")
BOUNDARY_MICROS = (1, 2, 3, 5)
# On CPython implementation_version tracks python_full_version, so a
# consulted marker on either splits the minor and emits mirror bounds.
VERSION_VARIABLES = ("python_full_version", "implementation_version")

_WITNESS_CLAUSE = re.compile(r'(\w+) == "([^"]+)"')

points = st.lists(
    st.tuples(st.sampled_from(MINORS), st.sampled_from(PLATFORMS)),
    min_size=1,
    max_size=4,
    unique=True,
)
boundaries = st.lists(
    st.sampled_from(BOUNDARY_MICROS), min_size=0, max_size=2, unique=True
)
version_variables = st.sampled_from(VERSION_VARIABLES)


def _emit(
    matrix_points: list[tuple[str, str]], micros: list[int], variable: str
) -> tuple[list[ResolveTarget], list[Marker]]:
    """Run the emit pipeline over a matrix, returning its targets and rows.

    Each ``(minor, platform)`` point becomes a bare-minor target; a
    consulted ``variable >= "{minor}.{micro}"`` marker per boundary splits
    it into slices, and every slice declares its own environment row.  With
    no boundaries the minor stays whole and emits one row.
    """
    targets: list[ResolveTarget] = []
    environments: list[Marker] = []
    for minor, platform in matrix_points:
        base = ResolveTarget.for_declared(
            python_version=minor, spec=PlatformSpec(platform)
        )
        consulted = [Marker(f'{variable} >= "{minor}.{micro}"') for micro in micros]
        slices = slices_from_points(base, micro_boundary_points(base, consulted))
        for piece in slices:
            targets.append(piece)
            environments.append(Marker(environment_declaration(piece, consulted)))
    return targets, environments


def _witness_environment(exc: CoverageError) -> dict[str, str]:
    """Reconstruct the interpreter the coverage error names.

    The message renders the witness as ``python_full_version`` plus every
    boundable axis it pins.  Add the derived ``python_version`` and, since
    the witness names a CPython interpreter where the two are equal,
    ``implementation_version``, so every emitted row and reference marker
    evaluates against a complete environment.
    """
    pins = dict(_WITNESS_CLAUSE.findall(str(exc)))
    release = Version(pins["python_full_version"]).release
    pins["python_version"] = f"{release[0]}.{release[1]}"
    pins["implementation_version"] = pins["python_full_version"]
    return pins


def _assert_witness_sound(
    exc: CoverageError,
    targets: list[ResolveTarget],
    environments: list[Marker],
) -> None:
    """Assert the witness is a real uncovered interpreter by evaluation.

    Some target's reference range admits the witness (it is an environment
    the resolve ran for) and no emitted row admits it (the declaration
    would refuse it), decided by evaluating the markers at the point.
    """
    env = _witness_environment(exc)
    references = [Marker(declared_range_marker(target)) for target in targets]
    assert any(reference.evaluate(env) for reference in references)
    assert not any(row.evaluate(env) for row in environments)


@pytest.mark.property
class TestCoverageDifferential:
    """The coverage gate against the real emitter and an evaluation oracle.

    The gate must never fire on what the emitter produces, must fire on a
    removed region, and every witness it names must be a real interpreter
    that a target resolved for and no row admits.
    """

    @given(matrix_points=points, micros=boundaries, variable=version_variables)
    @PROPERTY_SETTINGS
    def test_real_emit_pipeline_is_covered(
        self, matrix_points: list[tuple[str, str]], micros: list[int], variable: str
    ) -> None:
        """The gate never fires on rows the real emit pipeline produced.

        An ``IntractableMarkerSet`` on a large matrix is the algebra's
        bounded-failure escape hatch, a sound non-emit the design allows;
        only a false ``CoverageError`` is a coverage bug.
        """
        targets, environments = _emit(matrix_points, micros, variable)
        try:
            validate_marker_coverage(targets, environments=environments)
        except IntractableMarkerSet:
            pass

    @given(
        minor=st.sampled_from(MINORS),
        platform=st.sampled_from(PLATFORMS),
        micros=st.lists(
            st.sampled_from(BOUNDARY_MICROS), min_size=1, max_size=2, unique=True
        ),
        variable=version_variables,
        data=st.data(),
    )
    @PROPERTY_SETTINGS
    def test_dropping_a_slice_row_fires(
        self,
        minor: str,
        platform: str,
        micros: list[int],
        variable: str,
        data: st.DataObject,
    ) -> None:
        """Dropping one slice row fires, with the witness inside its region."""
        targets, environments = _emit([(minor, platform)], micros, variable)
        assert len(environments) >= 2
        drop = data.draw(st.integers(min_value=0, max_value=len(environments) - 1))
        remaining = [row for index, row in enumerate(environments) if index != drop]
        with pytest.raises(CoverageError) as excinfo:
            validate_marker_coverage(targets, environments=remaining)
        env = _witness_environment(excinfo.value)
        assert environments[drop].evaluate(env)
        _assert_witness_sound(excinfo.value, targets, remaining)

    @given(
        matrix_points=points,
        narrow_micro=st.sampled_from((1, 2, 3)),
        data=st.data(),
    )
    @PROPERTY_SETTINGS
    def test_narrowing_a_lower_bound_fires(
        self,
        matrix_points: list[tuple[str, str]],
        narrow_micro: int,
        data: st.DataObject,
    ) -> None:
        """Narrowing one row's lower bound fires below the new floor."""
        targets = [
            ResolveTarget.for_declared(
                python_version=minor, spec=PlatformSpec(platform)
            )
            for minor, platform in matrix_points
        ]
        environments = [
            Marker(environment_declaration(target, [])) for target in targets
        ]
        index = data.draw(st.integers(min_value=0, max_value=len(targets) - 1))
        minor = matrix_points[index][0]
        floor = f'python_full_version >= "{minor}.{narrow_micro}"'
        environments[index] = Marker(f"{environments[index]} and {floor}")
        with pytest.raises(CoverageError) as excinfo:
            validate_marker_coverage(targets, environments=environments)
        witness = Version(_witness_environment(excinfo.value)["python_full_version"])
        assert witness < Version(f"{minor}.{narrow_micro}")
        _assert_witness_sound(excinfo.value, targets, environments)


# The nab CI-lock matrix that overran the witness cell budget: 5 minors x 5
# platforms, 3.10 split at micro 2, giving 30 targets and 30 rows.
BLOWUP_MINORS = ("3.10", "3.11", "3.12", "3.13", "3.14")
BLOWUP_PLATFORMS = (
    "linux_x86_64",
    "linux_aarch64",
    "macos_arm64",
    "macos_x86_64",
    "windows_amd64",
)


def _blowup_matrix() -> tuple[list[ResolveTarget], list[Marker]]:
    """Build the 30-target / 30-row CI-lock fixture that overran the budget.

    Every minor stays whole (one row) except 3.10, which a consulted
    ``python_full_version >= "3.10.2"`` splits into two micro slices.
    """
    targets: list[ResolveTarget] = []
    environments: list[Marker] = []
    for minor in BLOWUP_MINORS:
        consulted = (
            [Marker('python_full_version >= "3.10.2"')] if minor == "3.10" else []
        )
        for platform in BLOWUP_PLATFORMS:
            base = ResolveTarget.for_declared(
                python_version=minor, spec=PlatformSpec(platform)
            )
            slices = slices_from_points(base, micro_boundary_points(base, consulted))
            for piece in slices:
                targets.append(piece)
                environments.append(Marker(environment_declaration(piece, consulted)))
    return targets, environments


class TestCoverageGateBlowupRegression:
    """The CI-lock fixture that crashed the gate with ``IntractableMarkerSet``.

    Complementing the 30-row union carried every axis every row named at once,
    overrunning the witness cell budget. The fix restricts the union to each
    reference's pinned axes first.
    """

    def test_full_ci_lock_matrix_is_decidable(self) -> None:
        """The covering 30x30 matrix validates without raising."""
        targets, environments = _blowup_matrix()
        assert len(targets) == 30
        assert len(environments) == 30
        validate_marker_coverage(targets, environments=environments)

    def test_dropping_a_matrix_row_fires_with_exact_witness(self) -> None:
        """Dropping 3.12 / linux_x86_64 fires naming that exact interpreter."""
        targets, environments = _blowup_matrix()
        drop = next(
            index
            for index, target in enumerate(targets)
            if target.python_version == "3.12"
            and target.marker_env["sys_platform"] == "linux"
            and target.marker_env["platform_machine"] == "x86_64"
        )
        remaining = [row for index, row in enumerate(environments) if index != drop]
        assert len(remaining) == len(environments) - 1
        with pytest.raises(CoverageError) as excinfo:
            validate_marker_coverage(targets, environments=remaining)
        message = str(excinfo.value)
        assert 'python_full_version == "3.12"' in message
        assert 'sys_platform == "linux"' in message
        assert 'platform_machine == "x86_64"' in message
        _assert_witness_sound(excinfo.value, targets, remaining)


def _random_points(rng: random.Random) -> list[tuple[str, str]]:
    """Draw 1-4 distinct ``(minor, platform)`` points."""
    grid = [(minor, platform) for minor in MINORS for platform in PLATFORMS]
    return rng.sample(grid, rng.randint(1, 4))


def _random_boundaries(rng: random.Random) -> list[int]:
    """Draw 0-2 distinct interior micro boundaries."""
    return rng.sample(BOUNDARY_MICROS, rng.randint(0, 2))


class TestCoverageOverRandomEmitShapes:
    """A bounded, deterministic differential over random emit shapes.

    The hypothesis properties shrink toward small counterexamples; this
    seeds a fixed pseudo-random stream and drives a larger corpus of whole
    emit shapes through the gate.  Every shape must be covering as emitted,
    and dropping any one row must leave a real gap the gate fires on with a
    sound witness.
    """

    def test_random_emit_shapes_cover_and_gaps_fire(self) -> None:
        """Emitted rows cover; a dropped row fires with a sound witness."""
        rng = random.Random(20260722)  # noqa: S311
        for _ in range(300):
            matrix_points = _random_points(rng)
            variable = rng.choice(VERSION_VARIABLES)
            targets, environments = _emit(
                matrix_points, _random_boundaries(rng), variable
            )
            try:
                validate_marker_coverage(targets, environments=environments)
            except IntractableMarkerSet:
                continue

            # Dropping the sole row empties ``environments``, which the gate
            # reads as an omitted field covering everything, so a gap needs a
            # row left behind.
            if len(environments) < 2:
                continue
            drop = rng.randrange(len(environments))
            remaining = [row for index, row in enumerate(environments) if index != drop]
            try:
                validate_marker_coverage(targets, environments=remaining)
            except IntractableMarkerSet:
                continue
            except CoverageError as exc:
                env = _witness_environment(exc)
                assert environments[drop].evaluate(env)
                _assert_witness_sound(exc, targets, remaining)
            else:
                msg = "dropping one row must leave a gap the gate fires on"
                raise AssertionError(msg)
