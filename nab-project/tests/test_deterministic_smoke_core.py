"""Tests for the deterministic benchmark fixture and semantic contracts."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType

import pytest

from nab_project.inputs import ResolveInputs
from nab_project.lockfile import IndexPin, TargetLock, WheelArtifact
from nab_project.resolve import ResolveResult, TargetResult
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version

pytestmark = pytest.mark.benchmark

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"


def _harness() -> ModuleType:
    """Load the benchmark script by path and keep one copy for the session.

    The benchmarks directory is not a package. Caching in sys.modules keeps
    every test working against the same module object, so monkeypatching it
    sticks.
    """
    name = "_nab_deterministic_smoke"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(
        name, _BENCHMARKS / "deterministic_smoke.py"
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class _LockValidationCase:
    """A valid fixture lock plus everything needed to re-validate a mutated copy."""

    harness: ModuleType
    lock: Pylock
    result: ResolveResult
    config: ResolveInputs
    requirements: Sequence[Requirement]
    distributions: Sequence[object]
    fixture: Path
    expected: Mapping[str, Mapping[str, str]]

    def validate(self, lock: Pylock) -> dict[str, dict[str, str]]:
        return self.harness.validate_nab_lock(
            lock,
            self.result,
            self.config,
            self.requirements,
            self.distributions,
            self.fixture,
        )


@pytest.fixture(scope="module")
def smoke_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the fixture once for every test that only reads it.

    Writing the corpus is the module's largest single cost, and the suite already
    refuses to run against a tree that changed underneath it, so sharing one root
    is safe. Tests about materialization or storage identity build their own.
    """
    harness = _harness()
    distributions, expected_digest = harness.load_fixture()
    index = tmp_path_factory.mktemp("deterministic-smoke") / "index"
    harness.materialize_fixture(index, distributions, expected_digest)
    return index


@pytest.fixture(scope="module")
def basic_lock_case(smoke_index: Path) -> _LockValidationCase:
    """Build the lock the basic scenario should produce, without resolving for it.

    Assembling the pins by hand gives the rejection cases a lock they can corrupt
    one field at a time, and keeps a resolver regression from showing up here as a
    validator failure.
    """
    harness = _harness()
    distributions, _expected_digest = harness.load_fixture()
    fixture = smoke_index

    basic = harness.load_scenarios()[0]
    prepared = harness.prepare_scenario(basic, fixture)
    target = prepared.targets[0]
    fixture_distributions = {
        (harness.canonicalize_name(item.name), item.version): item
        for item in distributions
    }

    pins: dict[str, IndexPin] = {}
    resolved_versions: dict[str, Version] = {}
    for name, version in prepared.expected[target.label].items():
        wheel = next(
            (fixture / "packages").glob(
                f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
            )
        )
        distribution = fixture_distributions[(name, version)]
        pins[name] = IndexPin(
            name=name,
            version=version,
            index=fixture.resolve().as_uri(),
            wheels=(
                WheelArtifact(
                    filename=wheel.name,
                    url=wheel.resolve().as_uri(),
                    hashes=(("sha256", harness.file_sha256(wheel)),),
                    size=wheel.stat().st_size,
                    local_path=wheel.resolve(),
                ),
            ),
            requires_python=distribution.requires_python,
        )
        resolved_versions[name] = Version(version)

    # The basic scenario's only edge, stated here so the emitted lock has
    # something for the edge checks to disagree with.
    target_lock = TargetLock(
        target=target,
        pins=pins,
        dependencies={"nab-smoke-basic": ("nab-smoke-basic-leaf",)},
        base_dependencies={"nab-smoke-basic": ("nab-smoke-basic-leaf",)},
    )
    result = ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target,
                success=True,
                pins=resolved_versions,
                lock=target_lock,
            )
        ],
    )

    lock = harness.build_pylock(
        harness.build_lock_input(result, inputs=prepared.config),
        lock_dir=fixture,
    )
    return _LockValidationCase(
        harness=harness,
        lock=lock,
        result=result,
        config=prepared.config,
        requirements=prepared.requirements,
        distributions=distributions,
        fixture=fixture,
        expected=prepared.expected,
    )


# One corruption per helper, each breaking a different clause of the lock
# contract. test_nab_lock_validation_rejects_corrupted_locks runs them all.


