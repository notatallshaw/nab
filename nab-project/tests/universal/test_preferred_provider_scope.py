"""Model trials retain actual target checks and discard provisional providers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

import pytest

from nab_project.inputs import ResolveInputs
from nab_project.resolve import resolve_with_coordinator
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import InvalidUploadTimeError, MissingExtraError
from nab_provider.policy import BuildPolicy, DecisionOrder
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)
from nab_resolver.errors import ResolutionTerminatedError
from nab_resolver.resolver import Resolver
from nab_resolver.types import RangeProtocol, RootRequirement


def target(platform: str, python: str = "3.12") -> ResolveTarget:
    """Build an explicit target without consulting the host environment."""
    return ResolveTarget.for_declared(
        python_version=python, spec=PlatformSpec(platform)
    )


def changing_registry() -> FakeFetchPort:
    """Make z's newest release change its x dependency across platforms."""
    declarations = {
        "a": {"1": ("x>=1", "z>=1")},
        "x": {"1": (), "2": (), "3": ()},
        "z": {
            "1": ("x==1",),
            "2": ('x==1; sys_platform == "linux"', 'x==2; sys_platform == "win32"'),
        },
    }
    listings = {}
    metadata = {}
    for package, versions in declarations.items():
        wheels = []
        for version, dependencies in versions.items():
            wheel = make_wheel(version, package=package, hashes=(("sha256", "a" * 64),))
            assert wheel.metadata_url is not None
            wheels.append(wheel)
            metadata[wheel.metadata_url] = make_metadata(
                package, version, *dependencies
            )
        listings[package] = wheels
    return make_coordinator(listings=listings, metadata_by_url=metadata)


def test_refused_model_cannot_carry_provisional_clauses_into_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_started: list[tuple[str, int]] = []
    refused_with_pending: list[int] = []
    trial_decisions: list[int] = []
    normal_decisions: list[int] = []
    original_resolve = Resolver.resolve
    original_trial = Resolver.try_resolve_preferred

    def resolve(
        self: Resolver[str, Version],
        requirements: Mapping[str, RangeProtocol[Version]]
        | Sequence[RootRequirement[str, Version]],
        constraints: Mapping[str, RangeProtocol[Version]] | None = None,
    ) -> dict[str, Version]:
        provider = self.provider

        assert isinstance(provider, Provider)
        assert provider.target is not None
        normal_started.append(
            (provider.target.marker_env["sys_platform"], len(provider.pending_clauses))
        )
        result = original_resolve(self, requirements, constraints)
        normal_decisions.append(self.stats.decisions)
        return result

    def trial(
        self: Resolver[str, Version],
        requirements: Mapping[str, RangeProtocol[Version]]
        | Sequence[RootRequirement[str, Version]],
        preferred: Mapping[str, Version],
        constraints: Mapping[str, RangeProtocol[Version]] | None = None,
    ) -> dict[str, Version] | None:
        result = original_trial(self, requirements, preferred, constraints)
        if result is None:
            provider = self.provider
            assert isinstance(provider, Provider)
            refused_with_pending.append(len(provider.pending_clauses))
            trial_decisions.append(self.stats.decisions)
        return result

    monkeypatch.setattr(Resolver, "resolve", resolve)
    monkeypatch.setattr(Resolver, "try_resolve_preferred", trial)
    result = resolve_with_coordinator(
        changing_registry(),
        [target("linux_x86_64"), target("windows_amd64")],
        [Requirement("a")],
        inputs=ResolveInputs(
            build_policy=BuildPolicy.NEVER, decision_order=DecisionOrder.STABLE
        ),
    )
    result.raise_for_failure()

    assert result.target_results[0].pins == {
        "a": Version("1"),
        "z": Version("2"),
        "x": Version("1"),
    }
    assert result.target_results[1].pins == {
        "a": Version("1"),
        "z": Version("2"),
        "x": Version("2"),
    }
    assert refused_with_pending
    assert all(refused_with_pending)
    assert normal_started == [("linux", 0), ("win32", 0)]
    assert (
        result.target_results[1].decisions == normal_decisions[1] + trial_decisions[0]
    )


