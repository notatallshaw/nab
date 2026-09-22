"""Scoped preference proofs use real candidate facts without preparing withdrawn files."""

from __future__ import annotations

import sys
from collections import defaultdict
from functools import partial

import pytest

from nab_project._testing.yanking import Extras, ReleaseRow, graph_port
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.policy import ExtrasMode
from nab_provider.provider import Provider
from nab_provider.yank_candidates import Candidate, YankCandidates, is_pin
from nab_provider.yank_preference import (
    IncompletePreferenceError,
    PreferenceScope,
    PreparationStatus,
    YankPreference,
    _Context,
    _KnownFacts,
)
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolverStats


def preference(
    rows: list[ReleaseRow],
    roots: list[str],
    *,
    max_steps: int = 100_000,
    extras: Extras | None = None,
) -> YankPreference:
    """Keep real metadata and original declarations while testing the query protocol."""
    port = graph_port(rows, extras)
    factory = partial(
        Provider, port, defer_yanked=True, extras_mode=ExtrasMode.BACKTRACK
    )
    catalogue = YankCandidates(factory(), factory, preferences={})
    requirements = [Requirement(root) for root in roots]
    return YankPreference(
        catalogue,
        {req.name: req.specifier.to_range() for req in requirements},
        [req for req in requirements if is_pin(req)],
        {},
        max_steps=max_steps,
    )


def item(name: str, version: str = "1", *, withdrawn: bool = False) -> Candidate:
    return Candidate(name, Version(version), withdrawn)


def fail_live_then_check(
    policy: YankPreference, candidate: Candidate, selected: dict[str, Candidate]
) -> PreparationStatus:
    """Finish a deliberately refuted live query, then expose the context verdict."""
    statuses = []

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        if scope.target:
            raise ResolutionError("the fixture's live branch is impossible")
        statuses.append(policy.check(candidate, selected, scope))
        return selected

    assert policy.run(trial) == selected
    return statuses[0]


def test_live_candidates_and_unmixed_yanks_need_no_query() -> None:
    policy = preference([("a", "1", True, [])], ["a==1"])
    assert (
        policy.check(item("live"), {}, PreferenceScope()) is PreparationStatus.ALLOWED
    )
    assert (
        policy.check(item("a", withdrawn=True), {}, PreferenceScope())
        is PreparationStatus.ALLOWED
    )
    assert not policy.catalogue.facts


def test_scope_preserves_only_independently_grounded_ancestors() -> None:
    policy = preference(
        [
            ("app", "1", False, ["helper"]),
            ("helper", "1", True, ["a==1"]),
            ("a", "1", False, []),
            ("a", "1", True, ["descendant"]),
            ("descendant", "1", False, ["a==1"]),
            ("unrelated", "1", False, []),
        ],
        ["app", "helper==1", "unrelated"],
    )
    selected = {
        name: item(name, withdrawn=name in {"helper", "a"})
        for name in ("descendant", "helper", "a", "app", "unrelated")
    }
    for candidate in selected.values():
        assert policy.catalogue.prepare(candidate) is not None
    assert policy.parents(selected["a"], selected) == frozenset(
        {selected["app"], selected["helper"]}
    )
    seen = []

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        seen.append(scope)
        if scope.target:
            return {"a": item("a")}
        policy.check(selected["a"], selected, scope)
        pytest.fail("a live proof must be requested")

    assert policy.run(trial) == {"a": item("a")}
    assert seen[1].fixed == frozenset({selected["app"], selected["helper"]})
    assert seen[1].live == frozenset({"app", "descendant", "unrelated", "a"})
    assert seen[1].target == "a"


def test_cached_unrooted_cycles_do_not_create_parent_assumptions() -> None:
    policy = preference(
        [
            ("a", "1", True, ["b==1"]),
            ("b", "1", True, ["a==1"]),
            ("unused", "1", False, []),
        ],
        ["a", "b"],
    )
    selected = {
        "a": item("a", withdrawn=True),
        "b": item("b", withdrawn=True),
        "unused": item("unused"),
    }
    for candidate in selected.values():
        assert policy.catalogue.prepare(candidate) is not None
    assert policy.parents(selected["a"], selected) == frozenset()
    policy.catalogue.facts.clear()
    assert policy.parents(selected["a"], selected) == frozenset()


