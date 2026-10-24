"""Cross-target builds require the exact candidate's dependency assertion."""

from __future__ import annotations

import pytest

from nab_provider._provider.build_remote import build_remote_sdist
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import OverrideConflictError, UnsupportedSdistError
from nab_provider.metadata import parse_metadata
from nab_provider.overrides import IndexOverride
from nab_provider.policy import BuildPolicy
from nab_provider.provider import Provider
from nab_provider.store import sdist_artifact_key
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_sdist,
    pkg_override,
)

_TARGET = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("macos_arm64")
)
_V1 = Version("1")
_V2 = Version("2")


def _build_port(
    *, dependency: str = "shared-dep", requires_python: str | None = None
) -> FakeFetchPort:
    """Serve one dynamic sdist whose backend returns the declared metadata."""
    pkg_info = (
        make_metadata("pkg", "1.0", metadata_version="2.4") + "Dynamic: Requires-Dist\n"
    )
    built = parse_metadata(
        make_metadata("pkg", "1.0", dependency, requires_python=requires_python)
    )
    return make_coordinator(
        [make_sdist()], sdist_pkg_info=pkg_info, build_sdist=lambda *_args: built
    )


def test_version_false_restricts_a_global_assertion() -> None:
    provider = Provider(
        make_coordinator(),
        _TARGET,
        build_policy=BuildPolicy.BUILD_REMOTE,
        assume_universal_deps=True,
        package_overrides=(pkg_override("pkg<2", assume_universal_deps=False),),
    )

    assert provider.effective_build_policy("pkg", _V1) is BuildPolicy.NEVER
    assert provider.effective_build_policy("pkg", _V2) is BuildPolicy.BUILD_REMOTE
    assert provider.effective_build_policy("other", _V1) is BuildPolicy.BUILD_REMOTE


def test_version_true_permits_only_the_selected_package_versions() -> None:
    provider = Provider(
        make_coordinator(),
        _TARGET,
        build_policy=BuildPolicy.BUILD_REMOTE,
        package_overrides=(pkg_override("pkg<2", assume_universal_deps=True),),
    )

    assert provider.effective_build_policy("pkg", _V1) is BuildPolicy.BUILD_REMOTE
    assert provider.effective_build_policy("pkg", _V2) is BuildPolicy.NEVER
    assert provider.effective_build_policy("other", _V1) is BuildPolicy.NEVER


def test_index_false_restricts_a_global_assertion() -> None:
    provider = Provider(
        make_coordinator(),
        _TARGET,
        build_policy=BuildPolicy.BUILD_REMOTE,
        assume_universal_deps=True,
        index_overrides={"private": IndexOverride(assume_universal_deps=False)},
    )

    assert provider.effective_build_policy("pkg", _V1, "private") is BuildPolicy.NEVER
    assert (
        provider.effective_build_policy("pkg", _V1, "public")
        is BuildPolicy.BUILD_REMOTE
    )


def test_build_policy_conflicts_remain_errors_without_an_assertion() -> None:
    provider = Provider(
        make_coordinator(),
        _TARGET,
        package_overrides=(pkg_override("pkg", build_policy=BuildPolicy.BUILD_REMOTE),),
        index_overrides={
            "private": IndexOverride(build_policy=BuildPolicy.BUILD_REMOTE)
        },
    )

    with pytest.raises(OverrideConflictError, match="build-policy"):
        provider.effective_build_policy("pkg", _V1, "private")


def test_assertion_conflicts_remain_errors_when_builds_are_requested() -> None:
    provider = Provider(
        make_coordinator(),
        _TARGET,
        build_policy=BuildPolicy.BUILD_REMOTE,
        package_overrides=(pkg_override("pkg", assume_universal_deps=True),),
        index_overrides={"private": IndexOverride(assume_universal_deps=True)},
    )

    with pytest.raises(OverrideConflictError, match="assume-universal-deps"):
        provider.effective_build_policy("pkg", _V1, "private")