def _replace_lock_package(
    lock: Pylock, package_index: int, **changes: object
) -> Pylock:
    packages = list(lock.packages)
    packages[package_index] = replace(packages[package_index], **changes)
    return replace(lock, packages=tuple(packages))


def _missing_environments(case: _LockValidationCase) -> Pylock:
    return replace(case.lock, environments=())


def _duplicate_package(case: _LockValidationCase) -> Pylock:
    first = case.lock.packages[0]
    return replace(case.lock, packages=(first, *case.lock.packages))


def _external_index(case: _LockValidationCase) -> Pylock:
    return _replace_lock_package(
        case.lock,
        0,
        index="https://example.invalid/simple",
    )


def _external_artifact(case: _LockValidationCase) -> Pylock:
    """Point the wheel one directory above the fixture, keeping its filename."""
    first_wheel = case.lock.packages[0].wheels[0]
    outside = case.fixture.parent / first_wheel.filename
    artifact = (
        replace(first_wheel, url=outside.as_uri())
        if first_wheel.url is not None
        else replace(first_wheel, path=str(outside))
    )
    return _replace_lock_package(case.lock, 0, wheels=(artifact,))


def _invalid_hash(case: _LockValidationCase) -> Pylock:
    first_wheel = case.lock.packages[0].wheels[0]
    wheel = replace(first_wheel, hashes={"sha256": "0" * 64})
    return _replace_lock_package(case.lock, 0, wheels=(wheel,))


def _missing_dependencies(case: _LockValidationCase) -> Pylock:
    packages = tuple(
        replace(package, dependencies=None) for package in case.lock.packages
    )
    return replace(case.lock, packages=packages)


def _extra_dependency_field(
    case: _LockValidationCase, name: str, value: object
) -> Pylock:
    """Add a field beside `name` in the first dependency entry that has one."""
    index, package = next(
        (index, package)
        for index, package in enumerate(case.lock.packages)
        if package.dependencies
    )
    first, *remaining = package.dependencies
    dependency = {**first, name: value}
    return _replace_lock_package(
        case.lock,
        index,
        dependencies=(dependency, *remaining),
    )


def _dependency_version(case: _LockValidationCase) -> Pylock:
    return _extra_dependency_field(case, "version", "0.0.0")


def _dependency_source(case: _LockValidationCase) -> Pylock:
    return _extra_dependency_field(
        case,
        "source",
        {"index": "https://example.invalid"},
    )


def _unknown_dependency_field(case: _LockValidationCase) -> Pylock:
    return _extra_dependency_field(case, "unknown", value=True)