def test_deeper_queries_keep_live_classes_but_reconsider_unrelated_fixed_choices() -> (
    None
):
    policy = preference(
        [
            ("a", "1", False, []),
            ("a", "1", True, []),
            ("b", "1", False, []),
            ("b", "1", True, []),
        ],
        ["a==1", "b==1"],
    )
    parent = item("parent")
    initial = PreferenceScope(frozenset({parent}), frozenset({"a", "unselected"}), "a")
    seen = []

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        if scope.target == "b":
            seen.append(scope)
            return {"a": item("a"), "b": item("b")}
        policy.check(
            item("b", withdrawn=True),
            {"a": item("a"), "b": item("b", withdrawn=True)},
            initial,
        )
        pytest.fail("a new live family must suspend the query")

    assert policy.run(trial) == {"a": item("a"), "b": item("b")}
    assert seen == [
        PreferenceScope(frozenset(), frozenset({"a", "b", "unselected"}), "b")
    ]


def test_failed_live_proof_checks_context_without_preparing_unknown_metadata() -> None:
    policy = preference([("a", "1", False, []), ("a", "1", True, [])], ["a==1"])
    selected = {"a": item("a", withdrawn=True)}
    assert (
        fail_live_then_check(policy, selected["a"], selected)
        is PreparationStatus.ALLOWED
    )
    assert not policy.catalogue.facts
    port = policy.catalogue.provider.coordinator
    assert all(
        "True.whl" not in str(call) for call in port.calls_to("request_metadata")
    )
    assert policy.stats.rounds > 0
    rounds = policy.stats.rounds
    assert (
        policy.check(selected["a"], selected, PreferenceScope())
        is PreparationStatus.ALLOWED
    )
    assert policy.stats.rounds == rounds


def test_new_facts_invalidate_positive_context_checks_but_not_refutations() -> None:
    policy = preference(
        [("a", "1", False, []), ("a", "1", True, []), ("b", "1", False, ["missing"])],
        ["a==1", "b"],
    )
    selected = {"a": item("a", withdrawn=True), "b": item("b")}
    assert (
        fail_live_then_check(policy, selected["a"], selected)
        is PreparationStatus.ALLOWED
    )
    assert policy.catalogue.prepare(selected["b"]) is not None
    assert (
        fail_live_then_check(policy, selected["a"], selected)
        is PreparationStatus.IMPOSSIBLE
    )
    rounds = policy.stats.rounds
    policy.catalogue.prepare(item("a"))
    assert (
        policy.check(selected["a"], selected, PreferenceScope())
        is PreparationStatus.IMPOSSIBLE
    )
    assert policy.stats.rounds == rounds
    assert selected["a"] not in policy.catalogue.facts


def test_offline_live_failure_remains_incomplete() -> None:
    policy = preference([("a", "1", False, []), ("a", "1", True, [])], ["a==1"])
    selected = {"a": item("a", withdrawn=True)}

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        if scope.target:
            raise IncompletePreferenceError("missing live metadata")
        assert (
            policy.check(selected["a"], selected, scope) is PreparationStatus.INCOMPLETE
        )
        return selected

    assert policy.run(trial) == selected
    assert not policy.catalogue.facts
    assert policy.stats.rounds == 0


def test_unknown_listing_cannot_refute_the_current_context() -> None:
    policy = preference(
        [("a", "1", False, []), ("a", "1", True, [])], ["a==1", "missing"]
    )
    policy.catalogue.provider.coordinator.index.store_listing(
        "missing", [], offline_miss=True
    )
    selected = {"a": item("a", withdrawn=True)}
    assert (
        fail_live_then_check(policy, selected["a"], selected)
        is PreparationStatus.ALLOWED
    )
    assert "missing" in policy.catalogue.missing_listings


@pytest.mark.parametrize(
    "error", [ResolutionError("no graph"), IncompletePreferenceError("unknown graph")]
)
def test_root_failure_preserves_its_classification(error: ResolutionError) -> None:
    policy = preference([], [])

    def trial(_scope: PreferenceScope) -> dict[str, Candidate]:
        raise error

    with pytest.raises(type(error), match=str(error)):
        policy.run(trial)


def test_callback_unwinds_and_spends_before_running_the_next_query() -> None:
    policy = preference(
        [("a", "1", False, []), ("a", "1", True, [])], ["a==1"], max_steps=2
    )
    selected = {"a": item("a", withdrawn=True)}
    visits = []

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        visits.append(scope)
        try:
            policy.check(selected["a"], selected, scope)
        finally:
            policy.spend(2)
        pytest.fail("the root needs a live query")

    with pytest.raises(IncompletePreferenceError, match="search limit"):
        policy.run(trial)
    assert visits == [PreferenceScope()]
    assert policy.remaining == 0


