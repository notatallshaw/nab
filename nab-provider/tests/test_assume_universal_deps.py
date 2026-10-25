"""A release can explicitly reuse dependency metadata across declared targets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import (
    ForeignMetadataError,
    IncompatiblePythonError,
    IndexAccessError,
    MetadataHashMismatchError,
    OverrideConflictError,
    UnsupportedSdistError,
)
from nab_provider.metadata import parse_metadata
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.provider import BuildPolicy, DistPolicy, Provider
from nab_provider.records import RangeMetadataResult, RangeOutcome, SdistFile, WheelFile
from nab_provider.store import sdist_artifact_key
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_sdist,
    make_wheel,
    pkg_override,
)
from nab_provider.yanking import mark_yanked
from nab_resolver.diagnostics import error_message

_VERSION = Version("1.0")
_MAC_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("macos_arm64")
)
_LINUX_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)
_WIN_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("windows_amd64")
)
_DYNAMIC_PKG_INFO = (
    make_metadata("pkg", "1.0", "untrusted", metadata_version="2.4")
    + "Dynamic: Requires-Dist\nDynamic: Provides-Extra\n"
)
_WHEEL_METADATA = make_metadata("pkg", "1.0", "wheel-dep")


def _foreign_wheel(
    *,
    version: str = "1.0",
    requires_python: str | None = None,
    upload_time: str | None = None,
    has_metadata: bool = True,
    metadata_hash: tuple[str, str] | None = None,
    hashes: tuple[tuple[str, str], ...] = (),
    local_path: Path | None = None,
    url: str | None = None,
) -> WheelFile:
    """Return a Linux wheel that the declared macOS target cannot install."""
    return make_wheel(
        version,
        tag="cp311-cp311-manylinux_2_17_x86_64",
        requires_python=requires_python,
        upload_time=upload_time,
        has_metadata=has_metadata,
        metadata_hash=metadata_hash,
        hashes=hashes,
        local_path=local_path,
        url=url,
    )


def _port(
    *wheels: WheelFile,
    pkg_info: str = _DYNAMIC_PKG_INFO,
    metadata: str | None = _WHEEL_METADATA,
) -> FakeFetchPort:
    """Serve a dynamic sdist and any candidate dependency-metadata wheels."""
    return make_coordinator(
        [*wheels, make_sdist()],
        metadata_text=metadata,
        sdist_pkg_info=pkg_info,
    )


def test_strict_default_refuses_dynamic_sdist_and_suggests_scoped_option() -> None:
    port = _port(_foreign_wheel())
    provider = Provider(port, _MAC_TARGET)

    assert provider.effective_assume_universal_deps("pkg", _VERSION) is False

    with pytest.raises(UnsupportedSdistError) as caught:
        provider.get_dependencies("pkg", _VERSION)

    assert 'match = ["pkg==1.0"]' in str(caught.value)
    assert "assume-universal-deps = true" in str(caught.value)
    assert port.calls_to("request_built_metadata") == []


@pytest.mark.parametrize(
    ("entry", "global_value"),
    [
        pytest.param(
            pkg_override("pkg<2", assume_universal_deps=False),
            True,
            id="existing-assertion",
        ),
        pytest.param(
            pkg_override("pkg", name_keyed=True, build_policy=BuildPolicy.NEVER),
            False,
            id="existing-bare-table",
        ),
    ],
)
def test_dynamic_sdist_hint_edits_the_existing_package_entry(
    entry: PackageOverride,
    *,
    global_value: bool,
) -> None:
    port = _port(_foreign_wheel())
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=global_value,
        package_overrides=(entry,),
    )

    with pytest.raises(UnsupportedSdistError) as caught:
        provider.get_dependencies("pkg", _VERSION)

    assert f"set assume-universal-deps = true in {entry.requirement};" in str(
        caught.value
    )

    repaired = Provider(
        port,
        _MAC_TARGET,
        package_overrides=(entry.replace(assume_universal_deps=True),),
    )
    assert set(repaired.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}


def test_dynamic_sdist_hint_edits_the_serving_index_assertion() -> None:
    port = _port(_foreign_wheel())
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        index_overrides={"pypi": IndexOverride(assume_universal_deps=False)},
    )

    with pytest.raises(UnsupportedSdistError) as caught:
        provider.get_dependencies("pkg", _VERSION)

    assert "set assume-universal-deps = true in index 'pypi';" in str(caught.value)

    repaired = Provider(
        port,
        _MAC_TARGET,
        index_overrides={"pypi": IndexOverride(assume_universal_deps=True)},
    )
    assert set(repaired.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}


def test_dynamic_sdist_hint_revises_an_overlapping_legacy_assertion() -> None:
    port = _port(_foreign_wheel())
    provider = Provider(
        port,
        _MAC_TARGET,
        package_overrides=(pkg_override("pkg===1.0.0.0", assume_universal_deps=False),),
    )

    with pytest.raises(UnsupportedSdistError) as caught:
        provider.get_dependencies("pkg", _VERSION)

    assert "revise the scoped assume-universal-deps entries" in str(caught.value)
    assert "and assume-universal-deps = true" not in str(caught.value)

    repaired = Provider(
        port,
        _MAC_TARGET,
        package_overrides=(pkg_override("pkg<2", assume_universal_deps=True),),
    )
    assert set(repaired.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}


def test_cached_sdist_failure_preserves_the_suggested_remedy() -> None:
    port = _port(_foreign_wheel())
    provider = Provider(port, _MAC_TARGET)

    with pytest.raises(UnsupportedSdistError) as first:
        provider.get_dependencies("pkg", _VERSION)
    with pytest.raises(UnsupportedSdistError) as cached:
        provider.get_dependencies("pkg", _VERSION)

    assert error_message(first.value).suggestions
    assert (
        error_message(cached.value).suggestions
        == error_message(first.value).suggestions
    )
    assert len(port.calls_to("request_sdist")) == 1


@pytest.mark.parametrize("global_value", [False, True])
def test_package_override_selects_version_and_preserves_explicit_false(
    *, global_value: bool
) -> None:
    provider = Provider(
        make_coordinator(),
        assume_universal_deps=global_value,
        package_overrides=(
            pkg_override("pkg<2", assume_universal_deps=not global_value),
        ),
    )

    assert provider.effective_assume_universal_deps("pkg", _VERSION) is not global_value
    assert provider.effective_assume_universal_deps("pkg", Version("2")) is global_value
    assert provider.effective_assume_universal_deps("other", _VERSION) is global_value


@pytest.mark.parametrize("global_value", [False, True])
def test_index_override_preserves_explicit_false(*, global_value: bool) -> None:
    provider = Provider(
        make_coordinator(),
        assume_universal_deps=global_value,
        index_overrides={
            "internal": IndexOverride(assume_universal_deps=not global_value)
        },
    )

    assert (
        provider.effective_assume_universal_deps("pkg", _VERSION, "internal")
        is not global_value
    )
    assert provider.effective_assume_universal_deps("pkg", _VERSION) is global_value


@pytest.mark.parametrize("index_value", [False, True])
def test_package_and_index_setting_conflict_even_when_values_agree(
    *, index_value: bool
) -> None:
    provider = Provider(
        make_coordinator(),
        package_overrides=(pkg_override("pkg", assume_universal_deps=True),),
        index_overrides={"internal": IndexOverride(assume_universal_deps=index_value)},
    )

    with pytest.raises(OverrideConflictError, match="assume-universal-deps"):
        provider.effective_assume_universal_deps("pkg", _VERSION, "internal")


@pytest.mark.parametrize("scope", ["global", "package", "index"])
def test_opt_in_reuses_wheel_deps_without_installing_foreign_wheel(scope: str) -> None:
    wheel = _foreign_wheel(metadata_hash=("sha256", "abcdef"))
    port = _port(wheel)
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=scope == "global",
        package_overrides=(
            (pkg_override("pkg==1.0", assume_universal_deps=True),)
            if scope == "package"
            else ()
        ),
        index_overrides=(
            {"pypi": IndexOverride(assume_universal_deps=True)}
            if scope == "index"
            else {}
        ),
    )

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}
    assert all(
        isinstance(dist, SdistFile) for _, dist in provider.fetch_versions("pkg")
    )
    assert port.calls_to("request_metadata") == [
        ("pkg", "1.0", wheel.metadata_url, wheel.metadata_hash)
    ]
    assert port.calls_to("request_built_metadata") == []


def test_borrowed_markers_and_extras_are_evaluated_for_each_resolve_target() -> None:
    metadata = make_metadata(
        "pkg",
        "1.0",
        'mac-dep; sys_platform == "darwin"',
        'linux-dep; sys_platform == "linux"',
        'win-dep; sys_platform == "win32"',
        'optional-dep; extra == "cli"',
        provides_extra=("cli",),
    )
    port = _port(_foreign_wheel(), metadata=metadata)
    macos = Provider(port, _MAC_TARGET, assume_universal_deps=True)
    windows = Provider(port, _WIN_TARGET, assume_universal_deps=True)
    linux = Provider(port, _LINUX_TARGET, assume_universal_deps=True)

    assert set(macos.get_dependencies("pkg", _VERSION)) == {"mac-dep"}
    assert set(windows.get_dependencies("pkg", _VERSION)) == {"win-dep"}
    assert set(linux.get_dependencies("pkg", _VERSION)) == {"linux-dep"}
    assert set(macos.get_dependencies("pkg[cli]", _VERSION)) == {"pkg", "optional-dep"}
    assert set(windows.get_dependencies("pkg[cli]", _VERSION)) == {
        "pkg",
        "optional-dep",
    }
    assert set(linux.get_dependencies("pkg[cli]", _VERSION)) == {"pkg", "optional-dep"}


def test_static_pyproject_takes_precedence_over_assumed_wheel_metadata() -> None:
    port = make_coordinator(
        [_foreign_wheel(), make_sdist()],
        metadata_text=_WHEEL_METADATA,
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        sdist_pyproject={"project": {"dependencies": ["static-dep"]}},
    )
    provider = Provider(port, _MAC_TARGET, assume_universal_deps=True)

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"static-dep"}
    assert port.calls_to("request_metadata") == []


@pytest.mark.parametrize("metadata_version", ["2.1", "2.4"])
def test_assumption_never_uses_unverified_pkginfo_dependencies(
    metadata_version: str,
) -> None:
    pkg_info = make_metadata(
        "pkg", "1.0", "untrusted", metadata_version=metadata_version
    )
    if metadata_version == "2.4":
        pkg_info += "Dynamic: Requires-Dist\n"
    port = _port(pkg_info=pkg_info)
    provider = Provider(port, _MAC_TARGET, assume_universal_deps=True)

    with pytest.raises(UnsupportedSdistError):
        provider.get_dependencies("pkg", _VERSION)

    assert ("pkg", _VERSION) not in provider.deps_cache


def test_static_sdist_dependencies_need_no_wheel_assumption() -> None:
    port = _port(
        _foreign_wheel(),
        pkg_info=make_metadata("pkg", "1.0", "static-dep", metadata_version="2.4"),
    )
    provider = Provider(port, _MAC_TARGET, assume_universal_deps=True)

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"static-dep"}
    assert port.calls_to("request_metadata") == []


def test_only_dependency_fields_are_borrowed() -> None:
    pkg_info = (
        make_metadata(
            "pkg", "1.0", "untrusted", requires_python=">=3.10", metadata_version="2.4"
        )
        + "Dynamic: Requires-Dist\n"
    )
    wheel_metadata = make_metadata(
        "PKG", "1.0.0", "wheel-dep", requires_python=">=3.12"
    )
    provider = Provider(
        _port(_foreign_wheel(), pkg_info=pkg_info, metadata=wheel_metadata),
        _MAC_TARGET,
        assume_universal_deps=True,
    )

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}

    metadata = provider.metadata_cache[("pkg", _VERSION)]

    assert metadata.name == "pkg"
    assert str(metadata.version) == "1.0"
    assert str(metadata.requires_python) == ">=3.10"
    assert metadata.metadata_version == "2.4"


def test_sdist_requires_python_still_rejects_target() -> None:
    pkg_info = (
        make_metadata("pkg", "1.0", requires_python=">=3.12", metadata_version="2.4")
        + "Dynamic: Requires-Dist\n"
    )
    provider = Provider(
        _port(_foreign_wheel(), pkg_info=pkg_info),
        _MAC_TARGET,
        assume_universal_deps=True,
    )

    with pytest.raises(IncompatiblePythonError):
        provider.get_dependencies("pkg", _VERSION)


@pytest.mark.parametrize(
    ("wrong_name", "wrong_version"), [("other", "1.0"), ("pkg", "2.0")]
)
def test_borrowed_metadata_must_declare_requested_release(
    wrong_name: str, wrong_version: str
) -> None:
    provider = Provider(
        _port(
            _foreign_wheel(), metadata=make_metadata(wrong_name, wrong_version, "dep")
        ),
        _MAC_TARGET,
        assume_universal_deps=True,
    )

    with pytest.raises(ForeignMetadataError):
        provider.get_dependencies("pkg", _VERSION)


def test_sidecar_integrity_error_is_not_replaced_by_build_fallback() -> None:
    wheel = _foreign_wheel()
    port = _port(wheel)
    error = MetadataHashMismatchError("sidecar hash mismatch")
    port.index.store_metadata_error("pkg", "1.0", error, wheel.metadata_url)
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        build_policy=BuildPolicy.BUILD_REMOTE,
    )

    with pytest.raises(MetadataHashMismatchError, match="sidecar hash mismatch"):
        provider.get_dependencies("pkg", _VERSION)

    assert port.calls_to("request_built_metadata") == []


def test_bare_wheel_uses_range_metadata_and_passes_integrity_hashes() -> None:
    wheel = _foreign_wheel(has_metadata=False, hashes=(("sha256", "abcdef"),))
    port = make_coordinator(
        [wheel, make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        range_result=RangeMetadataResult(_WHEEL_METADATA, RangeOutcome.PARTIAL),
    )
    provider = Provider(port, _MAC_TARGET, assume_universal_deps=True)

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}
    assert port.calls_to("request_range_metadata") == [
        ("pkg", "1.0", wheel.url, wheel.hashes)
    ]
    assert provider.stats.wheel_metadata_range_fetched == 1


def test_local_wheel_uses_host_metadata_reader(tmp_path: Path) -> None:
    wheel_path = tmp_path / "pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"
    wheel = _foreign_wheel(
        has_metadata=False, local_path=wheel_path, url=wheel_path.as_uri()
    )
    read_paths: list[Path] = []

    def read_wheel(path: Path) -> str:
        read_paths.append(path)
        return _WHEEL_METADATA

    port = make_coordinator(
        [wheel, make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        read_wheel=read_wheel,
    )
    provider = Provider(port, _MAC_TARGET, assume_universal_deps=True)

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}
    assert read_paths == [wheel_path]
    assert port.calls_to("request_range_metadata") == []


def test_unreadable_local_metadata_is_not_replaced_by_a_build(tmp_path: Path) -> None:
    wheel_path = tmp_path / "pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"
    wheel = _foreign_wheel(
        has_metadata=False, local_path=wheel_path, url=wheel_path.as_uri()
    )

    def unreadable(_path: Path) -> str:
        raise IndexAccessError("cannot read local wheel")

    port = make_coordinator(
        [wheel, make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        read_wheel=unreadable,
    )
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        build_policy=BuildPolicy.BUILD_REMOTE,
    )

    with pytest.raises(IndexAccessError, match="cannot read local wheel"):
        provider.get_dependencies("pkg", _VERSION)

    assert port.calls_to("request_built_metadata") == []


@pytest.mark.parametrize(
    "wheel_state", ["absent", "other-version", "filtered", "usable"]
)
def test_strict_sdist_hint_conditions_builds_on_metadata_wheel_availability(
    wheel_state: str,
) -> None:
    wheels = (
        []
        if wheel_state == "absent"
        else [
            _foreign_wheel(
                version="2.0" if wheel_state == "other-version" else "1.0",
                requires_python=">=3.12" if wheel_state == "filtered" else None,
            )
        ]
    )
    port = _port(*wheels)
    provider = Provider(port, _MAC_TARGET)

    with pytest.raises(UnsupportedSdistError) as caught:
        provider.get_dependencies("pkg", _VERSION)

    hint = error_message(caught.value).suggestions[0].action
    assert "assume-universal-deps = true" in hint
    assert 'build-policy = "build-remote"' in hint
    assert ("if no usable wheel metadata is available" in hint) is (
        wheel_state == "usable"
    )
    assert port.calls_to("request_built_metadata") == []


@pytest.mark.parametrize("scope", ["default", "package", "index"])
def test_dynamic_sdist_build_hint_repairs_the_effective_policy(scope: str) -> None:
    built = parse_metadata(make_metadata("pkg", "1.0", "built-dep"))
    port = make_coordinator(
        [make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        build_sdist=lambda *_args: built,
    )
    entry = pkg_override("pkg<2", build_policy=BuildPolicy.NEVER)
    package_overrides = (entry,) if scope == "package" else ()
    index_overrides = (
        {"pypi": IndexOverride(build_policy=BuildPolicy.NEVER)}
        if scope == "index"
        else {}
    )
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        package_overrides=package_overrides,
        index_overrides=index_overrides,
    )

    with pytest.raises(UnsupportedSdistError) as first:
        provider.get_dependencies("pkg", _VERSION)
    with pytest.raises(UnsupportedSdistError) as cached:
        provider.get_dependencies("pkg", _VERSION)

    report = error_message(first.value)
    assert report.suggestions == error_message(cached.value).suggestions
    assert report.suggestions
    hint = report.suggestions[0].action
    assert 'build-policy = "build-remote"' in hint
    assert "assume-universal-deps = true" not in hint
    if scope == "package":
        assert f"in {entry.requirement};" in hint
        package_overrides = (entry.replace(build_policy=BuildPolicy.BUILD_REMOTE),)
    elif scope == "index":
        assert "in index 'pypi';" in hint
        index_overrides = {"pypi": IndexOverride(build_policy=BuildPolicy.BUILD_REMOTE)}
    else:
        assert 'match = ["pkg==1.0"]' in hint
        package_overrides = (
            pkg_override("pkg==1.0", build_policy=BuildPolicy.BUILD_REMOTE),
        )

    repaired = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        package_overrides=package_overrides,
        index_overrides=index_overrides,
    )
    assert set(repaired.get_dependencies("pkg", _VERSION)) == {"built-dep"}
    assert len(port.calls_to("request_built_metadata")) == 1


@pytest.mark.parametrize("build_policy", [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL])
def test_assumption_alone_does_not_authorize_builds(build_policy: BuildPolicy) -> None:
    port = _port()
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        build_policy=build_policy,
    )

    with pytest.raises(UnsupportedSdistError) as caught:
        provider.get_dependencies("pkg", _VERSION)

    assert "assume-universal-deps = true" not in str(caught.value)
    assert port.calls_to("request_built_metadata") == []


@pytest.mark.parametrize("missing", ["no-wheel", "unreadable-wheel", "other-version"])
def test_no_usable_same_version_metadata_falls_back_to_explicit_build(
    missing: str,
) -> None:
    wheels = (
        []
        if missing == "no-wheel"
        else [_foreign_wheel(version="2.0" if missing == "other-version" else "1.0")]
    )
    built = parse_metadata(make_metadata("pkg", "1.0", "built-dep"))
    port = make_coordinator(
        [*wheels, make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        build_sdist=lambda *_args: built,
    )
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        build_policy=BuildPolicy.BUILD_REMOTE,
    )

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"built-dep"}
    assert len(port.calls_to("request_built_metadata")) == 1


@pytest.mark.parametrize(
    "restriction", ["requires-python", "cutoff", "sdist-only", "yanked"]
)
def test_assumption_relaxes_only_wheel_tags(restriction: str) -> None:
    wheel: WheelFile | SdistFile = _foreign_wheel(
        requires_python=">=3.12" if restriction == "requires-python" else None,
        upload_time="2026-01-02T00:00:00Z" if restriction == "cutoff" else None,
    )
    if restriction == "yanked":
        wheel = mark_yanked(wheel, reason="withdrawn")
    port = make_coordinator(
        [wheel, make_sdist(upload_time="2025-01-01T00:00:00Z")],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        metadata_text=_WHEEL_METADATA,
    )
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        dist_policy=DistPolicy.SDIST_ONLY
        if restriction == "sdist-only"
        else DistPolicy.PREFER_WHEEL,
        uploaded_prior_to=(
            datetime(2026, 1, 1, tzinfo=timezone.utc)
            if restriction == "cutoff"
            else None
        ),
    )

    with pytest.raises(UnsupportedSdistError):
        provider.get_dependencies("pkg", _VERSION)

    assert port.calls_to("request_metadata") == []


def test_yanked_wheel_metadata_can_be_used_after_explicit_release_admission() -> None:
    wheel = mark_yanked(_foreign_wheel(), reason="withdrawn")
    port = make_coordinator(
        [wheel, make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        metadata_text=_WHEEL_METADATA,
    )
    provider = Provider(port, _MAC_TARGET, assume_universal_deps=True)
    provider.yank_admissions = frozenset({("pkg", _VERSION)})

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}


def test_missing_preferred_sidecar_tries_another_same_version_wheel() -> None:
    first = _foreign_wheel()
    second = make_wheel(tag="cp310-cp310-win_amd64", has_metadata=False)
    port = make_coordinator(
        [second, first, make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        range_result=RangeMetadataResult(_WHEEL_METADATA, RangeOutcome.FULL_BODY),
    )
    provider = Provider(port, _MAC_TARGET, assume_universal_deps=True)

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}
    assert port.calls_to("request_metadata") == [
        ("pkg", "1.0", first.metadata_url, first.metadata_hash)
    ]
    assert port.calls_to("request_range_metadata") == [
        ("pkg", "1.0", second.url, second.hashes)
    ]


def test_failed_range_read_tries_the_next_wheel_before_building() -> None:
    first = make_wheel(tag="cp310-cp310-win_amd64", has_metadata=False)
    second = _foreign_wheel(has_metadata=False)
    port = make_coordinator(
        [second, first, make_sdist()],
        sdist_pkg_info=_DYNAMIC_PKG_INFO,
        range_by_url={
            first.url: RangeMetadataResult(None, RangeOutcome.MISSING),
            second.url: RangeMetadataResult(_WHEEL_METADATA, RangeOutcome.PARTIAL),
        },
    )
    provider = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        build_policy=BuildPolicy.BUILD_REMOTE,
    )

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}
    assert port.calls_to("request_range_metadata") == [
        ("pkg", "1.0", first.url, first.hashes),
        ("pkg", "1.0", second.url, second.hashes),
    ]
    assert port.calls_to("request_built_metadata") == []


@pytest.mark.parametrize("mechanism", ["wheel", "build"])
def test_assumed_metadata_does_not_leak_into_strict_provider(mechanism: str) -> None:
    port = _port(_foreign_wheel()) if mechanism == "wheel" else _port()
    if mechanism == "build":
        built = parse_metadata(make_metadata("pkg", "1.0", "built-dep"))
        port = make_coordinator(
            [make_sdist()],
            sdist_pkg_info=_DYNAMIC_PKG_INFO,
            build_sdist=lambda *_args: built,
        )
    assumed = Provider(
        port,
        _MAC_TARGET,
        assume_universal_deps=True,
        build_policy=BuildPolicy.BUILD_REMOTE,
    )

    assert set(assumed.get_dependencies("pkg", _VERSION)) == {
        "wheel-dep" if mechanism == "wheel" else "built-dep"
    }
    assert (
        port.index.get_resolved_sdist_metadata(
            "pkg", sdist_artifact_key("1.0", make_sdist().url)
        )
        is None
    )

    strict = Provider(port, _MAC_TARGET, build_policy=BuildPolicy.BUILD_REMOTE)
    with pytest.raises(UnsupportedSdistError):
        strict.get_dependencies("pkg", _VERSION)


def test_release_refused_wheels_keeps_borrowed_metadata_available() -> None:
    wheel = _foreign_wheel()
    original_url = wheel.url
    provider = Provider(
        _port(wheel),
        _MAC_TARGET,
        assume_universal_deps=True,
        release_refused_wheels=True,
    )

    assert set(provider.get_dependencies("pkg", _VERSION)) == {"wheel-dep"}
    assert wheel.url == original_url
    assert all(
        isinstance(dist, SdistFile) for _, dist in provider.fetch_versions("pkg")
    )


def test_declared_build_policy_requires_matching_assumption() -> None:
    port = make_coordinator()
    strict = Provider(port, _MAC_TARGET, build_policy=BuildPolicy.BUILD_REMOTE)
    scoped = Provider(
        port,
        _MAC_TARGET,
        build_policy=BuildPolicy.BUILD_REMOTE,
        package_overrides=(pkg_override("pkg<2", assume_universal_deps=True),),
    )

    assert strict.effective_build_policy("pkg", _VERSION) is BuildPolicy.NEVER
    assert scoped.effective_build_policy("pkg", _VERSION) is BuildPolicy.BUILD_REMOTE
    assert scoped.effective_build_policy("pkg", Version("2")) is BuildPolicy.NEVER
    assert scoped.effective_build_policy_for_source("pkg") is BuildPolicy.NEVER


@pytest.mark.parametrize("assume", [False, True])
def test_unversioned_source_override_controls_cross_target_build_policy(
    *, assume: bool
) -> None:
    provider = Provider(
        make_coordinator(),
        _MAC_TARGET,
        assume_universal_deps=not assume,
        build_policy=BuildPolicy.BUILD_REMOTE,
        package_overrides=(pkg_override("pkg", assume_universal_deps=assume),),
    )

    expected = BuildPolicy.BUILD_REMOTE if assume else BuildPolicy.NEVER

    assert provider.effective_build_policy_for_source("pkg") is expected
