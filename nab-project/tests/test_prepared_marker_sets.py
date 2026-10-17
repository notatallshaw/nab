"""Finalizer conversions remain fresh and validator reuse stays within one build."""

from __future__ import annotations

import importlib.util
import re
import sys
import warnings

import pytest
from packaging.markers import Marker as ExternalMarker

from nab_markersets.errors import IntractableMarkerSet
from nab_markersets.markersets import DecisionStore, MarkerSet
from nab_project._lockfile import disjointness, pylock
from nab_project._lockfile.disjointness import PreparedMarkerSets
from nab_provider._vendor.packaging.markers import (
    InvalidMarker,
    Marker,
    UndefinedComparison,
)
from nab_provider.conflict_kind import KIND_EXTRA


@pytest.fixture
def conversions(monkeypatch: pytest.MonkeyPatch) -> list[str | Marker | ExternalMarker]:
    """Observe the real factory without replacing its results or failures."""
    calls: list[str | Marker | ExternalMarker] = []
    original = MarkerSet.from_marker

    def observed(
        _cls: type[MarkerSet], marker: str | Marker | ExternalMarker
    ) -> MarkerSet:
        calls.append(marker)
        return original(marker)

    monkeypatch.setattr(MarkerSet, "from_marker", classmethod(observed))
    return calls


