"""Specifier collection construction from text and existing specifiers."""

from __future__ import annotations

import pytest
from typing_extensions import override

from nab_provider._vendor.packaging.specifiers import (
    InvalidSpecifier,
    Specifier,
    SpecifierSet,
)


@pytest.mark.parametrize("prereleases", [None, False, True])
@pytest.mark.parametrize(
    ("text", "clauses"),
    [
        ("", []),
        (" \t\n", []),
        (">=1", [">=1"]),
        (" \t>=1\n", [">=1"]),
        (">=1.0a1", [">=1.0a1"]),
        ("===legacy", ["===legacy"]),
        (" === ", ["==="]),
        (">=1,<2", [">=1", "<2"]),
        (",>=1,,<2,", [">=1", "<2"]),
        (">=1,>=1", [">=1", ">=1"]),
    ],
)
def test_text_matches_existing_specifiers(
    text: str, clauses: list[str], prereleases: bool | None
) -> None:
    actual = SpecifierSet(text, prereleases=prereleases)
    expected = SpecifierSet(
        [Specifier(clause) for clause in clauses], prereleases=prereleases
    )

    assert str(actual) == str(expected)
    assert actual.prereleases == expected.prereleases
    versions = ["0.dev0", "1.0a1", "1", "1.5", "2", "2+local"]
    assert list(actual.filter(versions)) == list(expected.filter(versions))


@pytest.mark.parametrize("text", ["bad", "^1", ">=1,broken", "!==1"])
def test_invalid_text_is_rejected(text: str) -> None:
    with pytest.raises(InvalidSpecifier):
        SpecifierSet(text)


def test_arbitrary_literal_is_retained() -> None:
    assert SpecifierSet(" ===legacy ").contains("legacy")


class _SplitOverride(str):
    """A string whose split method supplies different constraint text."""

    __slots__ = ()

    @override
    def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
        return [">=1", "<2"]


def test_string_subclass_keeps_its_split_behavior() -> None:
    assert str(SpecifierSet(_SplitOverride(">=99"))) == "<2,>=1"
