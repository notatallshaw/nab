"""Tests for ``SpecifierSet`` equality and hashing in the vendored packaging tree.

A set compares as the sorted tuple of its clauses, so the sort key decides
whether two sets holding the same clauses line up. Sorting on each clause's
equality key rather than its text lets ``!=v3.9.0`` match ``!=3.9.0`` and keeps
``__hash__`` agreeing with ``__eq__``.
"""

from __future__ import annotations

import sys

import pytest

from nab_provider._vendor.packaging.specifiers import SpecifierSet

PLAIN = ">=3.8,!=3.9.0,!=3.9.1"

#: Spellings PEP 440 makes equal to ``PLAIN``.
RESPELLINGS = [
    ">=3.8,!=v3.9.0,!=3.9.1",
    ">=3.8,!=3.9.0,!=0!3.9.1",
    ">=3.8,!=3.9.0.0,!=3.9.1",
    "!=3.9.1,>=v3.8.0,!=3.9.0",
]

_OVERSIZED = "9" * (sys.get_int_max_str_digits() + 1)


@pytest.mark.parametrize("respelling", RESPELLINGS)
def test_a_respelled_clause_compares_and_hashes_equal(respelling: str) -> None:
    plain = SpecifierSet(PLAIN)
    respelled = SpecifierSet(respelling)

    assert plain == respelled
    assert hash(plain) == hash(respelled)
    assert respelled in {plain}


@pytest.mark.parametrize("respelling", RESPELLINGS)
def test_a_changed_clause_stays_unequal_across_spellings(respelling: str) -> None:
    assert SpecifierSet(">=3.8,!=3.9.0,!=3.9.2") != SpecifierSet(respelling)


def test_equality_is_transitive_across_spellings() -> None:
    sets = [SpecifierSet(text) for text in [PLAIN, *RESPELLINGS]]

    for left in sets:
        for right in sets:
            assert left == right


def test_comparing_single_clause_sets_converts_no_version() -> None:
    # A set of at most one clause is canonical at construction, so comparing
    # these sorts nothing. The digit run passes int()'s limit, so converting
    # either version would raise.
    assert SpecifierSet("") != SpecifierSet(f">={_OVERSIZED}")
