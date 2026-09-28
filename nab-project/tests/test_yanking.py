"""Yank admission through the project engine and actual provider."""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from itertools import permutations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nab_project.resolve import TargetResult
    from nab_provider.resolver_inputs import MarkerHolds
    from nab_provider.testing import FakeFetchPort

import sys

import pytest

from nab_project._testing.coordinator_fake import make_coordinator
from nab_project._testing.yanking import Extras, ReleaseRow, graph_port
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import IndexPin, build_pylock
from nab_project.resolve import build_lock_input, resolve_with_coordinator
from nab_provider import yanked_resolution
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.overrides import PackageOverride
from nab_provider.policy import DistPolicy, ExtrasMode
from nab_provider.provider import Provider, ResolutionStrategy
from nab_provider.records import SdistFile
from nab_provider.resolver_inputs import ProxyConstraints, build_resolver_inputs
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, ResolveTarget
from nab_provider.testing import pkg_override
from nab_provider.vcs_admission import VcsConfig
from nab_provider.yanked_resolution import MetadataProxy, YankProxyProvider
from nab_provider.yanking import mark_yanked
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver


def run_graph(
    releases: Sequence[ReleaseRow],
    roots: Sequence[str],
    *,
    targets: Sequence[ResolveTarget] | None = None,
    constraints: Sequence[str] = (),
    provided_extras: Extras | None = None,
    resolution: ResolutionStrategy | None = None,
    overrides: Sequence[PackageOverride] = (),
    marker_holds: MarkerHolds | None = None,
) -> tuple[list[TargetResult], FakeFetchPort]:
    """Resolve the fixture through the same entry point as the CLI."""
    port = graph_port(releases, provided_extras)
    result = resolve_with_coordinator(
        port,
        [ResolveTarget.for_host()] if targets is None else targets,
        [Requirement(req) for req in roots],
        inputs=ResolveInputs(
            constraints=tuple(constraints), package_overrides=tuple(overrides)
        ),
        resolution_strategy=resolution,
        marker_holds=marker_holds,
    )
    return result.target_results, port


@pytest.mark.parametrize("roots", [("app",), ("dep>=2", "app"), ("app", "dep>=2")])
def test_transitive_pin(roots: Sequence[str]) -> None:
    results, _ = run_graph(
        [("app", "1", False, ["dep==2"]), ("dep", "2", True, [])], roots
    )
    assert results[0].success, results[0].error
    assert {name: str(version) for name, version in results[0].pins.items()} == {
        "app": "1",
        "dep": "2",
    }


def test_unpinned_yank_is_not_prepared() -> None:
    results, port = run_graph([("dep", "2", True, [])], ["dep"])
    assert not results[0].success
    assert not port.calls_to("request_metadata")
    assert not port.calls_to("request_metadata_batch")


def test_rejected_parent_does_not_leave_permission() -> None:
    results, _ = run_graph(
        [
            ("app", "2", False, ["dep==2", "missing"]),
            ("app", "1", False, ["dep>=2"]),
            ("dep", "2", True, []),
        ],
        ["app"],
    )
    assert not results[0].success


@pytest.mark.parametrize(("root", "success"), [("app", False), ("app==2", True)])
def test_yanked_cycle_needs_input_permission(root: str, success: bool) -> None:
    results, _ = run_graph(
        [("app", "2", True, ["dep==2"]), ("dep", "2", True, ["app==2"])], [root]
    )
    assert results[0].success is success


def test_live_dependencies_fail_before_yanked_fallback() -> None:
    results, _ = run_graph(
        [("dep", "2+live", False, ["missing"]), ("dep", "2", True, [])], ["dep==2"]
    )
    assert results[0].success, results[0].error
    assert str(results[0].pins["dep"]) == "2"


def test_live_file_at_same_version_fails_before_yanked_alternative() -> None:
    results, _ = run_graph(
        [("dep", "2", False, ["missing"]), ("dep", "2", True, [])], ["dep==2"]
    )
    assert results[0].success, results[0].error
    assert str(results[0].pins["dep"]) == "2"


@pytest.mark.parametrize(
    ("root", "live", "withdrawn", "expected"),
    [
        ("dep===2.0", "2.0", "2", "2.0"),
        ("dep===2.0", "2", "2.0", "2.0"),
        ("dep==2", "2.0", "2", "2.0"),
    ],
)
def test_yanked_alias_keeps_arbitrary_equality(
    root: str, live: str, withdrawn: str, expected: str
) -> None:
    results, _ = run_graph(
        [("dep", live, False, []), ("dep", withdrawn, True, [])], [root]
    )
    assert results[0].success, results[0].error
    assert str(results[0].pins["dep"]) == expected


