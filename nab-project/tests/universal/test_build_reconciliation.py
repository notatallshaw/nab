"""Indistinguishable builds can share older versions without changing other targets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from nab_index.client import WheelFile
from nab_project.lock_preferences import LockPreferences
from nab_project.lockfile import LockValidationError, build_pylock
from nab_project.resolve import (
    InstallContexts,
    ResolveFork,
    build_lock_input,
    resolve_with_coordinator,
)
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)
from nab_resolver.errors import ResolutionLimitError
from nab_resolver.resolver import Resolver
from nab_resolver.types import RangeProtocol, RootRequirement


def registry(
    *, shared_older: bool = True, older_dependencies: tuple[str, ...] = ()
) -> FakeFetchPort:
    """Model a portable release whose newer dependency lacks free-threaded wheels."""
    declarations = (
        ("asv", "1", "py3-none-any", ("pympler",)),
        (
            "pympler",
            "1",
            "py3-none-any" if shared_older else "cp313-cp313t-win_amd64",
            older_dependencies,
        ),
        ("pympler", "2", "py3-none-any", ('pywin32; sys_platform == "win32"',)),
        ("pywin32", "1", "cp313-cp313-win_amd64", ()),
        ("pywin32", "1", "cp314-cp314-win_amd64", ()),
        ("helper", "1", "py3-none-any", ()),
    )
    listings: dict[str, list[WheelFile]] = {}
    metadata: dict[str, str] = {}
    for name, version, tag, dependencies in declarations:
        wheel = make_wheel(
            version, package=name, tag=tag, hashes=(("sha256", "a" * 64),)
        )
        assert wheel.metadata_url is not None
        listings.setdefault(name, []).append(wheel)
        metadata[wheel.metadata_url] = make_metadata(name, version, *dependencies)
    return make_coordinator(listings=listings, metadata_by_url=metadata)


def targets() -> tuple[ResolveTarget, ...]:
    """Keep a distinguishable Linux environment ahead of the Windows build pair."""
    return tuple(
        ResolveTarget.for_declared(python_version="3.13", spec=spec)
        for spec in (
            PlatformSpec("linux_x86_64"),
            PlatformSpec("windows_amd64"),
            PlatformSpec("windows_amd64", free_threaded=True),
        )
    )


@pytest.mark.parametrize("reconcile", [False, True])
def test_reconcile_shared_transitive_version(reconcile: bool) -> None:
    result = resolve_with_coordinator(
        registry(), targets(), [Requirement("asv")], reconcile_build_variants=reconcile
    )
    assert result.success
    linux, regular, threaded = result.target_results
    assert linux.pins["pympler"] == Version("2")
    assert threaded.pins["pympler"] == Version("1")

    if not reconcile:
        assert regular.pins["pympler"] == Version("2")
        with pytest.raises(LockValidationError, match="package selections"):
            build_pylock(build_lock_input(result))
        return

    assert regular.pins == threaded.pins
    assert "pywin32" not in regular.pins
    lock = build_pylock(build_lock_input(result))
    for target_result in result.target_results:
        target = target_result.target
        selected = list(
            lock.select(environment=target.marker_env, tags=target.tags.ordered)
        )
        assert {
            str(package.name): package.version for package, _ in selected
        } == target_result.pins


def test_reconciliation_retains_no_common_solution_refusal() -> None:
    result = resolve_with_coordinator(
        registry(shared_older=False),
        targets(),
        [Requirement("asv")],
        reconcile_build_variants=True,
    )
    assert result.success
    assert result.target_results[1].pins["pympler"] == Version("2")
    assert result.target_results[2].pins["pympler"] == Version("1")
    with pytest.raises(LockValidationError, match="package selections"):
        build_pylock(build_lock_input(result))


def test_reconciliation_combines_previously_selected_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Resolver.resolve
    measurements: dict[str, list[tuple[int, int]]] = {}

    def measured(
        self: Resolver[str, Version],
        requirements: Mapping[str, RangeProtocol[Version]]
        | Sequence[RootRequirement[str, Version]],
        constraints: Mapping[str, RangeProtocol[Version]] | None = None,
    ) -> dict[str, Version]:
        """Record costs from the real resolver, including rejected shared choices."""
        pins = original(self, requirements, constraints)
        assert isinstance(self.provider, Provider)
        assert self.provider.target is not None
        measurements.setdefault(self.provider.target.label, []).append(
            (self.stats.decisions, self.stats.rounds)
        )
        return pins

    monkeypatch.setattr(Resolver, "resolve", measured)
    listings = {
        name: [
            make_wheel("1", package=name, hashes=(("sha256", "a" * 64),)),
            make_wheel("2", package=name, tag=tag, hashes=(("sha256", "b" * 64),)),
        ]
        for name, tag in (
            ("a", "cp313-cp313-win_amd64"),
            ("b", "cp313-cp313t-win_amd64"),
        )
    }
    result = resolve_with_coordinator(
        make_coordinator(listings=listings, auto_metadata=True),
        targets()[1:],
        [Requirement("a"), Requirement("b")],
        align_across_targets=False,
        reconcile_build_variants=True,
    )
    assert result.success
    assert [item.pins for item in result.target_results] == [
        {"a": Version("1"), "b": Version("1")},
        {"a": Version("1"), "b": Version("1")},
    ]
    build_pylock(build_lock_input(result))

    for item in result.target_results:
        attempts = measurements[item.target.label]
        assert len(attempts) == 4
        assert item.decisions == sum(decisions for decisions, _ in attempts)
        assert item.rounds == sum(rounds for _, rounds in attempts)


def test_reconciliation_updates_base_attribution() -> None:
    requirements = (Requirement("asv"),)
    result = resolve_with_coordinator(
        registry(),
        targets(),
        forks=[ResolveFork((("group", "test"),), requirements)],
        base_requirements=requirements,
        reconcile_build_variants=True,
    )
    assert result.success
    windows = [
        item
        for item in result.base_results
        if item.target.marker_env["sys_platform"] == "win32"
    ]
    assert len(windows) == 2
    assert windows[0].pins == windows[1].pins
    assert "pywin32" not in windows[0].pins
    for item in windows:
        assert result.env_base_names[
            tuple(sorted(item.target.marker_env.items()))
        ] == frozenset({"asv", "pympler"})
    build_pylock(build_lock_input(result))


def test_failed_resolution_does_not_start_optional_reconciliation() -> None:
    result = resolve_with_coordinator(
        registry(),
        targets(),
        [Requirement("asv"), Requirement('asv>=2; sys_platform == "linux"')],
        reconcile_build_variants=True,
    )
    assert not result.success
    assert result.target_results[1].pins["pympler"] == Version("2")
    assert result.target_results[2].pins["pympler"] == Version("1")


def test_reconciliation_rechecks_new_patch_boundaries() -> None:
    result = resolve_with_coordinator(
        registry(older_dependencies=('helper; python_full_version < "3.13.2"',)),
        targets(),
        [Requirement("asv")],
        reconcile_build_variants=True,
    )
    assert result.success
    lock = build_pylock(build_lock_input(result))
    for free_threaded in (False, True):
        for patch in ("3.13.1", "3.13.2"):
            target = ResolveTarget.for_declared(
                python_version="3.13",
                python_full_version=patch,
                spec=PlatformSpec("windows_amd64", free_threaded=free_threaded),
            )
            selected = {
                str(package.name): package.version
                for package, _ in lock.select(
                    environment=target.marker_env, tags=target.tags.ordered
                )
            }
            assert selected["pympler"] == Version("1")
            assert ("helper" in selected) == (patch == "3.13.1")


def test_reconciliation_stops_immediately_on_a_resource_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Resolver.resolve
    calls = 0

    def limited(
        self: Resolver[str, Version],
        requirements: Mapping[str, RangeProtocol[Version]]
        | Sequence[RootRequirement[str, Version]],
        constraints: Mapping[str, RangeProtocol[Version]] | None = None,
    ) -> dict[str, Version]:
        nonlocal calls
        calls += 1
        # Exhaust the budget before either differing build pair can be repaired.
        if calls == 6:
            raise ResolutionLimitError("caller work budget exhausted")
        return original(self, requirements, constraints)

    monkeypatch.setattr(Resolver, "resolve", limited)
    next_minor = tuple(
        ResolveTarget.for_declared(python_version="3.14", spec=spec)
        for spec in (
            PlatformSpec("windows_amd64"),
            PlatformSpec("windows_amd64", free_threaded=True),
        )
    )
    result = resolve_with_coordinator(
        registry(),
        (*targets(), *next_minor),
        [Requirement("asv")],
        reconcile_build_variants=True,
        align_across_targets=False,
    )
    assert not result.success
    assert calls == 6
    with pytest.raises(ResolutionLimitError, match="caller work budget"):
        result.raise_for_failure()


def test_reconciliation_does_not_merge_different_group_reachability() -> None:
    regular = make_wheel(
        "1", package="a", tag="cp313-cp313-win_amd64", hashes=(("sha256", "a" * 64),)
    )
    threaded = make_wheel(
        "1", package="a", tag="cp313-cp313t-win_amd64", hashes=(("sha256", "b" * 64),)
    )
    portable = make_wheel("1", package="b", hashes=(("sha256", "c" * 64),))
    assert regular.metadata_url is not None
    assert threaded.metadata_url is not None
    assert portable.metadata_url is not None
    coordinator = make_coordinator(
        listings={"a": [regular, threaded], "b": [portable]},
        metadata_by_url={
            regular.metadata_url: make_metadata("a", "1", "b"),
            threaded.metadata_url: make_metadata("a", "1"),
            portable.metadata_url: make_metadata("b", "1"),
        },
    )
    requirements = (Requirement("a"), Requirement("b"))
    contexts = InstallContexts(
        selectors={
            ("group", "alpha"): (requirements[0],),
            ("group", "beta"): (requirements[1],),
        }
    )
    result = resolve_with_coordinator(
        coordinator,
        targets()[1:],
        forks=[ResolveFork((), requirements, contexts)],
        reconcile_build_variants=True,
    )
    assert result.success
    with pytest.raises(LockValidationError, match="package selections"):
        build_pylock(build_lock_input(result))


def test_reconciliation_can_change_prior_lock_preferences() -> None:
    requirements = [Requirement("asv")]
    original = resolve_with_coordinator(registry(), targets()[:2], requirements)
    prior = LockPreferences(build_pylock(build_lock_input(original)))
    result = resolve_with_coordinator(
        registry(),
        targets(),
        requirements,
        lock_preferences=prior,
        reconcile_build_variants=True,
    )
    assert result.success
    assert result.target_results[0].pins["pympler"] == Version("2")
    assert result.target_results[1].pins["pympler"] == Version("1")
    assert result.target_results[1].pins == result.target_results[2].pins
    build_pylock(build_lock_input(result))
