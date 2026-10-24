"""Quoted marker values retain their contents and reject invalid literals."""

from __future__ import annotations

from typing import cast

import pytest

from nab_provider._vendor.packaging._parser import MarkerItem, parse_marker
from nab_provider._vendor.packaging._tokenizer import ParserSyntaxError


def parsed_value(marker: str) -> str:
    """The right-hand value of a marker holding one comparison."""
    return cast("MarkerItem", parse_marker(marker)[0])[2].value


class TestQuotedString:
    @pytest.mark.parametrize(
        ("marker", "expected"),
        [
            ("os_name == 'posix'", "posix"),
            ('os_name == "posix"', "posix"),
            ("os_name == 'it\"s'", 'it"s'),
            ('os_name == "it\'s"', "it's"),
            ("os_name == ''", ""),
            ('os_name == "3"', "3"),
            ('python_version >= "3.10"', "3.10"),
            ('os_name == "  spaced  "', "  spaced  "),
            ('os_name == "café"', "café"),
        ],
    )
    def test_the_body_between_the_quotes_is_the_value(
        self, marker: str, expected: str
    ) -> None:
        assert parsed_value(marker) == expected

    @pytest.mark.parametrize(
        ("marker", "expected"),
        [
            (r"os_name == 'a\nb'", "a\nb"),
            (r'os_name == "a\tb"', "a\tb"),
            (r"os_name == 'a\\b'", "a\\b"),
            (r"os_name == 'a\x41b'", "aAb"),
        ],
    )
    def test_a_backslash_still_reaches_the_literal_parse(
        self, marker: str, expected: str
    ) -> None:
        assert parsed_value(marker) == expected

    @pytest.mark.parametrize(
        "body",
        ["a\nb", "a\rb", "a\0b", "a\\", f"a{chr(0xD800)}b", f"a{chr(0xDC80)}b"],
    )
    def test_a_body_python_rejects_is_a_syntax_error(self, body: str) -> None:
        with pytest.raises(ParserSyntaxError, match="Invalid quoted string"):
            parse_marker(f"os_name == '{body}'")
