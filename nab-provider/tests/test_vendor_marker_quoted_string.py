"""Quoted marker values retain their contents and reject invalid literals."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.markers import InvalidMarker, Marker


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("os_name == 'posix'", "posix"),
        ('os_name == "posix"', "posix"),
        ("os_name == 'it\"s'", 'it"s'),
        ('os_name == "it\'s"', "it's"),
        ("os_name == ''", ""),
        ('os_name == "3"', "3"),
        ('os_name == "  spaced  "', "  spaced  "),
        ('os_name == "café"', "café"),
        (r"os_name == 'a\nb'", "a\nb"),
        (r'os_name == "a\tb"', "a\tb"),
        (r"os_name == 'a\\b'", "a\\b"),
        (r"os_name == 'a\x41b'", "aAb"),
    ],
)
def test_quoted_marker_matches_its_value(marker: str, expected: str) -> None:
    parsed = Marker(marker)
    assert parsed.evaluate({"os_name": expected})
    assert not parsed.evaluate({"os_name": "different"})


def test_quoted_version_boundary() -> None:
    marker = Marker('python_version >= "3.10"')
    assert marker.evaluate({"python_version": "3.10"})
    assert not marker.evaluate({"python_version": "3.9"})


@pytest.mark.parametrize(
    "body",
    ["a\nb", "a\rb", "a\0b", "a\\", f"a{chr(0xD800)}b", f"a{chr(0xDC80)}b"],
)
def test_a_body_python_rejects_is_invalid(body: str) -> None:
    with pytest.raises(InvalidMarker, match="Invalid quoted string"):
        Marker(f"os_name == '{body}'")
