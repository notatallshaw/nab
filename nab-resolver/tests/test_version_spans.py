"""Cached dependency runs preserve unread and policy boundaries."""

from nab_resolver.version_spans import cached_dependency_span


def test_span_includes_cached_equivalents_and_stops_at_unknown_or_different() -> None:
    versions = (1, 2, 3, 4, 5)
    dependencies = {versions[0]: {"dep": 1}, versions[1]: {}, versions[2]: {}}
    bounds = cached_dependency_span(versions[1], versions, dependencies.get)

    assert bounds == (versions[0], versions[3])


def test_lowest_policy_preserves_the_upper_neighbor_boundary() -> None:
    versions = (1, 2, 3)
    dependencies = {version: {} for version in versions}
    bounds = cached_dependency_span(
        versions[1], versions, dependencies.get, expand_higher=False
    )

    assert bounds == (None, versions[2])


def test_missing_selected_metadata_keeps_only_its_neighbor_gap() -> None:
    versions = (1, 2, 3, 4)
    bounds = cached_dependency_span(versions[2], versions, {}.get)

    assert bounds == (versions[1], versions[3])


def test_complete_equivalent_run_extends_beyond_both_listing_ends() -> None:
    versions = (1, 2, 3)
    dependencies = {version: {} for version in versions}
    bounds = cached_dependency_span(versions[1], versions, dependencies.get)

    assert bounds == (None, None)


def test_boundaries_keep_the_callers_ordered_version_domain() -> None:
    assert cached_dependency_span("2", ("1", "2", "3"), {}.get) == ("1", "3")
