"""Joint construction preserves names from the original marker syntax."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from packaging.markers import Marker as ExternalMarker

from nab_markersets import _markersets
from nab_markersets._packaging import InvalidMarker, Marker, UndefinedComparison
from nab_markersets.errors import IntractableMarkerSet
from nab_markersets.markersets import MarkerSet, variable_names


@pytest.mark.parametrize(
    ("source", "names"),
    [
        ("", frozenset()),
        ("   ", frozenset()),
        ('sys_platform == "linux"', frozenset({"sys_platform"})),
        ('python_version >= "3.9"', frozenset({"python_version"})),
        ('os.name == "posix"', frozenset({"os_name"})),
        (
            'python_implementation == "CPython"',
            frozenset({"platform_python_implementation"}),
        ),
        ('"3.9" == "python_version"', frozenset({"python_version"})),
        ('"linux" == sys_platform', frozenset({"sys_platform"})),
        ('"implementation_version" == "literal"', frozenset()),
        (
            '"a" == "b" and implementation_version == "3.13"',
            frozenset({"implementation_version"}),
        ),
        (
            'sys_platform == "linux" or (os_name == "posix" and python_version > "3.8")',
            frozenset({"sys_platform", "os_name", "python_version"}),
        ),
    ],
)
def test_joint_construction_preserves_raw_names(
    source: str, names: frozenset[str]
) -> None:
    converted, actual = MarkerSet.from_marker_with_variables(source)
    assert actual == names == variable_names(source)
    assert converted.equivalent(MarkerSet.from_marker(source))


def test_joint_construction_accepts_native_marker() -> None:
    converted, names = MarkerSet.from_marker_with_variables(
        Marker('sys_platform == "linux"')
    )
    assert names == frozenset({"sys_platform"})
    assert converted.evaluate({"sys_platform": "linux"})
    assert not converted.evaluate({"sys_platform": "win32"})


def test_joint_construction_accepts_foreign_marker() -> None:
    converted, names = MarkerSet.from_marker_with_variables(
        ExternalMarker('sys_platform == "linux"')
    )
    assert names == frozenset({"sys_platform"})
    assert converted.evaluate({"sys_platform": "linux"})
    assert not converted.evaluate({"sys_platform": "win32"})


def test_joint_construction_accepts_string_subclass() -> None:
    class Source(str):
        __slots__ = ()

        def __str__(self) -> str:
            return 'os_name == "nt"'

    converted, names = MarkerSet.from_marker_with_variables(
        Source('sys_platform == "linux"')
    )
    assert names == frozenset({"sys_platform"})
    assert converted.evaluate({"sys_platform": "linux"})
    assert not converted.evaluate({"sys_platform": "win32"})


def test_joint_construction_uses_one_rendered_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Marker] = []

    def changing(marker: Marker) -> str:
        calls.append(marker)
        return 'sys_platform == "linux"' if len(calls) == 1 else 'os_name == "nt"'

    marker = Marker('sys_platform == "linux"')
    monkeypatch.setattr(Marker, "__str__", changing)
    converted, names = MarkerSet.from_marker_with_variables(marker)
    assert calls == [marker]
    assert names == frozenset({"sys_platform"})
    assert converted.evaluate({"sys_platform": "linux"})
    assert not converted.evaluate({"sys_platform": "win32"})


def test_joint_construction_rejects_marker_subclasses_like_ordinary_construction() -> (
    None
):
    class Derived(Marker):
        pass

    marker = Derived('sys_platform == "linux"')
    for constructor in (MarkerSet.from_marker, MarkerSet.from_marker_with_variables):
        with pytest.raises(TypeError, match="expected str or packaging.markers.Marker"):
            constructor(marker)


def test_ordinary_construction_does_not_collect_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_node: Sequence[_markersets.MarkerNode], _names: set[str]) -> None:
        pytest.fail("ordinary construction collected variable names")

    monkeypatch.setattr(_markersets, "_collect_variables", unexpected)
    converted = MarkerSet.from_marker('sys_platform == "linux"')
    assert converted.evaluate({"sys_platform": "linux"})
    assert not converted.evaluate({"sys_platform": "win32"})


def test_joint_construction_parses_once(monkeypatch: pytest.MonkeyPatch) -> None:
    original = _markersets.parse_marker
    calls: list[str] = []

    def parse(source: str) -> Sequence[_markersets.MarkerNode]:
        calls.append(source)
        return original(source)

    monkeypatch.setattr(_markersets, "parse_marker", parse)
    converted, names = MarkerSet.from_marker_with_variables('python_version >= "3.9"')
    assert calls == ['python_version >= "3.9"']
    assert names == frozenset({"python_version"})
    assert converted.evaluate({"python_full_version": "3.10"})


def test_joint_constructor_failure_precedes_name_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _markersets._collect_variables
    calls: list[Sequence[_markersets.MarkerNode]] = []

    def collect(node: Sequence[_markersets.MarkerNode], names: set[str]) -> None:
        calls.append(node)
        original(node, names)

    monkeypatch.setattr(_markersets, "_collect_variables", collect)
    with pytest.raises(UndefinedComparison):
        MarkerSet.from_marker_with_variables('sys_platform ~= "linux"')
    assert calls == []


def test_joint_constructor_translates_parser_stack_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_source: str) -> Sequence[_markersets.MarkerNode]:
        raise RecursionError

    monkeypatch.setattr(_markersets, "parse_marker", refuse)
    with pytest.raises(IntractableMarkerSet):
        MarkerSet.from_marker_with_variables('sys_platform == "linux"')


@pytest.mark.parametrize("source", ["not a marker", 42])
def test_joint_constructor_rejects_invalid_input(source: str | int) -> None:
    with pytest.raises((InvalidMarker, TypeError)):
        MarkerSet.from_marker_with_variables(source)  # type: ignore[arg-type]
