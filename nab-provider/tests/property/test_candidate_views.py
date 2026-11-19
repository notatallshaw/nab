"""Shared candidate views agree with independent filtering on each target."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider.provider import ListingFilterCache, Provider
from nab_provider.records import SdistFile, WheelFile
from nab_provider.testing import make_coordinator, make_sdist, make_wheel

from ..declared_target import target

pytestmark = pytest.mark.property


@given(
    st.lists(
        st.tuples(
            st.sampled_from(("1", "1.0", "2rc1", "2", "3")),
            st.sampled_from(
                (
                    "py3-none-any",
                    "py2-none-any",
                    "cp312-cp312-manylinux_2_17_x86_64",
                    "cp312-cp312-win_amd64",
                    "bad",
                    "sdist",
                )
            ),
            st.sampled_from((None, ">=3.12", "<3.12", ">=3.12.5")),
        ),
        max_size=12,
    )
)
def test_shared_views_match_uncached_filters_and_range_counts(
    declarations: list[tuple[str, str, str | None]],
) -> None:
    files: list[WheelFile | SdistFile] = [
        make_sdist(version, requires_python=python)
        if tag == "sdist"
        else make_wheel(version, tag=tag, requires_python=python)
        for version, tag, python in declarations
    ]
    coordinator = make_coordinator(files, auto_metadata=True)
    cache = ListingFilterCache(2, target_count=4)
    targets = [target("3.11"), target(), target(platform="windows_amd64"), target()]
    for environment in targets:
        shared = Provider(coordinator, environment, listing_filter_cache=cache)
        fresh = Provider(coordinator, environment)
        actual = shared.filter_distributions("pkg", files)
        expected = fresh.filter_distributions("pkg", files)
        assert actual == expected
        assert shared.stats == fresh.stats
        assert (
            shared.tag_excluded_wheels_by_version
            == fresh.tag_excluded_wheels_by_version
        )
        for text in ("", ">=2", "<2", "==1.*"):
            wanted = SpecifierSet(text).to_range()
            assert shared.count_matching_records(
                "pkg", wanted, actual
            ) == fresh.count_matching_records("pkg", wanted, expected)