@pytest.mark.parametrize(
    ("spec", "succeeds"),
    [("dep==2.*", False), ("dep>=2,<=2", False), ("dep==2,>=1", True)],
)
def test_only_original_exact_syntax_admits(spec: str, succeeds: bool) -> None:
    results, _ = run_graph([("dep", "2", True, [])], [spec])
    assert results[0].success is succeeds


def test_constraint_pin_admits_a_required_package() -> None:
    results, _ = run_graph([("dep", "2", True, [])], ["dep"], constraints=["dep==2"])
    assert results[0].success, results[0].error


def test_unused_constraint_does_not_start_a_yanked_cycle() -> None:
    results, port = run_graph(
        [("app", "2", True, ["dep==2"]), ("dep", "2", True, ["app==2"])],
        ["app"],
        constraints=["dep==2"],
    )
    assert not results[0].success
    assert not port.calls_to("request_metadata")


def test_inactive_marker_does_not_admit_a_yank() -> None:
    results, _ = run_graph(
        [
            ("app", "1", False, ['dep==2; python_version < "2"', "dep>=2"]),
            ("dep", "2", True, []),
        ],
        ["app"],
    )
    assert not results[0].success


@pytest.mark.parametrize(("root", "succeeds"), [("app", False), ("app[x]", True)])
def test_extra_controls_the_enabling_pin(root: str, succeeds: bool) -> None:
    results, _ = run_graph(
        [
            ("app", "1", False, ['dep==2; extra == "x"', "dep>=2"]),
            ("dep", "2", True, []),
        ],
        [root],
        provided_extras={("app", "1", False): ("x",)},
    )
    assert results[0].success is succeeds


def test_late_extra_adds_a_pin() -> None:
    results, _ = run_graph(
        [
            ("app", "1", False, ['dep==2; extra == "x"', "dep>=2"]),
            ("other", "1", False, ["app[x]"]),
            ("dep", "2", True, []),
        ],
        ["app", "other"],
        provided_extras={("app", "1", False): ("x",)},
    )
    assert results[0].success, results[0].error


def test_withdrawn_artifact_can_supply_the_required_extra() -> None:
    results, _ = run_graph(
        [("app", "2", False, []), ("app", "2", True, [])],
        ["app[x]==2"],
        provided_extras={("app", "2", True): ("x",)},
    )
    assert results[0].success, results[0].error


def test_yank_does_not_suppress_a_live_prerelease() -> None:
    results, _ = run_graph(
        [("dep", "2", True, []), ("dep", "3rc1", False, [])], ["dep"]
    )
    assert results[0].success, results[0].error
    assert str(results[0].pins["dep"]) == "3rc1"


def test_final_rejection_still_allows_live_prerelease_fallback() -> None:
    results, _ = run_graph(
        [
            ("app", "1", False, ["dep"]),
            ("dep", "2", False, ["missing"]),
            ("dep", "3rc1", False, []),
            ("dep", "4", True, []),
        ],
        ["app"],
    )
    assert results[0].success, results[0].error
    assert str(results[0].pins["dep"]) == "3rc1"


def test_admission_does_not_cross_targets() -> None:
    targets = [
        ResolveTarget.for_host().with_marker_overrides({"sys_platform": platform})
        for platform in ("linux", "win32")
    ]
    results, _ = run_graph(
        [
            ("app", "1", False, ['dep==2; sys_platform == "linux"', "dep>=2"]),
            ("dep", "2", True, []),
        ],
        ["app"],
        targets=targets,
    )
    assert [result.success for result in results] == [True, False]


def test_root_marker_callback_is_not_evaluated_again() -> None:
    results, _ = run_graph(
        [("dep", "2", True, [])],
        ['dep==2; python_version < "2"'],
        marker_holds=lambda _marker, _environment: True,
    )
    assert results[0].success, results[0].error


def test_lowest_strategy_is_preserved() -> None:
    results, _ = run_graph(
        [
            ("app", "1", False, ["dep==2"]),
            ("app", "2", False, ["dep==2"]),
            ("dep", "2", True, []),
        ],
        ["app"],
        resolution=ResolutionStrategy.LOWEST,
    )
    assert results[0].success, results[0].error
    assert str(results[0].pins["app"]) == "1"


def test_withdrawn_sibling_is_tried_before_downgrading_its_parent() -> None:
    results, _ = run_graph(
        [
            ("app", "2", False, ["dep==2"]),
            ("app", "1", False, ["dep==1"]),
            ("dep", "2", False, ["missing"]),
            ("dep", "2", True, []),
            ("dep", "1", False, []),
        ],
        ["app"],
    )
    assert results[0].success, results[0].error
    assert {name: str(version) for name, version in results[0].pins.items()} == {
        "app": "2",
        "dep": "2",
    }