@pytest.mark.parametrize("global_assumption", [False, True])
def test_source_builds_ignore_version_scoped_assertions(
    *, global_assumption: bool
) -> None:
    provider = Provider(
        make_coordinator(),
        _TARGET,
        build_policy=BuildPolicy.BUILD_REMOTE,
        assume_universal_deps=global_assumption,
        package_overrides=(
            pkg_override("pkg<2", assume_universal_deps=not global_assumption),
        ),
    )

    expected = BuildPolicy.BUILD_REMOTE if global_assumption else BuildPolicy.NEVER

    assert provider.effective_build_policy_for_source("pkg") is expected


def test_host_built_metadata_cannot_supply_a_later_strict_declared_target() -> None:
    port = _build_port(dependency="host-only")
    host = Provider(
        port, ResolveTarget.for_host(), build_policy=BuildPolicy.BUILD_REMOTE
    )
    declared = Provider(port, _TARGET, build_policy=BuildPolicy.BUILD_REMOTE)

    assert set(host.get_dependencies("pkg", _V1)) == {"host-only"}
    assert declared.effective_build_policy("pkg", _V1) is BuildPolicy.NEVER

    with pytest.raises(UnsupportedSdistError):
        declared.get_dependencies("pkg", _V1)

    assert len(port.calls_to("request_built_metadata")) == 1


def test_opted_in_targets_build_each_source_only_once() -> None:
    port = _build_port()
    targets = (
        _TARGET,
        ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        ),
    )

    for target in targets:
        provider = Provider(
            port,
            target,
            build_policy=BuildPolicy.BUILD_REMOTE,
            assume_universal_deps=True,
        )
        assert set(provider.get_dependencies("pkg", _V1)) == {"shared-dep"}

    assert len(port.calls_to("request_built_metadata")) == 1


@pytest.mark.parametrize("invalid_field", ["requires-python", "name", "version"])
def test_cached_build_metadata_is_validated_for_each_target(invalid_field: str) -> None:
    port = _build_port(requires_python=">=3.12")
    sdist = make_sdist()
    first = Provider(
        port,
        _TARGET,
        build_policy=BuildPolicy.BUILD_REMOTE,
        assume_universal_deps=True,
    )

    assert set(first.get_dependencies("pkg", _V1)) == {"shared-dep"}

    if invalid_field != "requires-python":
        altered = parse_metadata(
            make_metadata(
                "other" if invalid_field == "name" else "pkg",
                "2.0" if invalid_field == "version" else "1.0",
                "shared-dep",
            )
        )
        port.index.store_built_metadata(
            "pkg", sdist_artifact_key(str(_V1), sdist.url), altered
        )
    target = ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    )
    second = Provider(
        port,
        target,
        build_policy=BuildPolicy.BUILD_REMOTE,
        assume_universal_deps=True,
    )

    expected_error = (
        "requires Python" if invalid_field == "requires-python" else "does not match"
    )
    with pytest.raises(UnsupportedSdistError, match=expected_error):
        second.get_dependencies("pkg", _V1)

    assert len(port.calls_to("request_built_metadata")) == 1


def test_build_helper_revalidates_cached_python_requirement() -> None:
    port = _build_port(requires_python=">=3.12")
    first = Provider(
        port, _TARGET, build_policy=BuildPolicy.BUILD_REMOTE, assume_universal_deps=True
    )
    first.get_dependencies("pkg", _V1)
    target = ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    )
    second = Provider(
        port, target, build_policy=BuildPolicy.BUILD_REMOTE, assume_universal_deps=True
    )
    second.fetch_versions("pkg")

    assert second.effective_build_policy("pkg", _V1) is BuildPolicy.BUILD_REMOTE

    with pytest.raises(UnsupportedSdistError, match="requires Python"):
        build_remote_sdist(second, "pkg", _V1)

    assert len(port.calls_to("request_built_metadata")) == 1
