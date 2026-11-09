"""Range filtering uses labels only when matching literal clauses."""

from __future__ import annotations

import pytest

from nab_provider._compat import override
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version


@pytest.mark.parametrize("prereleases", [None, False, True])
def test_universal_filter_accepts_versions_without_display(
    prereleases: bool | None,
) -> None:
    class UnformattedVersion(Version):
        @override
        def __str__(self) -> str:
            raise AssertionError("Filtering a value needs no text conversion")

    final = UnformattedVersion("1")
    pre = UnformattedVersion("2rc1")

    actual = list(VersionRange.full().filter([final, pre], prereleases=prereleases))

    assert actual == ([final, pre] if prereleases else [final])


@pytest.mark.parametrize("reject", [False, True])
def test_literal_filter_retains_version_text_matching(reject: bool) -> None:
    versions = [Version("1"), Version("1.0")]
    literal = SpecifierSet("===1.0").to_range()
    allowed = VersionRange.full().difference(literal) if reject else literal
    expected = [versions[0]] if reject else [versions[1]]

    assert list(allowed.filter(versions)) == expected
