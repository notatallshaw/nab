"""Tests for the vendored specifier-pattern patch.

The tokenizer rule is ``Specifier._regex`` itself, and that pattern matches no
surrounding whitespace, so ``Specifier.__init__`` strips before matching. ``re``
caches on the pattern text, so the identity assertion alone would still hold if
the tokenizer compiled the string a second time; the whitespace assertion is
what pins the sharing.

``_regex`` compiles ``_condensed_regex_str``, which is the verbose
``_specifier_regex_str`` with its whitespace and comments removed. Nothing in
the vendoring patch ties the two together, so the differential over ``CORPUS``
catches a condensed twin left stale by an upstream edit to the grammar.
"""

from __future__ import annotations

import itertools
import re

import pytest

from nab_provider._vendor.packaging._tokenizer import DEFAULT_RULES
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, Specifier

# Padding ``Specifier`` accepts, with the operator and version it parses to.
# ``str.strip()`` and ``\s`` have to accept the same characters, hence ``\xa0``.
PADDED = [
    (" ==1.0", ("==", "1.0")),
    ("==1.0 ", ("==", "1.0")),
    ("\t==1.0\n", ("==", "1.0")),
    ("\x0b==1.0\x0c", ("==", "1.0")),
    ("\xa0==1.0\xa0", ("==", "1.0")),
    ("  ===  ", ("===", "")),
]

# Strings the pattern refuses however they are padded.
REFUSED = ["", " ", "=1.0", "== ", "==1.0 2.0", "~=1"]

# Every operator the pattern accepts, plus two it must refuse.
OPERATORS = ["==", "!=", "<=", ">=", "<", ">", "~=", "===", "=", ""]

# Version cores reaching each optional segment of the grammar, and junk it refuses.
VERSION_CORES = [
    "",
    "1",
    "1.0",
    "1.0.0",
    "v1.0",
    "2!1.0",
    "1.0a1",
    "1.0PRE1",
    "1.0-alpha-2",
    "1.0.post1",
    "1.0.post",
    "1.0.dev0",
    "1.0dev",
    "1.0rc1.post2.dev3",
    "1.0+local.1",
    "1.0+",
    "1.0++a",
    "1.0.*",
    "1.*.*",
    "*",
    "1_0_0",
    "1.0-",
    "abc",
    "1.0 2.0",
    # Prerelease and post words the cores above leave out, plus "release",
    # which the grammar does not name.
    "1.0beta1",
    "1.0b2",
    "1.0c3",
    "1.0preview1",
    "1.0.rev2",
    "1.0r4",
    "1.0.release5",
    # The arbitrary-version class stops at ";" and ")" and runs through ",".
    "1.0;x",
    "1.0)",
    "1.0,2",
    # Under Unicode case folding these read as "1.0+s", "1.0.post1" and
    # "1.0preview1", so they tell the (?a:) scopes from a plain IGNORECASE group.
    "1.0+\u017f",
    "1.0.po\u017ft1",
    "1.0prev\u0131ew1",
]

# Padding goes before the operator, after it, and at the end.
PADDINGS = ["", " ", "\t", "\n"]

CORPUS = [
    f"{left}{operator}{middle}{core}{right}"
    for operator, core, left, middle, right in itertools.product(
        OPERATORS, VERSION_CORES, PADDINGS, PADDINGS, PADDINGS
    )
]


def verdicts(
    pattern: re.Pattern[str], candidate: str
) -> list[tuple[object, ...] | None]:
    """What ``match`` and then ``fullmatch`` make of one candidate: span and groups."""
    return [
        None if found is None else (found.span(), found.groups())
        for found in (pattern.match(candidate), pattern.fullmatch(candidate))
    ]


def test_the_tokenizer_rule_is_the_specifier_pattern_object() -> None:
    assert DEFAULT_RULES["SPECIFIER"] is Specifier._regex


def test_the_pattern_carries_no_surrounding_whitespace() -> None:
    assert not Specifier._regex.pattern.startswith(r"\s*")
    assert not Specifier._regex.pattern.endswith(r"\s*")


def test_the_condensed_pattern_reads_a_corpus_as_the_verbose_grammar_does() -> None:
    verbose = re.compile(Specifier._specifier_regex_str, re.VERBOSE | re.IGNORECASE)

    disagreements = [
        candidate
        for candidate in CORPUS
        if verdicts(verbose, candidate) != verdicts(Specifier._regex, candidate)
    ]

    # Sliced so a failure names a few candidates rather than thousands.
    assert disagreements[:5] == []

    assert Specifier._regex.groups == verbose.groups
    assert Specifier._regex.groupindex == verbose.groupindex


@pytest.mark.parametrize(("spec", "expected"), PADDED)
def test_padding_is_stripped_before_the_operator_split(
    spec: str, expected: tuple[str, str]
) -> None:
    parsed = Specifier(spec)

    assert (parsed.operator, parsed.version) == expected


@pytest.mark.parametrize("spec", REFUSED)
def test_a_refused_specifier_is_reported_with_its_padding(spec: str) -> None:
    padded = f" {spec}\t"
    message = re.escape(f"Invalid specifier: {padded!r}")

    with pytest.raises(InvalidSpecifier, match=message):
        Specifier(padded)


def test_the_tokenizer_reads_a_specifier_out_of_a_requirement() -> None:
    requirement = Requirement('foo >=1.0,<2.0; python_version >= "3.9"')

    assert str(requirement.specifier) == "<2.0,>=1.0"
    assert str(requirement.marker) == 'python_version >= "3.9"'