def test_context_request_also_unwinds_before_spending_its_budget() -> None:
    policy = preference(
        [("a", "1", False, []), ("a", "1", True, [])], ["a==1"], max_steps=3
    )
    selected = {"a": item("a", withdrawn=True)}

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        try:
            if scope.target:
                raise ResolutionError("no live graph")
            policy.check(selected["a"], selected, scope)
        finally:
            policy.spend(1)
        pytest.fail("a context proof must be requested")

    with pytest.raises(
        IncompletePreferenceError, match="search limit|context check stopped"
    ):
        policy.run(trial)
    assert policy.remaining <= 0


def test_excess_budget_and_cyclic_queries_are_incomplete() -> None:
    policy = preference(
        [("a", "1", False, []), ("a", "1", True, [])], ["a==1"], max_steps=1
    )
    with pytest.raises(IncompletePreferenceError, match="search limit"):
        policy.spend(2)
    with pytest.raises(IncompletePreferenceError, match="cyclic query"):
        policy.check(
            item("a", withdrawn=True), {}, PreferenceScope(live=frozenset({"a"}))
        )


def test_successful_nested_query_must_still_require_its_target() -> None:
    policy = preference([("a", "1", False, []), ("a", "1", True, [])], ["a==1"])

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        if scope.target:
            return {}
        policy.check(item("a", withdrawn=True), {}, scope)
        pytest.fail("a live proof must be requested")

    with pytest.raises(IncompletePreferenceError, match="target disappeared"):
        policy.run(trial)


def test_all_solver_counters_accumulate_without_aliasing_maps() -> None:
    policy = preference([], [])
    stats: ResolverStats[str] = ResolverStats(
        1, 2, 3, 4, 5, 6, 7, 8, defaultdict(int, {"a": 9}), defaultdict(int, {"b": 10})
    )
    policy.record_solver_stats(stats)
    policy.record_solver_stats(stats)
    assert policy.stats == ResolverStats(
        2,
        4,
        6,
        8,
        10,
        12,
        14,
        16,
        defaultdict(int, {"a": 18}),
        defaultdict(int, {"b": 20}),
    )
    assert stats.package_conflict_counts["a"] == 9
    assert stats.package_culprit_counts["b"] == 10


def test_known_facts_retains_identity_constraints_rejections_and_extras() -> None:
    policy = preference(
        [("a", "1", False, []), ("a", "1", True, []), ("a", "2", False, [])], ["a==1"]
    )
    one, two, withdrawn = item("a"), item("a", "2"), item("a", withdrawn=True)
    policy.catalogue.facts[two] = None
    context = _Context(PreferenceScope(live=frozenset({"a"})), frozenset())
    facts = _KnownFacts(
        policy.catalogue, {"a": VersionRange.singleton(Version("1"))}, context
    )
    allowed = facts.encode("a", VersionRange.full())
    assert facts.choose_version("a", allowed) == facts.identify(one)
    assert facts.has_satisfying_version("a", allowed)
    assert not facts.has_satisfying_version("a", Range.empty())
    assert facts.identify(withdrawn) not in allowed
    assert facts.prioritize("a", allowed, {}) == 3
    extra = item("a[x]")
    dependencies = facts.get_dependencies("a[x]", facts.identify(extra))
    assert dependencies == {"a": Range.singleton(facts.identify(one))}
    assert not policy.catalogue.facts.get(one)


def test_known_rejection_is_not_treated_as_unknown_metadata() -> None:
    policy = preference([("a", "1", False, []), ("a", "1", True, [])], ["a==1"])
    selected = {"a": item("a", withdrawn=True)}
    policy.catalogue.facts[selected["a"]] = None
    assert (
        fail_live_then_check(policy, selected["a"], selected)
        is PreparationStatus.IMPOSSIBLE
    )


def test_nested_live_queries_use_an_explicit_stack() -> None:
    count = 220
    rows = [(f"p{i}", "1", state, []) for i in range(count) for state in (False, True)]
    policy = preference(rows, [])
    visits = []

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        visits.append(len(scope.live))
        if len(scope.live) == count:
            return {scope.target: item(scope.target)}
        policy.check(item(f"p{len(scope.live)}", withdrawn=True), {}, scope)
        pytest.fail("a new live class must suspend the query")

    original_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(180)
        selected = policy.run(trial)
    finally:
        sys.setrecursionlimit(original_limit)
    assert selected == {f"p{count - 1}": item(f"p{count - 1}")}
    assert visits == list(range(count + 1))
    assert not policy.catalogue.facts