@pytest.mark.parametrize(
    ("original", "replacement", "succeeds"),
    [
        ("dep>=2", "dep==2", True),
        ("dep==2", "dep>=2", False),
    ],
)
def test_effective_metadata_supplies_the_pin(
    original: str, replacement: str, succeeds: bool
) -> None:
    override = pkg_override("app", dependencies=(Requirement(replacement),))
    results, _ = run_graph(
        [("app", "1", False, [original]), ("dep", "2", True, [])],
        ["app"],
        overrides=[override],
    )
    assert results[0].success is succeeds


def test_inactive_constraint_does_not_admit() -> None:
    results, _ = run_graph(
        [("dep", "2", True, [])], ["dep"], constraints=['dep==2; python_version < "2"']
    )
    assert not results[0].success


def test_search_limit_does_not_authorize_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(yanked_resolution, "_MAX_STEPS", 1)
    results, port = run_graph(
        [("dep", "2+z", True, []), ("dep", "2+a", False, [])], ["dep==2"]
    )
    assert not results[0].success
    assert "incomplete" in str(results[0].error)
    assert all(
        "True.whl" not in str(call) for call in port.calls_to("request_metadata")
    )
    assert all(
        "True.whl" not in str(call) for call in port.calls_to("request_metadata_batch")
    )


def test_unknown_offline_listing_does_not_certify_fallback() -> None:
    port = graph_port([("dep", "2+live", False, ["missing"]), ("dep", "2", True, [])])
    port.index.store_listing("missing", [], offline_miss=True)
    result = resolve_with_coordinator(
        port, [ResolveTarget.for_host()], [Requirement("dep==2")]
    )
    assert not result.success
    assert "missing offline" in str(result.target_results[0].error)
    assert all(
        "True.whl" not in str(call) for call in port.calls_to("request_metadata")
    )


def test_deep_admission_uses_no_recursive_solver_stack() -> None:
    count = 220
    releases = [
        (f"p{i}", "1", i == count - 1, [f"p{i + 1}==1"] if i + 1 < count else [])
        for i in range(count)
    ]
    port = graph_port(releases)
    target = ResolveTarget.for_host()
    roots = [Requirement("p0")]
    prepared = build_resolver_inputs(
        roots,
        VcsConfig(),
        environment=target.marker_env,
        marker_holds=dependency_marker_holds,
    )
    factory = partial(
        Provider,
        port,
        target=target,
        root_requirements=prepared.ranges,
        defer_yanked=True,
    )
    search = YankProxyProvider(
        factory(), factory, prepared.roots, roots, ProxyConstraints({}), preferences={}
    )
    original_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(180)
        pins, _ = search.resolve()
    finally:
        sys.setrecursionlimit(original_limit)
    assert len(pins) == count


def test_live_artifact_wins_without_reading_withdrawn_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    results, port = run_graph(
        [("dep", "2", False, []), ("dep", "2", True, ["missing"])],
        ["dep==2"],
    )
    assert results[0].success, results[0].error
    lock = results[0].lock
    assert lock is not None
    pin = lock.pins["dep"]
    assert isinstance(pin, IndexPin)
    assert [wheel.url for wheel in pin.wheels] == [
        "https://example.test/dep-2-False.whl"
    ]
    assert all(
        "True.whl" not in str(call) for call in port.calls_to("request_metadata")
    )
    assert "Selected yanked" not in caplog.text


def test_success_reports_the_selected_withdrawal_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    results, _ = run_graph([("dep", "2", True, [])], ["dep==2"])
    assert results[0].success, results[0].error
    assert "Selected yanked files for dep==2: withdrawn" in caplog.text


def test_failed_resolution_does_not_report_a_selected_yank(
    caplog: pytest.LogCaptureFixture,
) -> None:
    results, _ = run_graph([("dep", "2", True, ["missing"])], ["dep==2"])
    assert not results[0].success
    assert "Selected yanked" not in caplog.text


def withdrawn_sdist() -> SdistFile:
    """Return the withdrawn archive used to test preparation and install policy."""
    file = mark_yanked(
        SdistFile(
            filename="dep-2.tar.gz",
            url="https://example.test/dep-2.tar.gz",
            version="2",
            requires_python=None,
            upload_time=None,
            hashes=(("sha256", "a" * 64),),
        ),
        reason="broken source release",
    )
    assert isinstance(file, SdistFile)
    return file


def test_unpinned_sdist_is_never_downloaded_or_built() -> None:
    port = make_coordinator(listings={"dep": [withdrawn_sdist()]})
    result = resolve_with_coordinator(
        port, [ResolveTarget.for_host()], [Requirement("dep")]
    )
    assert not result.success
    for request in ("request_sdist", "request_sdist_archive", "request_built_metadata"):
        assert not port.calls_to(request)


