"""Generated TOML preserves values independently of its table layout."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timezone

import pytest

from nab_project import toml_io
from nab_project.toml_writer import dumps, dumps_value


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"empty": {}},
        {"items": []},
        {"text": 'line\n"\\\t\x00\x1f\x7f雪😀'},
        {"a.b": {"": "value", 'a"b': "quoted key"}},
        {"values": [1, True, False, 2.5, "text", []]},
        {"rows": [{"value": 1}, {"value": 2}]},
        {"rows": [{"nested": {"value": 1}}, {"nested": {"value": 2}}]},
        {"mixed": [1, {"nested": [True, {"quoted.key": "text"}]}]},
        {
            "created-at": datetime(2026, 1, 2, 3, 4, 5, 1, tzinfo=timezone.utc),
            "local-datetime": datetime(
                2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
            ).replace(tzinfo=None),
            "date": date(2026, 1, 2),
            "time": time(3, 4, 5, 123456),
        },
    ],
)
def test_roundtrip(document: dict[str, object]) -> None:
    assert toml_io.loads(dumps(document)) == document


def test_tuple_is_written_as_array() -> None:
    assert toml_io.loads(dumps({"values": (1, "text")})) == {"values": [1, "text"]}


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"tool": {"nab": {"version": "1"}}}, '[tool.nab]\nversion = "1"\n'),
        ({"tool": {}}, "[tool]\n"),
        ({"tool": {"nab": {}}}, "[tool.nab]\n"),
        ({"tool": {"nab": {"rows": [{}]}}}, "[[tool.nab.rows]]\n"),
    ],
)
def test_parent_headers_are_omitted_when_children_define_them(
    document: dict[str, object], expected: str
) -> None:
    text = dumps(document)
    assert text == expected
    assert toml_io.loads(text) == document


@pytest.mark.parametrize("key", ["environments", "command-line", "platforms"])
def test_array_entries_are_on_separate_lines(key: str) -> None:
    text = dumps({key: ["first", "second"]})
    assert text == f'{key} = [\n    "first",\n    "second",\n]\n'
    assert toml_io.loads(text) == {key: ["first", "second"]}


def test_dependency_references_use_inline_tables() -> None:
    document = {
        "packages": [
            {
                "name": "example",
                "dependencies": [{"name": "first"}, {"name": "second"}],
                "wheels": [{"name": "example.whl", "hashes": {"sha256": "a"}}],
            },
        ],
    }
    text = dumps(document)
    assert (
        'dependencies = [\n    { name = "first" },\n    { name = "second" },\n]\n'
        in text
    )
    assert "[[packages.wheels]]" in text
    assert toml_io.loads(text) == document


def test_datetimes_always_use_t() -> None:
    timestamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    text = dumps({"created-at": timestamp, "wheels": [{"upload-time": timestamp}]})
    assert "created-at = 2026-01-02T00:00:00+00:00" in text
    assert "upload-time = 2026-01-02T00:00:00+00:00" in text
    assert toml_io.loads(text)["created-at"] == timestamp


def test_numeric_display_overrides_do_not_change_values() -> None:
    class DisplayInt(int):
        def __repr__(self) -> str:
            return "integer display"

    class DisplayFloat(float):
        def __repr__(self) -> str:
            return "float display"

    parsed = toml_io.loads(
        dumps({"integer": DisplayInt(2), "float": DisplayFloat(2.5)})
    )
    assert parsed == {"integer": 2, "float": 2.5}


def test_nonfinite_floats() -> None:
    parsed = toml_io.loads(
        dumps({"nan": math.nan, "inf": math.inf, "negative": -math.inf})
    )
    assert math.isnan(parsed["nan"])
    assert parsed["inf"] == math.inf
    assert parsed["negative"] == -math.inf


def test_invalid_key() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        dumps({1: "value"})  # type: ignore[dict-item]  # Deliberately invalid input.


def test_unsupported_value() -> None:
    with pytest.raises(TypeError, match="Unsupported TOML value"):
        dumps({"value": object()})


def test_offset_time_is_refused() -> None:
    with pytest.raises(ValueError, match="offset times"):
        dumps({"time": time(3, 4, 5, tzinfo=timezone.utc)})


@pytest.mark.parametrize(
    "value",
    [
        {"package": {"dependencies": ["a>=1"]}},
        [{"name": "index", "url": "https://example.com/simple/"}],
        [],
        {},
        'a string with "quotes"',
        True,
        42,
    ],
)
def test_inline_values_roundtrip(value: object) -> None:
    assert toml_io.loads("value = " + dumps_value(value))["value"] == value