@pytest.mark.parametrize("root", ["a==1", "a"])
def test_explain_input_or_constraint_pin_without_inventing_demand(root: str) -> None:
    policy = preference([("a", "1", True, [])], [root])
    policy.input_pins = [Requirement("a==1"), Requirement("unused==1")]
    selected = {"a": item("a", withdrawn=True)}
    assert policy.catalogue.prepare(selected["a"]) is not None
    assert policy.explain(selected) == {"a": ("input pin a==1",)}
    assert policy.catalogue.provider.yank_admission_sources is None
    assert "yank_admission_sources" not in vars(policy.catalogue.provider)
    pins, provider = policy.adopt(selected)
    assert pins == {"a": Version("1")}
    assert provider.yank_admission_sources == {"a": ("input pin a==1",)}
    assert provider.yank_admissions == frozenset({("a", Version("1"))})


def test_explain_late_transitive_pin_uses_a_selected_grounded_parent() -> None:
    policy = preference(
        [
            ("root", "1", False, ["helper"]),
            ("helper", "2", False, ["dep==1"]),
            ("dep", "1", True, []),
        ],
        ["dep", "root"],
    )
    policy.input_pins = [Requirement("dep==2")]
    selected = {
        "dep": item("dep", withdrawn=True),
        "helper": item("helper", "2"),
        "root": item("root"),
    }
    for candidate in selected.values():
        assert policy.catalogue.prepare(candidate) is not None
    assert policy.explain(selected) == {"dep": ("helper==2 requires dep==1",)}


def test_explain_extra_pin_names_the_selected_extra() -> None:
    policy = preference(
        [("app", "1", False, ['dep==1; extra == "x"']), ("dep", "1", True, [])],
        ["app", "dep"],
        extras={("app", "1", False): ("x",)},
    )
    policy.roots = dict(policy.roots, **{"app[x]": VersionRange.full()})
    selected = {
        "app": item("app"),
        "app[x]": item("app[x]"),
        "dep": item("dep", withdrawn=True),
    }
    for candidate in selected.values():
        assert policy.catalogue.prepare(candidate) is not None
    assert policy.explain(selected) == {
        "dep": ('app[x]==1 requires dep==1; extra == "x"',)
    }
    del selected["app[x]"]
    policy.roots = {"app": VersionRange.full(), "dep": VersionRange.full()}
    with pytest.raises(IncompletePreferenceError, match="not rooted"):
        policy.explain(selected)


@pytest.mark.parametrize("root", ["a", "a==1"])
def test_only_rooted_cycles_have_admission_explanations(root: str) -> None:
    policy = preference(
        [("a", "1", True, ["b==1"]), ("b", "1", True, ["a==1"])], [root]
    )
    selected = {"a": item("a", withdrawn=True), "b": item("b", withdrawn=True)}
    for candidate in selected.values():
        assert policy.catalogue.prepare(candidate) is not None
    if root == "a":
        with pytest.raises(IncompletePreferenceError, match="not rooted"):
            policy.adopt(selected)
        assert policy.catalogue.provider.yank_admission_sources is None
        assert not policy.catalogue.provider.yank_admissions
    else:
        assert policy.explain(selected) == {
            "a": ("input pin a==1",),
            "b": ("a==1 requires b==1",),
        }


@pytest.mark.parametrize("prepared", [False, True])
def test_missing_selected_metadata_or_required_dependency_is_not_explained(
    *, prepared: bool
) -> None:
    policy = preference(
        [("app", "1", False, ["dep==1"]), ("dep", "1", True, [])], ["app"]
    )
    selected = {"app": item("app")}
    if prepared:
        assert policy.catalogue.prepare(selected["app"]) is not None
    with pytest.raises(IncompletePreferenceError, match="missing metadata"):
        policy.explain(selected)


def test_unreachable_selection_cannot_be_explained_by_a_constraint() -> None:
    policy = preference([("a", "1", True, [])], [])
    policy.input_pins = [Requirement("a==1")]
    selected = {"a": item("a", withdrawn=True)}
    assert policy.catalogue.prepare(selected["a"]) is not None
    with pytest.raises(IncompletePreferenceError, match="not rooted"):
        policy.explain(selected)
    assert policy.explain({}) == {}


def test_base_and_extra_share_one_rooted_explanation() -> None:
    policy = preference(
        [("a", "1", True, [])], ["a==1"], extras={("a", "1", True): ("x",)}
    )
    policy.roots = dict(policy.roots, **{"a[x]": VersionRange.full()})
    selected = {"a": item("a", withdrawn=True), "a[x]": item("a[x]", withdrawn=True)}
    for candidate in selected.values():
        assert policy.catalogue.prepare(candidate) is not None
    assert policy.explain(selected) == {"a": ("input pin a==1",)}


