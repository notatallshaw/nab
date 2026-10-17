"""Check order-independent pool reuse, independent outputs and bounded retention."""

from __future__ import annotations

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import DecisionStore, MarkerSet


def _entries(*texts: str) -> list[tuple[engine.Version, str]]:
    """Pair each endpoint with the full version text production uses."""
    return [(engine.Version(text), text) for text in texts]


def _gap_requests(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Observe the real gap-cache loop, including hits in its existing table."""
    requests: list[tuple[str, str]] = []
    original = engine._cached_version_gap

    def gap(
        vlow: engine.Version,
        low: str,
        vhigh: engine.Version,
        high: str,
        memo: engine.Memo,
    ) -> str | None:
        requests.append((low, high))
        return original(vlow, low, vhigh, high, memo)

    monkeypatch.setattr(engine, "_cached_version_gap", gap)
    return requests


def test_reordered_and_repeated_endpoints_reuse_a_completed_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _gap_requests(monkeypatch)
    store = engine.Memo()
    entries = _entries("3.10", "3.11", "3.10.0")
    expected = engine._version_pool(entries, memo=store)
    assert store.pools is None
    assert store.pool_remaining == engine._VERSION_POOL_MAX_UNITS
    assert engine._version_pool(entries, memo=store) == expected
    after = len(requests)
    assert after > 0
    assert store.pools

    # Repetitions do not enlarge the deduplicated pool's key.
    repeated = list(reversed(entries)) * 300
    assert engine._version_pool(repeated, memo=store) == expected
    assert len(requests) == after
    other = engine.Memo()
    assert engine._version_pool(entries, memo=other) == expected
    assert engine._version_pool(entries, memo=other) == expected
    assert len(requests) == 2 * after


def test_returned_lists_do_not_mutate_retained_pool_text() -> None:
    entries = _entries("3.10", "3.11")
    store = engine.Memo()
    expected = engine._version_pool(entries, memo=store)
    first = engine._version_pool(entries, memo=store)
    first[0] = "changed"
    first.append("changed")
    second = engine._version_pool(entries, memo=store)
    assert second == expected
    assert second is not first
    second.clear()
    assert engine._version_pool(entries, memo=store) == expected


@pytest.mark.parametrize(
    "texts",
    [
        ("6.0", "6.0.1"),
        ("6.0.0.0", "6.0.1"),
        ("6.0", "6.0.1.0.0"),
        ("1!2.0", "2!0"),
        ("1.0.dev0", "1.0a0", "1.0rc1", "1.0", "1.0.post0"),
        ("1+a", "1+a.a", "1+b"),
    ],
)
def test_pool_hits_preserve_exact_ordered_candidates(texts: tuple[str, ...]) -> None:
    entries = _entries(*texts)
    expected = engine._version_pool(entries, memo=engine.Memo())
    store = engine.Memo()
    engine._version_pool([], memo=store)
    assert engine._version_pool(entries, memo=store) == expected
    assert engine._version_pool(list(reversed(entries)), memo=store) == expected


def test_equal_versions_with_different_text_keep_distinct_pools() -> None:
    store = engine.Memo()
    engine._version_pool([], memo=store)
    for low, high, expected in (
        ("6.0", "6.0.1", "6.0.0.1"),
        ("6.0.0.0", "6.0.1", "6.0.0.0.1"),
        ("6.0", "6.0.1.0.0", "6.0.0.0.0.1"),
    ):
        result = engine._version_pool(_entries(low, high), memo=store)
        assert expected in result
        assert result == engine._version_pool(_entries(low, high), memo=engine.Memo())


@pytest.mark.parametrize("budget", [0, 12, 20, 64, 512, 4096])
def test_pool_budget_prices_retained_key_and_result_text(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    monkeypatch.setattr(engine, "_VERSION_POOL_MAX_UNITS", budget)
    store = engine.Memo()
    for texts in (("3.10", "3.11"), ("1+a", "1+a.a"), ("3.10.0", "3.12")):
        entries = _entries(*texts)
        expected = engine._version_pool(entries, memo=engine.Memo())
        for _ in range(3):
            assert engine._version_pool(entries, memo=store) == expected
    units = sum(
        12 + len(key) + len(points) + sum(map(len, key)) + sum(map(len, points))
        for key, points in (store.pools or {}).items()
    )
    assert units == budget - store.pool_remaining
    assert 0 <= units <= budget
    if not units:
        assert store.pools is None


@pytest.mark.parametrize("padding", [0, 120])
def test_exact_pool_budget_controls_recomputation(
    monkeypatch: pytest.MonkeyPatch, padding: int
) -> None:
    entries = _entries("1." + "0." * padding + "0", "2")
    expected = engine._version_pool(entries, memo=engine.Memo())
    key_text = {"0", "0.dev0", "99999", *(text for _, text in entries)}
    units = 12 + len(key_text) + len(expected)
    units += sum(map(len, key_text)) + sum(map(len, expected))
    requests = _gap_requests(monkeypatch)
    for budget in (units - 1, units):
        monkeypatch.setattr(engine, "_VERSION_POOL_MAX_UNITS", budget)
        store = engine.Memo()
        engine._version_pool(entries, memo=store)
        assert engine._version_pool(entries, memo=store) == expected
        after = len(requests)
        assert engine._version_pool(entries, memo=store) == expected
        assert (len(requests) == after) is (budget == units)
        assert store.pool_remaining == (budget if budget < units else 0)


def test_failed_pool_builds_install_no_completed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def failed(
        vlow: engine.Version,
        low: str,
        vhigh: engine.Version,
        high: str,
        memo: engine.Memo,
    ) -> str | None:
        calls.append((vlow, low, vhigh, high, memo))
        raise ValueError("pool gap failed")

    store = engine.Memo()
    engine._version_pool([], memo=store)
    monkeypatch.setattr(engine, "_cached_version_gap", failed)
    for _ in range(2):
        with pytest.raises(ValueError, match="pool gap failed"):
            engine._version_pool(_entries("3.10", "3.11"), memo=store)
    assert len(calls) == 2
    assert store.pools is None
    assert store.pool_remaining == engine._VERSION_POOL_MAX_UNITS


def test_public_pool_reuse_preserves_witnesses_and_cell_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _gap_requests(monkeypatch)
    markers = [
        MarkerSet.from_marker(f'{axis} > "3.10" and {axis} < "3.11"')
        for axis in (
            "implementation_version",
            "platform_release",
            "python_full_version",
        )
    ]
    monkeypatch.setattr(engine, "_VERSION_GAP_MAX_UNITS", 0)
    monkeypatch.setattr(engine, "_VERSION_POOL_MAX_UNITS", 0)
    expected = [marker.witness(store=DecisionStore()) for marker in markers]
    requests.clear()
    monkeypatch.setattr(engine._work_meter, "remaining", 1_000_000, raising=False)
    uncached = DecisionStore()
    assert [marker.witness(store=uncached) for marker in markers] == expected
    baseline = len(requests)
    remaining = engine._work_meter.remaining
    requests.clear()
    engine._work_meter.remaining = 1_000_000
    monkeypatch.setattr(engine, "_VERSION_POOL_MAX_UNITS", 4096)
    cached = DecisionStore()
    actual = [marker.witness(store=cached) for marker in markers]
    assert actual == expected
    assert 0 < len(requests) < baseline
    assert cached.pools
    assert cached.gaps is None
    assert engine._work_meter.remaining == remaining
    for marker, witness in zip(markers, actual, strict=True):
        assert witness is not None
        assert marker.evaluate(witness)


@pytest.mark.parametrize("budget", [0, 64])
def test_rejected_reference_keys_are_not_allocated(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    entries = _entries(*(str(value) for value in range(10, 90)))
    allocated = []

    def freeze(values: set[str]) -> frozenset[str]:
        allocated.append(len(values))
        return frozenset(values)

    monkeypatch.setattr(engine, "_VERSION_POOL_MAX_UNITS", budget)
    monkeypatch.setattr(engine, "frozenset", freeze, raising=False)
    store = engine.Memo()
    expected = engine._version_pool(entries, memo=store)
    assert engine._version_pool(entries, memo=store) == expected
    assert engine._version_pool(entries, memo=store) == expected
    assert allocated == []
    assert store.pools is None
    assert store.pool_remaining == budget
