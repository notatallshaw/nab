"""Conditional source archives through real provider and lock construction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nab_project.inputs import ResolveInputs
from nab_project.lockfile import IndexPin, build_pylock, build_target_lock
from nab_project.resolve import build_lock_input, resolve_with_coordinator
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.overrides import IndexOverride
from nab_provider.policy import DistPolicy, MetadataPolicy
from nab_provider.provider import Provider
from nab_provider.records import RangeMetadataResult, RangeOutcome, SdistFile, WheelFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    make_coordinator,
    make_metadata,
    make_sdist,
    make_wheel,
    pkg_override,
)
from nab_provider.value import replace

_HASH = (("sha256", "a" * 64),)
_LINUX = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)
_WINDOWS = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("windows_amd64")
)


def _wheel(version: str = "1.0", tag: str = "py3-none-any") -> WheelFile:
    """A hashed wheel for the release used by these lock scenarios."""
    return make_wheel(version, package="demo", tag=tag, hashes=_HASH)


def _sdist(version: str = "1.0") -> SdistFile:
    """A hashed source archive for the same release."""
    return make_sdist(version, package="demo", hashes=_HASH)


def _lock(
    files: list[WheelFile | SdistFile],
    targets: tuple[ResolveTarget, ...] = (_LINUX, _WINDOWS),
    requirement: str = "demo",
    *,
    metadata_by_url: dict[str, str | None] | None = None,
) -> Pylock:
    """Resolve and emit the requested matrix without network or build execution."""
    coordinator = make_coordinator(
        listings={"demo": files},
        auto_metadata=True,
        metadata_by_url=metadata_by_url,
        range_by_url={
            url: RangeMetadataResult(text, RangeOutcome.PARTIAL)
            for url, text in (metadata_by_url or {}).items()
        },
        sdist_pkg_info_by_version={
            version: make_metadata("demo", version, metadata_version="2.2")
            for version in {file.version for file in files}
        },
    )
    inputs = ResolveInputs(dist_policy=DistPolicy.SDIST_IF_NEEDED)
    result = resolve_with_coordinator(
        coordinator, targets, [Requirement(requirement)], inputs=inputs
    )
    assert result.success

    pylock = build_pylock(build_lock_input(result, inputs=inputs))
    pylock.validate()
    return pylock


@pytest.mark.parametrize(
    ("tags", "needs_sdist"),
    [
        pytest.param(("py3-none-any",), False, id="universal-wheel"),
        pytest.param(
            ("cp312-cp312-linux_x86_64", "cp312-cp312-win_amd64"),
            False,
            id="platform-wheels-cover-matrix",
        ),
        pytest.param(("cp312-cp312-linux_x86_64",), True, id="windows-needs-source"),
        pytest.param((), True, id="source-only"),
    ],
)
def test_matrix_artifact_selection(tags: tuple[str, ...], needs_sdist: bool) -> None:
    lock = _lock([*(_wheel(tag=tag) for tag in tags), _sdist()])
    (package,) = lock.packages
    assert (package.sdist is not None) is needs_sdist
    assert {wheel.name for wheel in package.wheels or ()} == {
        f"demo-1.0-{tag}.whl" for tag in tags
    }

    for target in (_LINUX, _WINDOWS):
        selected = list(
            lock.select(
                environment=target.marker_env,
                tags=target.tags.ordered,
            )
        )
        assert len(selected) == 1
        _, artifact = selected[0]
        if any(target.tags.accepts(f"demo-1.0-{tag}.whl") for tag in tags):
            assert artifact.name.endswith(".whl")
        else:
            assert artifact.name.endswith(".tar.gz")


def test_only_targets_needing_the_dependency_require_wheels() -> None:
    lock = _lock(
        [_wheel(tag="cp312-cp312-win_amd64"), _sdist()],
        requirement='demo; sys_platform == "win32"',
    )
    (package,) = lock.packages
    assert package.sdist is None
    assert package.marker is not None
    assert package.marker.evaluate(_WINDOWS.marker_env)
    assert not package.marker.evaluate(_LINUX.marker_env)


def test_source_release_does_not_lose_to_older_wheel_release() -> None:
    lock = _lock([_wheel("1.0"), _sdist("2.0")])
    (package,) = lock.packages
    assert package.version == Version("2.0")
    assert package.sdist is not None
    assert not package.wheels


@pytest.mark.parametrize("requires_python", [">=3.12.4", "!=3.12.4"])
def test_python_patch_gap_keeps_source(requires_python: str) -> None:
    lock = _lock([replace(_wheel(), requires_python=requires_python), _sdist()])
    assert lock.packages[0].sdist is not None


@pytest.mark.parametrize("has_metadata", [True, False], ids=["sidecar", "wheel"])
@pytest.mark.parametrize(
    "platforms",
    [
        pytest.param(("linux_x86_64",), id="single-target"),
        pytest.param(("linux_x86_64", "windows_amd64"), id="matrix"),
    ],
)
@pytest.mark.parametrize(
    ("requires_python", "python_full_version", "needs_sdist"),
    [
        pytest.param(">=3.12.4", None, True, id="patch-floor"),
        pytest.param("!=3.12.4", None, True, id="patch-hole"),
        pytest.param(">=3.12.4", "3.12.7", False, id="covered-micro"),
        pytest.param(None, None, False, id="unrestricted-metadata"),
    ],
)
def test_metadata_python_coverage_without_index_hint(
    requires_python: str | None,
    python_full_version: str | None,
    needs_sdist: bool,
    platforms: tuple[str, ...],
    *,
    has_metadata: bool,
) -> None:
    wheel = replace(_wheel(), has_metadata=has_metadata)
    targets = tuple(
        ResolveTarget.for_declared(
            python_version="3.12",
            python_full_version=python_full_version,
            spec=PlatformSpec(platform),
        )
        for platform in platforms
    )
    metadata = make_metadata("demo", "1.0", requires_python=requires_python)
    lock = _lock(
        [wheel, replace(_sdist(), requires_python=">=3.12")],
        targets=targets,
        metadata_by_url={wheel.metadata_url or wheel.url: metadata},
    )

    (package,) = lock.packages
    assert (package.sdist is not None) is needs_sdist
    assert [file.name for file in package.wheels or ()] == [wheel.filename]


@pytest.mark.parametrize("metadata_state", ["missing", "sdist", "sibling", "invalid"])
def test_unknown_wheel_python_coverage_keeps_source(metadata_state: str) -> None:
    wheel = _wheel()
    coordinator = make_coordinator(listings={"demo": [wheel, _sdist()]})
    metadata = make_metadata("demo", "1.0")

    if metadata_state == "sdist":
        coordinator.index.store_metadata(
            "demo", "1.0", None, metadata_url=wheel.metadata_url
        )
        coordinator.index.store_sdist_metadata("demo", "1.0", metadata)
    elif metadata_state == "sibling":
        sibling = _wheel(tag="cp312-cp312-linux_x86_64")
        coordinator.index.store_metadata(
            "demo", "1.0", metadata, metadata_url=sibling.metadata_url
        )
    elif metadata_state == "invalid":
        metadata = make_metadata("demo", "1.0", requires_python="not-a-specifier")
        coordinator.index.store_metadata(
            "demo", "1.0", metadata, metadata_url=wheel.metadata_url
        )

    provider = Provider(
        coordinator, target=_LINUX, dist_policy=DistPolicy.SDIST_IF_NEEDED
    )
    provider.fetch_versions("demo")
    pin = build_target_lock(provider, _LINUX, {"demo": Version("1.0")}).pins["demo"]

    assert isinstance(pin, IndexPin)
    assert pin.sdist is not None
    assert [file.filename for file in pin.wheels] == [wheel.filename]


@pytest.mark.parametrize(
    "wheel",
    [
        pytest.param(replace(_wheel(), requires_python=">=3.13"), id="python"),
        pytest.param(
            replace(_wheel(), upload_time="2026-11-02T00:00:00Z"), id="cutoff"
        ),
    ],
)
def test_refused_wheel_does_not_remove_source(wheel: WheelFile) -> None:
    source = replace(_sdist(), upload_time="2026-10-01T00:00:00Z")
    coordinator = make_coordinator(listings={"demo": [wheel, source]})
    provider = Provider(
        coordinator,
        target=_LINUX,
        dist_policy=DistPolicy.SDIST_IF_NEEDED,
        uploaded_prior_to=(
            datetime(2026, 11, 1, tzinfo=timezone.utc)
            if wheel.upload_time is not None
            else None
        ),
    )
    provider.fetch_versions("demo")
    pin = build_target_lock(provider, _LINUX, {"demo": Version("1.0")}).pins["demo"]
    assert isinstance(pin, IndexPin)
    assert pin.sdist is not None
    assert not pin.wheels


def test_unknown_tag_compatibility_keeps_source() -> None:
    target = _LINUX.with_marker_overrides({"sys_platform": "darwin"})
    coordinator = make_coordinator(listings={"demo": [_wheel(), _sdist()]})
    provider = Provider(
        coordinator, target=target, dist_policy=DistPolicy.SDIST_IF_NEEDED
    )
    provider.fetch_versions("demo")
    pin = build_target_lock(provider, target, {"demo": Version("1.0")}).pins["demo"]
    assert isinstance(pin, IndexPin)
    assert pin.sdist is not None


def test_package_policy_keeps_other_versions_unchanged() -> None:
    coordinator = make_coordinator(
        listings={
            "demo": [
                replace(_wheel(), requires_python=">=3.12"),
                _sdist(),
                _wheel("2.0"),
                _sdist("2.0"),
            ]
        }
    )
    provider = Provider(
        coordinator,
        target=_LINUX,
        package_overrides=(
            pkg_override("demo<2", dist_policy=DistPolicy.SDIST_IF_NEEDED),
        ),
    )
    provider.fetch_versions("demo")
    first = build_target_lock(provider, _LINUX, {"demo": Version("1.0")}).pins["demo"]
    second = build_target_lock(provider, _LINUX, {"demo": Version("2.0")}).pins["demo"]
    assert isinstance(first, IndexPin)
    assert isinstance(second, IndexPin)
    assert first.sdist is None
    assert second.sdist is not None


def test_index_policy_reaches_lock_construction() -> None:
    wheel = replace(_wheel(), requires_python=">=3.12")
    coordinator = make_coordinator(listings={"demo": [wheel, _sdist()]})
    provider = Provider(
        coordinator,
        target=_LINUX,
        index_overrides={"pypi": IndexOverride(dist_policy=DistPolicy.SDIST_IF_NEEDED)},
    )
    provider.fetch_versions("demo")
    pin = build_target_lock(provider, _LINUX, {"demo": Version("1.0")}).pins["demo"]
    assert isinstance(pin, IndexPin)
    assert pin.sdist is None


def test_default_policy_keeps_source() -> None:
    coordinator = make_coordinator(listings={"demo": [_wheel(), _sdist()]})
    provider = Provider(coordinator, target=_LINUX)
    provider.fetch_versions("demo")
    pin = build_target_lock(provider, _LINUX, {"demo": Version("1.0")}).pins["demo"]
    assert isinstance(pin, IndexPin)
    assert pin.sdist is not None


def test_unused_source_does_not_need_hashes() -> None:
    lock = _lock([_wheel(), replace(_sdist(), hashes=())])
    assert lock.packages[0].sdist is None


def test_failed_coverage_proof_keeps_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exhaust_budget(*_args: object, **_kwargs: object) -> bool:
        """Simulate exhaustion during version-range comparison."""
        raise RecursionError("coverage comparison")

    wheel = replace(_wheel(), requires_python=">=3.10")
    coordinator = make_coordinator(listings={"demo": [wheel, _sdist()]})
    provider = Provider(
        coordinator, target=_LINUX, dist_policy=DistPolicy.SDIST_IF_NEEDED
    )
    provider.fetch_versions("demo")
    monkeypatch.setattr(VersionRange, "is_subset", exhaust_budget)
    pin = build_target_lock(provider, _LINUX, {"demo": Version("1.0")}).pins["demo"]
    assert isinstance(pin, IndexPin)
    assert pin.sdist is not None


def test_different_versions_keep_their_own_coverage() -> None:
    older = [
        replace(file, requires_python=">=3.11,<3.12")
        for file in (_wheel("1.0"), _sdist("1.0"))
    ]
    newer = replace(_sdist("2.0"), requires_python=">=3.12")
    python311 = ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    )
    lock = _lock([*older, newer], targets=(python311, _LINUX))
    by_version = {str(package.version): package for package in lock.packages}
    assert by_version["1.0"].sdist is None
    assert by_version["2.0"].sdist is not None


@pytest.mark.parametrize("listing_hint", [None, ">=3.13"])
@pytest.mark.parametrize("override", ["", ">=3.12"])
def test_python_override_participates_in_wheel_coverage(
    listing_hint: str | None, override: str
) -> None:
    coordinator = make_coordinator(
        listings={"demo": [replace(_wheel(), requires_python=listing_hint), _sdist()]}
    )
    provider = Provider(
        coordinator,
        target=_LINUX,
        dist_policy=DistPolicy.SDIST_IF_NEEDED,
        package_overrides=(pkg_override("demo", requires_python=override),),
    )
    provider.fetch_versions("demo")
    pin = build_target_lock(provider, _LINUX, {"demo": Version("1.0")}).pins["demo"]
    assert isinstance(pin, IndexPin)
    assert pin.sdist is None
    assert pin.requires_python == override


@pytest.mark.parametrize(
    ("metadata_policy", "dependency"),
    [
        (MetadataPolicy.MATCH_ARTIFACT, "source-dep"),
        (MetadataPolicy.PREFER_WHEEL, "wheel-dep"),
    ],
)
def test_metadata_policy_reaches_the_resolve_and_preserves_source_installation(
    metadata_policy: MetadataPolicy, dependency: str
) -> None:
    wheel = _wheel()
    children = {
        name: make_wheel(package=name.replace("-", "_"), hashes=_HASH)
        for name in ("source-dep", "wheel-dep")
    }
    port = make_coordinator(
        listings={
            "demo": [wheel, _sdist()],
            **{name: [file] for name, file in children.items()},
        },
        metadata_by_url={
            wheel.metadata_url: make_metadata("demo", "1.0", "wheel-dep>=1"),
            **{
                file.metadata_url: make_metadata(name, "1.0")
                for name, file in children.items()
            },
        },
        sdist_pkg_info=make_metadata(
            "demo", "1.0", "source-dep>=1", metadata_version="2.2"
        ),
    )
    inputs = ResolveInputs(
        metadata_policy=metadata_policy,
        package_overrides=(pkg_override("demo", dist_policy=DistPolicy.SDIST_ONLY),),
    )
    result = resolve_with_coordinator(
        port, (_LINUX,), [Requirement("demo")], inputs=inputs
    )
    assert result.success
    assert set(result.target_results[0].pins) == {"demo", dependency}
    lock = build_pylock(build_lock_input(result, inputs=inputs))
    demo = next(package for package in lock.packages if package.name == "demo")
    assert demo.sdist is not None
    assert not demo.wheels
