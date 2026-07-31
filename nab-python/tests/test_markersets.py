"""Unit suite for MarkerSet.simplify and the corpus differential harness.

The direct-call cases pin byte-exact ``to_marker_string`` output and
within-universe equivalence for the operator's two tiers; the harness runs the
operator over the lock corpus and cross-checks soundness and minimality against
an independent brute-force oracle.
"""

from __future__ import annotations

import importlib.util
from collections import defaultdict
from functools import reduce
from pathlib import Path

import pytest
from packaging.markers import Marker

from nab_python._vendor.packaging import _markersets as engine
from nab_python._vendor.packaging.markersets import (
    _MAX_CELLS,
    IntractableMarkerSet,
    MarkerSet,
)

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


def test_non_dnf_product_overrun_raises() -> None:
    terms = [ms(f'extra == "a{i}"') | ms(f'extra == "b{i}"') for i in range(25)]
    conjunction = reduce(MarkerSet.intersection, terms)
    with pytest.raises(IntractableMarkerSet):
        conjunction.simplify(within=MarkerSet.full())


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
    assert context_aware == corpus.OPERATOR_CONTEXT_AWARE_CHARS
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


def _u(*texts: str) -> engine.Formula:
    return engine.make_or(engine.parse(t) for t in texts)


ORACLE_TRIPLES = [
    (SPAN, SPAN, _u(*PY_ROWS)),
    ('sys_platform == "linux"', SPAN, _u(*PY_ROWS)),
    ('sys_platform == "linux"', SPAN, engine.TRUE),
    (
        'python_version == "3.11" and sys_platform == "linux"',
        SPAN,
        _u(*PY_ROWS),
    ),
    (
        'sys_platform == "linux"',
        'sys_platform == "linux" and python_version == "3.11"',
        _u('python_version == "3.10"', 'python_version == "3.11"'),
    ),
    (
        'sys_platform == "linux" and python_version == "3.11"',
        'sys_platform == "linux"',
        _u('python_version == "3.10"', 'python_version == "3.11"'),
    ),
    (
        'sys_platform == "linux"',
        SPAN,
        engine.parse('python_version == "3.11" and sys_platform == "linux"'),
    ),
    (
        'sys_platform == "linux"',
        SPAN,
        _u(
            'python_version == "3.11"',
            'python_version == "3.11" and sys_platform == "linux"',
        ),
    ),
    (
        'sys_platform == "linux"',
        SPAN,
        engine.parse(
            'sys_platform == "linux" and '
            '(python_version == "3.10" or python_version == "3.11")'
        ),
    ),
    (
        'implementation_version == "3.11.0"',
        'implementation_version == "3.11.0"',
        _u('implementation_version == "3.11.0" and sys_platform == "linux"'),
    ),
    # A row's `==` on a version-dispatch variable is PEP 440 equality, not exact
    # string equality: `platform_release == "5.10"` still admits "5.10.0", so
    # the row must not substitute the literal into atoms reading the string.
    (
        'platform_release in "5.10 6.1"',
        'sys_platform == "linux" or sys_platform != "linux"',
        _u('platform_release == "5.10"'),
    ),
    (
        '"5.10.0" in platform_release',
        'sys_platform == "linux" and sys_platform != "linux"',
        _u('platform_release == "5.10"'),
    ),
    (
        '"5.10.0" in platform_release or python_version >= "3.12"',
        'python_version >= "3.12"',
        _u('platform_release == "5.10"'),
    ),
]


@pytest.mark.parametrize(("left", "right", "universe"), ORACLE_TRIPLES)
def test_row_oracle_matches_whole_matrix(
    left: str, right: str, universe: engine.Formula
) -> None:
    lf = engine.parse(left)
    rf = engine.parse(right)
    assert engine.equivalent_within_rows(lf, rf, universe, _MAX_CELLS) == (
        engine._equivalent_within(lf, rf, universe, _MAX_CELLS)
    )


def _wide_universe() -> MarkerSet:
    return union(*corpus.wide_universe())


def _narrow_universe() -> MarkerSet:
    return union(*corpus.narrow_universe())


def _narrow_linux_span() -> str:
    return " or ".join(
        f'(python_version == "{py}" and sys_platform == "linux" '
        'and platform_machine == "x86_64")'
        for py in corpus.NARROW_PYS
    )


def test_wide_full_span_raises_on_whole_matrix_today() -> None:
    marker = corpus.wide_curated()[0]["marker"]
    with pytest.raises(IntractableMarkerSet):
        engine._equivalent_within(
            engine.parse(marker),
            engine.parse(marker),
            _wide_universe()._tree,
            _MAX_CELLS,
        )


def test_wide_full_span_collapses_to_platform_pin() -> None:
    marker = corpus.wide_curated()[0]["marker"]
    within = _wide_universe()
    result = ms(marker).simplify(within=within)
    assert result.to_marker_string() == (
        'platform_machine == "x86_64" and sys_platform == "linux"'
    )
    assert engine.equivalent_within_rows(
        engine.parse(marker), result._tree, within._tree, _MAX_CELLS
    )