@pytest.mark.parametrize(("root", "succeeds"), [("dep", False), ("dep==2", True)])
def test_sdist_install_requires_permission_for_the_install_artifact(
    root: str,
    succeeds: bool,
) -> None:
    wheel_port = graph_port([("dep", "2", False, [])])
    live = wheel_port.index.get_listing("dep")
    assert live is not None
    port = make_coordinator(
        listings={"dep": [*live, withdrawn_sdist()]},
        metadata_text="Metadata-Version: 2.2\nName: dep\nVersion: 2\n\n",
        sdist_pkg_info="Metadata-Version: 2.2\nName: dep\nVersion: 2\n\n",
    )
    result = resolve_with_coordinator(
        port,
        [ResolveTarget.for_host()],
        [Requirement(root)],
        inputs=ResolveInputs(dist_policy=DistPolicy.SDIST_INSTALL),
    )
    assert result.success is succeeds
    if succeeds:
        lock = result.target_results[0].lock
        assert lock is not None
        pin = lock.pins["dep"]
        assert isinstance(pin, IndexPin)
        assert pin.sdist is not None
        assert pin.sdist.url == "https://example.test/dep-2.tar.gz"
        assert not pin.wheels
    else:
        assert not port.calls_to("request_sdist")
        assert not port.calls_to("request_sdist_archive")


def test_rejected_live_metadata_still_splits_the_universal_lock() -> None:
    port = graph_port(
        [
            ("dep", "2+live", False, ['missing; python_full_version < "3.12.4"']),
            ("dep", "2", True, []),
        ]
    )
    inputs = ResolveInputs(requires_python=">=3.12,<3.13")
    targets = Matrix(
        python=">=3.12,<3.13",
        platforms=(PlatformSpec("linux_x86_64"),),
    ).expand()
    result = resolve_with_coordinator(
        port,
        targets,
        [Requirement("dep==2")],
        inputs=inputs,
    )
    assert result.success
    assert {
        target.target.python_full_version: str(target.pins["dep"])
        for target in result.target_results
    } == {"3.12.0": "2", "3.12.4": "2+live"}

    lock = build_pylock(build_lock_input(result, inputs=inputs))
    lock.validate()
    for micro, expected in (("3.12.1", "2"), ("3.12.5", "2+live")):
        environment = {**targets[0].marker_env, "python_full_version": micro}
        active = [
            package
            for package in lock.packages
            if package.marker is None or package.marker.evaluate(environment)
        ]
        assert [(str(package.name), str(package.version)) for package in active] == [
            ("dep", expected)
        ]


def graph_search(
    releases: Sequence[ReleaseRow],
    roots: Sequence[str],
    *,
    extras_mode: ExtrasMode = ExtrasMode.BACKTRACK,
) -> YankProxyProvider:
    """Create a real provider search for admission-proof and metadata-boundary tests."""
    port = graph_port(releases)
    target = ResolveTarget.for_host()
    requirements = [Requirement(root) for root in roots]
    prepared = build_resolver_inputs(
        requirements,
        VcsConfig(),
        environment=target.marker_env,
        marker_holds=dependency_marker_holds,
    )
    factory = partial(
        Provider,
        port,
        target=target,
        root_requirements=prepared.ranges,
        defer_yanked=True,
        extras_mode=extras_mode,
    )
    return YankProxyProvider(
        factory(),
        factory,
        prepared.roots,
        requirements,
        ProxyConstraints({}),
        preferences={},
    )


def test_missing_offline_metadata_cannot_certify_live_failure() -> None:
    search = graph_search(
        [
            ("dep", "3", False, []),
            ("dep", "2+live", False, []),
            ("dep", "2", True, []),
        ],
        ["dep==2"],
    )
    index = search.catalogue.provider.coordinator.index
    url = "https://example.test/dep-2+live-False.whl.metadata"
    index.record_offline_metadata_miss("dep", "2+live", url)
    index.store_metadata("dep", "2+live", None, metadata_url=url)
    with pytest.raises(ResolutionError, match="metadata.*missing offline"):
        search.resolve()
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)


def test_missing_extra_rejects_a_candidate_without_enabling_its_pin() -> None:
    search = graph_search(
        [
            ("app", "1", False, ["dep==2"]),
            ("dep", "2", True, []),
        ],
        ["app[x]"],
    )
    candidate = search.catalogue.choices("app[x]")[0]
    assert search.catalogue.prepare(candidate) is None
    assert "does not provide extra" in search.catalogue.rejections[0]
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)
    with pytest.raises(ResolutionError, match="does not provide extra"):
        search.resolve()


def test_proxy_conflicts_stop_independent_choice_permutations() -> None:
    releases: list[ReleaseRow] = [
        (f"noise{i}", str(version), False, []) for i in range(5) for version in (1, 2)
    ]
    releases.extend(
        [
            ("app", "1", False, ["left", "right"]),
            ("left", "1", False, ["leaf==1"]),
            ("right", "1", False, ["leaf==2"]),
            ("leaf", "1", False, []),
            ("leaf", "2", False, []),
            ("withdrawn", "1", True, []),
        ]
    )
    search = graph_search(
        releases, [*(f"noise{i}" for i in range(5)), "app", "withdrawn==1"]
    )
    with pytest.raises(ResolutionError):
        search.resolve()
    assert search.stats.rounds < 500


