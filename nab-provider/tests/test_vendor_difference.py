"""Version subtraction across gaps, endpoints, and PEP 440 boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab_provider._vendor.packaging import ranges
from nab_provider._vendor.packaging.specifiers import SpecifierSet

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider._vendor.packaging._ranges import Interval


@pytest.mark.parametrize("prereleases", [None, False, True])
@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("<0", "!=2"),
        (">=3", "<2"),
        ("<2", ">=3"),
        (">=1,<=5", ">=2,<4"),
        (">=1,<=5", ">=2,<=5"),
        (">=1,<=5", ">=2"),
        (">=1,<=5", "<0"),
        ("!=1,!=3", "==2"),
        ("!=1,!=3", "!=2,!=4"),
        ("", ""),
        (">=1", ">1"),
        ("<=1", "<1"),
        (">1a1", "<1a2.dev0"),
        ("<=1.post0.dev0", "<=1"),
        ("==1.*", "==1.1.*"),
        ("==1+local", "==1"),
        ("!=0.dev0", "<1.dev0"),
        (">=2!1", "<2!1.post1"),
    ],
)
def test_difference_matches_complement_intersection(
    left: str, right: str, prereleases: bool | None
) -> None:
    first = SpecifierSet(left, prereleases=prereleases).to_range()
    second = SpecifierSet(right, prereleases=prereleases).to_range()

    assert first - second == first & ~second


def test_difference_preserves_arbitrary_equality() -> None:
    literal = SpecifierSet("===legacy").to_range()
    numeric = SpecifierSet(">=1").to_range()

    assert literal - numeric == literal
    assert (literal - literal).is_empty
    assert "legacy" in (literal | numeric) - numeric


def test_difference_does_not_construct_a_complement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SpecifierSet(">=1,<5").to_range()
    second = SpecifierSet("!=2,!=4").to_range()
    expected = first & ~second

    def refuse_complement(_bounds: Sequence[Interval]) -> list[Interval]:
        raise AssertionError("subtraction constructed a complement")

    monkeypatch.setattr(ranges, "_complement_ranges", refuse_complement)
    assert first - second == expected