def test_each_target_still_requires_an_admitted_wheel() -> None:
    linux = make_wheel(
        "2",
        package="a",
        tag="cp312-cp312-manylinux_2_17_x86_64",
        hashes=(("sha256", "a" * 64),),
    )
    portable = make_wheel("1", package="a", hashes=(("sha256", "b" * 64),))
    result = resolve_with_coordinator(
        make_coordinator(listings={"a": [linux, portable]}, auto_metadata=True),
        [target("linux_x86_64"), target("windows_amd64")],
        [Requirement("a")],
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
    )
    result.raise_for_failure()

    assert [item.pins for item in result.target_results] == [
        {"a": Version("2")},
        {"a": Version("1")},
    ]
    assert result.target_results[0].lock is not None
    assert result.target_results[1].lock is not None


def test_prerelease_fallback_is_checked_against_each_python() -> None:
    final = make_wheel(
        "1", package="a", requires_python=">=3.13", hashes=(("sha256", "a" * 64),)
    )
    preview = make_wheel(
        "2rc1", package="a", requires_python="<3.13", hashes=(("sha256", "b" * 64),)
    )
    result = resolve_with_coordinator(
        make_coordinator(listings={"a": [final, preview]}, auto_metadata=True),
        [
            target("linux_x86_64"),
            target("windows_amd64"),
            target("linux_x86_64", "3.13"),
        ],
        [Requirement("a>=1")],
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
    )
    result.raise_for_failure()

    assert [item.pins for item in result.target_results] == [
        {"a": Version("2rc1")},
        {"a": Version("2rc1")},
        {"a": Version("1")},
    ]


def test_trial_does_not_swallow_a_one_shot_termination() -> None:
    class Progress:
        def __init__(self) -> None:
            self.pin_counts: list[int] = []

        def on_fetch(self) -> None:
            """The registry is already materialized."""

        def on_pin(self, decided: int) -> None:
            self.pin_counts.append(decided)
            if len(self.pin_counts) == 2:
                raise ResolutionTerminatedError("operation cancelled")

    wheel = make_wheel("1", package="a", hashes=(("sha256", "a" * 64),))
    progress = Progress()
    result = resolve_with_coordinator(
        make_coordinator(listings={"a": [wheel]}, auto_metadata=True),
        [target("linux_x86_64"), target("windows_amd64")],
        [Requirement("a")],
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
        progress=progress,
    )

    assert result.target_results[0].success
    assert not result.target_results[1].success
    assert isinstance(result.target_results[1].error, ResolutionTerminatedError)
    assert progress.pin_counts == [2, 2]


def test_target_markers_can_add_and_remove_packages_from_the_hint() -> None:
    a = make_wheel("1", package="a", hashes=(("sha256", "a" * 64),))
    b = make_wheel("1", package="b", hashes=(("sha256", "b" * 64),))

    assert a.metadata_url is not None
    assert b.metadata_url is not None
    result = resolve_with_coordinator(
        make_coordinator(
            listings={"a": [a], "b": [b]},
            metadata_by_url={
                a.metadata_url: make_metadata("a", "1", 'b; sys_platform == "win32"'),
                b.metadata_url: make_metadata("b", "1"),
            },
        ),
        [target("linux_x86_64"), target("windows_amd64"), target("linux_x86_64")],
        [Requirement("a")],
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
    )
    result.raise_for_failure()

    assert [item.pins for item in result.target_results] == [
        {"a": Version("1")},
        {"a": Version("1"), "b": Version("1")},
        {"a": Version("1")},
    ]


