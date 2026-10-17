"""Bounded conversion reuse preserves the public factory's selected environments."""

from __future__ import annotations

import re
import sys
import warnings

import pytest
from packaging.markers import Marker as ExternalMarker

from nab_markersets import conversions as conversion_module
from nab_markersets._packaging import (
    InvalidMarker,
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
)
from nab_markersets.conversions import MarkerSetCache
from nab_markersets.errors import IntractableMarkerSet
from nab_markersets.markersets import MarkerSet

ENV = {
    "python_version": "3.11",
    "python_full_version": "3.11.2",
    "sys_platform": "linux",
    "os_name": "posix",
    "extra": frozenset(),
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
    evaluator = MarkerSetCache()
    marker = Marker('sys_platform == "linux"')
    assert evaluator.from_marker(marker).evaluate(ENV)
    assert not evaluator.from_marker(Marker(str(marker))).evaluate(
        {**ENV, "sys_platform": "win32"}
    )
    with pytest.raises(UndefinedEnvironmentName):
        evaluator.from_marker(marker).evaluate({})
    assert len(conversions) == 1
    assert not evaluator.from_marker(Marker('sys_platform == "win32"')).evaluate(ENV)
    assert len(conversions) == 2


@pytest.mark.parametrize("size", [255, 256, 257])
def test_text_admission_boundary(conversions: list[str | Marker], size: int) -> None:
    prefix = len(str(Marker('os_name == ""')))
    literal = "x" * (size - prefix)
    marker = Marker(f'os_name == "{literal}"')
    assert len(str(marker)) == size
    evaluator = MarkerSetCache()
    assert evaluator.from_marker(marker).evaluate({"os_name": literal})
    assert evaluator.from_marker(marker).evaluate({"os_name": literal})
    assert len(conversions) == (1 if size <= 256 else 2)


def test_capacity_keeps_admitted_hits_without_retaining_new_keys(
    monkeypatch: pytest.MonkeyPatch, conversions: list[str | Marker]
) -> None:
    monkeypatch.setattr(conversion_module, "_SHORT_SET_LIMIT", 2)
    evaluator = MarkerSetCache()
    markers = [Marker(f'os_name == "p{index}"') for index in range(3)]
    for marker in markers:
        assert not evaluator.from_marker(marker).evaluate(ENV)
    assert not evaluator.from_marker(markers[0]).evaluate(ENV)
    assert not evaluator.from_marker(markers[2]).evaluate(ENV)
    assert len(conversions) == 4


@pytest.mark.parametrize(
    "text",
    [
        '(os_name == "posix" or sys_platform == "linux") and extra == "a"',
        'os_name == "(posix"',
        'os_name == "posix)"',
        'os_name == "\\\\x41"',
    ],
)
def test_parentheses_and_escapes_keep_fresh_conversion(
    conversions: list[str | Marker], text: str
) -> None:
    evaluator = MarkerSetCache()
    marker = Marker(text)
    assert evaluator.from_marker(marker).evaluate(ENV) == evaluator.from_marker(
        marker
    ).evaluate(ENV)
    assert len(conversions) == 2


def test_failed_conversions_are_retried(conversions: list[str | Marker]) -> None:
    evaluator = MarkerSetCache()
    marker = Marker('os_name ~= "posix"')
    for _ in range(2):
        with pytest.raises(UndefinedComparison):
            evaluator.from_marker(marker).evaluate(ENV)
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
    evaluator = MarkerSetCache()
    assert evaluator.from_marker(marker).evaluate(ENV)
    assert evaluator.from_marker(marker).evaluate(ENV)
    assert conversions == [marker, marker]
    assert rendered == [marker, marker]


def test_native_escaped_rendering_keeps_the_reparsed_value(
    conversions: list[str | Marker],
) -> None:
    evaluator = MarkerSetCache()
    marker = Marker('os_name == "\\\\x41"')
    assert evaluator.from_marker(marker).evaluate({"os_name": "A"})
    assert not evaluator.from_marker(marker).evaluate({"os_name": "\\x41"})
    assert len(conversions) == 2


def test_recursion_limit_changes_start_a_new_conversion_context(
    conversions: list[str | Marker],
) -> None:
    evaluator = MarkerSetCache()
    marker = Marker('os_name == "posix"')
    previous = sys.getrecursionlimit()
    try:
        assert evaluator.from_marker(marker).evaluate(ENV)
        sys.setrecursionlimit(previous + 1)
        assert evaluator.from_marker(marker).evaluate(ENV)
        assert evaluator.from_marker(marker).evaluate(ENV)
        sys.setrecursionlimit(previous)
        assert evaluator.from_marker(marker).evaluate(ENV)
    finally:
        sys.setrecursionlimit(previous)
    assert len(conversions) == 2


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"), reason="requires a native digit limit"
)
def test_cached_short_conversion_still_checks_current_environment_digit_limits(
    conversions: list[str | Marker],
) -> None:
    evaluator = MarkerSetCache()
    marker = Marker('python_full_version >= "1.0"')
    previous = sys.get_int_max_str_digits()
    environment = {"python_full_version": "9" * 700}
    try:
        sys.set_int_max_str_digits(0)
        assert evaluator.from_marker(marker).evaluate(environment)
        sys.set_int_max_str_digits(640)
        with pytest.raises(IntractableMarkerSet, match="parse limit"):
            evaluator.from_marker(marker).evaluate(environment)
    finally:
        sys.set_int_max_str_digits(previous)
    assert len(conversions) == 1