def test_offline_metadata_miss_does_not_hide_a_cached_live_solution() -> None:
    search = graph_search(
        [
            ("dep", "2+z", False, []),
            ("dep", "2+a", False, []),
            ("dep", "2", True, []),
        ],
        ["dep==2"],
    )
    index = search.catalogue.provider.coordinator.index
    url = "https://example.test/dep-2+z-False.whl.metadata"
    index.record_offline_metadata_miss("dep", "2+z", url)
    index.store_metadata("dep", "2+z", None, metadata_url=url)

    pins, _ = search.resolve()
    assert pins == {"dep": Version("2+a")}
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)


def test_offline_listing_miss_does_not_hide_an_alternative_live_parent() -> None:
    search = graph_search(
        [
            ("app", "2", False, ["missing"]),
            ("app", "1", False, ["dep==2"]),
            ("dep", "2", True, []),
        ],
        ["app"],
    )
    search.catalogue.provider.coordinator.index.store_listing(
        "missing", [], offline_miss=True
    )
    pins, _ = search.resolve()
    assert pins == {"app": Version("1"), "dep": Version("2")}
    assert "missing" in search.catalogue.missing_listings


def test_incomplete_proofs_and_cached_contexts_remain_incomplete() -> None:
    search = graph_search([("app", "1", True, ["missing"])], ["app==1"])
    search.catalogue.provider.coordinator.index.store_listing(
        "missing", [], offline_miss=True
    )
    search.catalogue.prepare(search.catalogue.choices("app")[0])
    with pytest.raises(ResolutionError, match="missing offline"):
        search.resolve()
    assert "missing" in search.catalogue.missing_listings
    with pytest.raises(ResolutionError, match="missing offline"):
        search.resolve()


def test_unavailable_withdrawn_file_does_not_hide_another_admitted_file() -> None:
    search = graph_search(
        [
            ("dep", "2+z", True, []),
            ("dep", "2+a", True, []),
        ],
        ["dep==2"],
    )
    index = search.catalogue.provider.coordinator.index
    url = "https://example.test/dep-2+z-True.whl.metadata"
    index.record_offline_metadata_miss("dep", "2+z", url)
    index.store_metadata("dep", "2+z", None, metadata_url=url)
    unavailable = search.catalogue.choices("dep")[0]
    pins, _ = search.resolve()
    assert pins == {"dep": Version("2+a")}
    reads = search.catalogue.provider.stats.get_dependencies_calls
    assert search.catalogue.prepare(unavailable) is None
    assert search.catalogue.provider.stats.get_dependencies_calls == reads


def mark_offline_metadata(
    search: YankProxyProvider, package: str, version: str
) -> None:
    """Remove one fixture response and record why its live metadata is unavailable."""
    index = search.catalogue.provider.coordinator.index
    url = f"https://example.test/{package}-{version}-False.whl.metadata"
    index.record_offline_metadata_miss(package, version, url)
    index.store_metadata(package, version, None, metadata_url=url)


def test_changing_parents_does_not_disprove_missing_live_metadata() -> None:
    search = graph_search(
        [
            ("app", "2", False, ["dep==2", "zhelper"]),
            ("app", "1", False, ["dep==2"]),
            ("dep", "2+live", False, []),
            ("dep", "2", True, []),
            ("zhelper", "1", False, ["missing"]),
        ],
        ["app"],
    )
    mark_offline_metadata(search, "dep", "2+live")
    with pytest.raises(ResolutionError, match="missing offline"):
        search.resolve()
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)


def test_offline_independent_root_cannot_justify_another_yank() -> None:
    search = graph_search(
        [
            ("a", "1", False, []),
            ("a", "1", True, []),
            ("b", "1", False, []),
            ("b", "1", True, []),
        ],
        ["a==1", "b==1"],
    )
    mark_offline_metadata(search, "b", "1")
    with pytest.raises(ResolutionError, match="missing offline"):
        search.resolve()
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)


def test_new_pin_can_exclude_an_unavailable_live_version() -> None:
    search = graph_search(
        [
            ("app", "2", False, ["dep>=2", "zhelper"]),
            ("app", "1", False, ["dep==2"]),
            ("dep", "3", False, []),
            ("dep", "2", True, []),
            ("zhelper", "1", False, ["missing"]),
        ],
        ["app"],
    )
    mark_offline_metadata(search, "dep", "3")
    pins, _ = search.resolve()
    assert pins == {"app": Version("1"), "dep": Version("2")}


