"""Check completed version-gap reuse and its operation-local retention budget."""

from __future__ import annotations

import sys

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import DecisionStore, MarkerSet


def _entries(*texts: str) -> list[tuple[engine.Version, str]]:
    """Build the same version/text pairs that production pools receive."""
    return [(engine.Version(text), text) for text in texts]


def _between_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Observe actual gap computations while retaining the real candidate search."""
    calls: list[tuple[str, str]] = []
    original = engine._between

    def between(
        low_version: engine.Version,
        low: str,
        high_version: engine.Version,
        store: engine.Memo,
    ) -> str | None:
        calls.append((low, str(high_version)))
        return original(low_version, low, high_version, store)

    monkeypatch.setattr(engine, "_between", between)
    return calls


def test_first_pool_does_not_retain_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _between_calls(monkeypatch)
    store = engine.Memo()
    entries = _entries("3.10", "3.11")
    expected = engine._version_pool(entries, memo=store)
    first = len(calls)
    assert first > 0
    assert store.gaps is None
    assert store.gap_remaining == engine._VERSION_GAP_MAX_UNITS

    assert engine._version_pool(entries, memo=store) == expected
    assert len(calls) == 2 * first
    assert store.gaps
    assert engine._version_pool(entries, memo=store) == expected
    assert len(calls) == 2 * first

    other = engine.Memo()
    assert engine._version_pool(entries, memo=other) == expected
    assert len(calls) == 3 * first
    assert engine._version_pool(entries, memo=other) == expected
    assert len(calls) == 4 * first


@pytest.mark.parametrize(
    "texts",
    [
        ("6.0", "6.0.1"),
        ("6.0.0.0", "6.0.1"),
        ("1!2.0", "2!0"),
        ("1.0.dev0", "1.0.dev1", "1.0a0", "1.0rc1", "1.0", "1.0.post0"),
        ("1+a", "1+a.a", "1+b"),
        ("3.11", "3.11.0", "3.11.0.0", "3.11.1"),
    ],
)
def test_pool_order_matches_uncached_computation(
    monkeypatch: pytest.MonkeyPatch, texts: tuple[str, ...]
) -> None:
    entries = _entries(*texts)
    monkeypatch.setattr(engine, "_VERSION_GAP_MAX_UNITS", 0)
    disabled = engine.Memo()
    expected = engine._version_pool(entries, memo=disabled)
    monkeypatch.setattr(engine, "_VERSION_GAP_MAX_UNITS", 4096)
    store = engine.Memo()
    for _ in range(3):
        assert engine._version_pool(entries, memo=store) == expected
    assert store.gaps


def test_equal_versions_with_different_release_widths_keep_distinct_gaps() -> None:
    store = engine.Memo()
    engine._version_pool([], memo=store)
    short = _entries("6.0", "6.0.1")
    padded = _entries("6.0.0.0", "6.0.1")
    assert short[0][0] == padded[0][0]
    assert "6.0.0.1" in engine._version_pool(short, memo=store)
    result = engine._version_pool(padded, memo=store)
    assert "6.0.0.0.1" in result
    assert "6.0.0.1" not in result
    assert store.gaps is not None
    upper_padded = _entries("6.0", "6.0.1.0.0")
    assert "6.0.0.0.0.1" in engine._version_pool(upper_padded, memo=store)


def test_completed_negative_gap_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _between_calls(monkeypatch)
    low, high = "1+a", "1+a.a"
    store = engine.Memo()
    entries = _entries(low, high)
    engine._version_pool([], memo=store)
    expected = engine._version_pool(entries, memo=store)
    assert store.gaps is not None
    assert store.gaps[low, high] is None
    after = len(calls)
    assert engine._version_pool(entries, memo=store) == expected
    assert len(calls) == after


@pytest.mark.parametrize("budget", [0, 8, 32, 64, 4096])
def test_pool_budget_preserves_order_and_bounds_retention(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    entries = _entries("3.10", "3.11", "3.12", "1+a", "1+a.a")
    expected = engine._version_pool(entries, memo=engine.Memo())
    monkeypatch.setattr(engine, "_VERSION_GAP_MAX_UNITS", budget)
    store = engine.Memo()
    for _ in range(4):
        assert engine._version_pool(entries, memo=store) == expected
    units = sum(
        8 + len(low) + len(high) + len(result or "")
        for (low, high), result in (store.gaps or {}).items()
    )
    assert 0 <= store.gap_remaining <= budget
    assert units == budget - store.gap_remaining
    if not units:
        assert store.gaps is None


@pytest.mark.parametrize("padding", [0, 150])
def test_gap_budget_prices_both_endpoints_and_result(
    monkeypatch: pytest.MonkeyPatch, padding: int
) -> None:
    low = "1." + "0." * padding + "0"
    high = "2"
    vlow, vhigh = engine.Version(low), engine.Version(high)
    expected = engine._between(vlow, low, vhigh, engine.Memo())
    assert expected is not None
    units = 8 + len(low) + len(high) + len(expected)
    calls = _between_calls(monkeypatch)
    for budget in (units - 1, units):
        monkeypatch.setattr(engine, "_VERSION_GAP_MAX_UNITS", budget)
        store = engine.Memo()
        before = len(calls)
        for _ in range(2):
            assert engine._cached_version_gap(vlow, low, vhigh, high, store) == expected
        assert len(calls) - before == (2 if budget < units else 1)
        assert store.gap_remaining == (budget if budget < units else 0)
        assert (store.gaps is not None) is (budget == units)


def test_failed_gap_computations_install_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def failed(
        low_version: engine.Version,
        low: str,
        high_version: engine.Version,
        store: engine.Memo,
    ) -> str | None:
        calls.append((low_version, low, high_version, store))
        raise ValueError("gap computation failed")

    monkeypatch.setattr(engine, "_between", failed)
    store = engine.Memo()
    for _ in range(2):
        with pytest.raises(ValueError, match="gap computation failed"):
            engine._cached_version_gap(
                engine.Version("1"), "1", engine.Version("2"), "2", store
            )
    assert len(calls) == 2
    assert store.gaps is None
    assert store.gap_remaining == engine._VERSION_GAP_MAX_UNITS


def test_public_witnesses_reuse_gaps_across_axes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _between_calls(monkeypatch)
    markers = [
        MarkerSet.from_marker(f'{axis} > "3.10" and {axis} < "3.11"')
        for axis in (
            "implementation_version",
            "platform_release",
            "python_full_version",
        )
    ]
    monkeypatch.setattr(engine, "_VERSION_GAP_MAX_UNITS", 0)
    monkeypatch.setattr(engine._work_meter, "remaining", 1_000_000, raising=False)
    uncached = DecisionStore()
    expected = [marker.witness(store=uncached) for marker in markers]
    baseline_calls = len(calls)
    remaining = engine._work_meter.remaining
    calls.clear()
    engine._work_meter.remaining = 1_000_000
    monkeypatch.setattr(engine, "_VERSION_GAP_MAX_UNITS", 4096)
    cached = DecisionStore()
    assert [marker.witness(store=cached) for marker in markers] == expected
    assert 0 < len(calls) < baseline_calls
    assert cached.gaps
    assert engine._work_meter.remaining == remaining
    for marker, witness in zip(markers, expected, strict=True):
        assert witness is not None
        assert marker.evaluate(witness)


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"), reason="requires native integer limits"
)
def test_warm_gap_cache_preserves_native_literal_and_headroom_guards() -> None:
    original = sys.get_int_max_str_digits()
    literal = "1." + "9" * 700
    store = DecisionStore()
    try:
        sys.set_int_max_str_digits(0)
        for axis in ("implementation_version", "platform_release"):
            assert (
                MarkerSet.from_marker(f'{axis} >= "{literal}"').is_empty(store=store)
                is False
            )
        assert store.gaps
        sys.set_int_max_str_digits(640)
        with pytest.raises(
            engine.IntractableMarkerSet, match="exceeds the 640-digit parse limit"
        ):
            MarkerSet.from_marker(f'python_full_version < "{literal}"').is_empty(
                store=store
            )
        boundary = "9" * 640
        with pytest.raises(engine.IntractableMarkerSet):
            MarkerSet.from_marker(f'python_full_version > "{boundary}"').is_empty(
                store=store
            )
    finally:
        sys.set_int_max_str_digits(original)
