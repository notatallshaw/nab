"""Prepared environment rows retain the coverage projection decision."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from nab_markersets._packaging import UndefinedComparison
from nab_markersets.markersets import DecisionStore, MarkerSet
from nab_project._lockfile import coverage, pylock
from nab_project.lockfile import LockInput, TargetLock
from nab_provider._vendor.packaging.markers import Marker
from nab_provider.tags import PlatformSpec
from nab_provider.target import (
    ResolveTarget,
    environment_declaration,
    micro_boundary_points,
    slices_from_points,
)


@pytest.mark.parametrize(
    ("texts", "expected", "joint_calls"),
    [
        ([], False, 0),
        (['sys_platform == "linux"', 'os_name == "posix"'], False, 2),
        (['implementation_version >= "3.11"', 'os_name == "posix"'], True, 1),
        (['os_name == "posix"', 'implementation_version >= "3.11"'], True, 2),
        (
            ['"a" == "b" and implementation_version >= "3.11"', 'os_name == "nt"'],
            True,
            1,
        ),
    ],
)
def test_rows_are_fresh_and_collect_names_until_projection_is_needed(
    texts: list[str], expected: bool, joint_calls: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    markers = [Marker(text) for text in texts]
    converted_inputs: list[Marker] = []
    ordinary_inputs: list[Marker] = []
    original_joint = MarkerSet.from_marker_with_variables
    original_ordinary = MarkerSet.from_marker

    def joint(
        _cls: type[MarkerSet], marker: Marker
    ) -> tuple[MarkerSet, frozenset[str]]:
        converted_inputs.append(marker)
        return original_joint(marker)

    def ordinary(_cls: type[MarkerSet], marker: Marker) -> MarkerSet:
        ordinary_inputs.append(marker)
        return original_ordinary(marker)

    monkeypatch.setattr(MarkerSet, "from_marker_with_variables", classmethod(joint))
    monkeypatch.setattr(MarkerSet, "from_marker", classmethod(ordinary))
    first, flag = pylock._prepare_environment_rows(markers)
    second, again = pylock._prepare_environment_rows(markers)

    assert flag is again is expected
    assert converted_inputs == markers[:joint_calls] * 2
    assert ordinary_inputs == markers[joint_calls:] * 2
    assert all(a is not b for a, b in zip(first, second, strict=True))
    assert all(
        row.equivalent(original_ordinary(marker))
        for row, marker in zip(first, markers, strict=True)
    )


def test_later_conversion_still_fails_after_projection_flag_is_known() -> None:
    with pytest.raises(UndefinedComparison):
        pylock._prepare_environment_rows(
            [Marker('implementation_version >= "3.11"'), Marker('os_name ~= "posix"')]
        )


def _sliced_rows() -> tuple[list[ResolveTarget], list[Marker]]:
    """Emit both sides of a real mirrored CPython patch boundary."""
    target = ResolveTarget.for_declared(
        python_version="3.13", spec=PlatformSpec("linux_x86_64")
    )
    consulted = [Marker('implementation_version >= "3.13.2"')]
    targets = slices_from_points(target, micro_boundary_points(target, consulted))
    return targets, [
        Marker(environment_declaration(piece, consulted)) for piece in targets
    ]


def test_prepared_flag_preserves_mirrored_coverage_and_real_holes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets, markers = _sliced_rows()
    rows, flag = pylock._prepare_environment_rows(markers)
    assert flag

    def unexpected(_marker: str | Marker) -> frozenset[str]:
        pytest.fail("coverage reparsed prepared rows for their names")

    monkeypatch.setattr(coverage, "variable_names", unexpected)
    coverage.validate_marker_coverage(
        targets,
        environments=markers,
        environment_sets=rows,
        uses_implementation_version=flag,
    )
    with pytest.raises(coverage.CoverageError, match="python_full_version"):
        coverage.validate_marker_coverage(
            targets,
            environments=markers[:1],
            environment_sets=rows[:1],
            uses_implementation_version=flag,
        )


def test_omitted_flag_queries_rows_but_explicit_false_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers = [Marker('sys_platform == "linux"')]
    rows, flag = pylock._prepare_environment_rows(markers)
    assert not flag
    queried: list[Marker] = []
    original = coverage.variable_names

    def observed(marker: Marker) -> frozenset[str]:
        queried.append(marker)
        return original(marker)

    monkeypatch.setattr(coverage, "variable_names", observed)
    coverage.validate_marker_coverage([], environments=markers, environment_sets=rows)
    assert queried == markers

    coverage.validate_marker_coverage(
        [],
        environments=markers,
        environment_sets=rows,
        uses_implementation_version=flag,
    )
    assert queried == markers


@pytest.mark.parametrize("mirrored", [False, True])
def test_build_passes_its_own_row_snapshot_to_coverage(
    mirrored: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if mirrored:
        targets, markers = _sliced_rows()
    else:
        target = ResolveTarget.for_declared(
            python_version="3.13", spec=PlatformSpec("linux_x86_64")
        )
        targets = [target]
        markers = [Marker(environment_declaration(target, []))]
    lock = LockInput(
        targets={
            target.label: TargetLock(target=target, pins={}) for target in targets
        },
        environments=markers,
    )
    snapshots: list[tuple[Sequence[Marker], Sequence[MarkerSet], bool]] = []
    original = pylock.validate_marker_coverage

    def observed(
        targets: Sequence[ResolveTarget],
        *,
        environments: Sequence[Marker],
        store: DecisionStore | None = None,
        environment_sets: Sequence[MarkerSet] | None = None,
        uses_implementation_version: bool | None = None,
    ) -> None:
        assert environment_sets is not None
        assert uses_implementation_version is mirrored
        snapshots.append((environments, environment_sets, uses_implementation_version))
        original(
            targets,
            environments=environments,
            store=store,
            environment_sets=environment_sets,
            uses_implementation_version=uses_implementation_version,
        )

    monkeypatch.setattr(pylock, "validate_marker_coverage", observed)
    pylock.build_pylock(lock)
    pylock.build_pylock(lock)
    assert len(snapshots) == 2
    assert snapshots[0][1] is not snapshots[1][1]