def test_independent_choice_cannot_disprove_an_offline_live_alternative() -> None:
    search = graph_search(
        [
            ("dep", "2+live", False, ["helper", "feature==2"]),
            ("dep", "2", True, []),
            ("helper", "2", False, []),
            ("helper", "1", False, ["feature==1"]),
            ("feature", "1", False, []),
            ("feature", "2", False, []),
        ],
        ["dep==2", "helper"],
    )
    mark_offline_metadata(search, "helper", "2")
    with pytest.raises(ResolutionError, match="incomplete"):
        search.resolve()
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)


def test_declaring_parent_can_exclude_an_offline_alternative() -> None:
    search = graph_search(
        [
            ("helper", "2", False, []),
            ("helper", "1", False, ["dep==2", "feature==1"]),
            ("dep", "2+live", False, ["feature==2"]),
            ("dep", "2", True, []),
            ("feature", "1", False, []),
            ("feature", "2", False, []),
        ],
        ["helper"],
    )
    mark_offline_metadata(search, "helper", "2")
    pins, _ = search.resolve()
    assert pins == {
        "helper": Version("1"),
        "dep": Version("2"),
        "feature": Version("1"),
    }


def test_new_parent_inherits_a_previously_missing_dependency() -> None:
    search = graph_search(
        [
            ("app", "2", False, ["leaf==2"]),
            ("app", "1", False, ["dep==2"]),
            ("dep", "2+live", False, ["leaf==2"]),
            ("dep", "2", True, []),
            ("leaf", "2", False, []),
        ],
        ["app"],
    )
    mark_offline_metadata(search, "leaf", "2")
    with pytest.raises(ResolutionError, match="missing offline"):
        search.resolve()
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)


def test_new_parent_can_require_a_different_known_dependency() -> None:
    search = graph_search(
        [
            ("app", "2", False, ["leaf==2"]),
            ("app", "1", False, ["dep==2"]),
            ("dep", "2+live", False, ["leaf==1"]),
            ("dep", "2", True, []),
            ("leaf", "2", False, []),
            ("leaf", "1", False, ["missing"]),
        ],
        ["app"],
    )
    mark_offline_metadata(search, "leaf", "2")
    pins, _ = search.resolve()
    assert pins == {"app": Version("1"), "dep": Version("2")}


def test_candidate_queries_do_not_prepare_or_conflate_artifacts() -> None:
    search = graph_search([("dep", "2", False, []), ("dep", "2", True, [])], ["dep==2"])
    allowed = search.encode_range("dep", VersionRange.full())
    tokens = tuple(search.identify(c) for c in search.catalogue.choices("dep"))
    assert len(tokens) == 2
    assert all(token in allowed for token in tokens)
    assert search.has_satisfying_version("dep", allowed)
    assert not search.has_satisfying_version("dep", Range.empty())
    assert not search.catalogue.facts
    assert not search.catalogue.provider.coordinator.calls_to("request_metadata")


def test_permission_proofs_retain_the_complete_ancestor_path() -> None:
    search = graph_search(
        [
            ("root", "1", False, ["helper"]),
            ("helper", "1", False, ["dep==2"]),
            ("dep", "2", True, []),
        ],
        ["root"],
    )
    decisions = {}
    for name in ("root", "helper", "dep"):
        candidate = search.catalogue.choices(name)[0]
        if name != "dep":
            search.catalogue.prepare(candidate)
        decisions[name] = search.identify(candidate)
    search.receive_decision_scan_hint(
        {name: Range.singleton(token) for name, token in decisions.items()}, decisions
    )
    proxy, proofs = next(iter(search.proofs.items()))
    support, version = next(iter(proofs.items()))
    assert {search.items[token].package for token in support} == {"root", "helper"}
    dependencies = search.get_dependencies(proxy, version)
    assert set(dependencies) == {"root", "helper"}

    search.receive_decision_scan_hint({}, {})
    assert search.get_dependencies(proxy, version) == dependencies
    assert not search.is_ready(MetadataProxy(proxy.candidate))


def test_cached_cyclic_metadata_cannot_supply_initial_permission() -> None:
    search = graph_search(
        [("app", "2", True, ["dep==2"]), ("dep", "2", True, ["app==2"])], ["app"]
    )
    for package in ("app", "dep"):
        search.catalogue.prepare(search.catalogue.choices(package)[0])
    with pytest.raises(ResolutionError, match="active exact pin"):
        search.resolve()
    assert not search.proofs


@pytest.mark.parametrize("roots", [("app", "dep>=2"), ("dep>=2", "app")])
def test_closed_frontier_does_not_permanently_exclude_a_yank(
    roots: Sequence[str],
) -> None:
    results, _ = run_graph(
        [
            ("app", "2", False, ["dep>=2"]),
            ("app", "1", False, ["dep==2"]),
            ("dep", "2", True, []),
        ],
        roots,
    )
    assert results[0].success, results[0].error
    assert results[0].pins == {"app": Version("1"), "dep": Version("2")}


