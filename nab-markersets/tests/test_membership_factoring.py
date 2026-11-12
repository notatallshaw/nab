"""Factored membership markers preserve every selection and fail closed on refusal."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine
from nab_markersets import markersets
from nab_markersets._packaging import Marker
from nab_markersets.errors import IntractableMarkerSet
from nab_markersets.markersets import MarkerSet, factor_membership_marker

if TYPE_CHECKING:
    from collections.abc import Iterator


def _one_hot(variable: str) -> str:
    """A real conflict selector with shared exclusions and three permitted choices."""
    allowed = ("development", "documentation", "linting")
    names = (*allowed, "compatibility", "downstream")

    def predicate(name: str, chosen: str) -> str:
        if variable == "extra":
            return f'extra {"==" if name == chosen else "!="} "{name}"'
        return f'"{name}" {"in" if name == chosen else "not in"} {variable}'

    return " or ".join(
        "(" + " and ".join(predicate(name, chosen) for name in names) + ")"
        for chosen in allowed
    )


@pytest.mark.parametrize("variable", ["extra", "extras", "dependency_groups"])
@pytest.mark.parametrize("as_marker", [False, True])
def test_every_membership_selection_is_preserved(
    variable: str, as_marker: bool
) -> None:
    text = _one_hot(variable)
    original = MarkerSet.from_marker(text)
    factored = factor_membership_marker(Marker(text) if as_marker else text)
    assert len(factored.encode()) < len(text.encode())
    rebuilt = MarkerSet.from_marker(factored)
    emitted = Marker(factored)
    names = ("development", "documentation", "linting", "compatibility", "downstream")
    for flags in itertools.product((False, True), repeat=len(names)):
        selected = frozenset(
            name for name, active in zip(names, flags, strict=True) if active
        )
        env = {variable: selected}
        assert rebuilt.evaluate(env) == original.evaluate(env)
        if variable != "extra":
            assert emitted.evaluate(env, context="lock_file") == Marker(text).evaluate(
                env, context="lock_file"
            )
    if variable == "extra":
        for name in ("", *names):
            scalar_env = {variable: name}
            assert emitted.evaluate(scalar_env, context="lock_file") == Marker(
                text
            ).evaluate(scalar_env, context="lock_file")


@pytest.mark.parametrize(
    "text",
    [
        "",
        'python_version >= "3.12"',
        'platform_version ~= "3.12"',
        '"a" in extras or "b" in dependency_groups',
        '("a" in extras or "b" in extras) and "c" not in extras',
        'extra > "unused"',
        'extra in "a" or extra == "b"',
        'extra <= "a" or extra == "b"',
        'extras == "a" or "b" in extras',
        '"a" == extras or "b" in extras',
        'dependency_groups in "a" or "b" in dependency_groups',
        'extra == extra or extra == "b"',
        '"extra" == "constant" or extra == "b"',
        '"a" in extras',
        '"a" in extras or ("a" in extras and "b" in extras)',
        r'"a\x22b\x27c" in extras',
        r'"\ud800" in extras',
    ],
)
def test_unsupported_or_unhelpful_shapes_keep_the_original_text(text: str) -> None:
    assert factor_membership_marker(text) == text


def test_work_refusal_does_not_contaminate_the_next_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _one_hot("dependency_groups")
    with monkeypatch.context() as limits:
        limits.setattr(markersets, "_MAX_WORK", 1)
        assert factor_membership_marker(text) == text
    assert engine._work_meter.remaining is None
    assert len(factor_membership_marker(text)) < len(text)


def test_an_existing_work_budget_is_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine._work_meter, "remaining", 1)
    text = _one_hot("extras")
    assert factor_membership_marker(text) == text
    assert engine._work_meter.remaining < 0


@pytest.mark.parametrize(
    "rendered",
    [
        '"different" in dependency_groups',
        'sys_platform == "linux"',
        '"a" in extras or "b" in dependency_groups',
        '("a" in extras or "b" in extras) and ("c" in extras or "d" in extras)',
    ],
)
def test_roundtrip_rejects_changed_or_expanded_clauses(
    monkeypatch: pytest.MonkeyPatch, rendered: str
) -> None:
    monkeypatch.setattr(engine, "serialize", lambda _node: rendered)
    text = _one_hot("dependency_groups")
    assert factor_membership_marker(text) == text


def test_deep_factoring_refusal_keeps_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_clauses: object) -> engine.Formula:
        raise RecursionError

    monkeypatch.setattr(engine, "_factor_membership_clauses", refuse)
    text = _one_hot("extras")
    assert factor_membership_marker(text) == text


def test_empty_clause_list_is_false() -> None:
    assert engine._factor_membership_clauses(()) is engine.FALSE


def test_all_two_clause_formulas_over_three_memberships() -> None:
    names = ("feature-a", "feature-b", "feature-c")
    clauses = []
    for signs in itertools.product((None, False, True), repeat=len(names)):
        if all(sign is None for sign in signs):
            continue
        text = " and ".join(
            f'"{name}" {"in" if sign else "not in"} dependency_groups'
            for name, sign in zip(names, signs, strict=True)
            if sign is not None
        )
        clauses.append((signs, text))

    for left, right in itertools.combinations(clauses, 2):
        factored = MarkerSet.from_marker(
            factor_membership_marker(f"({left[1]}) or ({right[1]})")
        )
        for flags in itertools.product((False, True), repeat=len(names)):
            expected = any(
                all(
                    sign is None or sign == present
                    for sign, present in zip(signs, flags, strict=True)
                )
                for signs in (left[0], right[0])
            )
            selected = frozenset(
                name for name, present in zip(names, flags, strict=True) if present
            )
            assert factored.evaluate({"dependency_groups": selected}) == expected


def test_dnf_product_refuses_before_reading_an_oversized_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = engine.parse('"a" in extras or "b" in extras')
    right = engine.parse('"c" in extras or "d" in extras')
    source = engine.make_and((left, right))
    convert = engine._to_clauses
    reads: list[int] = []

    class RecordedClauses(list[frozenset[engine.Atom]]):
        def __iter__(self) -> Iterator[frozenset[engine.Atom]]:
            reads.append(len(self))
            return super().__iter__()

    def observed(
        node: engine.Formula, maximum: int, *, max_literal_references: int | None = None
    ) -> list[frozenset[engine.Atom]]:
        clauses = convert(node, maximum, max_literal_references=max_literal_references)
        return RecordedClauses(clauses) if node is right else clauses

    monkeypatch.setattr(engine, "_to_clauses", observed)
    with pytest.raises(IntractableMarkerSet, match="DNF clause count"):
        observed(source, 3)
    assert reads == []


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        ('"a" in extras', 0),
        ('"a" in extras or "b" in extras', 1),
        ('("a" in extras or "b" in extras) and ("c" in extras or "d" in extras)', 7),
    ],
)
def test_dnf_literal_reference_limit(text: str, limit: int) -> None:
    with pytest.raises(IntractableMarkerSet, match="literal references"):
        engine._to_clauses(engine.parse(text), 100, max_literal_references=limit)


def test_dnf_literal_reference_boundary_is_inclusive() -> None:
    tree = engine.parse(
        '("a" in extras or "b" in extras) and ("c" in extras or "d" in extras)'
    )
    clauses = engine._to_clauses(tree, 4, max_literal_references=8)
    assert len(clauses) == 4
    assert sum(map(len, clauses)) == 8
