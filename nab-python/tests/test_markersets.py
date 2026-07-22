"""Unit suite for MarkerSet.simplify and the corpus differential harness.

The direct-call cases pin byte-exact ``to_marker_string`` output and
within-universe equivalence for the operator's two tiers; the harness runs the
operator over the phase-0 lock corpus and cross-checks soundness and minimality
against an independent brute-force oracle.
"""

from __future__ import annotations

import importlib.util
from collections import defaultdict
from pathlib import Path

import pytest

from nab_python._vendor.packaging.markersets import MarkerSet

_spec = importlib.util.spec_from_file_location(
    "simplify_corpus_fixtures",
    Path(__file__).with_name("simplify_corpus_fixtures.py"),
)
assert _spec is not None
assert _spec.loader is not None
corpus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(corpus)


def ms(text: str) -> MarkerSet:
    return MarkerSet.from_marker(text)


def union(*texts: str) -> MarkerSet:
    result = MarkerSet.empty()
    for text in texts:
        result = result | ms(text)
    return result


SPAN = (
    '(python_version == "3.10" and sys_platform == "linux") '
    'or (python_version == "3.11" and sys_platform == "linux") '
    'or (python_version == "3.12" and sys_platform == "linux")'
)
PY_ROWS = (
    'python_version == "3.10"',
    'python_version == "3.11"',
    'python_version == "3.12"',
)


def test_context_free_factoring_keeps_python_axis() -> None:
    result = ms(SPAN).simplify(within=MarkerSet.full())
    assert result.to_marker_string() == (
        'sys_platform == "linux" and (python_version == "3.10" '
        'or python_version == "3.11" or python_version == "3.12")'
    )
    assert result.equivalent(ms(SPAN))


def test_universe_aware_drop_and_dedupe_collapses() -> None:
    within = union(*PY_ROWS)
    result = ms(SPAN).simplify(within=within)
    assert result.to_marker_string() == 'sys_platform == "linux"'
    assert (result & within).equivalent(ms(SPAN) & within)


def test_universe_aware_diverges_outside_universe() -> None:
    result = ms(SPAN).simplify(within=union(*PY_ROWS))
    outside = {
        "python_version": "3.13",
        "python_full_version": "3.13.0",
        "sys_platform": "linux",
    }
    assert result.evaluate(outside) is True
    assert ms(SPAN).evaluate(outside) is False


def test_partial_span_keeps_win32_branch() -> None:
    marker = SPAN + ' or (python_version == "3.10" and sys_platform == "win32")'
    result = ms(marker).simplify(within=union(*PY_ROWS))
    assert result.to_marker_string() == (
        '(python_version == "3.10" and sys_platform == "win32") '
        'or sys_platform == "linux"'
    )


def test_single_minor_tautology_drops() -> None:
    marker = 'python_version == "3.11" and sys_platform == "linux"'
    result = ms(marker).simplify(within=union('python_version == "3.11"'))
    assert result.to_marker_string() == 'sys_platform == "linux"'


def test_membership_atoms_survive_while_python_drops() -> None:
    marker = 'python_version == "3.11" and "cpu" in extras and "gpu" not in extras'
    result = ms(marker).simplify(within=union('python_version == "3.11"'))
    assert result.to_marker_string() == ('"cpu" in extras and "gpu" not in extras')


def test_dev0_split_boundary_bounds_drop() -> None:
    minor = 'python_full_version >= "3.11.dev0" and python_full_version < "3.12"'
    marker = minor + ' and sys_platform == "linux"'
    result = ms(marker).simplify(within=union(minor))
    assert result.to_marker_string() == 'sys_platform == "linux"'


def test_full_set_serialises_to_none() -> None:
    result = MarkerSet.full().simplify(within=MarkerSet.full())
    assert result.to_marker_string() is None


def test_empty_self_stays_empty() -> None:
    result = MarkerSet.empty().simplify(within=MarkerSet.full())
    assert result.is_empty() is True


def test_empty_within_universe_collapses_to_empty() -> None:
    result = ms('sys_platform == "win32"').simplify(
        within=union('sys_platform == "linux"')
    )
    assert result.is_empty() is True