def test_prepare_is_fresh_but_validation_reuses_matching_text(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    marker = Marker('sys_platform == "linux"')
    owner.prepare(marker)
    owner.prepare(marker)
    assert len(conversions) == 2

    same = owner.convert(Marker(str(marker)))
    assert same.evaluate({"sys_platform": "linux"})
    assert not same.evaluate({"sys_platform": "win32"})
    assert not owner.convert(Marker('sys_platform == "win32"')).evaluate(
        {"sys_platform": "linux"}
    )
    assert len(conversions) == 3

    another = PreparedMarkerSets()
    assert another.convert(marker).evaluate({"sys_platform": "linux"})
    assert another.convert(marker).evaluate({"sys_platform": "linux"})
    assert owner.convert(None).evaluate({})
    assert len(conversions) == 5


@pytest.mark.parametrize(
    "literal", ["x" * 64, "x" * 65, "'" * 64, "'" * 65, '"' * 64, '"' * 65]
)
def test_original_quoted_literal_boundary(
    conversions: list[str | Marker | ExternalMarker], literal: str
) -> None:
    quote = "'" if '"' in literal else '"'
    marker = Marker(f"os_name == {quote}{literal}{quote}")
    owner = PreparedMarkerSets()
    owner.prepare(marker)
    assert owner.convert(marker).evaluate({"os_name": literal})
    assert len(conversions) == (1 if len(literal) <= 64 else 2)


def test_capacity_keeps_existing_handoffs(
    monkeypatch: pytest.MonkeyPatch,
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    monkeypatch.setattr(disjointness, "_PREPARED_SET_LIMIT", 2)
    markers = [Marker(f'os_name == "p{index}"') for index in range(3)]
    owner = PreparedMarkerSets()
    for marker in markers:
        owner.prepare(marker)
    assert owner.convert(markers[0]).evaluate({"os_name": "p0"})
    assert not owner.convert(markers[1]).evaluate({"os_name": "p0"})
    assert owner.convert(markers[2]).evaluate({"os_name": "p2"})
    assert len(conversions) == 4


def test_total_text_budget_counts_each_distinct_key_once(
    monkeypatch: pytest.MonkeyPatch,
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    first, second, third = (Marker(f'os_name == "p{index}"') for index in range(3))
    monkeypatch.setattr(
        disjointness, "_PREPARED_TEXT_LIMIT", len(str(first)) + len(str(second))
    )
    owner = PreparedMarkerSets()
    owner.prepare(first)
    owner.prepare(first)
    owner.prepare(second)
    owner.prepare(third)
    assert owner.convert(first).evaluate({"os_name": "p0"})
    assert owner.convert(second).evaluate({"os_name": "p1"})
    assert owner.convert(third).evaluate({"os_name": "p2"})
    assert len(conversions) == 5


def test_failed_preparation_leaves_validation_fresh(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    marker = Marker('os_name ~= "posix"')
    owner = PreparedMarkerSets()

    for convert in (owner.prepare, owner.convert, owner.prepare):
        with pytest.raises(UndefinedComparison):
            convert(marker)
    assert len(conversions) == 3


def test_foreign_and_custom_renderers_are_never_handed_off(
    monkeypatch: pytest.MonkeyPatch,
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    foreign = ExternalMarker('sys_platform == "linux"')
    owner.prepare(foreign)
    assert owner.convert(foreign).evaluate({"sys_platform": "linux"})
    assert conversions == [foreign, foreign]

    original = Marker.__str__
    rendered: list[Marker] = []

    def observed(marker: Marker) -> str:
        rendered.append(marker)
        return original(marker)

    monkeypatch.setattr(Marker, "__str__", observed)
    native = Marker('os_name == "posix"')
    owner.prepare(native)
    assert owner.convert(native).evaluate({"os_name": "posix"})
    assert conversions[2:] == [native, native]
    assert rendered == [native, native]


def test_handoff_keys_the_rendering_converted_by_the_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    texts = iter(
        (
            'sys_platform == "linux"',
            'sys_platform == "win32"',
            'sys_platform == "win32"',
        )
    )

    def changing(_marker: Marker) -> str:
        return next(texts)

    monkeypatch.setattr(Marker, "__str__", changing)
    spec = importlib.util.spec_from_file_location(
        "prepared_renderer_boundary", disjointness.__file__
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owner = module.PreparedMarkerSets()
    marker = Marker('sys_platform == "linux"')
    assert owner.prepare(marker).evaluate({"sys_platform": "linux"})
    assert not owner.convert(marker).evaluate({"sys_platform": "linux"})


def test_warning_filter_changes_keep_escaped_conversions_fresh(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        marker = Marker(r'os_name == "\q"')
        assert owner.prepare(marker).evaluate({"os_name": r"\q"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            expected = MarkerSet.from_marker(marker)
        except (InvalidMarker, SyntaxWarning, DeprecationWarning) as error:
            with pytest.raises(type(error), match=re.escape(str(error))):
                owner.convert(marker)
        else:
            assert owner.convert(marker).evaluate(
                {"os_name": r"\q"}
            ) == expected.evaluate({"os_name": r"\q"})
    assert len(conversions) == 3


def test_current_recursion_limit_selects_the_conversion_context(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    marker = Marker('os_name == "posix"')
    previous = sys.getrecursionlimit()
    try:
        owner.prepare(marker)
        sys.setrecursionlimit(previous + 1)
        assert owner.convert(marker).evaluate({"os_name": "posix"})
        sys.setrecursionlimit(previous)
        assert owner.convert(marker).evaluate({"os_name": "posix"})
    finally:
        sys.setrecursionlimit(previous)
    assert len(conversions) == 2


def test_context_change_during_conversion_prevents_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = MarkerSet.from_marker
    calls = 0
    previous = sys.getrecursionlimit()

    def observed(_cls: type[MarkerSet], source: str | Marker) -> MarkerSet:
        nonlocal calls
        calls += 1
        result = original(source)
        if calls == 1:
            sys.setrecursionlimit(previous + 1)
        return result

    monkeypatch.setattr(MarkerSet, "from_marker", classmethod(observed))
    owner = PreparedMarkerSets()
    marker = Marker('os_name == "posix"')
    try:
        owner.prepare(marker)
        assert owner.convert(marker).evaluate({"os_name": "posix"})
        sys.setrecursionlimit(previous)
        assert owner.convert(marker).evaluate({"os_name": "posix"})
    finally:
        sys.setrecursionlimit(previous)
    assert calls == 3


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"), reason="requires a native digit limit"
)
def test_folded_away_long_original_literal_is_not_retained(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    previous = sys.get_int_max_str_digits()
    owner = PreparedMarkerSets()
    try:
        sys.set_int_max_str_digits(0)
        marker = Marker(
            'extras == "unused" and python_full_version ~= "' + "9" * 700 + '.0"'
        )
        assert owner.prepare(marker).is_empty()
        sys.set_int_max_str_digits(640)
        with pytest.raises(IntractableMarkerSet, match="parse limit"):
            owner.convert(marker)
    finally:
        sys.set_int_max_str_digits(previous)
    assert len(conversions) == 2


def test_native_rendering_recursion_uses_the_factory_error() -> None:
    frame, depth = sys._getframe(), 0
    while frame is not None:
        frame, depth = frame.f_back, depth + 1
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(previous, depth + 1000))
        marker = Marker("(" * 100 + 'os_name == "posix"' + ")" * 100)
        sys.setrecursionlimit(depth + 75)
        with pytest.raises(IntractableMarkerSet) as expected:
            MarkerSet.from_marker(marker)
        with pytest.raises(IntractableMarkerSet) as actual:
            PreparedMarkerSets().convert(marker)
        assert actual.value.args == expected.value.args
    finally:
        sys.setrecursionlimit(previous)


def test_finalizer_hands_off_the_emitted_and_fallback_sets(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    raw = Marker('sys_platform == "linux" and os_name == "posix"')
    within = MarkerSet.from_marker('os_name == "posix"')
    emitted = pylock._finalize_marker(raw, within, prepared_sets=owner)
    assert emitted is not None
    assert str(emitted) != str(raw)
    before = len(conversions)
    assert owner.convert(emitted).evaluate({"sys_platform": "linux", "os_name": "nt"})
    assert not owner.convert(emitted).evaluate(
        {"sys_platform": "win32", "os_name": "nt"}
    )
    assert len(conversions) == before

    raw = Marker('extras == "unused"')
    assert pylock._finalize_marker(raw, MarkerSet.full(), prepared_sets=owner) is raw
    before = len(conversions)
    assert owner.convert(raw).is_empty()
    assert len(conversions) == before


def test_final_assembly_hands_off_the_complete_gated_marker(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    parts = [
        pylock._GatedMarker(Marker('sys_platform == "linux"'), ((KIND_EXTRA, "a"),)),
        pylock._GatedMarker(Marker('sys_platform == "win32"'), ((KIND_EXTRA, "b"),)),
    ]
    marker = pylock._finalize_parts(
        parts, MarkerSet.full(), "demo", {}, DecisionStore(), prepared_sets=owner
    )
    assert marker is not None
    before = len(conversions)
    converted = owner.convert(marker)
    assert converted.evaluate({"sys_platform": "linux", "extras": frozenset({"a"})})
    assert not converted.evaluate({"sys_platform": "linux", "extras": frozenset({"b"})})
    assert converted.evaluate({"sys_platform": "win32", "extras": frozenset({"b"})})
    assert not converted.evaluate({"sys_platform": "win32", "extras": frozenset({"a"})})
    assert len(conversions) == before