def test_warning_filter_changes_still_reparse_escaped_literals(
    conversions: list[str | Marker],
) -> None:
    evaluator = MarkerSetCache()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        marker = Marker('os_name == "\\q"')
        assert evaluator.from_marker(marker).evaluate({"os_name": "\\q"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            expected = MarkerSet.from_marker(marker).evaluate({"os_name": "\\q"})
        except (InvalidMarker, SyntaxWarning, DeprecationWarning) as error:
            with pytest.raises(type(error), match=re.escape(str(error))):
                evaluator.from_marker(marker).evaluate({"os_name": "\\q"})
        else:
            assert (
                evaluator.from_marker(marker).evaluate({"os_name": "\\q"}) is expected
            )
    assert len(conversions) == 3


@pytest.mark.skipif(Marker is ExternalMarker, reason="requires both packaging backends")
def test_foreign_markers_keep_the_original_conversion_path(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    evaluator = MarkerSetCache()
    native = Marker('sys_platform == "linux"')
    foreign = ExternalMarker('sys_platform == "linux"')
    assert evaluator.from_marker(native).evaluate(ENV)
    assert evaluator.from_marker(foreign).evaluate(ENV)
    assert evaluator.from_marker(foreign).evaluate(ENV)
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
        with pytest.raises(IntractableMarkerSet) as expected:
            MarkerSet.from_marker(marker).evaluate({"os_name": "posix"})
        with pytest.raises(IntractableMarkerSet) as actual:
            MarkerSetCache().from_marker(marker).evaluate({"os_name": "posix"})
        assert actual.value.args == expected.value.args
    finally:
        sys.setrecursionlimit(previous)


@pytest.mark.parametrize(
    "source",
    [
        'sys_platform == "linux"',
        'sys_platform != "linux"',
        'os_name == "posix" and sys_platform == "linux"',
        'os_name == "posix" or sys_platform == "win32"',
        'python_version >= "3.13"',
        'python_full_version < "3.13.2"',
        'implementation_version >= "3.13"',
        '"3.13" == "python_version"',
        '"a" == "a"',
        '"a" == "b"',
        'extra == "feature"',
        '"gpu" in extras and "dev" not in dependency_groups',
        'os_name == "posix" and os_name == "nt"',
        'sys_platform == "linux" or sys_platform != "linux"',
    ],
)
def test_cached_sets_match_fresh_construction_across_environments(source: str) -> None:
    cache = MarkerSetCache()
    marker = Marker(source)
    environments = [
        {
            **ENV,
            "implementation_version": "3.13.2",
            "python_full_version": "3.13.2",
            "extra": frozenset({"feature"}),
            "extras": frozenset({"gpu"}),
            "dependency_groups": frozenset(),
        },
        {
            **ENV,
            "os_name": "nt",
            "sys_platform": "win32",
            "implementation_version": "3.12.4",
            "python_full_version": "3.12.4",
            "extra": frozenset(),
            "extras": frozenset(),
            "dependency_groups": frozenset({"dev"}),
        },
    ]
    for current in (marker, Marker(str(marker)), marker):
        reused = cache.from_marker(current)
        fresh = MarkerSet.from_marker(current)
        assert reused.equivalent(fresh)
        assert reused.is_empty() == fresh.is_empty()
        assert reused.is_full() == fresh.is_full()
        assert [reused.evaluate(env) for env in environments] == [
            fresh.evaluate(env) for env in environments
        ]


def test_strings_and_string_subclasses_remain_fresh(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    class Source(str):
        """A string subclass accepted by the ordinary constructor."""

        __slots__ = ()

    cache = MarkerSetCache()
    for source in ('os_name == "posix"', Source('os_name == "posix"')):
        assert cache.from_marker(source).evaluate(ENV)
        assert cache.from_marker(source).evaluate(ENV)
    assert len(conversions) == 4
    assert type(conversions[-1]) is Source


def test_context_change_during_conversion_is_not_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = MarkerSet.from_marker
    previous = sys.getrecursionlimit()
    calls = 0

    def changed(_cls: type[MarkerSet], source: str | Marker) -> MarkerSet:
        nonlocal calls
        calls += 1
        result = original(source)
        if calls == 1:
            sys.setrecursionlimit(previous + 1)
        return result

    monkeypatch.setattr(MarkerSet, "from_marker", classmethod(changed))
    cache = MarkerSetCache()
    marker = Marker('os_name == "posix"')
    try:
        assert cache.from_marker(marker).evaluate(ENV)
        assert cache.from_marker(marker).evaluate(ENV)
        sys.setrecursionlimit(previous)
        assert cache.from_marker(marker).evaluate(ENV)
        assert cache.from_marker(marker).evaluate(ENV)
    finally:
        sys.setrecursionlimit(previous)
    assert calls == 3