def test_idempotence() -> None:
    once = ms(SPAN).simplify(within=MarkerSet.full())
    text = once.to_marker_string()
    assert text is not None
    twice = ms(text).simplify(within=MarkerSet.full())
    assert twice.to_marker_string() == text


def test_determinism_under_shuffled_input() -> None:
    shuffled = (
        '(sys_platform == "linux" and python_version == "3.12") '
        'or (sys_platform == "linux" and python_version == "3.10") '
        'or (python_version == "3.11" and sys_platform == "linux")'
    )
    assert (
        ms(shuffled).simplify(within=MarkerSet.full()).to_marker_string()
        == ms(SPAN).simplify(within=MarkerSet.full()).to_marker_string()
    )


def test_within_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty set"):
        ms(SPAN).simplify(within=MarkerSet.empty())


def test_within_full_equals_context_free() -> None:
    via_full = ms(SPAN).simplify(within=MarkerSet.full())
    assert via_full.to_marker_string() == (
        'sys_platform == "linux" and (python_version == "3.10" '
        'or python_version == "3.11" or python_version == "3.12")'
    )


def _grouped_fixtures() -> dict[tuple[str, tuple[str, ...]], list[dict]]:
    groups: dict[tuple[str, tuple[str, ...]], list[dict]] = defaultdict(list)
    for fixture in corpus.FIXTURES:
        key = (fixture["lock"], tuple(fixture["environments"]))
        groups[key].append(fixture)
    return groups


def test_corpus_operator_sound_minimal_and_reproduces_tiers() -> None:
    total = context_aware = context_free = 0
    for (_lock, environments), items in _grouped_fixtures().items():
        within = union(*environments)
        universe = [corpus.env_dict_from_marker(e) for e in environments]
        marker_strings = [item["marker"] for item in items]
        marker_vars = corpus.marker_vars_of(marker_strings)
        observed = corpus.observed_values(marker_strings, universe, marker_vars)
        grid = corpus.build_grid_universe(marker_vars, observed)

        for item in items:
            marker = item["marker"]
            total += len(marker)
            ca_text = ms(marker).simplify(within=within).to_marker_string()
            cf_text = ms(marker).simplify(within=MarkerSet.full()).to_marker_string()
            context_aware += len(ca_text or "")
            context_free += len(cf_text or "")

            original_selection = corpus.selects(marker, universe)
            assert corpus.selects(ca_text, universe) == original_selection
            assert corpus.selects(cf_text, universe) == original_selection

            ca_oracle = corpus.achievable_min_marker(
                original_selection, universe, marker_vars
            )
            cf_oracle = corpus.achievable_min_marker(
                corpus.selects(marker, grid), grid, marker_vars
            )
            assert len(ca_text or "") <= len(ca_oracle)
            assert len(cf_text or "") <= len(cf_oracle)

    assert total == corpus.TOTAL_CHARS
    context_aware_pct = round(100 * (total - context_aware) / total, 1)
    context_free_pct = round(100 * (total - context_free) / total, 1)
    assert context_aware_pct >= corpus.CONTEXT_AWARE_PCT
    assert context_aware == 1010
    assert context_free == corpus.CONTEXT_FREE_CHARS
    assert context_free_pct == corpus.CONTEXT_FREE_PCT


def test_corpus_oracle_reproduces_documented_tiers() -> None:
    total = context_aware = context_free = 0
    for (_lock, environments), items in _grouped_fixtures().items():
        universe = [corpus.env_dict_from_marker(e) for e in environments]
        marker_strings = [item["marker"] for item in items]
        marker_vars = corpus.marker_vars_of(marker_strings)
        observed = corpus.observed_values(marker_strings, universe, marker_vars)
        grid = corpus.build_grid_universe(marker_vars, observed)
        for item in items:
            marker = item["marker"]
            total += len(marker)
            context_aware += len(
                corpus.achievable_min_marker(
                    corpus.selects(marker, universe), universe, marker_vars
                )
            )
            context_free += len(
                corpus.achievable_min_marker(
                    corpus.selects(marker, grid), grid, marker_vars
                )
            )
    assert total == corpus.TOTAL_CHARS
    assert context_aware == corpus.CONTEXT_AWARE_CHARS
    assert context_free == corpus.CONTEXT_FREE_CHARS
