"""Report the sources of conflicting requirements and constraints."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider.requirements_file import (
    raise_for_unsatisfiable,
)
from nab_resolver.errors import ResolutionError


class TestRaiseForUnsatisfiable:
    """``raise_for_unsatisfiable`` flags a folded range that went empty."""

    def test_satisfiable_ranges_are_silent(self) -> None:
        """A non-empty range produces no error."""
        ranges = {"foo": SpecifierSet(">=1.0").to_range()}
        raise_for_unsatisfiable(ranges, {"foo": ["foo>=1.0"]}, kind="requirement")

    def test_empty_range_raises_naming_every_source(self) -> None:
        """An empty range raises and names every conflicting requirement."""
        empty = SpecifierSet("==1.0").to_range() & SpecifierSet("==2.0").to_range()
        with pytest.raises(ResolutionError) as info:
            raise_for_unsatisfiable(
                {"foo": empty},
                {"foo": ["foo==1.0", "foo==2.0"]},
                kind="requirement",
            )
        message = str(info.value)
        assert "foo==1.0" in message
        assert "foo==2.0" in message
        assert "conflicting requirements" in message

    def test_kind_shapes_the_wording(self) -> None:
        """``kind`` selects the noun used in the message."""
        empty = SpecifierSet("==1.0").to_range() & SpecifierSet("==2.0").to_range()
        with pytest.raises(ResolutionError, match="conflicting constraints"):
            raise_for_unsatisfiable(
                {"foo": empty},
                {"foo": ["foo==1.0", "foo==2.0"]},
                kind="constraint",
            )

    def test_every_unsatisfiable_package_is_listed(self) -> None:
        """Each package with an empty range appears in the message."""
        empty_a = SpecifierSet("==1").to_range() & SpecifierSet("==2").to_range()
        empty_b = SpecifierSet("==3").to_range() & SpecifierSet("==4").to_range()
        with pytest.raises(ResolutionError) as info:
            raise_for_unsatisfiable(
                {"aaa": empty_a, "bbb": empty_b},
                {"aaa": ["aaa==1", "aaa==2"], "bbb": ["bbb==3", "bbb==4"]},
                kind="requirement",
            )
        message = str(info.value)
        assert "aaa: aaa==1, aaa==2" in message
        assert "bbb: bbb==3, bbb==4" in message
