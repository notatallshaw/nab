"""Check proxy resolution, preparation and conditional conflicts against a graph oracle."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st

from .strategies import PROPERTY_SETTINGS
from .yanking_harness import Harness
from .yanking_model import (
    Artifact,
    Case,
    Request,
    assert_preferred,
    cases,
    requests,
    solutions,
)

pytestmark = pytest.mark.property

LATE_PIN = Case(
    (
        Artifact("a", "2", withdrawn=False, dependencies=(Request("b", ">=1,<=1"),)),
        Artifact("a", "1", withdrawn=False, dependencies=(Request("b", "==1"),)),
        Artifact("b", "1", withdrawn=True),
    ),
    (Request("b"), Request("a")),
)

INDEPENDENT_OFFLINE = Case(
    (
        Artifact("a", "1", withdrawn=False),
        Artifact("a", "1", withdrawn=True),
        Artifact("b", "1", withdrawn=False),
        Artifact("b", "1", withdrawn=True),
    ),
    (Request("a", "==1"), Request("b", "==1")),
)

MIXED_FILES = Case(
    (
        Artifact("a", "1", withdrawn=False, dependencies=(Request("missing"),)),
        Artifact("a", "1", withdrawn=True),
    ),
    (Request("a", "==1"),),
)
UNSEEDED_CYCLE = Case(
    (
        Artifact("a", "1", withdrawn=True, dependencies=(Request("b", "==1"),)),
        Artifact("b", "1", withdrawn=True, dependencies=(Request("a", "==1"),)),
    ),
    (Request("a"), Request("b")),
)
LATE_EXTRA = Case(
    (
        Artifact(
            "a",
            "1",
            withdrawn=False,
            dependencies=(Request("b", "==1", marker="extra"),),
        ),
        Artifact("b", "1", withdrawn=True),
        Artifact("c", "1", withdrawn=False, dependencies=(Request("a", extra=True),)),
    ),
    (Request("b"), Request("a"), Request("c")),
)
LATE_UNKNOWN = Case(
    (
        Artifact("a", "2", withdrawn=False, dependencies=(Request("c", "==1"),)),
        Artifact("a", "1", withdrawn=False, dependencies=(Request("b", "==1"),)),
        Artifact("b", "1", withdrawn=False, dependencies=(Request("c", "==1"),)),
        Artifact("b", "1", withdrawn=True),
        Artifact("c", "1", withdrawn=False),
    ),
    (Request("a"),),
)


@given(case=cases())
@example(case=LATE_PIN)
@example(case=MIXED_FILES)
@example(case=UNSEEDED_CYCLE)
@example(case=replace(UNSEEDED_CYCLE, roots=(Request("a", "==1"),)))
@example(case=LATE_EXTRA)
@example(case=Case((Artifact("a", "1", withdrawn=True),), (Request("a", "==1.*"),)))
@PROPERTY_SETTINGS
def test_proxy_matches_exhaustive_preparation_oracle(case: Case) -> None:
    valid = solutions(case)
    harness = Harness(case)
    actual = harness.resolve()
    event(
        f"prepared withdrawn={any(o.artifact.withdrawn for o in harness.observations)}"
    )
    event(f"contextual conflict={bool(harness.clauses)}")
    event(f"backjump={bool(harness.search.stats.backjumps)}")
    assert (actual is not None) == bool(valid), (case, valid, harness.error)
    if actual is not None:
        assert actual in valid
        assert_preferred(case, actual, valid)
    assert harness.error is None or "incomplete" not in str(harness.error)
    harness.check_preparations(valid)
    harness.check_clauses(valid)


@given(case=cases())
@example(case=LATE_PIN)
@PROPERTY_SETTINGS
def test_input_permutations_preserve_satisfiability(case: Case) -> None:
    valid = solutions(case)
    reordered = replace(
        case,
        roots=tuple(reversed(case.roots)),
        constraints=tuple(reversed(case.constraints)),
        artifacts=tuple(
            replace(item, dependencies=tuple(reversed(item.dependencies)))
            for item in reversed(case.artifacts)
        ),
    )
    for ordered in (case, reordered):
        harness = Harness(ordered)
        actual = harness.resolve()
        assert (actual is not None) == bool(valid), (ordered, valid, harness.error)
        if actual is not None:
            assert actual in valid
            assert_preferred(ordered, actual, valid)
        harness.check_preparations(valid)
        harness.check_clauses(valid)


@given(case=cases(), choice=st.integers(min_value=0, max_value=100))
@example(case=INDEPENDENT_OFFLINE, choice=2)
@example(case=LATE_UNKNOWN, choice=4)
@PROPERTY_SETTINGS
def test_offline_data_does_not_authorize_a_yanked_fallback(
    case: Case, choice: int
) -> None:
    missing = case.artifacts[choice % len(case.artifacts)]
    valid = solutions(case)
    empty_completion = replace(
        case,
        artifacts=tuple(
            replace(item, dependencies=()) if item.key == missing.key else item
            for item in case.artifacts
        ),
    )
    possible = valid | solutions(empty_completion)
    harness = Harness(case, unavailable=frozenset({missing.key}))
    actual = harness.resolve()
    if actual is not None:
        assert actual in valid
        assert missing.key not in actual.files
    elif possible:
        assert harness.error is not None
        assert "incomplete" in str(harness.error) or "missing offline" in str(
            harness.error
        )
    harness.check_preparations(possible)


@given(
    case=cases(),
    choices=st.sets(st.integers(min_value=0, max_value=100), min_size=1, max_size=3),
    completion=st.lists(requests(("a", "b", "c", "missing")), max_size=2),
)
@example(case=INDEPENDENT_OFFLINE, choices={0, 2}, completion=[])
@example(
    case=Case(
        (
            Artifact("a", "1", withdrawn=False),
            Artifact("a", "1", withdrawn=True),
            Artifact("b", "1", withdrawn=True),
        ),
        (Request("a", "==1"), Request("b")),
    ),
    choices={0},
    completion=[Request("b", "==1")],
)
@PROPERTY_SETTINGS
def test_missing_metadata_completions_cannot_justify_fallback(
    case: Case, choices: set[int], completion: list[Request]
) -> None:
    """Unknown files may add admitting declarations as well as remove conflicts."""
    unavailable = frozenset(
        case.artifacts[choice % len(case.artifacts)].key for choice in choices
    )
    completed = replace(
        case,
        artifacts=tuple(
            replace(item, dependencies=tuple(completion))
            if item.key in unavailable
            else item
            for item in case.artifacts
        ),
    )
    valid = solutions(case)
    possible = valid | solutions(completed)
    harness = Harness(case, unavailable=unavailable)
    actual = harness.resolve()
    if actual is not None:
        assert actual in valid
        assert not actual.files & unavailable
    elif possible:
        assert harness.error is not None
        assert "incomplete" in str(harness.error) or "missing offline" in str(
            harness.error
        )
    harness.check_preparations(possible)