def _listing_arrival_states(packages: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Listing sets to land before a resolve, from none of them up to all of them.

    Landing a prefix leaves the rest in flight, so each state hands the priority
    scan a different view of what has arrived without changing anything the
    resolver is asked to solve. The empty state repeats because it is the one a
    user gets, and the one whose package order the machine's timing settles.
    """
    quarter = len(packages) // 4
    counts = (0, 0, 0, quarter, 2 * quarter, 3 * quarter, len(packages))
    return {
        f"run{index}-landed-{count}": tuple(packages[:count])
        for index, count in enumerate(counts)
    }


def test_fixture_is_content_addressed_and_reusable(tmp_path: Path) -> None:
    harness = _harness()
    distributions, expected_digest = harness.load_fixture()
    fixture = tmp_path / "index"

    first = harness.materialize_fixture(fixture, distributions, expected_digest)
    second = harness.materialize_fixture(fixture, distributions, expected_digest)

    assert first == second == expected_digest
    assert len(distributions) == 226


def test_fixture_access_distinguishes_storage_but_accepts_symlink_aliases(
    tmp_path: Path,
) -> None:
    harness = _harness()
    distributions, expected_digest = harness.load_fixture()
    first = tmp_path / "first"
    second = tmp_path / "second"
    harness.materialize_fixture(first, distributions, expected_digest)
    harness.materialize_fixture(second, distributions, expected_digest)
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)

    first_access = harness.fixture_access_identity(
        first, expected_digest, mode="caller-materialized"
    )
    alias_access = harness.fixture_access_identity(
        alias, expected_digest, mode="caller-materialized"
    )
    second_access = harness.fixture_access_identity(
        second, expected_digest, mode="caller-materialized"
    )

    assert harness.fixture_digest(first) == harness.fixture_digest(second)
    assert alias_access == first_access
    assert second_access != first_access
    assert second_access["resolved_root"] != first_access["resolved_root"]
    assert second_access["stat_manifest_sha256"] != first_access["stat_manifest_sha256"]


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_materialized_fixture_validation_rejects_missing_or_empty_storage(
    tmp_path: Path,
    state: str,
) -> None:
    harness = _harness()
    root = tmp_path / "fixture"
    if state == "empty":
        root.mkdir()

    with pytest.raises(harness.SmokeContractError, match="does not exist|empty"):
        harness.validate_materialized_fixture(
            root,
            "0" * 64,
            mode="caller-materialized",
        )


def test_suite_rejects_same_bytes_recreated_at_the_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness()
    distributions, expected_digest = harness.load_fixture()
    fixture = tmp_path / "fixture"
    harness.materialize_fixture(fixture, distributions, expected_digest)
    replaced = False

    # Same bytes, same path, different inode. Only the stat manifest can tell.
    def replace_fixture(*_args: object) -> dict[str, str]:
        nonlocal replaced
        if not replaced:
            fixture.rename(tmp_path / "original-fixture")
            harness.materialize_fixture(fixture, distributions, expected_digest)
            replaced = True
        return {"id": "basic"}

    monkeypatch.setattr(harness, "run_scenario", replace_fixture)

    with pytest.raises(harness.SmokeContractError, match="storage changed"):
        harness.run_suite(
            fixture,
            [harness.load_scenarios()[0]],
            runs=1,
            fixture_sha256=expected_digest,
        )


def test_performance_scenarios_use_scaled_batches_and_two_warmups() -> None:
    scenarios = _harness().load_scenarios()

    assert {
        scenario.id: (scenario.warmups, scenario.batch_size)
        for scenario in scenarios
        if scenario.lane == "performance"
    } == {
        "pip-deep-backtracking": (2, 8),
        "pip-deep-backtracking-unsatisfiable": (2, 8),
        "deep-backjump": (2, 8),
        "universal-aligned": (2, 192),
    }


def test_scenarios_use_product_defaults_except_declared_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness()
    scenarios = {scenario.id: scenario for scenario in harness.load_scenarios()}

    assert {
        scenario.id: scenario.resolution.value
        for scenario in scenarios.values()
        if scenario.resolution is not None
    } == {
        "strategy-lowest": "lowest",
        "strategy-lowest-direct": "lowest-direct",
    }
    assert {
        scenario.id: scenario.align_across_targets
        for scenario in scenarios.values()
        if scenario.align_across_targets is not None
    } == {"universal-independent": False}

    highest = harness.prepare_scenario(scenarios["strategy-highest"], tmp_path)
    independent = harness.prepare_scenario(scenarios["universal-independent"], tmp_path)
    assert highest.config.resolution.value == "highest"
    assert highest.align_across_targets is True
    assert independent.config.resolution.value == "highest"
    assert independent.align_across_targets is False

    calls: list[dict[str, object]] = []

    def resolve(*_args: object, **kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        harness,
        "FetchCoordinator",
        lambda *_args, **_kwargs: contextlib.nullcontext(object()),
    )
    monkeypatch.setattr(harness, "Urllib3AsyncTransport", object)
    monkeypatch.setattr(harness, "resolve_with_coordinator", resolve)

    # No listings to warm, so the stub coordinator is never asked for one.
    harness._resolve_once(highest, ())
    harness._resolve_once(independent, ())

    # A scenario that declares nothing must reach the resolver with the argument
    # absent, so the suite tracks the shipped default instead of restating it.
    assert "align_across_targets" not in calls[0]
    assert calls[1]["align_across_targets"] is False


def test_cli_preserves_manifest_order_and_can_only_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = _harness()
    fixture = tmp_path / "fixture"
    output = tmp_path / "report.json"
    selected: list[str] = []

    def run_suite(
        _fixture: Path,
        scenarios: list[object],
        _runs: int,
        _digest: str,
    ) -> dict[str, object]:
        selected.extend(scenario.id for scenario in scenarios)
        return {"schema": 1, "scenarios": []}

    monkeypatch.setattr(harness, "run_suite", run_suite)
    monkeypatch.setattr(harness, "_print_summary", lambda _report: None)

    # Requested out of manifest order, to show the report follows the manifest
    # rather than the command line.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deterministic_smoke.py",
            "--fixture-dir",
            str(fixture),
            "--scenario",
            "strategy-lowest",
            "--scenario",
            "basic-highest",
            "--json",
            str(output),
        ],
    )
    harness.main()

    assert selected == ["basic-highest", "strategy-lowest"]
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == 1

    materialized = tmp_path / "materialized-only"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deterministic_smoke.py",
            "--fixture-dir",
            str(materialized),
            "--materialize-only",
        ],
    )
    harness.main()

    printed = capsys.readouterr().out
    assert str(materialized) in printed
    assert harness.fixture_digest(materialized) == harness.load_fixture()[1]
    assert selected == ["basic-highest", "strategy-lowest"]


@pytest.mark.timeout(30)
def test_basic_highest_local_coordinator_completes_bounded(smoke_index: Path) -> None:
    harness = _harness()
    fixture = smoke_index
    basic = next(
        scenario
        for scenario in harness.load_scenarios()
        if scenario.id == "basic-highest"
    )

    # The timeout is the assertion: an offline coordinator that waits on a network
    # response would otherwise hang here rather than fail.
    result = harness.run_scenario(basic, fixture, runs=1)
    expected = {target.target: target.pins for target in basic.expected}

    assert result["pins_per_target"] == expected
    assert result["lock_projection_per_target"] == expected


def test_await_listings_dispatches_every_request_before_waiting() -> None:
    """Every listing is requested first, then every one is awaited.

    Waiting is what takes fetch timing out of the measurement, and dispatching
    the whole set before the first wait is what keeps the reads overlapping.
    Neither shows up in a resolve that happens to run on an idle machine, so
    both are pinned here instead.
    """
    harness = _harness()
    requested: list[str] = []

    class _Event:
        def __init__(self) -> None:
            self.requests_at_wait: int | None = None

        def wait(self) -> None:
            self.requests_at_wait = len(requested)

    events: list[_Event] = []

    class _Coordinator:
        def request_listing(self, package: str) -> _Event:
            requested.append(package)
            event = _Event()
            events.append(event)
            return event

    harness._await_listings(_Coordinator(), ("beta", "alpha", "gamma"))

    assert requested == ["beta", "alpha", "gamma"]
    assert [event.requests_at_wait for event in events] == [3, 3, 3]


def test_pins_hold_however_the_listings_arrive(smoke_index: Path) -> None:
    """Repeating one resolve reaches the same pins from any listing arrival state.

    A resolve races its own fetches. The priority scan sorts a package whose
    listing has not landed behind the ones that have, so package order, and with
    it the decision and conflict counts, follows whichever listings the fetcher
    thread delivered first. Landing every listing before the resolve is right
    for a timing harness and wrong here: the race is what a user resolving
    against a real index gets, and the pins are the part they depend on. So this
    keeps the race, layers further arrival states on top of it, and compares
    only the pins. The counters move between these runs and are not the property
    under test.

    Comparing against the scenario's declared pins rather than against the first
    run keeps the assertion from being an equality between a value and itself.
    """
    harness = _harness()
    distributions, _digest = harness.load_fixture()

    # deep-backjump carries a competitor at every level, so which listings have
    # landed decides how much of the graph the search walks.
    backjump = next(
        scenario
        for scenario in harness.load_scenarios()
        if scenario.id == "deep-backjump"
    )
    prepared = harness.prepare_scenario(backjump, smoke_index)
    packages = harness._reachable_listing_packages(
        harness._fixture_dependency_names(distributions),
        harness._scenario_root_names(prepared),
    )

    observed: dict[str, dict[str, dict[str, str]]] = {}
    for label, landed in _listing_arrival_states(packages).items():
        result, _elapsed = harness._resolve_once(prepared, landed)
        observed[label] = harness._pins(result)

    expected = harness._expected(backjump)
    assert observed == dict.fromkeys(observed, expected)


def test_a_scenario_prefetches_only_the_listings_it_can_reach(tmp_path: Path) -> None:
    """The prefetch set follows the scenario's own graph, not the whole manifest.

    The walk is blind to specifiers and markers, so a dependency only some
    environment activates still counts as reachable.
    """
    harness = _harness()
    distributions, _digest = harness.load_fixture()
    dependencies = harness._fixture_dependency_names(distributions)

    def prefetched(scenario_id: str) -> tuple[str, ...]:
        scenario = next(
            scenario
            for scenario in harness.load_scenarios()
            if scenario.id == scenario_id
        )
        prepared = harness.prepare_scenario(scenario, tmp_path)
        return harness._reachable_listing_packages(
            dependencies, harness._scenario_root_names(prepared)
        )

    # Every package the fixture publishes, so the sets below have a denominator.
    assert len(dependencies) == 34

    assert prefetched("pip-deep-backtracking") == (
        "nab-smoke-pip-a",
        "nab-smoke-pip-b",
        "nab-smoke-pip-c",
    )

    # nab-smoke-extra-speed sits behind an extra and nab-smoke-marker-leaf
    # behind a Python marker, and both are still reached.
    assert prefetched("extra-and-python-marker") == (
        "nab-smoke-extra-app",
        "nab-smoke-extra-base",
        "nab-smoke-extra-speed",
        "nab-smoke-marker-leaf",
    )


def test_the_walk_seeds_constraints_and_unions_across_releases(tmp_path: Path) -> None:
    """Two widenings the corpus does not exercise on its own.

    The one scenario with constraints constrains a package it already
    requires, and the one package whose dependencies vary across releases
    omits a name another edge reaches anyway, so dropping either rule leaves
    all 11 prefetch sets untouched. A scenario that needed one would
    under-fetch in silence, so pin both directly.
    """
    harness = _harness()

    releases = (
        harness.Distribution(
            name="Fixture_Pkg", version="1.0", dependencies=("early",)
        ),
        harness.Distribution(
            name="fixture-pkg", version="2.0", dependencies=("late>=2",)
        ),
    )
    assert harness._fixture_dependency_names(releases) == {
        "fixture-pkg": frozenset({"early", "late"})
    }

    ceiling = next(
        scenario
        for scenario in harness.load_scenarios()
        if scenario.id == "constraint-ceiling"
    )
    prepared = harness.prepare_scenario(ceiling, tmp_path)
    widened = replace(
        prepared,
        config=prepared.config.replace(constraints=("nab-smoke-pip-a<2.0.0",)),
    )

    assert set(harness._scenario_root_names(widened)) == {
        "nab-smoke-constrained",
        "nab-smoke-pip-a",
    }


def test_no_scenario_asks_for_a_listing_its_prefetch_left_out(
    smoke_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every listing a resolve requests was already landed before it started.

    Under-fetching does not fail loudly: the resolve blocks on the fetcher
    instead, and the priority scan then orders packages by whichever listing
    arrived first, which is what drifts the recorded search counters. So the
    property is coverage, taken from what the resolver asks for rather than
    from the walk that produced the set.
    """
    harness = _harness()
    distributions, _digest = harness.load_fixture()
    dependencies = harness._fixture_dependency_names(distributions)
    requested: list[str] = []

    class _RecordingCoordinator(harness.FetchCoordinator):
        """A real coordinator that records the listings it is asked for."""

        def request_listing(
            self, package: str, *, speculative: bool = False
        ) -> threading.Event:
            requested.append(package)
            return super().request_listing(package, speculative=speculative)

    monkeypatch.setattr(harness, "FetchCoordinator", _RecordingCoordinator)

    uncovered: dict[str, set[str]] = {}
    for scenario in harness.load_scenarios():
        prepared = harness.prepare_scenario(scenario, smoke_index)
        packages = harness._reachable_listing_packages(
            dependencies, harness._scenario_root_names(prepared)
        )
        requested.clear()
        harness._resolve_once(prepared, packages)
        missing = set(requested) - set(packages)
        if missing:
            uncovered[scenario.id] = missing

    assert uncovered == {}


def test_asyncio_wakeup_preflight_reports_restricted_execution() -> None:
    harness = _harness()
    calls = 0

    def accepted() -> None:
        nonlocal calls
        calls += 1

    def denied() -> None:
        raise PermissionError(1, "Operation not permitted")

    harness._validate_asyncio_wakeup_transport(probe=accepted)
    assert calls == 1
    with pytest.raises(
        harness.SmokeContractError,
        match=(
            "socketpair transport required by asyncio cross-thread wakeups:"
            " PermissionError \\(errno 1\\)"
        ),
    ):
        harness._validate_asyncio_wakeup_transport(probe=denied)


def test_every_smoke_scenario_satisfies_its_contract(smoke_index: Path) -> None:
    harness = _harness()
    _distributions, expected_digest = harness.load_fixture()
    fixture = smoke_index

    # The manifest's batch sizes size a resolve up until a wall-clock reading is
    # stable, which is the CLI's job. Two batches of two still exercise warmups,
    # batching, and cross-run agreement, and the declared values stay pinned by
    # test_performance_scenarios_use_scaled_batches_and_two_warmups.
    scenarios = [
        replace(scenario, warmups=1, batch_size=2)
        if scenario.lane == "performance"
        else scenario
        for scenario in harness.load_scenarios()
    ]

    report = harness.run_suite(
        fixture,
        scenarios,
        runs=2,
        fixture_sha256=expected_digest,
    )

    results = {item["id"]: item for item in report["scenarios"]}
    assert len(results) == 11
    assert {
        item["id"] for item in report["scenarios"] if item["lane"] == "performance"
    } == {
        "pip-deep-backtracking",
        "pip-deep-backtracking-unsatisfiable",
        "deep-backjump",
        "universal-aligned",
    }
    assert {item["build_policy"] for item in results.values()} == {"never"}

    for result in results.values():
        assert len(result["search_per_target"]) == len(result["targets"])

        # One fetch reading per measured resolve, and every reading identical:
        # a resolve that saw a different slice of the index is not a repeat.
        expected_fetch_samples = (
            result["measured_resolves"] if result["lane"] == "performance" else 1
        )
        assert all(
            len(target["distributions_seen"]) == expected_fetch_samples
            and len(target["metadata_fetched"]) == expected_fetch_samples
            and len(set(target["distributions_seen"])) == 1
            and len(set(target["metadata_fetched"])) == 1
            for target in result["search_per_target"]
        )

        if result["outcome"] == "satisfiable":
            assert result["pins_per_target"] == result["lock_projection_per_target"]
            assert result["lock_validation"] == (
                "exact PEP 751 domain, fixture sources, wheels, hashes, and edges"
            )
        else:
            assert result["lock_projection_per_target"] is None
            assert result["lock_validation"] is None
            assert all(not pins for pins in result["pins_per_target"].values())
            assert all(
                failure["type"] == "ResolutionError"
                and failure["has_incompatibility"] is True
                for failure in result["failures_per_target"].values()
            )

        if result["lane"] == "semantic":
            assert result["sample_count"] == 0
            assert result["wall_time_ns"] is None
        else:
            assert result["sample_count"] == 2
            assert len(result["wall_time_ns"]["aggregate_samples"]) == 2
            assert all(
                len(sample) == result["batch_size"]
                and all(value > 0 for value in sample)
                for sample in result["wall_time_ns"]["raw_inner_samples"]
            )

    # Exact counters, so a change in how the solver walks these graphs surfaces
    # here rather than inside a wall-clock number.
    assert results["pip-deep-backtracking"]["search"] == {
        "decisions": 27,
        "rounds": 73,
        "conflicts": 23,
        "backjumps": 23,
    }
    assert results["pip-deep-backtracking-unsatisfiable"]["search"] == {
        "decisions": 25,
        "rounds": 73,
        "conflicts": 25,
        "backjumps": 24,
    }
    # These hold only because _resolve_once lands every listing first; a
    # resolve racing its own fetches walks this graph differently.
    assert results["deep-backjump"]["search"] == {
        "decisions": 85,
        "rounds": 105,
        "conflicts": 19,
        "backjumps": 19,
    }
    assert results["universal-aligned"]["search"] == {
        "decisions": 20,
        "rounds": 20,
        "conflicts": 0,
        "backjumps": 0,
    }
    assert len(results["universal-aligned"]["targets"]) == 4

    assert report["schema"] == 1
    assert report["fixture_sha256"] == report["fixture_sha256_after"]
    assert report["fixture_access"] == report["fixture_access_after"]
    assert report["fixture_access"]["resolved_root"] == str(fixture.resolve())


def test_scenario_and_fixture_contracts_reject_malformed_inputs(
    tmp_path: Path,
) -> None:
    harness = _harness()

    # Each case edits the shipped manifest rather than hand-writing a stub, so a
    # rename in the real file breaks the test instead of quietly bypassing it.
    scenarios = (_BENCHMARKS / "smoke" / "scenarios.toml").read_text(encoding="utf-8")

    unknown = tmp_path / "unknown.toml"
    unknown.write_text(
        scenarios.replace('mode = "specific"', 'mode = "specific"\nunknown = true', 1),
        encoding="utf-8",
    )
    with pytest.raises(harness.SmokeContractError, match="unknown keys"):
        harness.load_scenarios(unknown)

    non_boolean = tmp_path / "non-boolean.toml"
    non_boolean.write_text(
        scenarios.replace(
            "align-across-targets = false",
            'align-across-targets = "false"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(harness.SmokeContractError, match="must be a boolean"):
        harness.load_scenarios(non_boolean)

    semantic_timing = tmp_path / "semantic-timing.toml"
    semantic_timing.write_text(
        scenarios.replace(
            'lane = "semantic"',
            'lane = "semantic"\nwarmups = 1',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(harness.SmokeContractError, match="cannot set timing"):
        harness.load_scenarios(semantic_timing)

    boolean_batch = tmp_path / "boolean-batch.toml"
    boolean_batch.write_text(
        scenarios.replace("batch-size = 8", "batch-size = true", 1),
        encoding="utf-8",
    )
    with pytest.raises(harness.SmokeContractError, match="positive integer"):
        harness.load_scenarios(boolean_batch)

    wrong_container = tmp_path / "wrong-container.toml"
    wrong_container.write_text(
        'scenario = "not an array"\n[suite]\nschema = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(harness.SmokeContractError, match="array of tables"):
        harness.load_scenarios(wrong_container)

    # A name is turned into a filename during materialization, so one carrying
    # path separators has to be rejected while it is still a manifest value.
    path_shaped_name = tmp_path / "path-shaped-name.toml"
    fixture = (_BENCHMARKS / "smoke" / "fixture.toml").read_text(encoding="utf-8")
    path_shaped_name.write_text(
        fixture.replace(
            'name = "nab-smoke-basic"',
            'name = "../nab-smoke-basic"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(harness.SmokeContractError, match="distribution name"):
        harness.load_fixture(path_shaped_name)


def test_nab_lock_validation_accepts_the_fixture_lock(
    basic_lock_case: _LockValidationCase,
) -> None:
    assert basic_lock_case.validate(basic_lock_case.lock) == basic_lock_case.expected


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(_missing_environments, "target cells", id="missing-environments"),
        pytest.param(
            _duplicate_package,
            "duplicate artifact records",
            id="duplicate-package",
        ),
        pytest.param(_external_index, "non-fixture index", id="external-index"),
        pytest.param(
            _external_artifact,
            "outside the fixture",
            id="external-artifact",
        ),
        pytest.param(_invalid_hash, "SHA-256", id="invalid-wheel-hash"),
        pytest.param(
            _missing_dependencies,
            "emitted lock edges differ",
            id="missing-dependencies",
        ),
        pytest.param(
            _dependency_version,
            "exactly one name field",
            id="versioned-dependency",
        ),
        pytest.param(
            _dependency_source,
            "exactly one name field",
            id="dependency-source",
        ),
        pytest.param(
            _unknown_dependency_field,
            "exactly one name field",
            id="unknown-dependency-field",
        ),
    ],
)
def test_nab_lock_validation_rejects_corrupted_locks(
    basic_lock_case: _LockValidationCase,
    mutate: Callable[[_LockValidationCase], Pylock],
    message: str,
) -> None:
    corrupted = mutate(basic_lock_case)
    with pytest.raises(basic_lock_case.harness.SmokeContractError, match=message):
        basic_lock_case.validate(corrupted)


def test_file_url_helper_round_trips_and_rejects_nonlocal_urls(
    tmp_path: Path,
) -> None:
    harness = _harness()
    artifact = (tmp_path / "fixture wheel.whl").resolve()

    assert harness._artifact_location(artifact.as_uri(), tmp_path) == artifact
    assert harness._artifact_location(str(artifact), tmp_path) == artifact
    with pytest.raises(harness.SmokeContractError, match="fixture file URL"):
        harness._artifact_location("https://example.invalid/wheel.whl", tmp_path)
    with pytest.raises(harness.SmokeContractError, match="fixture file URL"):
        harness._artifact_location("file://remotehost/wheel.whl", tmp_path)