def test_grounded_parent_pin_excludes_unrelated_live_versions_without_query() -> None:
    policy = preference(
        [
            ("app", "1", False, ["dep==2"]),
            ("dep", "1", False, []),
            ("dep", "2", True, []),
        ],
        ["app"],
    )
    selected = {"app": item("app"), "dep": item("dep", "2", withdrawn=True)}
    assert policy.catalogue.prepare(selected["app"]) is not None
    assert (
        policy.check(selected["dep"], selected, PreferenceScope())
        is PreparationStatus.ALLOWED
    )
    assert not policy.outcomes
    assert policy.stats.rounds == 0
    assert selected["dep"] not in policy.catalogue.facts


@pytest.mark.parametrize(
    "source", ["root", "extra-root", "constraint", "extra-constraint", "parent-extra"]
)
def test_only_required_target_ranges_can_exclude_live_choices(source: str) -> None:
    policy = preference(
        [
            ("app", "1", False, ["dep[x]>=2"]),
            ("dep", "1", False, []),
            ("dep", "2", True, []),
        ],
        ["dep"],
    )
    two = VersionRange.singleton(Version("2"))
    selected = {"dep": item("dep", "2", withdrawn=True)}
    if source == "root":
        policy.roots = {"dep": two}
    elif source == "extra-root":
        policy.roots = {"dep": VersionRange.full(), "dep[x]": two}
    elif source == "constraint":
        policy.constraints = {"dep": two}
    elif source == "extra-constraint":
        policy.roots = {"dep": VersionRange.full(), "dep[x]": VersionRange.full()}
        policy.constraints = {"dep[x]": two}
    else:
        policy.roots = {"app": VersionRange.full()}
        selected["app"] = item("app")
        assert policy.catalogue.prepare(selected["app"]) is not None
    assert (
        policy.check(selected["dep"], selected, PreferenceScope())
        is PreparationStatus.ALLOWED
    )
    assert not policy.outcomes
    assert selected["dep"] not in policy.catalogue.facts


def test_inactive_extra_constraint_does_not_exclude_a_live_alternative() -> None:
    policy = preference([("a", "1", False, []), ("a", "1", True, [])], ["a==1"])
    policy.constraints = {"a[x]": VersionRange.singleton(Version("2"))}
    selected = {"a": item("a", withdrawn=True)}
    visited = []

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        visited.append(scope)
        if scope.target:
            return {"a": item("a")}
        policy.check(selected["a"], selected, scope)
        pytest.fail("the unused extra constraint must not remove the live choice")

    assert policy.run(trial) == {"a": item("a")}
    assert len(visited) == 2
    assert not policy.catalogue.facts


def test_cached_target_cycle_cannot_narrow_its_live_alternatives() -> None:
    policy = preference(
        [
            ("a", "2+live", False, []),
            ("a", "2", True, ["b==1"]),
            ("b", "1", False, ["a===2"]),
        ],
        ["a==2"],
    )
    selected = {"a": item("a", "2", withdrawn=True), "b": item("b")}
    for candidate in selected.values():
        assert policy.catalogue.prepare(candidate) is not None
    visited = []

    def trial(scope: PreferenceScope) -> dict[str, Candidate]:
        visited.append(scope)
        if scope.target:
            return {"a": item("a", "2+live")}
        policy.check(selected["a"], selected, scope)
        pytest.fail("cached target dependencies must not remove the live choice")

    assert policy.run(trial) == {"a": item("a", "2+live")}
    assert len(visited) == 2
    assert visited[1].fixed == frozenset()


def test_fixed_parent_requirement_blocks_a_mismatching_yank_before_preparation() -> (
    None
):
    policy = preference([("app", "1", False, ["a==2"]), ("a", "1", True, [])], ["app"])
    selected = {"app": item("app"), "a": item("a", withdrawn=True)}
    assert policy.catalogue.prepare(selected["app"]) is not None
    assert (
        policy.check(selected["a"], selected, PreferenceScope())
        is PreparationStatus.IMPOSSIBLE
    )
    assert selected["a"] not in policy.catalogue.facts
    assert not policy.outcomes


def test_root_and_extra_intersection_can_refute_every_artifact() -> None:
    policy = preference([("a", "1", False, []), ("a", "2", True, [])], ["a==2"])
    policy.roots = dict(policy.roots, **{"a[x]": VersionRange.singleton(Version("1"))})
    assert (
        policy.check(item("a", "2", withdrawn=True), {}, PreferenceScope())
        is PreparationStatus.IMPOSSIBLE
    )
    assert not policy.catalogue.facts