def test_rejected_witness_does_not_remove_an_independent_pin() -> None:
    results, _ = run_graph(
        [
            ("app", "2", False, ["dep==2", "missing"]),
            ("app", "1", False, ["dep>=2"]),
            ("other", "1", False, ["helper"]),
            ("helper", "1", False, ["dep==2"]),
            ("dep", "2", True, []),
        ],
        ["app", "other"],
    )
    assert results[0].success, results[0].error
    assert results[0].pins == {
        "app": Version("1"),
        "other": Version("1"),
        "helper": Version("1"),
        "dep": Version("2"),
    }


@pytest.mark.parametrize("budget", [2, 100_000])
def test_offline_dependency_cycles_are_bounded(
    budget: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(yanked_resolution, "_MAX_STEPS", budget)
    search = graph_search(
        [
            ("a", "1", False, ["b", "c"]),
            ("a", "1", True, []),
            ("b", "1", False, ["c", "leaf"]),
            ("c", "1", False, ["b", "leaf"]),
            ("leaf", "1", False, []),
        ],
        ["a==1"],
    )
    mark_offline_metadata(search, "leaf", "1")
    with pytest.raises(ResolutionError, match="incomplete"):
        search.resolve()
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)
    assert search.stats.rounds <= budget


@pytest.mark.parametrize("roots", tuple(permutations(("b", "a==1", "c==1"))))
def test_live_file_can_downgrade_an_independent_root(roots: Sequence[str]) -> None:
    search = graph_search(
        [
            ("a", "1", False, ["b==1"]),
            ("a", "1", True, []),
            ("b", "2", False, []),
            ("b", "1", False, []),
            ("c", "1", True, []),
        ],
        roots,
    )
    pins, provider = search.resolve()
    assert pins == {"a": Version("1"), "b": Version("1"), "c": Version("1")}
    assert not any(file.yanked for file in provider.dist_files_for("a", Version("1")))
    assert not any(
        candidate.withdrawn and candidate.base == "a"
        for candidate in search.catalogue.facts
    )


@pytest.mark.parametrize("roots", tuple(permutations(("b", "app", "c==1"))))
def test_late_parent_preserves_live_preference(roots: Sequence[str]) -> None:
    search = graph_search(
        [
            ("app", "1", False, ["a==1"]),
            ("a", "1", False, ["b==1"]),
            ("a", "1", True, []),
            ("b", "2", False, []),
            ("b", "1", False, []),
            ("c", "1", True, []),
        ],
        roots,
    )
    pins, provider = search.resolve()
    assert pins["b"] == Version("1")
    assert not any(file.yanked for file in provider.dist_files_for("a", Version("1")))
    assert not any(
        candidate.withdrawn and candidate.base == "a"
        for candidate in search.catalogue.facts
    )


def test_live_preference_keeps_a_hard_independent_pin() -> None:
    search = graph_search(
        [
            ("a", "1", False, ["b==1"]),
            ("a", "1", True, []),
            ("b", "1", False, []),
            ("b", "2", False, []),
        ],
        ["b==2", "a==1"],
    )
    pins, provider = search.resolve()
    assert pins["b"] == Version("2")
    assert all(file.yanked for file in provider.dist_files_for("a", Version("1")))


def test_live_preference_keeps_the_declaring_parent() -> None:
    search = graph_search(
        [
            ("app", "2", False, ["a==2"]),
            ("app", "1", False, ["a==1"]),
            ("a", "2", True, []),
            ("a", "1", False, []),
        ],
        ["app"],
    )
    pins, _ = search.resolve()
    assert pins == {"app": Version("2"), "a": Version("2")}


@pytest.mark.parametrize("roots", [("a==1", "b==1"), ("b==1", "a==1")])
def test_live_preference_does_not_exchange_which_root_is_yanked(
    roots: Sequence[str],
) -> None:
    search = graph_search(
        [
            ("a", "1", False, ["c==1"]),
            ("a", "1", True, []),
            ("b", "1", False, ["c==2"]),
            ("b", "1", True, []),
            ("c", "1", False, []),
            ("c", "2", False, []),
        ],
        roots,
    )
    _, provider = search.resolve()
    assert (
        sum(
            any(file.yanked for file in provider.dist_files_for(name, Version("1")))
            for name in ("a", "b")
        )
        == 1
    )


@pytest.mark.parametrize("roots", [("a==1", "b==1"), ("b==1", "a==1")])
def test_impossible_independent_live_context_does_not_prepare_another_yank(
    roots: Sequence[str],
) -> None:
    search = graph_search(
        [
            ("a", "1", False, []),
            ("a", "1", True, []),
            ("b", "1", False, ["x", "y"]),
            ("b", "1", True, []),
            ("x", "1", False, ["z==1"]),
            ("y", "1", False, ["z==2"]),
            ("z", "1", False, []),
            ("z", "2", False, []),
        ],
        roots,
    )
    _, provider = search.resolve()
    assert not any(file.yanked for file in provider.dist_files_for("a", Version("1")))
    assert not any(
        candidate.withdrawn and candidate.base == "a"
        for candidate in search.catalogue.facts
    )


