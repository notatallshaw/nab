"""Resolve-owned conversions keep evaluation and rendering boundaries live."""

from __future__ import annotations

import re
import sys
import warnings

import pytest
from packaging.markers import Marker as ExternalMarker

from nab_markersets.markersets import MarkerSet
from nab_provider import marker_holds
from nab_provider._vendor.packaging.markers import (
    InvalidMarker,
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


@pytest.mark.parametrize("size", [255, 256, 257])
def test_text_admission_boundary(conversions: list[str | Marker], size: int) -> None:
    prefix = len(str(Marker('os_name == ""')))
    literal = "x" * (size - prefix)
    marker = Marker(f'os_name == "{literal}"')
    assert len(str(marker)) == size
    evaluator = DependencyMarkerEvaluator()
    assert evaluator(marker, {"os_name": literal})
    assert evaluator(marker, {"os_name": literal})
    assert len(conversions) == (1 if size <= 256 else 2)


def test_capacity_keeps_admitted_hits_without_retaining_new_keys(
    monkeypatch: pytest.MonkeyPatch, conversions: list[str | Marker]
) -> None:
    monkeypatch.setattr(marker_holds, "_MARKER_SET_CACHE_SIZE", 2)
    evaluator = DependencyMarkerEvaluator()
    markers = [Marker(f'os_name == "p{index}"') for index in range(3)]
    for marker in markers:
        assert not evaluator(marker, ENV)
    assert not evaluator(markers[0], ENV)
    assert not evaluator(markers[2], ENV)
    assert len(conversions) == 4


@pytest.mark.parametrize(
    "text",
    [
        '(os_name == "posix" or sys_platform == "linux") and extra == "a"',
        'os_name == "(posix"',
        'os_name == "posix)"',
        r'os_name == "\\x41"',
    ],
)
def test_parentheses_and_escapes_keep_fresh_conversion(
    conversions: list[str | Marker], text: str
) -> None:
    evaluator = DependencyMarkerEvaluator()
    marker = Marker(text)
    assert evaluator(marker, ENV) == evaluator(marker, ENV)
    assert len(conversions) == 2


def test_failed_conversions_are_retried(conversions: list[str | Marker]) -> None:
    evaluator = DependencyMarkerEvaluator()
    marker = Marker('os_name ~= "posix"')
    for _ in range(2):
        with pytest.raises(UnevaluableMarkerError):
            evaluator(marker, ENV)
    assert len(conversions) == 2


def test_custom_native_rendering_stays_on_the_original_path(
    monkeypatch: pytest.MonkeyPatch, conversions: list[str | Marker]
) -> None:
    original = Marker.__str__
    rendered: list[Marker] = []

    def observed(marker: Marker) -> str:
        rendered.append(marker)
        return original(marker)

    monkeypatch.setattr(Marker, "__str__", observed)
    marker = Marker('sys_platform == "linux"')
    evaluator = DependencyMarkerEvaluator()
    assert evaluator(marker, ENV)
    assert evaluator(marker, ENV)
    assert conversions == [marker, marker]
    assert rendered == [marker, marker]


def test_native_escaped_rendering_keeps_the_reparsed_value(
    conversions: list[str | Marker],
) -> None:
    evaluator = DependencyMarkerEvaluator()
    marker = Marker(r'os_name == "\\x41"')
    assert evaluator(marker, {"os_name": "A"})
    assert not evaluator(marker, {"os_name": r"\x41"})
    assert len(conversions) == 2


def test_recursion_limit_changes_start_a_new_conversion_context(
    conversions: list[str | Marker],
) -> None:
    evaluator = DependencyMarkerEvaluator()
    marker = Marker('os_name == "posix"')
    previous = sys.getrecursionlimit()
    try:
        assert evaluator(marker, ENV)
        sys.setrecursionlimit(previous + 1)
        assert evaluator(marker, ENV)
        assert evaluator(marker, ENV)
        sys.setrecursionlimit(previous)
        assert evaluator(marker, ENV)
    finally:
        sys.setrecursionlimit(previous)
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


def test_warning_filter_changes_still_reparse_escaped_literals(
    conversions: list[str | Marker],
) -> None:
    evaluator = DependencyMarkerEvaluator()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        marker = Marker(r'os_name == "\q"')
        assert evaluator(marker, {"os_name": r"\q"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            expected = marker_holds.dependency_marker_holds(marker, {"os_name": r"\q"})
        except (InvalidMarker, SyntaxWarning, DeprecationWarning) as error:
            with pytest.raises(type(error), match=re.escape(str(error))):
                evaluator(marker, {"os_name": r"\q"})
        else:
            assert evaluator(marker, {"os_name": r"\q"}) is expected
    assert len(conversions) == 3


def test_foreign_markers_keep_the_original_conversion_path(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    evaluator = DependencyMarkerEvaluator()
    native = Marker('sys_platform == "linux"')
    foreign = ExternalMarker('sys_platform == "linux"')
    assert evaluator(native, ENV)
    assert evaluator(foreign, ENV)
    assert evaluator(foreign, ENV)
    assert len(conversions) == 3
    assert conversions[1] is foreign
    assert conversions[2] is foreign


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