def test_wide_full_span_under_full_keeps_python_axis() -> None:
    marker = corpus.wide_curated()[0]["marker"]
    text = ms(marker).simplify(within=MarkerSet.full()).to_marker_string()
    assert text is not None
    assert "python_version" in text


def test_wide_curated_dual_soundness() -> None:
    for case in corpus.wide_curated():
        marker = case["marker"]
        environments = case["environments"]
        within = union(*environments)
        result = ms(marker).simplify(within=within)
        text = result.to_marker_string()
        assert engine.equivalent_within_rows(
            engine.parse(marker), engine.parse(text or ""), within._tree, _MAX_CELLS
        )
        raw = Marker(marker)
        simplified = None if text is None else Marker(text)
        for row in environments:
            env = corpus.env_dict_from_marker(row)
            expected = raw.evaluate(env)
            got = True if simplified is None else simplified.evaluate(env)
            assert got == expected


def test_partial_span_keeps_python_atom() -> None:
    marker = " or ".join(
        f'(python_version == "{py}" and sys_platform == "linux" '
        'and platform_machine == "x86_64")'
        for py in corpus.NARROW_PYS[2:]
    )
    within = _narrow_universe()
    text = ms(marker).simplify(within=within).to_marker_string()
    assert text is not None
    assert "python_version" in text
    assert engine.equivalent_within_rows(
        engine.parse(marker), engine.parse(text), within._tree, _MAX_CELLS
    )


def test_membership_and_negated_extra_survive_over_universe() -> None:
    marker = f'({_narrow_linux_span()}) and "cpu" in extras and "gpu" not in extras'
    text = ms(marker).simplify(within=_narrow_universe()).to_marker_string()
    assert text is not None
    assert '"cpu" in extras' in text
    assert '"gpu" not in extras' in text
    assert "python_version" not in text


def test_dev0_split_not_merged_with_release_neighbor() -> None:
    lower = 'python_full_version >= "3.11.dev0" and python_full_version < "3.12"'
    upper = 'python_full_version >= "3.12" and python_full_version < "3.13"'
    marker = f'({lower} and sys_platform == "linux")'
    within = union(lower, upper)
    text = ms(marker).simplify(within=within).to_marker_string()
    assert text is not None
    outside = {
        "python_version": "3.12",
        "python_full_version": "3.12.0",
        "sys_platform": "linux",
    }
    assert Marker(marker).evaluate(outside) is False
    assert Marker(text).evaluate(outside) is False


def test_determinism_under_shuffled_universe() -> None:
    marker = _narrow_linux_span()
    rows = corpus.narrow_universe()
    forward = ms(marker).simplify(within=union(*rows)).to_marker_string()
    reverse = ms(marker).simplify(within=union(*reversed(rows))).to_marker_string()
    assert (
        forward == reverse == 'platform_machine == "x86_64" and sys_platform == "linux"'
    )


def test_universe_aware_idempotence() -> None:
    within = _narrow_universe()
    once = ms(_narrow_linux_span()).simplify(within=within).to_marker_string()
    assert once is not None
    twice = ms(once).simplify(within=within).to_marker_string()
    assert twice == once


def test_degeneration_pins_nothing_simplifies_under_budget() -> None:
    marker = 'python_version >= "3.9" and sys_platform == "linux"'
    within = union('python_version >= "3.9"')
    result = ms(marker).simplify(within=within)
    assert result.to_marker_string() == 'sys_platform == "linux"'


def test_degeneration_pins_nothing_overruns_and_raises() -> None:
    span = " or ".join(
        f'(python_version == "{py}" and sys_platform == "{sp}" '
        f'and platform_machine == "{mach}")'
        for py in corpus.WIDE_PYS
        for sp, mach in corpus.WIDE_PLATS
    )
    sysp = " or ".join(f'sys_platform == "{sp}"' for sp in ("linux", "darwin", "win32"))
    mach = " or ".join(
        f'platform_machine == "{m}"' for m in ("x86_64", "aarch64", "arm64", "AMD64")
    )
    pyv = " or ".join(f'python_version == "{py}"' for py in corpus.WIDE_PYS)
    universe = engine.parse(f"({sysp}) and ({mach}) and ({pyv})")
    with pytest.raises(IntractableMarkerSet):
        engine.equivalent_within_rows(
            engine.parse(span), engine.parse(span), universe, _MAX_CELLS
        )


def test_work_budget_overrun_raises() -> None:
    """A run past the work budget raises rather than running to a fixpoint.

    The same input succeeds under the shipped budget.
    """
    within = _narrow_universe()
    marker = engine.parse(_narrow_linux_span())
    with pytest.raises(IntractableMarkerSet, match="max_work"):
        engine.simplify_within(marker, within._tree, _MAX_CELLS, 1)
    assert ms(_narrow_linux_span()).simplify(within=within).to_marker_string() == (
        'platform_machine == "x86_64" and sys_platform == "linux"'
    )


def test_work_meter_is_unset_outside_a_simplify() -> None:
    within = _narrow_universe()
    ms(_narrow_linux_span()).simplify(within=within)
    assert getattr(engine._work_meter, "remaining", None) is None
    assert getattr(engine._partition_cache, "store", None) is None