def test_scoped_live_proof_with_unknown_metadata_stays_incomplete() -> None:
    search = graph_search(
        [
            ("a", "1", False, ["b==1"]),
            ("a", "1", True, []),
            ("b", "2", False, []),
            ("b", "1", False, []),
        ],
        ["b", "a==1"],
    )
    index = search.catalogue.provider.coordinator.index
    url = "https://example.test/b-1-False.whl.metadata"
    index.record_offline_metadata_miss("b", "1", url)
    index.store_metadata("b", "1", None, metadata_url=url)
    with pytest.raises(ResolutionError, match="incomplete"):
        search.resolve()
    assert not any(candidate.withdrawn for candidate in search.catalogue.facts)


def test_cached_facts_wait_for_their_solver_dependency_clauses() -> None:
    search = graph_search(
        [
            ("a", "1", False, []),
            ("c", "1", False, ["a==2"]),
            ("c", "1", True, []),
        ],
        ["c==1", "a"],
    )
    pins, provider = search.resolve()
    assert pins == {"c": Version("1"), "a": Version("1")}
    assert all(file.yanked for file in provider.dist_files_for("c", Version("1")))


def test_many_independent_downgrades_do_not_require_forced_backtracking() -> None:
    releases: list[ReleaseRow] = [("c", "1", True, [])]
    roots = []
    for index in range(65):
        first, second = f"a{index}", f"b{index}"
        releases.extend(
            [
                (first, "1", False, [second + "==1"]),
                (first, "1", True, []),
                (second, "2", False, []),
                (second, "1", False, []),
            ]
        )
        roots.append(second)
    roots.extend(f"a{index}==1" for index in range(65))
    roots.append("c==1")
    search = graph_search(releases, roots)
    pins, _ = search.resolve()
    assert all(pins[f"b{index}"] == Version("1") for index in range(65))
    assert not any(
        candidate.withdrawn and candidate.base != "c"
        for candidate in search.catalogue.facts
    )


@pytest.mark.parametrize(
    ("roots", "constraints", "source"),
    [
        (["dep==2"], [], "input pin dep==2"),
        (["dep"], ["dep==2"], "input pin dep==2"),
        (["app"], [], "app==1 requires dep==2"),
    ],
)
def test_selected_yank_warning_names_the_admitting_declaration(
    roots: list[str],
    constraints: list[str],
    source: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    results, _ = run_graph(
        [("app", "1", False, ["dep==2"]), ("dep", "2", True, [])],
        roots,
        constraints=constraints,
    )
    assert results[0].success
    assert (
        f"Selected yanked files for dep==2: withdrawn (admitted by {source})"
        in caplog.text
    )


def test_extra_yank_warning_names_the_active_extra_declaration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    results, _ = run_graph(
        [("app", "1", False, ['dep==2; extra == "x"']), ("dep", "2", True, [])],
        ["app[x]"],
        provided_extras={("app", "1", False): ("x",)},
    )
    assert results[0].success
    assert 'admitted by app[x]==1 requires dep==2; extra == "x"' in caplog.text


def test_yanked_cycle_warning_uses_grounded_declarations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    results, _ = run_graph(
        [("a", "1", True, ["b==1"]), ("b", "1", True, ["a==1"])],
        ["a==1"],
    )
    assert results[0].success
    assert (
        "Selected yanked files for a==1: withdrawn (admitted by input pin a==1)"
        in caplog.text
    )
    assert (
        "Selected yanked files for b==1: withdrawn (admitted by a==1 requires b==1)"
        in caplog.text
    )


@pytest.mark.parametrize("withdrawn_dependencies", [[], ["missing"]])
def test_target_stats_include_every_scoped_solver_call(
    withdrawn_dependencies: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[int, int, int, int]] = []
    original = Resolver.resolve

    def recorded_resolve(
        resolver: Resolver[Any, Any],
        *args: Any,
        **kwargs: Any,
    ) -> dict[Any, Any]:
        """Snapshot real counters before the project aggregates completed searches."""
        try:
            return original(resolver, *args, **kwargs)
        finally:
            stats = resolver.stats
            observed.append(
                (stats.rounds, stats.decisions, stats.conflicts, stats.backjumps)
            )

    monkeypatch.setattr(Resolver, "resolve", recorded_resolve)
    results, _ = run_graph(
        [
            ("dep", "2+local", False, ["missing"]),
            ("dep", "2", True, withdrawn_dependencies),
        ],
        ["dep==2"],
    )
    result = results[0]
    assert result.success is (not withdrawn_dependencies)
    assert len(observed) > 1
    expected = tuple(sum(values) for values in zip(*observed, strict=True))
    assert (
        result.rounds,
        result.decisions,
        result.conflicts,
        result.backjumps,
    ) == expected
