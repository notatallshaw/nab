"""Tests for the guarded PEP 508 requirement parser."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from packaging.requirements import InvalidRequirement

from nab_provider.pep508 import parse_requirement


class TestParseRequirement:
    def test_parses_a_requirement(self) -> None:
        req = parse_requirement("click>=8 ; os_name == 'posix'")
        assert req.name == "click"
        assert str(req.specifier) == ">=8"
        assert req.marker is not None

    def test_malformed_string_raises_invalid_requirement(self) -> None:
        with pytest.raises(InvalidRequirement):
            parse_requirement("requests >= 2.0 extra junk")

    def test_moderate_nesting_still_parses(self) -> None:
        """Ordinary parenthesised grouping is untouched by the guard."""
        marker = "(" * 100 + "os_name == 'posix'" + ")" * 100
        assert parse_requirement(f"foo ; {marker}").marker is not None

    def test_parser_recursion_error_is_an_invalid_requirement(self) -> None:
        failure = RecursionError()
        with (
            patch("nab_provider.pep508.Requirement", side_effect=failure),
            pytest.raises(InvalidRequirement, match="nested too deeply") as caught,
        ):
            parse_requirement("foo>=1")

        assert caught.value.__cause__ is failure