def test_metadata_requires_python_can_refuse_the_preferred_version() -> None:
    newer = make_wheel("2", package="a", hashes=(("sha256", "a" * 64),))
    older = make_wheel("1", package="a", hashes=(("sha256", "b" * 64),))

    assert newer.metadata_url is not None
    assert older.metadata_url is not None
    result = resolve_with_coordinator(
        make_coordinator(
            listings={"a": [newer, older]},
            metadata_by_url={
                newer.metadata_url: make_metadata("a", "2", requires_python=">=3.13"),
                older.metadata_url: make_metadata("a", "1", requires_python=">=3.12"),
            },
        ),
        [target("linux_x86_64", "3.13"), target("linux_x86_64")],
        [Requirement("a")],
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
    )
    result.raise_for_failure()

    assert [item.pins for item in result.target_results] == [
        {"a": Version("2")},
        {"a": Version("1")},
    ]


def test_missing_target_extra_is_reported_by_ordinary_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary_targets: list[str] = []
    original_resolve = Resolver.resolve

    def resolve(
        self: Resolver[str, Version],
        requirements: Mapping[str, RangeProtocol[Version]]
        | Sequence[RootRequirement[str, Version]],
        constraints: Mapping[str, RangeProtocol[Version]] | None = None,
    ) -> dict[str, Version]:
        provider = self.provider

        assert isinstance(provider, Provider)
        assert provider.target is not None
        ordinary_targets.append(provider.target.marker_env["sys_platform"])
        return original_resolve(self, requirements, constraints)

    monkeypatch.setattr(Resolver, "resolve", resolve)
    linux = make_wheel(
        "1",
        package="a",
        tag="cp312-cp312-manylinux_2_17_x86_64",
        hashes=(("sha256", "a" * 64),),
    )
    windows = make_wheel(
        "1",
        package="a",
        tag="cp312-cp312-win_amd64",
        hashes=(("sha256", "b" * 64),),
    )

    assert linux.metadata_url is not None
    assert windows.metadata_url is not None
    with pytest.raises(MissingExtraError):
        resolve_with_coordinator(
            make_coordinator(
                listings={"a": [linux, windows]},
                metadata_by_url={
                    linux.metadata_url: make_metadata(
                        "a", "1", provides_extra=("speed",)
                    ),
                    windows.metadata_url: make_metadata("a", "1"),
                },
            ),
            [target("linux_x86_64"), target("windows_amd64")],
            [Requirement("a[speed]")],
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
        )

    assert ordinary_targets == ["linux", "win32"]


def test_preferred_trial_filters_only_complete_selected_release() -> None:
    history = [
        make_wheel(str(version), package="a", hashes=(("sha256", "a" * 64),))
        for version in range(1, 21)
    ]
    alias = make_wheel(
        "20.0", package="a", tag="py2.py3-none-any", hashes=(("sha256", "b" * 64),)
    )

    result = resolve_with_coordinator(
        make_coordinator(listings={"a": [*history, alias]}, auto_metadata=True),
        [target("linux_x86_64"), target("windows_amd64")],
        [Requirement("a")],
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
    )
    result.raise_for_failure()

    first, second = result.target_results

    assert first.pins == second.pins == {"a": Version("20")}

    # Later targets keep both artifacts without filtering the other releases.
    assert first.distributions_seen == 21
    assert second.distributions_seen == 2
    assert second.lock is not None
    assert len(second.lock.pins["a"].wheels) == 2


def test_upload_cutoff_still_validates_historical_rows() -> None:
    selected = make_wheel("2", package="a", upload_time="2020-01-01T00:00:00Z")
    historical = make_wheel(
        "1", package="a", requires_python=">=3.13", upload_time="2020-01-01T00:00:00"
    )

    # The later Python admits the old artifact and must report its invalid time.
    with pytest.raises(InvalidUploadTimeError):
        resolve_with_coordinator(
            make_coordinator(
                listings={"a": [selected, historical]}, auto_metadata=True
            ),
            [target("linux_x86_64"), target("linux_x86_64", "3.13")],
            [Requirement("a")],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                uploaded_prior_to=datetime(2021, 1, 1, tzinfo=timezone.utc),
            ),
        )
