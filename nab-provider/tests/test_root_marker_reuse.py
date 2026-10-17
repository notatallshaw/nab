"""Resolve-owned conversions keep evaluation and rendering boundaries live."""

from __future__ import annotations

import sys

import pytest
from packaging.markers import Marker as ExternalMarker

from nab_markersets.markersets import MarkerSet
from nab_provider import marker_holds
from nab_provider._vendor.packaging.markers import (
    Marker,
    UndefinedEnvironmentName,
)
from nab_provider.marker_holds import (
    DependencyMarkerEvaluator,
    IntractableMarkerError,
    UnevaluableMarkerError,
)

ENV = {
    "python_version": "3.11",
    "python_full_version": "3.11.2",
    "sys_platform": "linux",
    "os_name": "posix",
}


@pytest.fixture
def conversions(monkeypatch: pytest.MonkeyPatch) -> list[str | Marker | ExternalMarker]:
    """Observe actual factory calls while retaining their original behavior."""
    calls: list[str | Marker | ExternalMarker] = []
    original = MarkerSet.from_marker

    def observed(
        _cls: type[MarkerSet], source: str | Marker | ExternalMarker
    ) -> MarkerSet:
        calls.append(source)
        return original(source)

    monkeypatch.setattr(MarkerSet, "from_marker", classmethod(observed))
    return calls


def test_equal_native_texts_reuse_conversion_but_evaluate_each_environment(
    conversions: list[str | Marker],
) -> None:
    evaluator = DependencyMarkerEvaluator()
    marker = Marker('sys_platform == "linux"')
    assert evaluator(marker, ENV)
    assert not evaluator(Marker(str(marker)), {**ENV, "sys_platform": "win32"})
    with pytest.raises(UndefinedEnvironmentName):
        evaluator(marker, {})
    assert len(conversions) == 1

    assert not evaluator(Marker('sys_platform == "win32"'), ENV)
    assert len(conversions) == 2


def test_another_resolve_and_the_standalone_function_keep_their_own_work(
    conversions: list[str | Marker],
) -> None:
    marker = Marker('sys_platform == "linux"')
    first, second = DependencyMarkerEvaluator(), DependencyMarkerEvaluator()
    assert first(marker, ENV)
    assert first(marker, ENV)
    assert second(marker, ENV)
    assert marker_holds.dependency_marker_holds(marker, ENV)
    assert marker_holds.dependency_marker_holds(marker, ENV)
    assert len(conversions) == 4


def test_failed_conversions_are_retried(conversions: list[str | Marker]) -> None:
    evaluator = DependencyMarkerEvaluator()
    marker = Marker('os_name ~= "posix"')
    for _ in range(2):
        with pytest.raises(UnevaluableMarkerError):
            evaluator(marker, ENV)
    assert len(conversions) == 2


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"), reason="requires a native digit limit"
)
def test_cached_short_conversion_still_checks_current_environment_digit_limits(
    conversions: list[str | Marker],
) -> None:
    evaluator = DependencyMarkerEvaluator()
    marker = Marker('python_full_version >= "1.0"')
    previous = sys.get_int_max_str_digits()
    environment = {"python_full_version": "9" * 700}
    try:
        sys.set_int_max_str_digits(0)
        assert evaluator(marker, environment)
        sys.set_int_max_str_digits(640)
        with pytest.raises(IntractableMarkerError, match="parse limit"):
            evaluator(marker, environment)
    finally:
        sys.set_int_max_str_digits(previous)
    assert len(conversions) == 1


def test_native_rendering_recursion_keeps_the_bounded_error() -> None:
    frame = sys._getframe()
    depth = 0
    while frame is not None:
        depth += 1
        frame = frame.f_back
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(previous, depth + 1000))
        marker = Marker("(" * 100 + 'os_name == "posix"' + ")" * 100)
        sys.setrecursionlimit(depth + 75)
        with pytest.raises(IntractableMarkerError) as expected:
            marker_holds.dependency_marker_holds(marker, {"os_name": "posix"})
        with pytest.raises(IntractableMarkerError) as actual:
            DependencyMarkerEvaluator()(marker, {"os_name": "posix"})
        assert actual.value.args == expected.value.args
    finally:
        sys.setrecursionlimit(previous)


@pytest.mark.parametrize(
    ("text", "environment", "expected"),
    [
        ('extra == "feature"', {}, False),
        ('extra == "feature"', {"extra": frozenset({"feature"})}, True),
        ('extra != "feature"', {}, True),
        ('"gpu" in extras', {}, False),
        ('"gpu" in extras', {"extras": frozenset({"gpu"})}, False),
        (
            '"dev" in dependency_groups',
            {"dependency_groups": frozenset({"dev"})},
            False,
        ),
        ('"dev" not in dependency_groups', {}, True),
    ],
)
def test_cached_evaluator_keeps_dependency_membership_defaults(
    conversions: list[str | Marker],
    text: str,
    environment: dict[str, str | frozenset[str]],
    expected: bool,
) -> None:
    original = environment.copy()
    marker = Marker(text)
    assert marker_holds.dependency_marker_holds(marker, environment) is expected
    evaluator = DependencyMarkerEvaluator()
    assert evaluator(marker, environment) is expected
    assert evaluator(marker, environment) is expected
    assert len(conversions) == 2
    assert environment == original
