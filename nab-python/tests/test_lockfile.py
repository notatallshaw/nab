"""Tests for nab_python.lockfile (PEP 751 emission)."""

from __future__ import annotations

import itertools
import logging
import random
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import IndexConfig
from nab_python._lockfile.builder import _common_requires_python
from nab_python._lockfile.disjointness import (
    validate_marker_disjointness,
)
from nab_python._lockfile.pylock import (
    _or_markers,
    _pin_discriminator,
    _pin_to_package,
    _relativize_path,
)
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._testing.overrides import pkg_override
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.markersets import IntractableMarkerSet, MarkerSet
from nab_python._vendor.packaging.pylock import Package, PackageWheel, Pylock
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
    NabProjectConfig,
    conflict_exclusion_groups,
    conflict_member_groups,
)
from nab_python.lockfile import (
    LOCK_VERSION,
    ArchivePin,
    DisjointnessError,
    DivergentBaseDependencyError,
    IndexPin,
    LocalPin,
    LockInput,
    MissingHashError,
    MissingSdistError,
    MissingVcsCommitError,
    PinShape,
    Provenance,
    SdistArtifact,
    TargetLock,
    VcsPin,
    WheelArtifact,
    build_pylock,
    build_target_lock,
    drop_workspace_pins,
    package_metadata_override_records,
    read_lockfile_anchor,
    read_lockfile_packages,
    write_lock,
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from nab_python.provider import (
    ArchiveSource,
    BuildPolicy,
    DistPolicy,
    LocalSource,
    Provider,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_python.resolve import (
    ResolveResult,
    TargetResult,
    build_lock_input,
    resolve_with_coordinator,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget


def _target(
    python_version: str = "3.11", platform: str = "linux_x86_64"
) -> ResolveTarget:
    """One declared (python, platform) target of a resolve."""
    return ResolveTarget.for_declared(
        python_version=python_version, spec=PlatformSpec(platform)
    )


# A resolve always runs against at least one target, so one entry is the
# smallest lock there is; its packages carry no marker.
_HOST = _target()


def _one(
    pins: Mapping[str, PinShape],
    dependencies: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, TargetLock]:
    """Return the one-target map a single-environment resolve produces."""
    return {
        _HOST.label: TargetLock(
            target=_HOST, pins=dict(pins), dependencies=dict(dependencies or {})
        )
    }


def _targets(
    *entries: tuple[ResolveTarget, Mapping[str, PinShape]],
) -> dict[str, TargetLock]:
    """Return the per-target map for the given ``(target, pins)`` pairs."""
    return {
        target.label: TargetLock(target=target, pins=dict(pins))
        for target, pins in entries
    }


def _lock_from(target_lock: TargetLock) -> LockInput:
    """Wrap one target's contribution as the lock its resolve would write."""
    return LockInput(targets={target_lock.target.label: target_lock})


def _env_signature(target: ResolveTarget) -> tuple[tuple[str, str], ...]:
    """Return the ``env_base_names`` key for ``target``'s environment."""
    return tuple(sorted(target.marker_env.items()))


def _wheel(name: str = "foo", version: str = "1.0") -> WheelArtifact:
    return WheelArtifact(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}-py3-none-any.whl",
        hashes=(("sha256", "a" * 64),),
        size=1024,
    )


def _sdist(name: str = "foo", version: str = "1.0") -> SdistArtifact:
    return SdistArtifact(
        filename=f"{name}-{version}.tar.gz",
        url=f"https://example.com/{name}-{version}.tar.gz",
        hashes=(("sha256", "b" * 64),),
        size=2048,
    )


def _index_pin(
    name: str = "foo",
    version: str = "1.0",
    index: str = "pypi",
) -> IndexPin:
    return IndexPin(
        name=name,
        version=version,
        index=index,
        sdist=_sdist(name, version),
        wheels=(_wheel(name, version),),
        requires_python=">=3.10",
    )


class TestSingleTarget:
    def test_index_pin_round_trips(self) -> None:
        text = write_lock(LockInput(targets=_one({"foo": _index_pin()})))
        data = tomllib.loads(text)
        assert data["lock-version"] == LOCK_VERSION
        assert data["created-by"] == "nab"
        assert len(data["packages"]) == 1
        package = data["packages"][0]
        assert package["name"] == "foo"
        assert package["version"] == "1.0"
        assert package["index"] == "pypi"
        assert len(package["wheels"]) == 1
        assert package["sdist"]["url"].endswith("foo-1.0.tar.gz")

    def test_local_pin_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "libs" / "foo"
        text = write_lock(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(src))}
                )
            ),
            output_path=tmp_path / "pylock.toml",
        )
        data = tomllib.loads(text)
        package = data["packages"][0]
        # PEP 751: directory.path is relative to the lock file.
        assert package["directory"]["path"] == "libs/foo"
        assert "wheels" not in package
        assert "sdist" not in package
        # PEP 751: version omitted for directory sources (not deterministic).
        assert "version" not in package

    def test_vcs_pin(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": VcsPin(
                            name="foo",
                            version="1.0",
                            repo_url="https://github.com/x/y.git",
                            bare_repo_url="https://github.com/x/y.git",
                            commit_id="a" * 40,
                            subdirectory="pkg",
                        ),
                    }
                )
            )
        )
        data = tomllib.loads(text)
        package = data["packages"][0]
        vcs = package["vcs"]
        assert vcs["type"] == "git"
        assert vcs["url"] == "https://github.com/x/y.git"
        assert vcs["commit-id"] == "a" * 40
        assert vcs["subdirectory"] == "pkg"
        # PEP 751: version omitted for VCS sources (not deterministic).
        assert "version" not in package

    def test_vcs_pin_non_git_type(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": VcsPin(
                            name="foo",
                            version="1.0",
                            repo_url="https://example.com/x/y",
                            bare_repo_url="https://example.com/x/y",
                            commit_id="a" * 40,
                            vcs_type="hg",
                        ),
                    }
                )
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["vcs"]["type"] == "hg"

    def test_archive_pin(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": ArchivePin(
                            name="foo",
                            version="1.0",
                            url="https://ex.com/foo-1.0.tar.gz",
                            hashes=(("sha256", "e" * 64),),
                            subdirectory="pkg",
                        ),
                    }
                )
            )
        )
        data = tomllib.loads(text)
        package = data["packages"][0]
        archive = package["archive"]
        assert archive["url"] == "https://ex.com/foo-1.0.tar.gz"
        assert archive["hashes"] == {"sha256": "e" * 64}
        assert archive["subdirectory"] == "pkg"
        # An archive is content-pinned, so unlike vcs/directory it keeps a version.
        assert package["version"] == "1.0"

    def test_archive_pin_requirements_line(self) -> None:
        pins = {
            "foo": ArchivePin(
                name="foo",
                version="1.0",
                url="https://ex.com/foo-1.0.tar.gz",
                hashes=(("sha256", "e" * 64),),
                subdirectory="pkg",
            ),
        }
        text = write_requirements_with_hashes(LockInput(targets=_one(pins)))
        assert (
            "foo @ https://ex.com/foo-1.0.tar.gz#sha256="
            + "e" * 64
            + "&subdirectory=pkg"
        ) in text

    def test_archive_pin_requirements_line_no_subdirectory(self) -> None:
        pins = {
            "foo": ArchivePin(
                name="foo",
                version="1.0",
                url="https://ex.com/foo-1.0.tar.gz",
                hashes=(("sha256", "e" * 64),),
            ),
        }
        text = write_requirements_with_hashes(LockInput(targets=_one(pins)))
        assert "foo @ https://ex.com/foo-1.0.tar.gz#sha256=" + "e" * 64 in text
        assert "subdirectory" not in text

    def test_multiple_packages_sorted_by_name(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": _index_pin("foo"),
                        "bar": _index_pin("bar"),
                        "baz": _index_pin("baz"),
                    }
                )
            )
        )
        data = tomllib.loads(text)
        names = [p["name"] for p in data["packages"]]
        assert names == ["bar", "baz", "foo"]

    def test_writes_to_file(self, tmp_path: Path) -> None:
        out = tmp_path / "pylock.toml"
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()})),
            output_path=out,
        )
        assert out.read_text(encoding="utf-8") == text

    def test_extras_canonicalised(self) -> None:
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()}), extras=("My-Extra",))
        )
        data = tomllib.loads(text)
        assert "my-extra" in data["extras"]

    def test_canonicalises_package_name(self) -> None:
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin(name="Foo_Bar")})),
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["name"] == "foo-bar"


class TestPerTargetMarkerSimplification:
    def test_all_targets_agree_no_marker(self) -> None:
        text = write_lock(
            LockInput(
                targets=_targets(
                    (_target("3.10"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.11"), {"foo": _index_pin(version="1.0")}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert "marker" not in data["packages"][0]

    def test_diverging_pins_get_markers(self) -> None:
        text = write_lock(
            LockInput(
                targets=_targets(
                    (_target("3.10"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.11"), {"foo": _index_pin(version="2.0")}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2
        markers = [p.get("marker") for p in data["packages"]]
        assert all(m is not None for m in markers)
        assert any('python_version == "3.10"' in m for m in markers)
        assert any('python_version == "3.11"' in m for m in markers)

    def test_three_targets_two_groups(self) -> None:
        # 3.10 + 3.11 share v1.0; 3.12 has v2.0 -> two groups
        text = write_lock(
            LockInput(
                targets=_targets(
                    (_target("3.10"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.11"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.12"), {"foo": _index_pin(version="2.0")}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2
        # The v1.0 group has an OR marker; the v2.0 group has a single marker
        v1_pkg = next(p for p in data["packages"] if p["version"] == "1.0")
        v2_pkg = next(p for p in data["packages"] if p["version"] == "2.0")
        assert " or " in v1_pkg["marker"]
        assert " or " not in v2_pkg["marker"]

    def test_local_pin_per_target(self) -> None:
        # LocalPin discriminator coverage
        text = write_lock(
            LockInput(
                targets=_targets(
                    (
                        _target("3.10"),
                        {"foo": LocalPin(name="foo", version="1.0", path="/a")},
                    ),
                    (
                        _target("3.11"),
                        {"foo": LocalPin(name="foo", version="1.0", path="/b")},
                    ),
                )
            )
        )
        data = tomllib.loads(text)
        # Different paths -> different groups -> two packages
        assert len(data["packages"]) == 2

    def test_target_specific_wheels_merged_within_group(self) -> None:
        """Two targets sharing version/index keep both targets' wheel filenames."""
        linux_wheel = WheelArtifact(
            filename="foo-1.0-cp310-cp310-linux_x86_64.whl",
            url="https://example.com/foo-1.0-cp310-cp310-linux_x86_64.whl",
            hashes=(("sha256", "c" * 64),),
            size=1024,
        )
        macos_wheel = WheelArtifact(
            filename="foo-1.0-cp310-cp310-macosx_11_0_arm64.whl",
            url="https://example.com/foo-1.0-cp310-cp310-macosx_11_0_arm64.whl",
            hashes=(("sha256", "d" * 64),),
            size=1024,
        )
        text = write_lock(
            LockInput(
                targets=_targets(
                    (
                        _target(platform="linux_x86_64"),
                        {
                            "foo": IndexPin(
                                name="foo",
                                version="1.0",
                                index="pypi",
                                wheels=(linux_wheel,),
                            )
                        },
                    ),
                    (
                        _target(platform="macos_arm64"),
                        {
                            "foo": IndexPin(
                                name="foo",
                                version="1.0",
                                index="pypi",
                                wheels=(macos_wheel,),
                            )
                        },
                    ),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        wheel_names = sorted(w["name"] for w in data["packages"][0]["wheels"])
        assert wheel_names == [
            "foo-1.0-cp310-cp310-linux_x86_64.whl",
            "foo-1.0-cp310-cp310-macosx_11_0_arm64.whl",
        ]

    def test_requires_python_drops_when_targets_disagree(self) -> None:
        """``requires_python`` survives merging only when every target agrees."""
        wheel = _wheel()
        text = write_lock(
            LockInput(
                targets=_targets(
                    (
                        _target("3.10"),
                        {
                            "foo": IndexPin(
                                name="foo",
                                version="1.0",
                                index="pypi",
                                wheels=(wheel,),
                                requires_python=">=3.10",
                            )
                        },
                    ),
                    (
                        _target("3.11"),
                        {
                            "foo": IndexPin(
                                name="foo",
                                version="1.0",
                                index="pypi",
                                wheels=(wheel,),
                                requires_python=">=3.11",
                            )
                        },
                    ),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert "requires-python" not in data["packages"][0]

    def test_sdist_filled_from_any_target(self) -> None:
        """An sdist appearing in only some targets is preserved on the merge."""
        sdist = _sdist()
        wheel = _wheel()
        text = write_lock(
            LockInput(
                targets=_targets(
                    (
                        _target("3.10"),
                        {
                            "foo": IndexPin(
                                name="foo",
                                version="1.0",
                                index="pypi",
                                sdist=None,
                                wheels=(wheel,),
                            )
                        },
                    ),
                    (
                        _target("3.11"),
                        {
                            "foo": IndexPin(
                                name="foo",
                                version="1.0",
                                index="pypi",
                                sdist=sdist,
                                wheels=(wheel,),
                            )
                        },
                    ),
                )
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["sdist"]["url"].endswith("foo-1.0.tar.gz")

    def test_vcs_pin_per_target(self) -> None:
        # VcsPin discriminator coverage
        pin = VcsPin(
            name="foo",
            version="1.0",
            repo_url="https://x/y.git",
            bare_repo_url="https://x/y.git",
            commit_id="a" * 40,
        )
        text = write_lock(
            LockInput(
                targets=_targets(
                    (_target("3.10"), {"foo": pin}),
                    (_target("3.11"), {"foo": pin}),
                )
            )
        )
        data = tomllib.loads(text)
        # Same VCS commit -> single group -> one package
        assert len(data["packages"]) == 1

    def test_archive_pin_per_target(self) -> None:
        # ArchivePin merged across targets: same archive -> one group -> one package.
        pin = ArchivePin(
            name="foo",
            version="1.0",
            url="https://ex.com/foo-1.0.tar.gz",
            hashes=(("sha256", "e" * 64),),
        )
        text = write_lock(
            LockInput(
                targets=_targets(
                    (_target("3.10"), {"foo": pin}),
                    (_target("3.11"), {"foo": pin}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert data["packages"][0]["archive"]["url"] == "https://ex.com/foo-1.0.tar.gz"

    def test_vcs_pin_per_target_diverging_commit(self) -> None:
        # Diverging commit -> two groups -> two packages
        text = write_lock(
            LockInput(
                targets=_targets(
                    (
                        _target("3.10"),
                        {
                            "foo": VcsPin(
                                name="foo",
                                version="1.0",
                                repo_url="https://x/y.git",
                                bare_repo_url="https://x/y.git",
                                commit_id="a" * 40,
                            )
                        },
                    ),
                    (
                        _target("3.11"),
                        {
                            "foo": VcsPin(
                                name="foo",
                                version="1.0",
                                repo_url="https://x/y.git",
                                bare_repo_url="https://x/y.git",
                                commit_id="b" * 40,
                            )
                        },
                    ),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2
        commits = {p["vcs"]["commit-id"] for p in data["packages"]}
        assert commits == {"a" * 40, "b" * 40}

    def test_discriminator_separates_vcs_commit(self) -> None:
        one = VcsPin(
            name="foo",
            version="1.0",
            repo_url="https://x/y.git",
            bare_repo_url="https://x/y.git",
            commit_id="a" * 40,
        )
        other = VcsPin(
            name="foo",
            version="1.0",
            repo_url="https://x/y.git",
            bare_repo_url="https://x/y.git",
            commit_id="b" * 40,
        )
        assert _pin_discriminator(one) != _pin_discriminator(other)

    def test_discriminator_separates_vcs_subdirectory(self) -> None:
        plain = VcsPin(
            name="foo",
            version="1.0",
            repo_url="https://x/y.git",
            bare_repo_url="https://x/y.git",
            commit_id="a" * 40,
        )
        sub = VcsPin(
            name="foo",
            version="1.0",
            repo_url="https://x/y.git",
            bare_repo_url="https://x/y.git",
            commit_id="a" * 40,
            subdirectory="pkg",
        )
        assert _pin_discriminator(plain) != _pin_discriminator(sub)

    def test_package_in_only_one_of_two_targets_gets_marker(self) -> None:
        # ``foo`` resolves on py3.10 only, so its entry is gated on that
        # target's marker rather than emitted unconditionally.
        py310 = _target("3.10")
        text = write_lock(
            LockInput(
                targets=_targets(
                    (py310, {"foo": _index_pin(version="1.0")}),
                    (_target("3.11"), {}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert data["packages"][0]["marker"] == (
            'platform_machine == "x86_64" and python_version == "3.10"'
            ' and sys_platform == "linux"'
        )

    def test_package_in_two_of_four_targets_gets_or_marker(self) -> None:
        # ``foo`` resolves on py3.10 and py3.11 only with the same pin.
        # The marker is the OR of those two targets' markers.
        text = write_lock(
            LockInput(
                targets=_targets(
                    (_target("3.10"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.11"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.12"), {}),
                    (_target("3.13"), {}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        marker = data["packages"][0]["marker"]
        assert 'python_version == "3.10"' in marker
        assert 'python_version == "3.11"' in marker
        assert 'python_version == "3.12"' not in marker
        assert 'python_version == "3.13"' not in marker
        assert " or " in marker

    def test_package_in_all_four_targets_same_version_no_marker(self) -> None:
        # Regression check: the "all four agree on a single version" path
        # stays unmarkered.
        text = write_lock(
            LockInput(
                targets=_targets(
                    (_target("3.10"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.11"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.12"), {"foo": _index_pin(version="1.0")}),
                    (_target("3.13"), {"foo": _index_pin(version="1.0")}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert "marker" not in data["packages"][0]

    def test_package_in_three_of_four_with_two_versions(self) -> None:
        # ``foo`` resolves on py3.10 (v1) and py3.11+py3.12 (v2); absent
        # from py3.13.  Two groups; both must be marker-gated, and
        # neither group should claim py3.13.
        py310 = _target("3.10")
        text = write_lock(
            LockInput(
                targets=_targets(
                    (py310, {"foo": _index_pin(version="1.0")}),
                    (_target("3.11"), {"foo": _index_pin(version="2.0")}),
                    (_target("3.12"), {"foo": _index_pin(version="2.0")}),
                    (_target("3.13"), {}),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2
        v1_marker = next(p["marker"] for p in data["packages"] if p["version"] == "1.0")
        v2_marker = next(p["marker"] for p in data["packages"] if p["version"] == "2.0")
        assert v1_marker == (
            'platform_machine == "x86_64" and python_version == "3.10"'
            ' and sys_platform == "linux"'
        )
        assert 'python_version == "3.11"' in v2_marker
        assert 'python_version == "3.12"' in v2_marker
        assert 'python_version == "3.13"' not in v2_marker


class TestConflictForkBaseDepMarkers:
    """A base dep present in every fork of an environment drops the
    conflict-fork membership clause, so it installs even when no member
    is selected (the env-conditional-base-dep regression)."""

    _LINUX: ClassVar[ResolveTarget] = _target(platform="linux_x86_64")
    _WIN: ClassVar[ResolveTarget] = _target(platform="windows_amd64")
    _CPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "cpu"),)
    _GPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "gpu"),)

    def _lock_input(self) -> LockInput:
        # ``tensorrt`` is a member-only dep: required by every fork but
        # absent from the base resolve, so its membership clause must
        # survive even though it appears in both linux forks at the same
        # version.  Without ``env_base_names`` the writer cannot tell
        # this case apart from a true base dep.
        targets = _targets(
            (
                self._LINUX.with_selection(self._CPU),
                {
                    "basepkg": _index_pin(name="basepkg", version="1.0"),
                    "tensorrt": _index_pin(name="tensorrt", version="1.0"),
                    "torch": _index_pin(name="torch", version="2.0+cpu"),
                    "universal": _index_pin(name="universal", version="9.0"),
                },
            ),
            (
                self._LINUX.with_selection(self._GPU),
                {
                    "basepkg": _index_pin(name="basepkg", version="1.0"),
                    "tensorrt": _index_pin(name="tensorrt", version="1.0"),
                    "torch": _index_pin(name="torch", version="2.0+gpu"),
                    "universal": _index_pin(name="universal", version="9.0"),
                },
            ),
            (
                self._WIN.with_selection(self._CPU),
                {
                    "torch": _index_pin(name="torch", version="2.0+cpu"),
                    "universal": _index_pin(name="universal", version="9.0"),
                },
            ),
            (
                self._WIN.with_selection(self._GPU),
                {
                    "torch": _index_pin(name="torch", version="2.0+gpu"),
                    "universal": _index_pin(name="universal", version="9.0"),
                },
            ),
        )

        # Mirror the resolver shape: a base pass ran for both envs and
        # told the writer which deps install regardless of which member
        # is selected.  ``tensorrt`` is intentionally absent.
        env_base_names = {
            _env_signature(self._LINUX): frozenset({"basepkg", "universal"}),
            _env_signature(self._WIN): frozenset({"universal"}),
        }

        conflicts = (
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "cpu"),
                    ConflictMember(ConflictKind.EXTRA, "gpu"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
        )
        return LockInput(
            targets=targets,
            env_base_names=env_base_names,
            extras=("cpu", "gpu"),
            conflicts=conflicts,
        )

    def test_base_dep_drops_membership_clause(self) -> None:
        pylock = build_pylock(self._lock_input())
        by_name = {str(p.name): p for p in pylock.packages}

        base_marker = by_name["basepkg"].marker
        assert base_marker is not None
        # The bug ORs the per-fork membership markers, which is False on
        # linux with no extras.  The fix emits the env-only marker.
        assert base_marker.evaluate(self._LINUX.env_with_membership())
        assert not base_marker.evaluate(self._WIN.env_with_membership())
        assert "in extras" not in str(base_marker)

    def test_conflicting_dep_keeps_membership_markers(self) -> None:
        pylock = build_pylock(self._lock_input())
        torch = sorted(
            (p for p in pylock.packages if str(p.name) == "torch"),
            key=lambda p: str(p.version),
        )
        cpu = next(p for p in torch if str(p.version) == "2.0+cpu")
        gpu = next(p for p in torch if str(p.version) == "2.0+gpu")
        assert '"cpu" in extras' in str(cpu.marker)
        assert '"gpu" in extras' in str(gpu.marker)

    def test_member_only_dep_present_in_every_fork_keeps_membership(self) -> None:
        """A dep required by every fork but absent from the base resolve
        keeps its membership OR, so it does not install when no member
        is selected (at_most_one permits zero)."""
        pylock = build_pylock(self._lock_input())
        tensorrt = next(p for p in pylock.packages if str(p.name) == "tensorrt")
        marker = tensorrt.marker
        assert marker is not None
        assert '"cpu" in extras' in str(marker)
        assert '"gpu" in extras' in str(marker)
        assert not marker.evaluate(self._LINUX.env_with_membership())
        assert marker.evaluate(
            {**self._LINUX.env_with_membership(), "extras": frozenset({"cpu"})}
        )

    def test_fully_universal_dep_has_no_marker(self) -> None:
        pylock = build_pylock(self._lock_input())
        universal = next(p for p in pylock.packages if str(p.name) == "universal")
        assert universal.marker is None


class TestConflictForkGateMerge:
    """Forks of one environment merge their gates on a collapsing entry.

    A base dep present in every fork drops its membership clause, so
    only the selections that reach it still gate the entry, and those
    differ per fork: a fork reaches it through its own member as well as
    through the non-conflicting selections every fork shares.
    """

    _LINUX: ClassVar[ResolveTarget] = _target()
    _CPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "cpu"),)
    _GPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "gpu"),)

    def _marker(
        self,
        cpu_gate: tuple[tuple[str, str], ...],
        gpu_gate: tuple[tuple[str, str], ...],
    ) -> Marker | None:
        pins: dict[str, PinShape] = {"shared": _index_pin(name="shared", version="1.0")}
        cpu = self._LINUX.with_selection(self._CPU)
        gpu = self._LINUX.with_selection(self._GPU)
        lock_input = LockInput(
            targets={
                cpu.label: TargetLock(
                    target=cpu, pins=dict(pins), package_gates={"shared": cpu_gate}
                ),
                gpu.label: TargetLock(
                    target=gpu, pins=dict(pins), package_gates={"shared": gpu_gate}
                ),
            },
            env_base_names={_env_signature(self._LINUX): frozenset({"shared"})},
            extras=("cpu", "gpu", "docs"),
            conflicts=(
                ConflictSet(
                    members=(
                        ConflictMember(ConflictKind.EXTRA, "cpu"),
                        ConflictMember(ConflictKind.EXTRA, "gpu"),
                    ),
                    policy=ConflictPolicy.AT_MOST_ONE,
                ),
            ),
        )
        pylock = build_pylock(lock_input)
        shared = next(p for p in pylock.packages if str(p.name) == "shared")
        return shared.marker

    def test_own_member_disjoins_with_the_shared_selection(self) -> None:
        """cpu and docs both reach it; either one on its own installs it."""
        marker = self._marker(
            (("extra", "cpu"), ("extra", "docs")), (("extra", "docs"),)
        )
        assert str(marker) == '"cpu" in extras or "docs" in extras'

    def test_an_ungated_fork_leaves_the_entry_unconditional(self) -> None:
        """The cpu fork's own dependencies reach it, so no gate holds."""
        assert self._marker((), (("extra", "gpu"),)) is None


class TestConflictForkBaseDepDivergence:
    """A base dep pinned differently across the conflict forks of one
    environment cannot drop the membership clause on any entry, so no
    entry would fire when no member is selected (at_most_one permits
    zero).  The writer raises instead of emitting a lock that silently
    skips the dependency."""

    _LINUX: ClassVar[ResolveTarget] = _target()
    _CPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "cpu"),)
    _GPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "gpu"),)

    def _lock_input(
        self,
        *,
        base_names: frozenset[str] = frozenset({"shared"}),
        gpu_has_shared: bool = True,
    ) -> LockInput:
        cpu_pins: dict[str, PinShape] = {
            "shared": _index_pin(name="shared", version="1.0")
        }
        gpu_pins: dict[str, PinShape] = {
            "shared": _index_pin(name="shared", version="2.0")
        }
        if not gpu_has_shared:
            del gpu_pins["shared"]
        return LockInput(
            targets=_targets(
                (self._LINUX.with_selection(self._CPU), cpu_pins),
                (self._LINUX.with_selection(self._GPU), gpu_pins),
            ),
            env_base_names={_env_signature(self._LINUX): base_names},
            extras=("cpu", "gpu"),
            conflicts=(
                ConflictSet(
                    members=(
                        ConflictMember(ConflictKind.EXTRA, "cpu"),
                        ConflictMember(ConflictKind.EXTRA, "gpu"),
                    ),
                    policy=ConflictPolicy.AT_MOST_ONE,
                ),
            ),
        )

    def test_divergent_base_dep_raises_with_per_fork_pins(self) -> None:
        cpu = self._LINUX.with_selection(self._CPU)
        gpu = self._LINUX.with_selection(self._GPU)
        with pytest.raises(DivergentBaseDependencyError) as info:
            build_pylock(self._lock_input())
        message = str(info.value)
        assert "shared" in message
        assert f"{cpu.label} -> 1.0" in message
        assert f"{gpu.label} -> 2.0" in message

    def test_write_lock_raises_and_writes_nothing(self, tmp_path: Path) -> None:
        target = tmp_path / "pylock.toml"
        with pytest.raises(DivergentBaseDependencyError):
            write_lock(self._lock_input(), output_path=target)
        assert not target.exists()

    def test_divergent_member_only_dep_keeps_membership_entries(self) -> None:
        """The same divergence on a dep the base pass did not pin is
        representable: each entry keeps its membership clause and the
        no-member context correctly installs neither."""
        pylock = build_pylock(self._lock_input(base_names=frozenset()))
        markers = sorted(
            str(p.marker) for p in pylock.packages if str(p.name) == "shared"
        )
        assert len(markers) == 2
        assert '"cpu" in extras' in markers[0]
        assert '"gpu" in extras' in markers[1]

    def test_base_dep_missing_from_one_fork_keeps_membership(self) -> None:
        """A base dep absent from one fork is outside the env-only
        collapse (it needs presence in every fork), so the present
        fork's entry keeps its membership clause and nothing raises."""
        pylock = build_pylock(self._lock_input(gpu_has_shared=False))
        shared = next(p for p in pylock.packages if str(p.name) == "shared")
        assert '"cpu" in extras' in str(shared.marker)


class TestConflictForkBaseDepDivergentClosure:
    """A base dep is agreed across the conflict forks, but the forks pin
    it lower than the independent base pass, so its emitted transitive
    dep is absent from ``env_base_names``.  That dep installs whenever the
    unconditional base dep does, so it must drop its membership clause
    too, or the no-member context installs the base dep without a
    dependency it declares."""

    _LINUX: ClassVar[ResolveTarget] = _target()
    _CPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "cpu"),)
    _GPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "gpu"),)

    def _lock_input(self) -> LockInput:
        # Both forks pin foo==2.0 (foo -> baz) and baz==1.0.  The base
        # pass independently resolved foo==3.0 (foo -> bar), so
        # ``env_base_names`` names foo and bar but never baz.
        fork_pins: dict[str, PinShape] = {
            "foo": _index_pin(name="foo", version="2.0"),
            "baz": _index_pin(name="baz", version="1.0"),
        }
        fork_deps = {"foo": ("baz",)}
        return self._lock_input_with(
            cpu_pins=fork_pins,
            cpu_deps=fork_deps,
            cpu_base_deps=fork_deps,
            gpu_pins=fork_pins,
            gpu_deps=fork_deps,
            gpu_base_deps=fork_deps,
            base_names=frozenset({"foo", "bar"}),
        )

    def test_transitive_dep_of_base_dep_installs_with_no_member(self) -> None:
        pylock = build_pylock(self._lock_input())
        by_name = {str(p.name): p for p in pylock.packages}
        no_member = self._LINUX.env_with_membership()

        foo = by_name["foo"]
        assert foo.marker is None or foo.marker.evaluate(no_member)

        baz = by_name["baz"]
        assert "in extras" not in str(baz.marker)
        assert baz.marker is None or baz.marker.evaluate(no_member)

    def _lock_input_with(
        self,
        cpu_pins: Mapping[str, PinShape],
        cpu_deps: Mapping[str, tuple[str, ...]],
        cpu_base_deps: Mapping[str, tuple[str, ...]],
        gpu_pins: Mapping[str, PinShape],
        gpu_deps: Mapping[str, tuple[str, ...]],
        gpu_base_deps: Mapping[str, tuple[str, ...]],
        base_names: frozenset[str],
    ) -> LockInput:
        cpu = self._LINUX.with_selection(self._CPU)
        gpu = self._LINUX.with_selection(self._GPU)
        return LockInput(
            targets={
                cpu.label: TargetLock(
                    target=cpu,
                    pins=dict(cpu_pins),
                    dependencies=dict(cpu_deps),
                    base_dependencies=dict(cpu_base_deps),
                ),
                gpu.label: TargetLock(
                    target=gpu,
                    pins=dict(gpu_pins),
                    dependencies=dict(gpu_deps),
                    base_dependencies=dict(gpu_base_deps),
                ),
            },
            env_base_names={_env_signature(self._LINUX): base_names},
            extras=("cpu", "gpu"),
            conflicts=(
                ConflictSet(
                    members=(
                        ConflictMember(ConflictKind.EXTRA, "cpu"),
                        ConflictMember(ConflictKind.EXTRA, "gpu"),
                    ),
                    policy=ConflictPolicy.AT_MOST_ONE,
                ),
            ),
        )

    def test_shared_extra_activated_dep_stays_member_gated(self) -> None:
        # foo is the only base dep; every fork installs it as plain foo.
        # Both members activate foo[telemetry], so ``telemetrylib`` is a
        # foo -> telemetrylib edge in every fork's full graph, but it is
        # extra-activated (absent from ``base_dependencies``).  It must
        # not be promoted to base: plain foo does not pull it in the
        # no-member context.
        cpu_pins: dict[str, PinShape] = {
            "foo": _index_pin(name="foo", version="1.0"),
            "telemetrylib": _index_pin(name="telemetrylib", version="1.0"),
            "torch-cpu": _index_pin(name="torch-cpu", version="1.0"),
        }
        gpu_pins: dict[str, PinShape] = {
            "foo": _index_pin(name="foo", version="1.0"),
            "telemetrylib": _index_pin(name="telemetrylib", version="1.0"),
            "torch-gpu": _index_pin(name="torch-gpu", version="1.0"),
        }
        lock_input = self._lock_input_with(
            cpu_pins=cpu_pins,
            cpu_deps={"foo": ("telemetrylib",)},
            cpu_base_deps={},
            gpu_pins=gpu_pins,
            gpu_deps={"foo": ("telemetrylib",)},
            gpu_base_deps={},
            base_names=frozenset({"foo"}),
        )
        pylock = build_pylock(lock_input)
        by_name = {str(p.name): p for p in pylock.packages}

        assert '"cpu" in extras' in str(by_name["telemetrylib"].marker)

    def test_divergent_extra_activated_dep_emits_cleanly(self) -> None:
        # Same shape, but the extra-activated telemetrylib pins diverge
        # across the forks.  It is member-gated, not base, so the two
        # entries stay disjoint and no DivergentBaseDependencyError fires.
        cpu_pins: dict[str, PinShape] = {
            "foo": _index_pin(name="foo", version="1.0"),
            "telemetrylib": _index_pin(name="telemetrylib", version="1.0"),
        }
        gpu_pins: dict[str, PinShape] = {
            "foo": _index_pin(name="foo", version="1.0"),
            "telemetrylib": _index_pin(name="telemetrylib", version="2.0"),
        }
        lock_input = self._lock_input_with(
            cpu_pins=cpu_pins,
            cpu_deps={"foo": ("telemetrylib",)},
            cpu_base_deps={},
            gpu_pins=gpu_pins,
            gpu_deps={"foo": ("telemetrylib",)},
            gpu_base_deps={},
            base_names=frozenset({"foo"}),
        )
        pylock = build_pylock(lock_input)
        markers = [
            str(p.marker) for p in pylock.packages if str(p.name) == "telemetrylib"
        ]
        assert len(markers) == 2
        assert all("in extras" in marker for marker in markers)

    def test_base_edge_only_some_forks_carry_stays_member_gated(self) -> None:
        # cudalib is a base dep of foo, but only the cpu fork pins it, so
        # foo -> cudalib is a base edge one fork carries and the other
        # drops.  An edge not shared by every fork must not promote
        # cudalib to base.
        lock_input = self._lock_input_with(
            cpu_pins={
                "foo": _index_pin(name="foo", version="2.0"),
                "baz": _index_pin(name="baz", version="1.0"),
                "cudalib": _index_pin(name="cudalib", version="1.0"),
            },
            cpu_deps={"foo": ("baz", "cudalib")},
            cpu_base_deps={"foo": ("baz", "cudalib")},
            gpu_pins={
                "foo": _index_pin(name="foo", version="2.0"),
                "baz": _index_pin(name="baz", version="1.0"),
            },
            gpu_deps={"foo": ("baz",)},
            gpu_base_deps={"foo": ("baz",)},
            base_names=frozenset({"foo"}),
        )
        pylock = build_pylock(lock_input)
        by_name = {str(p.name): p for p in pylock.packages}

        assert "in extras" not in str(by_name["baz"].marker)
        assert '"cpu" in extras' in str(by_name["cudalib"].marker)

    def test_diamond_closure_promotes_shared_dep_once(self) -> None:
        pins: dict[str, PinShape] = {
            "foo": _index_pin(name="foo", version="2.0"),
            "baz": _index_pin(name="baz", version="1.0"),
            "qux": _index_pin(name="qux", version="1.0"),
        }
        deps = {"foo": ("baz", "qux"), "baz": ("qux",)}
        lock_input = self._lock_input_with(
            cpu_pins=pins,
            cpu_deps=deps,
            cpu_base_deps=deps,
            gpu_pins=pins,
            gpu_deps=deps,
            gpu_base_deps=deps,
            base_names=frozenset({"foo"}),
        )
        pylock = build_pylock(lock_input)
        by_name = {str(p.name): p for p in pylock.packages}
        no_member = self._LINUX.env_with_membership()

        for name in ("baz", "qux"):
            marker = by_name[name].marker
            assert "in extras" not in str(marker)
            assert marker is None or marker.evaluate(no_member)

    def test_workspace_drop_keeps_base_closure(self) -> None:
        # app -> member -> foo, all unconditional, and app is the only
        # declared base name.  foo is base only through the member, so
        # dropping the member must not cut the closure walk short.
        fork_pins: dict[str, PinShape] = {
            "app": _index_pin(name="app", version="2.0"),
            "member": LocalPin(name="member", version="1.0", path="packages/member"),
            "foo": _index_pin(name="foo", version="1.0"),
        }
        fork_deps = {"app": ("member",), "member": ("foo",)}

        lock_input = self._lock_input_with(
            cpu_pins=fork_pins,
            cpu_deps=fork_deps,
            cpu_base_deps=fork_deps,
            gpu_pins=fork_pins,
            gpu_deps=fork_deps,
            gpu_base_deps=fork_deps,
            base_names=frozenset({"app"}),
        )

        pylock = build_pylock(drop_workspace_pins(lock_input, frozenset({"member"})))
        by_name = {str(p.name): p for p in pylock.packages}

        assert "member" not in by_name
        foo = by_name["foo"]
        assert "in extras" not in str(foo.marker)
        assert foo.marker is None or foo.marker.evaluate(
            self._LINUX.env_with_membership()
        )


class TestConflictForksWithoutBaseAttribution:
    """When conflict forks ran but the caller did not supply base
    requirements, ``env_base_names`` is empty.  Base status is unknowable,
    so a dep present in every fork at the same version must keep its
    membership OR rather than collapse to an env-only marker."""

    _LINUX: ClassVar[ResolveTarget] = _target()
    _CPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "cpu"),)
    _GPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "gpu"),)

    def _lock_input(self) -> LockInput:
        conflicts = (
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "cpu"),
                    ConflictMember(ConflictKind.EXTRA, "gpu"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
        )
        return LockInput(
            targets=_targets(
                (
                    self._LINUX.with_selection(self._CPU),
                    {"shared": _index_pin(name="shared", version="1.0")},
                ),
                (
                    self._LINUX.with_selection(self._GPU),
                    {"shared": _index_pin(name="shared", version="1.0")},
                ),
            ),
            extras=("cpu", "gpu"),
            conflicts=conflicts,
        )

    def test_shared_pin_keeps_membership_or(self) -> None:
        pylock = build_pylock(self._lock_input())
        shared = next(p for p in pylock.packages if str(p.name) == "shared")
        marker = shared.marker
        assert marker is not None
        assert '"cpu" in extras' in str(marker)
        assert '"gpu" in extras' in str(marker)
        assert not marker.evaluate(self._LINUX.env_with_membership())


class TestConflictForkRequiresPythonMerge:
    """Same-(name, version, index) pins from different conflict forks
    collapse to one entry; ``requires_python`` survives only when every
    fork agreed, matching :func:`_common_requires_python`'s rule."""

    _LINUX: ClassVar[ResolveTarget] = _target()
    _CPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "cpu"),)
    _GPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "gpu"),)

    @staticmethod
    def _pin(requires_python: str | None) -> IndexPin:
        return IndexPin(
            name="foo",
            version="1.0",
            index="pypi",
            sdist=_sdist("foo", "1.0"),
            wheels=(_wheel("foo", "1.0"),),
            requires_python=requires_python,
        )

    def _build(self, cpu_req: str | None, gpu_req: str | None) -> LockInput:
        return LockInput(
            targets=_targets(
                (self._LINUX.with_selection(self._CPU), {"foo": self._pin(cpu_req)}),
                (self._LINUX.with_selection(self._GPU), {"foo": self._pin(gpu_req)}),
            ),
            extras=("cpu", "gpu"),
            conflicts=(
                ConflictSet(
                    members=(
                        ConflictMember(ConflictKind.EXTRA, "cpu"),
                        ConflictMember(ConflictKind.EXTRA, "gpu"),
                    ),
                    policy=ConflictPolicy.AT_MOST_ONE,
                ),
            ),
        )

    def test_disagreeing_requires_python_drops_to_none(self) -> None:
        pylock = build_pylock(self._build(">=3.10", ">=3.11"))
        foo = next(p for p in pylock.packages if str(p.name) == "foo")
        assert foo.requires_python is None

    def test_agreeing_requires_python_survives_the_merge(self) -> None:
        pylock = build_pylock(self._build(">=3.10", ">=3.10"))
        foo = next(p for p in pylock.packages if str(p.name) == "foo")
        assert foo.requires_python is not None
        assert str(foo.requires_python) == ">=3.10"

    def test_unconstrained_fork_drops_requires_python(self) -> None:
        """A fork with no Requires-Python imposes no Python floor, so a
        merge that mixes it with a constrained fork records ``None``."""
        pylock = build_pylock(self._build(">=3.10", None))
        foo = next(p for p in pylock.packages if str(p.name) == "foo")
        assert foo.requires_python is None


class TestConflictForkByteStability:
    """``write_lock`` is deterministic across multiple conflict forks.

    Every per-target grouping, marker disjunction, and wheel listing must
    pivot through sorted iteration so two calls on the same
    :class:`LockInput` produce byte-identical TOML.  Without this, a
    re-resolve that only re-orders dict insertion would write a
    spurious diff."""

    _LINUX: ClassVar[ResolveTarget] = _target(platform="linux_x86_64")
    _DARWIN: ClassVar[ResolveTarget] = _target(platform="macos_arm64")

    def _two_by_two(self) -> LockInput:
        # Two platforms (linux, darwin) x two conflict members (cpu, gpu).
        entries: list[tuple[ResolveTarget, Mapping[str, PinShape]]] = []
        for platform in (self._DARWIN, self._LINUX):
            for member in ("gpu", "cpu"):
                entries.append(
                    (
                        platform.with_selection((("extra", member),)),
                        {
                            "torch": _index_pin(name="torch", version=f"2.0+{member}"),
                            "universal": _index_pin(name="universal", version="9.0"),
                        },
                    )
                )
        return LockInput(
            targets=_targets(*entries),
            extras=("cpu", "gpu"),
            conflicts=(
                ConflictSet(
                    members=(
                        ConflictMember(ConflictKind.EXTRA, "cpu"),
                        ConflictMember(ConflictKind.EXTRA, "gpu"),
                    ),
                    policy=ConflictPolicy.AT_MOST_ONE,
                ),
            ),
        )

    def test_pylock_byte_stable_across_two_writes(self) -> None:
        lock_input = self._two_by_two()
        assert write_lock(lock_input) == write_lock(lock_input)


class TestBuildPylockReturnsValidPylock:
    def test_can_be_validated(self) -> None:
        pylock = build_pylock(LockInput(targets=_one({"foo": _index_pin()})))
        assert isinstance(pylock, Pylock)
        # Should not raise
        pylock.validate()


class TestErrorPaths:
    def test_unknown_pin_shape_raises(self) -> None:
        class Weird:
            pass

        with pytest.raises(TypeError, match="unknown pin shape"):
            _pin_to_package(Weird(), lock_dir=Path("/tmp"))  # type: ignore[arg-type]

    def test_or_markers_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            _or_markers([])

    def test_or_markers_single_returns_unchanged(self) -> None:
        m = Marker("python_version >= '3.10'")
        assert _or_markers([m]) is m

    def test_unknown_discriminator_in_pin_raises(self) -> None:
        class Weird:
            pass

        with pytest.raises(TypeError, match="unknown pin shape"):
            _pin_discriminator(Weird())  # type: ignore[arg-type]

    def test_wheel_primary_digest_requires_acceptable_hash(self) -> None:
        """A wheel artefact whose hashes contain only unacceptable
        algorithms (e.g. md5) raises rather than emitting a
        non-PEP-751 lockfile entry."""
        artifact = WheelArtifact(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0-py3-none-any.whl",
            hashes=(("md5", "0" * 32),),
        )
        with pytest.raises(ValueError, match="acceptable hash"):
            _ = artifact.primary_digest

    def test_sdist_primary_digest_requires_acceptable_hash(self) -> None:
        """sdist artefacts hold their digest under the same contract."""
        artifact = SdistArtifact(
            filename="foo-1.0.tar.gz",
            url="https://example.com/foo-1.0.tar.gz",
            hashes=(("md5", "0" * 32),),
        )
        with pytest.raises(ValueError, match="acceptable hash"):
            _ = artifact.primary_digest


class TestFailedWriteKeepsCommittedFile:
    """A write that dies partway must not leave a shortened lockfile behind.

    Both formats are newline-delimited records, so a prefix of the new text
    parses as a complete lock holding a subset of the pins.
    """

    def _committed(self) -> LockInput:
        return LockInput(targets=_one({"foo": _index_pin()}))

    def _bigger(self) -> LockInput:
        return LockInput(
            targets=_one(
                {
                    name: _index_pin(name=name, version="2.0")
                    for name in ("aaa", "bbb", "ccc", "ddd", "eee", "fff")
                }
            )
        )

    def test_pylock(
        self,
        tmp_path: Path,
        cap_writes: Callable[[int], AbstractContextManager[None]],
    ) -> None:
        target = tmp_path / "pylock.toml"
        write_lock(self._committed(), output_path=target)
        committed = target.read_bytes()
        half = len(write_lock(self._bigger())) // 2

        with cap_writes(half), pytest.raises(OSError, match="No space left"):
            write_lock(self._bigger(), output_path=target)

        assert target.read_bytes() == committed
        assert [p.name for p in tmp_path.iterdir()] == ["pylock.toml"]

    @pytest.mark.parametrize(
        "write",
        [write_requirements_with_hashes, write_requirements_without_hashes],
        ids=["with_hashes", "without_hashes"],
    )
    def test_requirements(
        self,
        write: Callable[..., str],
        tmp_path: Path,
        cap_writes: Callable[[int], AbstractContextManager[None]],
    ) -> None:
        target = tmp_path / "requirements.txt"
        write(self._committed(), output_path=target)
        committed = target.read_bytes()
        half = len(write(self._bigger())) // 2

        with cap_writes(half), pytest.raises(OSError, match="No space left"):
            write(self._bigger(), output_path=target)

        assert target.read_bytes() == committed
        assert [p.name for p in tmp_path.iterdir()] == ["requirements.txt"]


class TestRoundTrip:
    def test_pylock_can_be_re_parsed(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": _index_pin(),
                        "bar": LocalPin(name="bar", version="2.0", path="/tmp/bar"),
                    }
                )
            )
        )
        data = tomllib.loads(text)
        re_parsed = Pylock.from_dict(data)
        names = sorted(str(p.name) for p in re_parsed.packages)
        assert names == ["bar", "foo"]


class TestProvenance:
    def test_emits_tool_nab_block(self) -> None:
        prov = Provenance(
            nab_version="9.9.9",
            created_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
            command_line=("nab", "lock", "pyproject.toml"),
            input_path="pyproject.toml",
            mode="specific",
        )
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()}), provenance=prov)
        )
        data = tomllib.loads(text)
        assert data["tool"]["nab"]["nab-version"] == "9.9.9"
        assert data["tool"]["nab"]["mode"] == "specific"
        assert data["tool"]["nab"]["input-path"] == "pyproject.toml"
        assert data["tool"]["nab"]["command-line"] == [
            "nab",
            "lock",
            "pyproject.toml",
        ]
        assert "python-specifier" not in data["tool"]["nab"]
        assert "platforms" not in data["tool"]["nab"]
        assert "cli-project-overrides" not in data["tool"]["nab"]

    def test_emits_cli_project_overrides(self) -> None:
        prov = Provenance(
            nab_version="9.9.9",
            created_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
            command_line=("nab", "lock"),
            input_path="pyproject.toml",
            mode="specific",
            cli_project_overrides=(
                ("--project-resolution", "lowest"),
                ("--project-constraint", "urllib3<2"),
            ),
        )
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()}), provenance=prov)
        )
        data = tomllib.loads(text)
        assert data["tool"]["nab"]["cli-project-overrides"] == [
            "--project-resolution=lowest",
            "--project-constraint=urllib3<2",
        ]

    def test_emits_universal_matrix_fields(self) -> None:
        prov = Provenance(
            nab_version="9.9.9",
            created_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
            command_line=("nab", "lock"),
            input_path="pyproject.toml",
            mode="universal",
            python_specifier=">=3.11,<3.14",
            platforms=("linux_x86_64", "macos_arm64"),
        )
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()}), provenance=prov)
        )
        data = tomllib.loads(text)
        assert data["tool"]["nab"]["mode"] == "universal"
        assert data["tool"]["nab"]["python-specifier"] == ">=3.11,<3.14"
        assert data["tool"]["nab"]["platforms"] == [
            "linux_x86_64",
            "macos_arm64",
        ]

    def test_emits_package_metadata_overrides(self) -> None:
        prov = Provenance(
            nab_version="9.9.9",
            created_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
            command_line=("nab", "lock"),
            input_path="pyproject.toml",
            mode="specific",
            package_metadata_overrides=(
                ("chumpy", ("dependencies",)),
                ("broken<=1.0", ("dependencies",)),
            ),
        )
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()}), provenance=prov)
        )
        data = tomllib.loads(text)
        assert data["tool"]["nab"]["package-metadata-overrides"] == [
            "chumpy: dependencies",
            "broken<=1.0: dependencies",
        ]

    def test_no_package_metadata_overrides_key_when_empty(self) -> None:
        prov = Provenance(
            nab_version="9.9.9",
            created_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
            command_line=("nab", "lock"),
            input_path="pyproject.toml",
            mode="specific",
        )
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()}), provenance=prov)
        )
        data = tomllib.loads(text)
        assert "package-metadata-overrides" not in data["tool"]["nab"]

    def test_absent_provenance_means_no_tool_block(self) -> None:
        text = write_lock(LockInput(targets=_one({"foo": _index_pin()})))
        data = tomllib.loads(text)
        assert "tool" not in data


class TestPackageMetadataOverrideRecords:
    """``package_metadata_override_records`` summarises metadata overrides."""

    def test_records_dependencies_override(self) -> None:
        overrides = (pkg_override("foo <= 2", dependencies=(Requirement("bar"),)),)
        assert package_metadata_override_records(overrides) == (
            ("foo<=2", ("dependencies",)),
        )

    def test_empty_dependencies_still_recorded(self) -> None:
        # An empty tuple is set (not None), so it is recorded.
        overrides = (pkg_override("foo", dependencies=()),)
        assert package_metadata_override_records(overrides) == (
            ("foo", ("dependencies",)),
        )

    def test_non_metadata_override_skipped(self) -> None:
        # A policy-only entry sets no metadata field and is not recorded.
        overrides = (pkg_override("foo", dist_policy=DistPolicy.SDIST_ONLY),)
        assert package_metadata_override_records(overrides) == ()

    def test_records_requires_python_override(self) -> None:
        overrides = (pkg_override("foo", requires_python=">=3.6"),)
        assert package_metadata_override_records(overrides) == (
            ("foo", ("requires-python",)),
        )

    def test_records_provides_extra_override(self) -> None:
        overrides = (pkg_override("foo", provides_extra=("dotenv",)),)
        assert package_metadata_override_records(overrides) == (
            ("foo", ("provides-extra",)),
        )

    def test_records_full_bundle_in_field_order(self) -> None:
        overrides = (
            pkg_override(
                "foo",
                dependencies=(Requirement("bar"),),
                requires_python=">=3.6",
                provides_extra=("dotenv",),
            ),
        )
        assert package_metadata_override_records(overrides) == (
            ("foo", ("dependencies", "requires-python", "provides-extra")),
        )


class TestReadLockfileAnchor:
    """``read_lockfile_anchor`` extracts ``[tool.nab].created-at``."""

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert read_lockfile_anchor(tmp_path / "missing.toml") is None

    def test_returns_none_when_file_is_directory(self, tmp_path: Path) -> None:
        # ``is_file`` returns False for directories; the helper skips.
        assert read_lockfile_anchor(tmp_path) is None

    def test_returns_none_when_toml_invalid(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.toml"
        path.write_text("this is not [[[ valid TOML")
        assert read_lockfile_anchor(path) is None

    def test_returns_none_when_not_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_bytes(b"\xff\xfe not utf-8")
        assert read_lockfile_anchor(path) is None

    def test_returns_none_when_no_tool_nab(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text('lock-version = "1.0"\n')
        assert read_lockfile_anchor(path) is None

    def test_reads_offset_datetime(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text(
            "[tool.nab]\ncreated-at = 2026-05-01T00:00:00+00:00\n",
        )
        assert read_lockfile_anchor(path) == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_reads_iso_string(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        # Some writers may emit the timestamp as a quoted string instead
        # of a TOML offset-date-time; the reader handles both shapes.
        path.write_text(
            '[tool.nab]\ncreated-at = "2026-05-01T00:00:00+00:00"\n',
        )
        assert read_lockfile_anchor(path) == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_reads_iso_string_with_fractional_seconds(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text(
            '[tool.nab]\ncreated-at = "2026-05-01T00:00:00.5+00:00"\n',
        )
        assert read_lockfile_anchor(path) == datetime(
            2026, 5, 1, 0, 0, 0, 500000, tzinfo=timezone.utc
        )

    def test_naive_datetime_coerced_to_utc(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text(
            "[tool.nab]\ncreated-at = 2026-05-01T00:00:00\n",
        )
        assert read_lockfile_anchor(path) == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_naive_iso_string_coerced_to_utc(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text(
            '[tool.nab]\ncreated-at = "2026-05-01T00:00:00"\n',
        )
        assert read_lockfile_anchor(path) == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_invalid_iso_string_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text(
            '[tool.nab]\ncreated-at = "not-a-date"\n',
        )
        assert read_lockfile_anchor(path) is None

    def test_non_datetime_value_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text("[tool.nab]\ncreated-at = 1234\n")
        assert read_lockfile_anchor(path) is None

    def test_non_table_tool_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text('tool = "not-a-table"\n')
        assert read_lockfile_anchor(path) is None

    def test_non_table_tool_nab_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text('tool = {nab = "oops"}\n')
        assert read_lockfile_anchor(path) is None


class TestReadLockfilePackages:
    """``read_lockfile_packages`` extracts the prior pin set for diffing."""

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert read_lockfile_packages(tmp_path / "missing.toml") is None

    def test_returns_none_when_file_is_directory(self, tmp_path: Path) -> None:
        assert read_lockfile_packages(tmp_path) is None

    def test_returns_none_when_toml_invalid(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text("this is not [[[ valid TOML")
        assert read_lockfile_packages(path) is None

    def test_returns_none_when_not_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_bytes(b"\xff\xfe not utf-8")
        assert read_lockfile_packages(path) is None

    def test_returns_none_when_not_a_pylock(self, tmp_path: Path) -> None:
        # Valid TOML but missing the required PEP 751 keys.
        path = tmp_path / "pylock.toml"
        path.write_text('title = "not a lockfile"\n')
        assert read_lockfile_packages(path) is None

    def test_reads_name_to_version_map(self, tmp_path: Path) -> None:
        lock_input = LockInput(
            targets=_one(
                {
                    "foo": _index_pin("foo", "1.2.3"),
                    "bar": _index_pin("bar", "4.5"),
                }
            )
        )
        path = tmp_path / "pylock.toml"
        write_lock(lock_input, output_path=path)
        assert read_lockfile_packages(path) == {
            "foo": Version("1.2.3"),
            "bar": Version("4.5"),
        }

    def test_skips_packages_without_version(self, tmp_path: Path) -> None:
        # A directory (path) package carries no version key.
        path = tmp_path / "pylock.toml"
        path.write_text(
            'lock-version = "1.0"\n'
            'created-by = "nab"\n\n'
            "[[packages]]\n"
            'name = "foo"\n'
            "[packages.directory]\n"
            'path = "./foo"\n',
        )
        assert read_lockfile_packages(path) == {}


class TestDependencyGroups:
    def test_emits_top_level_arrays(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one({"foo": _index_pin()}),
                dependency_groups=("dev", "docs"),
                default_groups=("dev",),
            )
        )
        data = tomllib.loads(text)
        assert data["dependency-groups"] == ["dev", "docs"]
        assert data["default-groups"] == ["dev"]

    def test_omits_arrays_when_empty(self) -> None:
        text = write_lock(LockInput(targets=_one({"foo": _index_pin()})))
        data = tomllib.loads(text)
        assert "dependency-groups" not in data
        assert "default-groups" not in data

    def test_active_groups_unions_selection_and_defaults(self) -> None:
        lock_input = LockInput(
            targets=_one({"foo": _index_pin()}),
            dependency_groups=("docs", "dev"),
            default_groups=("dev", "lint"),
        )
        assert lock_input.active_groups == ("docs", "dev", "lint")

    def test_group_names_normalized(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one({"foo": _index_pin()}),
                dependency_groups=("Dev_Group", "Doc.s"),
                default_groups=("Dev_Group",),
            )
        )
        data = tomllib.loads(text)
        assert data["dependency-groups"] == ["dev-group", "doc-s"]
        assert data["default-groups"] == ["dev-group"]


class TestSupportedKeysDocumented:
    """The lockfile reference lists every key the pylock emitter writes."""

    def _documented_keys(self) -> set[str]:
        doc = Path(__file__).resolve().parents[2] / "docs" / "reference" / "lockfile.md"
        text = doc.read_text(encoding="utf-8")
        start = text.index("### Supported keys")
        end = text.index("\n### ", start + 1)
        section = text[start:end]
        return set(re.findall(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)*)`", section))

    def test_every_emitted_key_is_documented(self) -> None:
        target_lock = TargetLock(
            target=_HOST,
            pins={
                "foo": _index_pin("foo", "1.0"),
                "mytool": _index_pin("mytool", "2.0"),
                "mylocal": LocalPin(name="mylocal", version="3.0", path="libs/mylocal"),
                "myvcs": VcsPin(
                    name="myvcs",
                    version="4.0",
                    repo_url="https://github.com/x/y.git",
                    bare_repo_url="https://github.com/x/y.git",
                    commit_id="a" * 40,
                ),
                "myarchive": ArchivePin(
                    name="myarchive",
                    version="5.0",
                    url="https://ex.com/myarchive-5.0.tar.gz",
                    hashes=(("sha256", "e" * 64),),
                ),
            },
            dependencies={"mytool": ("foo",)},
            package_gates={"mytool": (("extra", "cli"),)},
        )
        lock_input = LockInput(
            targets={_HOST.label: target_lock},
            extras=("cli",),
            dependency_groups=("dev",),
            default_groups=("dev",),
            requires_python=">=3.10",
            environments=[Marker(_HOST.environment_marker_string)],
        )

        data = tomllib.loads(write_lock(lock_input))
        emitted = set(data)
        for package in data["packages"]:
            emitted |= set(package)

        undocumented = emitted - self._documented_keys()
        assert not undocumented, f"undocumented emitted keys: {sorted(undocumented)}"


class TestMarkerDisjointness:
    def _pkg(self, name: str, version: str, marker: str | None = None) -> Package:
        return Package(
            name=canonicalize_name(name),
            version=Version(version),
            marker=Marker(marker) if marker else None,
            wheels=(
                PackageWheel(
                    name=f"{name}-{version}-py3-none-any.whl",
                    url=f"https://x/{name}-{version}-py3-none-any.whl",
                    hashes={"sha256": "a" * 64},
                ),
            ),
        )

    def test_passes_when_no_environments_declared(self) -> None:
        # No environments => single-env mode, validator no-ops.
        validate_marker_disjointness(
            [self._pkg("foo", "1.0"), self._pkg("foo", "2.0")],
            environments={},
            extras=(),
            groups=(),
        )

    def test_passes_when_markers_partition_environments(self) -> None:
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
            "macos": {
                "python_version": "3.11",
                "sys_platform": "darwin",
                "platform_machine": "arm64",
            },
        }
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "sys_platform == 'linux'"),
                self._pkg("foo", "2.0", "sys_platform == 'darwin'"),
            ],
            environments=envs,
            extras=(),
            groups=(),
        )

    def test_raises_when_two_entries_match_one_env(self) -> None:
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        with pytest.raises(DisjointnessError, match="foo"):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "sys_platform == 'linux'"),
                    self._pkg(
                        "foo",
                        "2.0",
                        "sys_platform == 'linux' or sys_platform == 'darwin'",
                    ),
                ],
                environments=envs,
                extras=(),
                groups=(),
            )

    def test_raises_when_marker_none_collides(self) -> None:
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        with pytest.raises(DisjointnessError):
            validate_marker_disjointness(
                [self._pkg("foo", "1.0"), self._pkg("foo", "2.0")],
                environments=envs,
                extras=(),
                groups=(),
            )

    def test_extras_axis_disjoint_with_negation(self) -> None:
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        # Without a conflict declaration, the extras set {cpu, gpu} is
        # a legal user request, so plain ``'cpu' in extras`` and
        # ``'gpu' in extras`` collide on that point.  Mutual exclusion
        # has to be encoded into the marker itself via ``not in``.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "'cpu' in extras and 'gpu' not in extras"),
                self._pkg("foo", "2.0", "'gpu' in extras and 'cpu' not in extras"),
            ],
            environments=envs,
            extras=("cpu", "gpu"),
            groups=(),
        )

    def test_extras_axis_collision_without_negation(self) -> None:
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        # ``{cpu, gpu}`` is a valid extras subset; both markers fire
        # there.  A conflict declaration would let the producer
        # remove that subset from the universe, but until then the
        # validator must report the collision.
        with pytest.raises(DisjointnessError, match="extras="):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "'cpu' in extras"),
                    self._pkg("foo", "2.0", "'gpu' in extras"),
                ],
                environments=envs,
                extras=("cpu", "gpu"),
                groups=(),
            )

    def test_unreferenced_extras_add_no_membership_axis(self) -> None:
        # Airflow declares ~120 extras.  When no candidate marker
        # references ``extras``, the algebra reads the parsed atoms and
        # adds no membership axis for the variable, so the check does not
        # scale with the declared list.
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        many_extras = tuple(f"e{i}" for i in range(120))
        # Markers that only constrain the environment succeed regardless
        # of how many extras are declared: with no extras axis, 120
        # declared names cost the same as none.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "sys_platform == 'linux'"),
                self._pkg("foo", "2.0", "sys_platform == 'darwin'"),
            ],
            environments=envs,
            extras=many_extras,
            groups=(),
        )

    def test_extras_collision_with_normalization_mismatch(self) -> None:
        # PEP 685 compares extra names under normalization.  The
        # declared names and the marker literals here differ only by
        # case and separator, so the algebra must canonicalize both
        # sides; otherwise the referenced literals miss the declared
        # extras and the {cpu, fast-io} collision goes unreported.
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        with pytest.raises(DisjointnessError, match="extras="):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "'CPU' in extras"),
                    self._pkg("foo", "2.0", "'fast_io' in extras"),
                ],
                environments=envs,
                extras=("cpu", "fast-io"),
                groups=(),
            )

    def test_large_conflict_fork_negation_stays_within_budget(self) -> None:
        # A fork that negates every co-member of three declared 5-member
        # sets carries 15 membership atoms.  The conflict-respecting
        # selection binds all of them, so the pair is decided without
        # the unrestricted membership powerset that would overrun the
        # cell budget.
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }

        sets = [tuple(f"{letter}{i}" for i in range(5)) for letter in ("a", "b", "c")]
        all_names = tuple(name for members in sets for name in members)

        def _fork_marker(picks: tuple[str, ...]) -> str:
            clauses = [f"'{name}' in extras" for name in picks]
            for members in sets:
                clauses += [
                    f"'{name}' not in extras" for name in members if name not in picks
                ]
            return " and ".join(clauses)

        exclusive = [frozenset(("extra", name) for name in members) for members in sets]
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", _fork_marker(("a0", "b0", "c0"))),
                self._pkg("foo", "2.0", _fork_marker(("a1", "b1", "c1"))),
            ],
            environments=envs,
            extras=all_names,
            groups=(),
            exclusive_groups=exclusive,
        )

    def test_unreferenced_groups_add_no_membership_axis(self) -> None:
        # Symmetric behaviour for ``dependency_groups``: a project with
        # many declared groups whose markers do not reference the
        # variable adds no membership axis for it, so the check does not
        # scale with the declared list.
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        many_groups = tuple(f"g{i}" for i in range(40))
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "sys_platform == 'linux'"),
                self._pkg("foo", "2.0", "sys_platform == 'darwin'"),
            ],
            environments=envs,
            extras=(),
            groups=many_groups,
        )

    def test_quoted_extras_value_is_not_a_membership_reference(self) -> None:
        # A marker whose quoted value is the token ``extras``
        # (``platform_release == "extras"``) references no membership
        # variable: the algebra reads the parsed atoms, not the marker
        # text, so it adds no extras axis.  The 40 declared extras then
        # cost nothing and the pair does not collide.
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "platform_release": "",
            },
        }
        many_extras = tuple(f"e{i}" for i in range(40))
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "sys_platform == 'linux'"),
                self._pkg("foo", "2.0", 'platform_release == "extras"'),
            ],
            environments=envs,
            extras=many_extras,
            groups=(),
        )

    def test_only_referenced_extras_form_membership_axis(self) -> None:
        # Of 50 declared extras, only ``"cpu"`` and ``"gpu"`` appear
        # in markers; the membership axis spans only those two, so the
        # check stays small instead of scaling with the declared list.
        # The collision on ``{cpu, gpu}`` is still reported.
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        many_extras = ("cpu", "gpu", *(f"e{i}" for i in range(50)))
        with pytest.raises(DisjointnessError, match="extras="):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "'cpu' in extras"),
                    self._pkg("foo", "2.0", "'gpu' in extras"),
                ],
                environments=envs,
                extras=many_extras,
                groups=(),
            )

    _LINUX: ClassVar[dict[str, dict[str, str]]] = {
        "linux": {
            "python_version": "3.11",
            "sys_platform": "linux",
            "platform_machine": "x86_64",
        },
    }

    def test_declared_extra_conflict_prunes_collision(self) -> None:
        # The bare-marker case that collides without a declaration now
        # passes once cpu and gpu are declared mutually exclusive: the
        # {cpu, gpu} point is pruned from the universe.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "'cpu' in extras"),
                self._pkg("foo", "2.0", "'gpu' in extras"),
            ],
            environments=self._LINUX,
            extras=("cpu", "gpu"),
            groups=(),
            exclusive_groups=[frozenset({("extra", "cpu"), ("extra", "gpu")})],
        )

    def test_declared_group_conflict_prunes_collision(self) -> None:
        # The datamodel-code-generator case: mutually-exclusive
        # dependency groups gated by bare ``in dependency_groups``
        # markers validate once the groups are declared exclusive.
        validate_marker_disjointness(
            [
                self._pkg("black", "22.1", "'black22' in dependency_groups"),
                self._pkg("black", "23.12", "'black23' in dependency_groups"),
                self._pkg("black", "24.1", "'black24' in dependency_groups"),
            ],
            environments=self._LINUX,
            extras=(),
            groups=("black22", "black23", "black24"),
            exclusive_groups=[
                frozenset(
                    {
                        ("group", "black22"),
                        ("group", "black23"),
                        ("group", "black24"),
                    }
                )
            ],
        )

    def test_conflict_does_not_prune_unrelated_collision(self) -> None:
        # A collision outside the pruned subspace still raises: cpu/gpu
        # are declared exclusive, but these two entries collide on the
        # cpu point itself (both fire when cpu is selected).
        with pytest.raises(DisjointnessError, match="foo"):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "'cpu' in extras"),
                    self._pkg("foo", "2.0", "'cpu' in extras or 'gpu' in extras"),
                ],
                environments=self._LINUX,
                extras=("cpu", "gpu"),
                groups=(),
                exclusive_groups=[frozenset({("extra", "cpu"), ("extra", "gpu")})],
            )

    def test_collision_hint_points_at_conflicts_key(self) -> None:
        # A membership-driven collision with no declaration surfaces a
        # hint pointing at the conflicts key.
        with pytest.raises(DisjointnessError, match=r"\[tool.nab\].conflicts"):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "'cpu' in extras"),
                    self._pkg("foo", "2.0", "'gpu' in extras"),
                ],
                environments=self._LINUX,
                extras=("cpu", "gpu"),
                groups=(),
            )

    def test_environment_only_collision_has_no_conflict_hint(self) -> None:
        # An environment-driven collision is not helped by a conflict
        # declaration, so no hint is appended.
        with pytest.raises(DisjointnessError) as excinfo:
            validate_marker_disjointness(
                [self._pkg("foo", "1.0"), self._pkg("foo", "2.0")],
                environments=self._LINUX,
                extras=(),
                groups=(),
            )
        assert "[tool.nab].conflicts" not in str(excinfo.value)

    def test_membership_markers_do_not_collide_at_empty_extras(self) -> None:
        """``'cpu' in frozenset()`` must evaluate False through
        :class:`Marker`, so the empty-extras selection never makes two
        membership-gated entries collide.  Asserts the marker-eval
        primitive in isolation so a later switch to a different Marker
        library cannot regress this without breaking a focused test."""
        empty_context = {
            "sys_platform": "linux",
            "extras": frozenset(),
            "dependency_groups": frozenset(),
        }
        assert not Marker("'cpu' in extras").evaluate(empty_context)
        assert not Marker("'gpu' in extras").evaluate(empty_context)

        # End-to-end: two membership-gated entries are not a collision at
        # the empty-extras witness even without a declared conflict, so
        # the validator stays silent on that point.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "'cpu' in extras"),
                self._pkg("foo", "2.0", "'gpu' in extras"),
            ],
            environments=self._LINUX,
            extras=("cpu", "gpu"),
            groups=(),
            exclusive_groups=[frozenset({("extra", "cpu"), ("extra", "gpu")})],
        )

    def test_conflict_member_normalization_prunes_collision(self) -> None:
        # The declared member name and the marker literal differ by
        # case/separator; canonicalisation must still prune the point.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "'CPU' in extras"),
                self._pkg("foo", "2.0", "'fast_io' in extras"),
            ],
            environments=self._LINUX,
            extras=("cpu", "fast-io"),
            groups=(),
            exclusive_groups=[frozenset({("extra", "cpu"), ("extra", "fast-io")})],
        )

    def test_undeclared_extra_reference_is_pinned_absent(self) -> None:
        # The universe only selects declared names.  Both entries gate on
        # an extra the producer never declared, so no install context
        # selects it and the two never fire together: no collision, even
        # with no conflict declared.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "'ghost' in extras"),
                self._pkg("foo", "2.0", "'ghost' in extras"),
            ],
            environments=self._LINUX,
            extras=("cpu",),
            groups=(),
        )

    def test_collision_only_outside_declared_envs_does_not_raise(self) -> None:
        # foo 1.0 fires only on darwin, foo 2.0 fires everywhere; they
        # overlap only at darwin, which is not a declared environment.
        # The universe is the declared envs, so the pair is disjoint.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "sys_platform == 'darwin'"),
                self._pkg("foo", "2.0"),
            ],
            environments=self._LINUX,
            extras=(),
            groups=(),
        )

    def test_collision_inside_declared_env_still_raises(self) -> None:
        # The mirror of the above: the overlap is at a declared env
        # (linux), so it must raise.
        with pytest.raises(DisjointnessError, match="foo"):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "sys_platform == 'linux'"),
                    self._pkg("foo", "2.0"),
                ],
                environments=self._LINUX,
                extras=(),
                groups=(),
            )

    def test_bare_positive_conflict_forks_validate_across_many_sets(self) -> None:
        # Five independent at-most-one pairs; the 32 bare-positive forks
        # of one name are pairwise disjoint only because the conflict
        # declaration prunes every co-selection.  The algebra decides
        # this without enumerating the 3**5 install-context points.
        sets = [[f"s{i}a", f"s{i}b"] for i in range(5)]
        declared = [m for s in sets for m in s]
        forks = list(itertools.product(*sets))
        packages = [
            self._pkg(
                "torch",
                f"1.{fi}.0",
                "python_version == '3.11'"
                + "".join(f" and '{m}' in extras" for m in sorted(fork)),
            )
            for fi, fork in enumerate(forks)
        ]
        exclusive = [frozenset({("extra", a), ("extra", b)}) for a, b in sets]
        validate_marker_disjointness(
            packages,
            environments=self._LINUX,
            extras=declared,
            groups=(),
            exclusive_groups=exclusive,
            declared_groups=exclusive,
        )

    def test_passes_when_all_names_distinct(self) -> None:
        # No same-name pair exists, so there is nothing to check and the
        # validator returns before touching the algebra.
        validate_marker_disjointness(
            [self._pkg("foo", "1.0"), self._pkg("bar", "2.0")],
            environments=self._LINUX,
            extras=(),
            groups=(),
        )

    def test_contains_over_approximation_reports_without_witness(self) -> None:
        # A ``contains`` atom is an opaque, over-approximating boolean.
        # ``"ab" in v`` implies ``"a" in v``, so no realisable
        # platform_version satisfies ``"ab" in v and "a" not in v``; the
        # algebra cannot rule the pair out (over-approximates to
        # non-disjoint) and no concrete witness exists.  The gate stays
        # conservative and reports the pair without a point.  A declared
        # env supplying platform_version would fold the atoms away, so
        # this uses an env that omits it.
        marker = "'ab' in platform_version and 'a' not in platform_version"
        with pytest.raises(DisjointnessError, match="not disjoint"):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", marker),
                    self._pkg("foo", "2.0", marker),
                ],
                environments={
                    "linux": {
                        "python_version": "3.11",
                        "python_full_version": "3.11.0",
                        "sys_platform": "linux",
                    },
                },
                extras=(),
                groups=(),
            )

    def test_group_membership_drives_conflict_hint(self) -> None:
        # The collision fires at a witness where a referenced group is
        # active (extras play no part), so the groups branch of the hint
        # gate must trigger.
        with pytest.raises(DisjointnessError, match=r"\[tool.nab\].conflicts"):
            validate_marker_disjointness(
                [
                    self._pkg("black", "22.1", "'black22' in dependency_groups"),
                    self._pkg("black", "23.12", "'black23' in dependency_groups"),
                ],
                environments=self._LINUX,
                extras=(),
                groups=("black22", "black23"),
            )

    def test_env_collision_with_membership_marker_has_no_hint(self) -> None:
        # One marker mentions an extra, but the collision fires at the
        # empty-extras witness driven purely by the environment.  A
        # conflict declaration cannot prune that point, so no hint.
        with pytest.raises(DisjointnessError) as excinfo:
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "sys_platform == 'linux'"),
                    self._pkg(
                        "foo", "2.0", "sys_platform == 'linux' or 'cpu' in extras"
                    ),
                ],
                environments=self._LINUX,
                extras=("cpu",),
                groups=(),
            )
        assert "[tool.nab].conflicts" not in str(excinfo.value)

    def test_at_least_one_does_not_prune_collision(self) -> None:
        # conflict_exclusion_groups DROPS at_least_one, so the projection
        # passed for an at_least_one-only declaration is empty: the
        # colliding context survives and the validator still raises.
        conflicts = [
            ConflictSet(
                members=(
                    ConflictMember(kind=ConflictKind.GROUP, name="a"),
                    ConflictMember(kind=ConflictKind.GROUP, name="b"),
                ),
                policy=ConflictPolicy.AT_LEAST_ONE,
            )
        ]
        with pytest.raises(DisjointnessError, match="black"):
            validate_marker_disjointness(
                [
                    self._pkg("black", "1.0", "'a' in dependency_groups"),
                    self._pkg("black", "2.0", "'b' in dependency_groups"),
                ],
                environments=self._LINUX,
                extras=(),
                groups=("a", "b"),
                exclusive_groups=conflict_exclusion_groups(conflicts),
            )

    def test_already_declared_at_least_one_hint_recommends_tightening(self) -> None:
        # The colliding members are already declared, just under a policy
        # that permits co-selection; the hint must point at tightening
        # rather than suggesting the user declare them again.
        conflicts = [
            ConflictSet(
                members=(
                    ConflictMember(kind=ConflictKind.GROUP, name="a"),
                    ConflictMember(kind=ConflictKind.GROUP, name="b"),
                ),
                policy=ConflictPolicy.AT_LEAST_ONE,
            )
        ]
        with pytest.raises(DisjointnessError) as info:
            validate_marker_disjointness(
                [
                    self._pkg("black", "1.0", "'a' in dependency_groups"),
                    self._pkg("black", "2.0", "'b' in dependency_groups"),
                ],
                environments=self._LINUX,
                extras=(),
                groups=("a", "b"),
                exclusive_groups=conflict_exclusion_groups(conflicts),
                declared_groups=conflict_member_groups(conflicts),
            )
        message = str(info.value)
        assert "switch to at-most-one or exactly-one" in message
        assert "If these are intentionally mutually exclusive" not in message

    def test_at_most_one_prunes_same_collision(self) -> None:
        # The same collision under an at_most_one exclusion is pruned.
        conflicts = [
            ConflictSet(
                members=(
                    ConflictMember(kind=ConflictKind.GROUP, name="a"),
                    ConflictMember(kind=ConflictKind.GROUP, name="b"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            )
        ]
        validate_marker_disjointness(
            [
                self._pkg("black", "1.0", "'a' in dependency_groups"),
                self._pkg("black", "2.0", "'b' in dependency_groups"),
            ],
            environments=self._LINUX,
            extras=(),
            groups=("a", "b"),
            exclusive_groups=conflict_exclusion_groups(conflicts),
        )

    def test_exactly_one_prunes_collision(self) -> None:
        # exactly_one forbids co-selection like at_most_one, so its
        # member set is projected as an exclusive group and the colliding
        # context is pruned.
        conflicts = [
            ConflictSet(
                members=(
                    ConflictMember(kind=ConflictKind.GROUP, name="a"),
                    ConflictMember(kind=ConflictKind.GROUP, name="b"),
                ),
                policy=ConflictPolicy.EXACTLY_ONE,
            )
        ]
        validate_marker_disjointness(
            [
                self._pkg("black", "1.0", "'a' in dependency_groups"),
                self._pkg("black", "2.0", "'b' in dependency_groups"),
            ],
            environments=self._LINUX,
            extras=(),
            groups=("a", "b"),
            exclusive_groups=conflict_exclusion_groups(conflicts),
        )

    def test_two_exclusion_sets_second_prunes_point(self) -> None:
        # Two independent exclusive sets: the {cpu, gpu} extras set does
        # not cover this point, so the exclusion check must iterate past
        # it to the {black22, black23} groups set that does prune it.
        validate_marker_disjointness(
            [
                self._pkg("black", "22.1", "'black22' in dependency_groups"),
                self._pkg("black", "23.12", "'black23' in dependency_groups"),
            ],
            environments=self._LINUX,
            extras=("cpu", "gpu"),
            groups=("black22", "black23"),
            exclusive_groups=[
                frozenset({("extra", "cpu"), ("extra", "gpu")}),
                frozenset({("group", "black22"), ("group", "black23")}),
            ],
        )

    def test_two_exclusion_sets_point_outside_both_raises(self) -> None:
        # A collision outside both exclusive sets still raises even when
        # two independent sets are declared.
        with pytest.raises(DisjointnessError, match="black"):
            validate_marker_disjointness(
                [
                    self._pkg("black", "1.0", "'black22' in dependency_groups"),
                    self._pkg(
                        "black",
                        "2.0",
                        "'black22' in dependency_groups"
                        " or 'black23' in dependency_groups",
                    ),
                ],
                environments=self._LINUX,
                extras=("cpu", "gpu"),
                groups=("black22", "black23"),
                exclusive_groups=[
                    frozenset({("extra", "cpu"), ("extra", "gpu")}),
                    frozenset({("group", "black22"), ("group", "black23")}),
                ],
            )

    def test_repeated_environments_still_raise_on_collision(self) -> None:
        # A conflict-forked lock repeats one physical env under several
        # selection labels.  Dedup must not hide a genuine collision and
        # must keep the first-seen label so the message stays stable.
        envs = {
            "linux-cpu": dict(self._LINUX["linux"]),
            "linux-gpu": dict(self._LINUX["linux"]),
        }
        with pytest.raises(DisjointnessError, match="linux-cpu"):
            validate_marker_disjointness(
                [self._pkg("foo", "1.0"), self._pkg("foo", "2.0")],
                environments=envs,
                extras=(),
                groups=(),
            )

    def test_repeated_environments_pass_when_disjoint(self) -> None:
        # The same repeated-env lock passes when the entries partition
        # the install context, matching the pre-dedup behaviour.
        envs = {
            "linux-cpu": dict(self._LINUX["linux"]),
            "linux-gpu": dict(self._LINUX["linux"]),
        }
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "'cpu' in extras"),
                self._pkg("foo", "2.0", "'cpu' not in extras"),
            ],
            environments=envs,
            extras=("cpu",),
            groups=(),
        )

    def test_distinct_environments_kept_when_collision_only_in_second(
        self,
    ) -> None:
        # Dedup collapses identical env dicts only; two genuinely
        # different envs must each get evaluated.  Collision shows
        # under darwin only, and the error must name darwin.
        envs = {
            "linux": dict(self._LINUX["linux"]),
            "darwin": {
                "python_version": "3.11",
                "sys_platform": "darwin",
                "platform_machine": "arm64",
            },
        }
        with pytest.raises(DisjointnessError, match="darwin"):
            validate_marker_disjointness(
                [
                    self._pkg("foo", "1.0", "sys_platform == 'darwin'"),
                    self._pkg("foo", "2.0", "sys_platform == 'darwin'"),
                ],
                environments=envs,
                extras=(),
                groups=(),
            )

    def test_large_mutually_exclusive_set_partitioned_pair_emits(self) -> None:
        # Ten mutually-exclusive extras, split five and five across two
        # same-name entries.  The conflict declaration forbids any two
        # members co-selecting, so the entries never fire together and the
        # pair is disjoint.  The selection walk decides this without the
        # powerset over ten membership names.
        members = [f"m{i}" for i in range(10)]
        left = " or ".join(f"'{m}' in extras" for m in members[:5])
        right = " or ".join(f"'{m}' in extras" for m in members[5:])
        validate_marker_disjointness(
            [self._pkg("foo", "1.0", left), self._pkg("foo", "2.0", right)],
            environments=self._LINUX,
            extras=tuple(members),
            groups=(),
            exclusive_groups=[frozenset(("extra", m) for m in members)],
        )

    def test_large_free_membership_set_fails_loud(self) -> None:
        # Seventeen declared extras in no conflict set are free, so the
        # selection count is 2**17.  That exceeds the guard, so the walk
        # raises rather than iterating an unbounded number of selections.
        free = [f"e{i}" for i in range(17)]
        left = " or ".join(f"'{m}' in extras" for m in free[:9])
        right = " or ".join(f"'{m}' in extras" for m in free[9:])
        with pytest.raises(IntractableMarkerSet):
            validate_marker_disjointness(
                [self._pkg("foo", "1.0", left), self._pkg("foo", "2.0", right)],
                environments=self._LINUX,
                extras=tuple(free),
                groups=(),
            )

    def test_overlapping_conflict_sets_prune_collision(self) -> None:
        # Two conflict sets share member x: {x, y} and {x, z}.  foo 1.0
        # needs x, foo 2.0 needs y or z.  Every selection that fires both
        # co-activates a conflict set (x with y, or x with z), so all are
        # pruned and the pair is disjoint.
        validate_marker_disjointness(
            [
                self._pkg("foo", "1.0", "'x' in extras"),
                self._pkg("foo", "2.0", "'y' in extras or 'z' in extras"),
            ],
            environments=self._LINUX,
            extras=("x", "y", "z"),
            groups=(),
            exclusive_groups=[
                frozenset({("extra", "x"), ("extra", "y")}),
                frozenset({("extra", "x"), ("extra", "z")}),
            ],
        )


class _FakeIndex:
    """Stub for the InMemoryIndex slice the lockfile builder reads."""

    def __init__(self, listing_indexes: dict[str, str] | None = None) -> None:
        self._listing_indexes = listing_indexes or {}

    def get_listing_index(self, package: str) -> str | None:
        return self._listing_indexes.get(package)


class _FakeCoordinator:
    """Stub for the FetchCoordinator slice the lockfile builder reads."""

    def __init__(self, listing_indexes: dict[str, str] | None = None) -> None:
        self.index = _FakeIndex(listing_indexes)


class _FakeProvider:
    """Minimal stand-in for Provider for builder tests."""

    def __init__(
        self,
        listings: dict[str, list[tuple[Version, WheelFile | SdistFile]]] | None = None,
        local_sources: dict[str, LocalSource] | None = None,
        vcs_sources: dict[str, VcsSource] | None = None,
        archive_sources: dict[str, ArchiveSource] | None = None,
        vcs_pins: dict[str, str] | None = None,
        listing_indexes: dict[str, str] | None = None,
        dist_policy_overrides: dict[str, DistPolicy] | None = None,
        requires_python_overrides: dict[str, str] | None = None,
        deps_cache: dict[tuple[str, Version], dict[str, object]] | None = None,
        extra_deps_map: (
            dict[tuple[str, Version], dict[str, dict[str, object]]] | None
        ) = None,
        tag_excluded_counts: dict[tuple[str, Version], int] | None = None,
    ) -> None:
        self._listings = listings or {}
        self._local = local_sources or {}
        self._vcs = vcs_sources or {}
        self._archive = archive_sources or {}
        self._vcs_pins = vcs_pins or {}
        self._dist_policy_overrides = dist_policy_overrides or {}
        self._requires_python_overrides = requires_python_overrides or {}
        self.coordinator = _FakeCoordinator(listing_indexes)
        self.deps_cache = deps_cache or {}
        self.extra_deps_map = extra_deps_map or {}
        self._tag_excluded_counts = tag_excluded_counts or {}

    def local_source_for(self, canonical: str) -> LocalSource | None:
        return self._local.get(canonical)

    def vcs_source_for(self, canonical: str) -> VcsSource | None:
        return self._vcs.get(canonical)

    def archive_source_for(self, canonical: str) -> ArchiveSource | None:
        return self._archive.get(canonical)

    def vcs_pin_for(self, canonical: str) -> str | None:
        return self._vcs_pins.get(canonical)

    def dist_files_for(
        self, canonical: str, version: Version
    ) -> list[WheelFile | SdistFile]:
        return [d for v, d in self._listings.get(canonical, []) if v == version]

    def effective_dist_policy(
        self, canonical: str, version: Version, index_name: str | None = None
    ) -> DistPolicy:
        return self._dist_policy_overrides.get(canonical, DistPolicy.WHEEL_OR_SDIST)

    def effective_requires_python(self, canonical: str, version: Version) -> str | None:
        return self._requires_python_overrides.get(canonical)

    def tag_excluded_wheel_count(self, canonical: str, version: Version) -> int:
        return self._tag_excluded_counts.get((canonical, version), 0)


def _wheel_file(
    name: str = "foo",
    version: str = "1.0",
    *,
    requires_python: str | None = ">=3.10",
    sha256: str | None = "a" * 64,
    local_path: Path | None = None,
) -> WheelFile:
    hashes = (("sha256", sha256),) if sha256 is not None else ()
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://pypi.org/simple/{name}/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=requires_python,
        has_metadata=False,
        upload_time=None,
        hashes=hashes,
        size=1234,
        local_path=local_path,
    )


def _sdist_file(name: str = "foo", version: str = "1.0") -> SdistFile:
    return SdistFile(
        filename=f"{name}-{version}.tar.gz",
        url=f"https://pypi.org/simple/{name}/{name}-{version}.tar.gz",
        version=version,
        requires_python=">=3.10",
        upload_time=None,
        hashes=(("sha256", "b" * 64),),
        size=4321,
    )


class TestBuildTargetLock:
    def test_index_pin_from_listing(self) -> None:
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), _wheel_file()),
                    (Version("1.0"), _sdist_file()),
                ]
            }
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.version == "1.0"
        assert pin.index == "https://pypi.org/simple/"
        assert pin.requires_python == ">=3.10"
        assert pin.sdist is not None
        assert pin.sdist.hashes == (("sha256", "b" * 64),)
        assert len(pin.wheels) == 1
        assert pin.wheels[0].hashes == (("sha256", "a" * 64),)
        assert lock.target is _HOST

    def test_index_pin_prefers_requires_python_override(self) -> None:
        """A widened requires-python override is what the pin records.

        The Simple-API files say ``>=3.10``; the user widened the package
        to ``>=3.9``.  The pin must carry the overridden specifier so a
        conforming PEP 751 installer does not reject a pin the resolver
        admitted against the wider floor.
        """
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), _wheel_file(requires_python=">=3.10")),
                    (Version("1.0"), _sdist_file()),
                ]
            },
            requires_python_overrides={"foo": ">=3.9"},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python == ">=3.9"

    def test_unconstrained_wheel_drops_requires_python(self) -> None:
        """A version that mixes a Requires-Python wheel with an
        unconstrained ``py3-none-any`` wheel records ``None``: the
        unconstrained wheel installs everywhere, so there is no floor.
        """
        constrained = WheelFile(
            filename="foo-1.0-cp312-cp312-linux_x86_64.whl",
            url="https://pypi.org/simple/foo/foo-1.0-cp312-cp312-linux_x86_64.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time=None,
            hashes=(("sha256", "a" * 64),),
            size=1234,
            local_path=None,
        )
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), constrained),
                    (Version("1.0"), _wheel_file(requires_python=None)),
                ]
            },
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python is None

    def test_empty_requires_python_override_omits_lock_key(self) -> None:
        """An empty-string override records verbatim and drops the lock key.

        The files declare ``>=3.10``; the override clears the specifier to
        ``""``.  The pin carries the empty string, and the writer's
        truthiness check leaves ``requires-python`` out of the rendered lock.
        """
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), _wheel_file(requires_python=">=3.10")),
                    (Version("1.0"), _sdist_file()),
                ]
            },
            requires_python_overrides={"foo": ""},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python == ""
        assert "requires-python" not in write_lock(_lock_from(lock))

    def test_malformed_requires_python_dropped_and_emittable(self) -> None:
        """A malformed listing Requires-Python is dropped, so the lock still emits.

        ``excluded_by_python`` admits a dist whose Requires-Python is an invalid
        PEP 440 specifier, so the pin must not carry it into ``write_lock``,
        whose ``SpecifierSet`` parse would otherwise crash.
        """
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file(requires_python=">=3.6.*"))]}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python is None
        text = write_lock(_lock_from(lock))
        assert ">=3.6.*" not in text

    def test_common_requires_python_malformed_with_valid_stays_unconstrained(
        self,
    ) -> None:
        """A malformed value leaves the pin unconstrained beside a valid one.

        ``excluded_by_python`` admits the malformed artefact on any Python, so
        emitting the sibling's floor would over-constrain the pin below it.
        """
        files = [
            _wheel_file(requires_python=">=3.6.*"),
            _wheel_file(requires_python=">=3.7"),
        ]
        assert _common_requires_python(files) is None

    def test_skip_fetch_override_pins_sdist_without_metadata(self) -> None:
        """A skip-fetch package still produces a complete sdist lock pin.

        A complete ``dependencies`` override resolves the version without a
        METADATA fetch or build; the sdist listing (its hash recorded during
        ``fetch_versions``) still flows into the pin and the rendered lock.
        """
        coordinator = make_coordinator([_sdist_file("pkg", "2.0")], package="pkg")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.12.0"),
            build_policy=BuildPolicy.NEVER,
            package_overrides=(
                pkg_override("pkg", dependencies=(Requirement("dep-a>=1"),)),
            ),
        )
        provider.fetch_versions("pkg")
        deps = provider.get_dependencies("pkg", Version("2.0"))
        assert "dep-a" in deps
        coordinator.request_metadata.assert_not_called()
        coordinator.request_metadata_batch.assert_not_called()

        lock = build_target_lock(provider, _HOST, {"pkg": Version("2.0")})
        text = write_lock(_lock_from(lock))
        assert "pkg-2.0.tar.gz" in text
        assert "b" * 64 in text

    def test_archive_source_emits_archive_pin(self) -> None:
        """A configured ArchiveSource builds an ArchivePin from its URL + hash."""
        source = ArchiveSource(
            name="foo",
            url=(
                "https://ex.com/foo-1.0.tar.gz#sha256=" + "e" * 64 + "&subdirectory=pkg"
            ),
        )
        provider = _FakeProvider(archive_sources={"foo": source})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, ArchivePin)
        assert pin.url == "https://ex.com/foo-1.0.tar.gz"
        assert pin.hashes == (("sha256", "e" * 64),)
        assert pin.subdirectory == "pkg"
        assert pin.version == "1.0"

    def test_archive_source_strips_credentials(self) -> None:
        """An archive on an authenticated host keeps its token out of the lock."""
        source = ArchiveSource(
            name="foo",
            url="https://user:token@private.example/foo-1.0.tar.gz#sha256=" + "e" * 64,
        )
        provider = _FakeProvider(archive_sources={"foo": source})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, ArchivePin)
        assert pin.url == "https://private.example/foo-1.0.tar.gz"
        assert "user:token@" not in write_lock(_lock_from(lock))
        assert "user:token@" not in write_requirements_with_hashes(_lock_from(lock))

    def test_local_path_threads_to_artifact(self, tmp_path: Path) -> None:
        """A WheelFile.local_path reaches the emitted WheelArtifact."""
        wheel_path = tmp_path / "foo-1.0-py3-none-any.whl"
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file(local_path=wheel_path))]}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].local_path == wheel_path

    def test_index_pin_sdist_install_drops_wheels(self) -> None:
        """Wheels seen during resolution stay out of the pin under sdist-install.

        The listing carried both a wheel and an sdist so the resolver
        could read the wheel's METADATA, but the lockfile must pin
        only the sdist so an installer downloads (and builds) that
        archive.  Mirrors what
        :attr:`nab_python.provider.DistPolicy.SDIST_INSTALL` is for.
        """
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), _wheel_file()),
                    (Version("1.0"), _sdist_file()),
                ]
            },
            dist_policy_overrides={"foo": DistPolicy.SDIST_INSTALL},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels == ()
        assert pin.sdist is not None
        assert pin.sdist.hashes == (("sha256", "b" * 64),)

    def test_index_pin_sdist_install_without_sdist_raises(self) -> None:
        """A wheel-only version under sdist-install has nothing to pin."""
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            dist_policy_overrides={"foo": DistPolicy.SDIST_INSTALL},
        )
        with pytest.raises(MissingSdistError, match="foo==1.0 has no sdist"):
            build_target_lock(provider, _HOST, {"foo": Version("1.0")})

    def test_index_pin_records_serving_index(self) -> None:
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), _wheel_file()),
                    (Version("1.0"), _sdist_file()),
                ],
            },
            listing_indexes={"foo": "torch-cpu"},
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"foo": Version("1.0")},
            indexes=(
                IndexConfig("pypi", "https://pypi.org/simple/"),
                IndexConfig("torch-cpu", "https://download.pytorch.org/whl/cpu/"),
            ),
        )
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.index == "https://download.pytorch.org/whl/cpu/"

    def test_index_pin_raises_when_serving_index_unrecorded(self) -> None:
        """No recorded serving index raises instead of guessing one."""
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), _wheel_file()),
                    (Version("1.0"), _sdist_file()),
                ],
            },
        )
        with pytest.raises(AssertionError, match="not one of the configured indexes"):
            build_target_lock(
                provider,
                _HOST,
                {"foo": Version("1.0")},
                indexes=(IndexConfig("custom", "https://custom.example/simple/"),),
            )

    def test_index_pin_strips_credentials_from_url(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "private"},
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"foo": Version("1.0")},
            indexes=(
                IndexConfig(
                    "private", "https://user:token@Private.Example:8443/simple/"
                ),
            ),
        )
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.index == "https://Private.Example:8443/simple/"

    def test_index_pin_keeps_url_without_credentials(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "plain"},
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"foo": Version("1.0")},
            indexes=(IndexConfig("plain", "https://Plain.Example/simple/"),),
        )
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.index == "https://Plain.Example/simple/"

    def test_index_pin_strips_username_only_credentials(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "useronly"},
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"foo": Version("1.0")},
            indexes=(IndexConfig("useronly", "https://user@example.com/simple/"),),
        )
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.index == "https://example.com/simple/"

    def test_artifact_urls_strip_credentials(self) -> None:
        """A private index serving same-host file URLs keeps creds out of the lock.

        The index field already drops embedded userinfo, but a private
        index commonly serves wheel/sdist URLs on the same authenticated
        host. Those URLs must not carry the token into the committed lock.
        """
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://user:token@private.example/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time=None,
            hashes=(("sha256", "a" * 64),),
            size=1234,
        )
        sdist = SdistFile(
            filename="foo-1.0.tar.gz",
            url="https://user:token@private.example/simple/foo/foo-1.0.tar.gz",
            version="1.0",
            requires_python=">=3.10",
            upload_time=None,
            hashes=(("sha256", "b" * 64),),
            size=4321,
        )
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), wheel), (Version("1.0"), sdist)]},
            listing_indexes={"foo": "private"},
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"foo": Version("1.0")},
            indexes=(
                IndexConfig("private", "https://user:token@private.example/simple/"),
            ),
        )
        text = write_lock(_lock_from(lock))
        assert "user:token@" not in text
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].url == (
            "https://private.example/simple/foo/foo-1.0-py3-none-any.whl"
        )
        assert pin.sdist is not None
        assert pin.sdist.url == "https://private.example/simple/foo/foo-1.0.tar.gz"

    def test_local_source_emits_local_pin(self, tmp_path: Path) -> None:
        provider = _FakeProvider(
            local_sources={"foo": LocalSource(name="foo", path=str(tmp_path))}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+local")})
        pin = lock.pins["foo"]
        assert isinstance(pin, LocalPin)
        assert pin.path == str(tmp_path.resolve())

    def test_vcs_source_emits_vcs_pin_with_commit(self) -> None:
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url="git+https://example.com/r.git@" + "a" * 40,
                ),
            },
            vcs_pins={"foo": "a" * 40},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.commit_id == "a" * 40

    def test_vcs_source_records_subdirectory(self) -> None:
        """The ``#subdirectory=`` fragment survives into the VcsPin."""
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url=f"git+https://example.com/r.git@{sha}#subdirectory=pkg/sub",
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.subdirectory == "pkg/sub"

    def test_vcs_pin_carries_bare_repo_url(self) -> None:
        """The bare repo URL is carried through from the parsed source.

        ``repo_url`` is the installable form re-pinned to ``commit_id``;
        ``bare_repo_url`` holds the plain repository URL with no ``git+``
        prefix, ``@<ref>``, or ``#subdirectory`` fragment.
        """
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url="git+https://example.com/r.git@release/1.0#subdirectory=pkg/sub",
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert (
            pin.repo_url == f"git+https://example.com/r.git@{sha}#subdirectory=pkg/sub"
        )
        assert pin.bare_repo_url == "https://example.com/r.git"

    def test_vcs_pin_repo_url_encodes_subdirectory(self) -> None:
        """A subdirectory with a URL-reserved char is percent-encoded.

        The requirements.txt ``name @ <url>`` line is parsed as PEP 508; an
        unencoded space terminates the URL token, so ``repo_url`` must carry
        the fragment encoded to stay installable.
        """
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url=f"git+https://example.com/r.git@{sha}#subdirectory=my pkg",
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert (
            pin.repo_url == f"git+https://example.com/r.git@{sha}#subdirectory=my%20pkg"
        )

    def test_vcs_requirements_line_pins_to_commit(self) -> None:
        """A branch/tag-pinned VCS source renders the resolved commit.

        lockfile.md documents the requirements.txt VCS line as
        ``git+<repo>@<sha>``, and the pylock writer pins to the
        resolved ``commit-id``.  The requirements emitter must match,
        not echo the moving ``@<ref>`` the user supplied.
        """
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(name="foo", url="git+https://example.com/r.git@main"),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        text = write_requirements_with_hashes(_lock_from(lock))
        assert f"foo @ git+https://example.com/r.git@{sha}" in text
        assert "@main" not in text

    def test_vcs_pin_pylock_url_is_bare_repo(self) -> None:
        """vcs.url is the bare repository URL; ref and subdirectory are separate fields."""
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url="git+https://example.com/r.git@release/1.0#subdirectory=pkg/sub",
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        vcs = tomllib.loads(write_lock(_lock_from(lock)))["packages"][0]["vcs"]
        assert vcs["url"] == "https://example.com/r.git"
        assert vcs["commit-id"] == sha
        assert vcs["subdirectory"] == "pkg/sub"
        assert vcs["requested-revision"] == "release/1.0"

    def test_vcs_pin_bare_repo_url_strips_credentials(self) -> None:
        """Embedded userinfo is dropped from the bare URL, as for repo_url."""
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url=f"git+https://user:pass@example.com/r.git@{sha}",
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.bare_repo_url == "https://example.com/r.git"

    def test_vcs_pin_ssh_keeps_login(self) -> None:
        """An SSH login (``git@``) is the protocol login, not a credential.

        Stripping it yields a URL the installer cannot clone over SSH, so
        it must survive into both the bare URL and the pinned repo URL.
        """
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url=f"git+ssh://git@github.com/foo/bar.git@{sha}",
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.bare_repo_url == "ssh://git@github.com/foo/bar.git"
        assert pin.repo_url == f"git+ssh://git@github.com/foo/bar.git@{sha}"

    def test_vcs_pin_ssh_drops_embedded_password(self) -> None:
        """An embedded password in an SSH URL is still dropped; login stays."""
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url=f"git+ssh://git:secret@github.com/foo/bar.git@{sha}",
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.bare_repo_url == "ssh://git@github.com/foo/bar.git"

    def test_vcs_source_without_resolved_sha_raises(self) -> None:
        """A pinned VCS source with no recorded SHA is an invariant breach.

        materialize_vcs_source records the post-clone SHA before any
        version can be pinned, so reaching the builder without one is
        impossible through a real resolve; guard it loudly rather than
        emit a branch name as the commit id.
        """
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(name="foo", url="git+https://example.com/r.git@main"),
            },
        )
        with pytest.raises(MissingVcsCommitError, match="resolved commit SHA"):
            build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})

    def test_vcs_source_prefers_resolved_sha_over_url_ref(self) -> None:
        """The post-clone SHA on the provider wins over the URL's ``@<ref>``."""
        resolved = "b" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url="git+https://example.com/r.git@v1.0",
                ),
            },
            vcs_pins={"foo": resolved},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.commit_id == resolved

    def test_vcs_source_strips_credentials_from_url(self) -> None:
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url="git+https://user:token@GitHub.com/org/repo.git",
                ),
            },
            vcs_pins={"foo": "a" * 40},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.repo_url == "git+https://GitHub.com/org/repo.git@" + "a" * 40

    def test_vcs_source_keeps_url_without_credentials(self) -> None:
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(name="foo", url="git+https://github.com/org/repo.git"),
            },
            vcs_pins={"foo": "a" * 40},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.repo_url == "git+https://github.com/org/repo.git@" + "a" * 40

    def test_vcs_source_records_requested_revision_for_floating_ref(self) -> None:
        """A named ``@<ref>`` resolved to a different SHA is recorded."""
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url="git+https://example.com/r.git@v1.0",
                ),
            },
            vcs_pins={"foo": "b" * 40},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.requested_revision == "v1.0"

    def test_vcs_source_no_requested_revision_when_pinned_to_sha(self) -> None:
        """When the user pinned the SHA, requested-revision stays None."""
        sha = "a" * 40
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(
                    name="foo",
                    url="git+https://example.com/r.git@" + sha,
                ),
            },
            vcs_pins={"foo": sha},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.requested_revision is None

    def test_vcs_source_no_requested_revision_without_url_ref(self) -> None:
        """A URL with no ``@<ref>`` yields no requested-revision."""
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(name="foo", url="git+https://example.com/r.git"),
            },
            vcs_pins={"foo": "a" * 40},
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+vcs")})
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.requested_revision is None

    def test_local_source_threads_editable(self, tmp_path: Path) -> None:
        provider = _FakeProvider(
            local_sources={
                "foo": LocalSource(name="foo", path=str(tmp_path), editable=True)
            }
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+local")})
        pin = lock.pins["foo"]
        assert isinstance(pin, LocalPin)
        assert pin.editable is True

    def test_local_source_threads_subdirectory(self, tmp_path: Path) -> None:
        provider = _FakeProvider(
            local_sources={
                "foo": LocalSource(
                    name="foo", path=str(tmp_path), subdirectory="pkg/lib"
                )
            }
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+local")})
        pin = lock.pins["foo"]
        assert isinstance(pin, LocalPin)
        assert pin.subdirectory == "pkg/lib"

    def test_local_source_defaults_not_editable_no_subdirectory(
        self, tmp_path: Path
    ) -> None:
        provider = _FakeProvider(
            local_sources={"foo": LocalSource(name="foo", path=str(tmp_path))}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("0.0.0+local")})
        pin = lock.pins["foo"]
        assert isinstance(pin, LocalPin)
        assert pin.editable is False
        assert pin.subdirectory is None

    def test_wheel_upload_time_parsed_from_index(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time="2026-05-01T12:00:00Z",
            hashes=(("sha256", "a" * 64),),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].upload_time == datetime(
            2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc
        )

    def test_sdist_upload_time_parsed_from_index(self) -> None:
        sdist = SdistFile(
            filename="foo-1.0.tar.gz",
            url="https://pypi.org/simple/foo/foo-1.0.tar.gz",
            version="1.0",
            requires_python=">=3.10",
            upload_time="2026-05-01T12:00:00Z",
            hashes=(("sha256", "b" * 64),),
            size=4321,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), sdist)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.sdist is not None
        assert pin.sdist.upload_time == datetime(
            2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize(
        ("fraction", "microsecond"),
        [
            ("", 0),
            (".1", 100000),
            (".12", 120000),
            (".123", 123000),
            (".1234", 123400),
            (".12345", 123450),
            (".123456", 123456),
        ],
    )
    def test_wheel_upload_time_keeps_every_pep700_fraction_width(
        self, fraction: str, microsecond: int
    ) -> None:
        """PEP 700 permits 0 through 6 fractional digits; all reach the lock."""
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time=f"2026-05-01T12:00:00{fraction}Z",
            hashes=(("sha256", "a" * 64),),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].upload_time == datetime(
            2026, 5, 1, 12, 0, 0, microsecond, tzinfo=timezone.utc
        )

    def test_upload_time_none_when_index_omits_it(self) -> None:
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), _wheel_file())]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].upload_time is None

    def test_upload_time_invalid_string_is_dropped(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time="not-a-timestamp",
            hashes=(("sha256", "a" * 64),),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].upload_time is None

    def test_upload_time_non_utc_offset_normalized_to_utc(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time="2026-05-01T17:00:00+05:00",
            hashes=(("sha256", "a" * 64),),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].upload_time == datetime(
            2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc
        )

    def test_upload_time_naive_string_is_dropped(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time="2026-05-01T12:00:00",
            hashes=(("sha256", "a" * 64),),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].upload_time is None

    def test_missing_acceptable_hash_raises(self) -> None:
        wheel = _wheel_file(sha256=None)
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        with pytest.raises(MissingHashError, match="no acceptable hash"):
            write_lock(_lock_from(lock))

    def test_unsupported_algorithm_raises(self) -> None:
        wheel_md5_only = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time=None,
            hashes=(("md5", "0" * 32),),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel_md5_only)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        with pytest.raises(MissingHashError, match="no acceptable hash"):
            write_lock(_lock_from(lock))

    def test_sha384_and_sha512_emit(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time=None,
            hashes=(("sha384", "d" * 96), ("sha512", "e" * 128)),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert dict(pin.wheels[0].hashes) == {
            "sha384": "d" * 96,
            "sha512": "e" * 128,
        }
        assert pin.wheels[0].primary_digest == ("sha384", "d" * 96)

    def test_md5_dropped_when_sha256_present(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://pypi.org/simple/foo/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.10",
            has_metadata=False,
            upload_time=None,
            hashes=(("md5", "0" * 32), ("sha256", "a" * 64)),
            size=1234,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})

        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].hashes == (("sha256", "a" * 64),)

        text = write_requirements_with_hashes(_lock_from(lock))
        assert text == f"foo==1.0 \\\n    --hash=sha256:{'a' * 64}\n"

    def test_diverging_requires_python_drops_field(self) -> None:
        wheel_a = _wheel_file(requires_python=">=3.10")
        wheel_b = WheelFile(
            filename="foo-1.0-cp311-cp311-linux_x86_64.whl",
            url="https://example.com/foo-1.0-cp311-cp311-linux_x86_64.whl",
            version="1.0",
            requires_python=">=3.11",
            has_metadata=False,
            upload_time=None,
            hashes=(("sha256", "c" * 64),),
            size=1024,
        )
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), wheel_a), (Version("1.0"), wheel_b)]}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python is None

    def test_index_pin_raises_when_serving_index_unconfigured(self) -> None:
        """A serving index not among the configured indexes raises."""
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "gone"},
        )
        with pytest.raises(AssertionError, match="not one of the configured indexes"):
            build_target_lock(
                provider,
                _HOST,
                {"foo": Version("1.0")},
                indexes=(IndexConfig("primary", "https://primary.example/simple/"),),
            )

    def test_only_md5_hash_raises(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0.whl",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
            hashes=(("md5", "x" * 32),),
            size=None,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        with pytest.raises(MissingHashError, match="sha256"):
            write_requirements_with_hashes(_lock_from(lock))

    def test_files_without_requires_python_drop_field(self) -> None:
        wheel = _wheel_file(requires_python=None)
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python is None


class TestMissingHashFormatAware:
    """The hash requirement is enforced per output format, not at build."""

    def test_build_tolerates_hashless_artifact(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file(sha256=None))]}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].hashes == ()

    def test_build_drops_unacceptable_algorithm(self) -> None:
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0.whl",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
            hashes=(("md5", "x" * 32),),
            size=None,
        )
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        pin = lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].hashes == ()

    def test_without_hashes_emits_hashless_pin(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file(sha256=None))]}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        text = write_requirements_without_hashes(_lock_from(lock))
        assert text.strip() == "foo==1.0"

    def test_with_hashes_raises_on_hashless_pin(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file(sha256=None))]}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        with pytest.raises(MissingHashError, match="no acceptable hash"):
            write_requirements_with_hashes(_lock_from(lock))

    def test_pylock_raises_on_hashless_pin(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file(sha256=None))]}
        )
        lock = build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        with pytest.raises(MissingHashError, match="no acceptable hash"):
            write_lock(_lock_from(lock))


class TestDirectoryFields:
    """PEP 751 ``packages.directory`` editable + subdirectory emission."""

    def test_editable_emitted_when_true(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": LocalPin(
                            name="foo",
                            version="1.0",
                            path=str(tmp_path),
                            editable=True,
                        )
                    }
                )
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["editable"] is True

    def test_editable_false_by_default(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(tmp_path))}
                )
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["editable"] is False

    def test_subdirectory_emitted(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": LocalPin(
                            name="foo",
                            version="1.0",
                            path=str(tmp_path),
                            subdirectory="pkg/lib",
                        )
                    }
                )
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["subdirectory"] == "pkg/lib"

    def test_subdirectory_omitted_when_none(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(tmp_path))}
                )
            )
        )
        data = tomllib.loads(text)
        assert "subdirectory" not in data["packages"][0]["directory"]

    def test_discriminator_separates_editable(self) -> None:
        editable = LocalPin(name="foo", version="1.0", path="/a", editable=True)
        plain = LocalPin(name="foo", version="1.0", path="/a", editable=False)
        assert _pin_discriminator(editable) != _pin_discriminator(plain)

    def test_discriminator_separates_subdirectory(self) -> None:
        sub = LocalPin(name="foo", version="1.0", path="/a", subdirectory="lib")
        plain = LocalPin(name="foo", version="1.0", path="/a")
        assert _pin_discriminator(sub) != _pin_discriminator(plain)

    def test_per_target_editable_diverges(self) -> None:
        text = write_lock(
            LockInput(
                targets=_targets(
                    (
                        _target("3.10"),
                        {
                            "foo": LocalPin(
                                name="foo", version="1.0", path="/a", editable=True
                            )
                        },
                    ),
                    (
                        _target("3.11"),
                        {
                            "foo": LocalPin(
                                name="foo", version="1.0", path="/a", editable=False
                            )
                        },
                    ),
                )
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2


class TestVcsRequestedRevision:
    """PEP 751 ``packages.vcs.requested-revision`` emission."""

    def test_requested_revision_emitted(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": VcsPin(
                            name="foo",
                            version="1.0",
                            repo_url="https://github.com/x/y.git",
                            bare_repo_url="https://github.com/x/y.git",
                            commit_id="a" * 40,
                            requested_revision="v2.1.0",
                        ),
                    }
                )
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["vcs"]["requested-revision"] == "v2.1.0"

    def test_requested_revision_omitted_when_none(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": VcsPin(
                            name="foo",
                            version="1.0",
                            repo_url="https://github.com/x/y.git",
                            bare_repo_url="https://github.com/x/y.git",
                            commit_id="a" * 40,
                        ),
                    }
                )
            )
        )
        data = tomllib.loads(text)
        assert "requested-revision" not in data["packages"][0]["vcs"]


class TestUploadTime:
    """PEP 751 ``packages.wheels/sdist.upload-time`` emission."""

    def test_wheel_upload_time_emitted(self) -> None:
        ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        pin = IndexPin(
            name="foo",
            version="1.0",
            index="pypi",
            wheels=(
                WheelArtifact(
                    filename="foo-1.0-py3-none-any.whl",
                    url="https://example.com/foo-1.0-py3-none-any.whl",
                    hashes=(("sha256", "a" * 64),),
                    upload_time=ts,
                ),
            ),
        )
        text = write_lock(LockInput(targets=_one({"foo": pin})))
        data = tomllib.loads(text)
        assert data["packages"][0]["wheels"][0]["upload-time"] == ts

    def test_sdist_upload_time_emitted(self) -> None:
        ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        pin = IndexPin(
            name="foo",
            version="1.0",
            index="pypi",
            sdist=SdistArtifact(
                filename="foo-1.0.tar.gz",
                url="https://example.com/foo-1.0.tar.gz",
                hashes=(("sha256", "b" * 64),),
                upload_time=ts,
            ),
        )
        text = write_lock(LockInput(targets=_one({"foo": pin})))
        data = tomllib.loads(text)
        assert data["packages"][0]["sdist"]["upload-time"] == ts

    def test_upload_time_omitted_when_none(self) -> None:
        text = write_lock(LockInput(targets=_one({"foo": _index_pin()})))
        data = tomllib.loads(text)
        assert "upload-time" not in data["packages"][0]["wheels"][0]
        assert "upload-time" not in data["packages"][0]["sdist"]


class TestRelativeDirectoryPath:
    """PEP 751: ``packages.directory.path`` is relative to the lock file (#6)."""

    def test_path_inside_lock_dir_is_relative(self, tmp_path: Path) -> None:
        src = tmp_path / "libs" / "foo"
        text = write_lock(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(src))}
                )
            ),
            output_path=tmp_path / "pylock.toml",
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["path"] == "libs/foo"

    def test_path_outside_lock_dir_uses_parent_prefix(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "foo"
        out_dir = tmp_path / "locks"
        out_dir.mkdir()
        text = write_lock(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(src))}
                )
            ),
            output_path=out_dir / "pylock.toml",
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["path"] == "../src/foo"

    def test_no_output_path_relativizes_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        src = tmp_path / "pkg"
        text = write_lock(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(src))}
                )
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["path"] == "pkg"

    def test_build_pylock_honours_explicit_lock_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "a" / "b" / "foo"
        pylock = build_pylock(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(src))}
                )
            ),
            lock_dir=tmp_path / "a",
        )
        directory = pylock.packages[0].directory
        assert directory is not None
        assert directory.path == "b/foo"


class TestRelativeArtifactPath:
    """PEP 751: local wheel/sdist artefacts become relative paths (#22).

    A local artefact carries its filesystem path in ``local_path``;
    the writer emits that as a relative ``path`` rather than reversing
    the ``file:`` URL, which is lossy across platforms.
    """

    def test_local_wheel_emitted_as_relative_path(self, tmp_path: Path) -> None:
        wheel_path = tmp_path / "wheels" / "foo-1.0-py3-none-any.whl"
        pin = IndexPin(
            name="foo",
            version="1.0",
            index="https://example.com/simple/",
            wheels=(
                WheelArtifact(
                    filename="foo-1.0-py3-none-any.whl",
                    url=wheel_path.as_uri(),
                    hashes=(("sha256", "a" * 64),),
                    local_path=wheel_path,
                ),
            ),
        )
        text = write_lock(
            LockInput(targets=_one({"foo": pin})), output_path=tmp_path / "pylock.toml"
        )
        wheel = tomllib.loads(text)["packages"][0]["wheels"][0]
        assert wheel["path"] == "wheels/foo-1.0-py3-none-any.whl"
        assert "url" not in wheel

    def test_local_sdist_emitted_as_relative_path(self, tmp_path: Path) -> None:
        sdist_path = tmp_path / "dist" / "foo-1.0.tar.gz"
        pin = IndexPin(
            name="foo",
            version="1.0",
            index="https://example.com/simple/",
            sdist=SdistArtifact(
                filename="foo-1.0.tar.gz",
                url=sdist_path.as_uri(),
                hashes=(("sha256", "b" * 64),),
                local_path=sdist_path,
            ),
        )
        text = write_lock(
            LockInput(targets=_one({"foo": pin})), output_path=tmp_path / "pylock.toml"
        )
        sdist = tomllib.loads(text)["packages"][0]["sdist"]
        assert sdist["path"] == "dist/foo-1.0.tar.gz"
        assert "url" not in sdist

    def test_remote_artifacts_keep_url(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(targets=_one({"foo": _index_pin()})),
            output_path=tmp_path / "pylock.toml",
        )
        package = tomllib.loads(text)["packages"][0]
        assert package["wheels"][0]["url"].startswith("https://")
        assert "path" not in package["wheels"][0]
        assert package["sdist"]["url"].startswith("https://")
        assert "path" not in package["sdist"]

    def test_local_artifact_outside_lock_dir(self, tmp_path: Path) -> None:
        wheel_path = tmp_path / "wheelhouse" / "foo-1.0-py3-none-any.whl"
        out_dir = tmp_path / "build" / "locks"
        out_dir.mkdir(parents=True)
        pin = IndexPin(
            name="foo",
            version="1.0",
            index="https://example.com/simple/",
            wheels=(
                WheelArtifact(
                    filename="foo-1.0-py3-none-any.whl",
                    url=wheel_path.as_uri(),
                    hashes=(("sha256", "a" * 64),),
                    local_path=wheel_path,
                ),
            ),
        )
        text = write_lock(
            LockInput(targets=_one({"foo": pin})), output_path=out_dir / "pylock.toml"
        )
        wheel = tomllib.loads(text)["packages"][0]["wheels"][0]
        assert wheel["path"] == "../../wheelhouse/foo-1.0-py3-none-any.whl"


class TestPathHelpers:
    """Unit coverage for the path-relativisation helper."""

    def test_relativize_path_uses_posix_separators(self, tmp_path: Path) -> None:
        rel = _relativize_path(tmp_path / "a" / "b", tmp_path)
        assert rel == "a/b"
        assert "\\" not in rel

    def test_relativize_path_cross_drive_falls_back_to_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cross-drive ValueError falls back to the absolute path."""

        def cross_drive(*_args: object, **_kwargs: object) -> str:
            msg = "path is on mount 'D:', start on mount 'C:'"
            raise ValueError(msg)

        monkeypatch.setattr("nab_python._lockfile.pylock.os.path.relpath", cross_drive)
        target = tmp_path / "elsewhere"
        assert _relativize_path(target, tmp_path) == target.as_posix()


class TestWriteRequirementsWithHashes:
    def test_index_pin_emits_hashes(self) -> None:
        text = write_requirements_with_hashes(
            LockInput(targets=_one({"foo": _index_pin()}))
        )
        assert "foo==1.0" in text
        assert "--hash=sha256:" + "a" * 64 in text
        assert "--hash=sha256:" + "b" * 64 in text

    def test_hash_order_canonical_across_wheel_order(self) -> None:
        wheel_a = WheelArtifact(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0-py3-none-any.whl",
            hashes=(("sha256", "a" * 64),),
            size=1024,
        )
        wheel_b = WheelArtifact(
            filename="foo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
            url="https://example.com/foo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
            hashes=(("sha256", "b" * 64),),
            size=1024,
        )
        forward = IndexPin(
            name="foo", version="1.0", index="pypi", wheels=(wheel_a, wheel_b)
        )
        reverse = IndexPin(
            name="foo", version="1.0", index="pypi", wheels=(wheel_b, wheel_a)
        )
        text_forward = write_requirements_with_hashes(
            LockInput(targets=_one({"foo": forward}))
        )
        text_reverse = write_requirements_with_hashes(
            LockInput(targets=_one({"foo": reverse}))
        )
        assert text_forward == text_reverse

    def test_hash_lines_sorted_across_sdist_and_wheel(self) -> None:
        sdist = SdistArtifact(
            filename="foo-1.0.tar.gz",
            url="https://example.com/foo-1.0.tar.gz",
            hashes=(("sha256", "f" * 64),),
        )
        wheel = WheelArtifact(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0-py3-none-any.whl",
            hashes=(("sha256", "0" * 64), ("sha512", "9" * 128)),
        )
        pin = IndexPin(
            name="foo", version="1.0", index="pypi", sdist=sdist, wheels=(wheel,)
        )

        text = write_requirements_with_hashes(LockInput(targets=_one({"foo": pin})))
        assert text == (
            "foo==1.0 \\\n"
            f"    --hash=sha256:{'0' * 64} \\\n"
            f"    --hash=sha256:{'f' * 64} \\\n"
            f"    --hash=sha512:{'9' * 128}\n"
        )

    def test_local_pin_uses_file_url(self, tmp_path: Path) -> None:
        text = write_requirements_with_hashes(
            LockInput(
                targets=_one(
                    {"foo": LocalPin(name="foo", version="1.0", path=str(tmp_path))}
                )
            )
        )
        assert "foo @ file://" in text

    def test_local_pin_editable_renders_dash_e(self, tmp_path: Path) -> None:
        text = write_requirements_with_hashes(
            LockInput(
                targets=_one(
                    {
                        "foo": LocalPin(
                            name="foo", version="1.0", path=str(tmp_path), editable=True
                        )
                    }
                )
            )
        )
        assert text.strip() == f"-e {tmp_path.resolve().as_uri()}"

    def test_local_pin_subdirectory_renders_fragment(self, tmp_path: Path) -> None:
        text = write_requirements_without_hashes(
            LockInput(
                targets=_one(
                    {
                        "foo": LocalPin(
                            name="foo",
                            version="1.0",
                            path=str(tmp_path),
                            subdirectory="packages/foo",
                        )
                    }
                )
            )
        )
        url = tmp_path.resolve().as_uri()
        assert text.strip() == f"foo @ {url}#subdirectory=packages/foo"

    def test_local_pin_editable_with_subdirectory(self, tmp_path: Path) -> None:
        text = write_requirements_with_hashes(
            LockInput(
                targets=_one(
                    {
                        "foo": LocalPin(
                            name="foo",
                            version="1.0",
                            path=str(tmp_path),
                            editable=True,
                            subdirectory="packages/foo",
                        )
                    }
                )
            )
        )
        url = tmp_path.resolve().as_uri()
        assert text.strip() == f"-e {url}#subdirectory=packages/foo"

    def test_local_pin_subdirectory_encodes_reserved_char(self, tmp_path: Path) -> None:
        """A subdirectory with a URL-reserved char is percent-encoded.

        An unencoded space terminates the URL token of the PEP 508
        ``name @ <url>`` line, so the fragment must stay encoded.
        """
        text = write_requirements_without_hashes(
            LockInput(
                targets=_one(
                    {
                        "foo": LocalPin(
                            name="foo",
                            version="1.0",
                            path=str(tmp_path),
                            subdirectory="my pkg",
                        )
                    }
                )
            )
        )
        url = tmp_path.resolve().as_uri()
        assert text.strip() == f"foo @ {url}#subdirectory=my%20pkg"

    def test_vcs_pin_round_trips_url(self) -> None:
        text = write_requirements_with_hashes(
            LockInput(
                targets=_one(
                    {
                        "foo": VcsPin(
                            name="foo",
                            version="1.0",
                            repo_url="git+https://example.com/r.git@abc",
                            bare_repo_url="https://example.com/r.git",
                            commit_id="abc",
                        ),
                    }
                )
            )
        )
        assert "foo @ git+https://example.com/r.git@abc" in text

    def test_index_pin_without_hashes_falls_back(self) -> None:
        bare = IndexPin(name="foo", version="1.0", index="pypi")
        text = write_requirements_with_hashes(LockInput(targets=_one({"foo": bare})))
        assert text.strip() == "foo==1.0"

    def test_writes_to_file(self, tmp_path: Path) -> None:
        out = tmp_path / "requirements.txt"
        text = write_requirements_with_hashes(
            LockInput(targets=_one({"foo": _index_pin()})),
            output_path=out,
        )
        assert out.read_text(encoding="utf-8") == text


class TestWriteRequirementsPerTarget:
    def test_blocks_sorted_by_label(self) -> None:
        # Blocks must come out in sorted label order regardless of the
        # targets insertion order, matching the pylock writer so
        # equivalent matrices declared in a different order render the
        # same bytes.
        py310 = _target("3.10")
        py311 = _target("3.11")
        text = write_requirements_without_hashes(
            LockInput(
                targets=_targets(
                    (py311, {"foo": _index_pin(version="2.0")}),
                    (py310, {"foo": _index_pin(version="1.0")}),
                )
            )
        )
        assert text.index(f"# {py310.label}") < text.index(f"# {py311.label}")

    def test_one_target_renders_flat(self) -> None:
        # One target is one installable file, so it carries no label header.
        text = write_requirements_without_hashes(
            LockInput(targets=_one({"foo": _index_pin(version="1.0")}))
        )
        assert text == "foo==1.0\n"


def test_lock_input_ignores_vcs_policy() -> None:
    """VcsConfig gates the provider, not the lock builder.

    Building the same resolve under ALLOW and BLOCK yields an identical
    lock input. The pin is VCS-sourced so a policy leaking into the
    builder would have something to act on.
    """
    lock = TargetLock(
        target=_HOST,
        pins={
            "foo": VcsPin(
                name="foo",
                version="1.0",
                repo_url="https://github.com/x/y.git",
                bare_repo_url="https://github.com/x/y.git",
                commit_id="a" * 40,
            )
        },
    )
    result = ResolveResult(
        targets=(_HOST,),
        target_results=[TargetResult(target=_HOST, success=True, lock=lock)],
    )

    allow = build_lock_input(
        result, config=NabProjectConfig(vcs=VcsConfig(policy=VcsPolicy.ALLOW))
    )
    block = build_lock_input(
        result, config=NabProjectConfig(vcs=VcsConfig(policy=VcsPolicy.BLOCK))
    )

    assert allow == block
    assert "foo" in allow.targets[_HOST.label].pins


class TestDependencyGraph:
    def test_base_deps_filtered_to_locked(self) -> None:
        provider = _FakeProvider(
            listings={
                name: [(Version("1.0"), _wheel_file(name))]
                for name in ("foo", "bar", "baz")
            },
            deps_cache={
                ("foo", Version("1.0")): dict.fromkeys(["bar", "missing"]),
                ("bar", Version("1.0")): {},
            },
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"foo": Version("1.0"), "bar": Version("1.0"), "baz": Version("1.0")},
            resolved_keys=("foo", "bar", "baz"),
        )
        # ``missing`` is not locked, so it is dropped; bar and baz have no
        # locked dependencies, so they are absent from the graph.  These
        # are all base (unconditional) edges, so both graphs agree.
        assert lock.dependencies == {"foo": ("bar",)}
        assert lock.base_dependencies == {"foo": ("bar",)}

    def test_activated_extra_edges_join_graph(self) -> None:
        provider = _FakeProvider(
            listings={
                name: [(Version("1.0"), _wheel_file(name))]
                for name in ("foo", "plugin")
            },
            deps_cache={("foo", Version("1.0")): {}},
            extra_deps_map={
                ("foo", Version("1.0")): {"cli": dict.fromkeys(["plugin"])}
            },
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"foo": Version("1.0"), "plugin": Version("1.0")},
            resolved_keys=("foo", "foo[cli]", "foo[doc]", "plugin"),
        )
        assert lock.dependencies == {"foo": ("plugin",)}
        # plugin is pulled only by the cli extra, so it is not a base edge.
        assert lock.base_dependencies == {}

    def test_umbrella_extra_drops_self_edge(self) -> None:
        # ``all`` pulls ``mypkg[graphviz]`` and ``mypkg[otel]``, so its recorded
        # deps name ``mypkg`` itself; the graph keeps only the transitive deps.
        provider = _FakeProvider(
            listings={
                name: [(Version("1.0"), _wheel_file(name))]
                for name in ("mypkg", "graphviz-lib", "otel-lib")
            },
            deps_cache={("mypkg", Version("1.0")): {}},
            extra_deps_map={
                ("mypkg", Version("1.0")): {
                    "all": dict.fromkeys(["mypkg[graphviz]", "mypkg[otel]"]),
                    "graphviz": dict.fromkeys(["graphviz-lib"]),
                    "otel": dict.fromkeys(["otel-lib"]),
                }
            },
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {
                "mypkg": Version("1.0"),
                "graphviz-lib": Version("1.0"),
                "otel-lib": Version("1.0"),
            },
            resolved_keys=(
                "mypkg",
                "mypkg[all]",
                "mypkg[graphviz]",
                "mypkg[otel]",
                "graphviz-lib",
                "otel-lib",
            ),
        )
        assert lock.dependencies == {"mypkg": ("graphviz-lib", "otel-lib")}
        # Both transitive deps arrive through extras, so neither is a base
        # edge and mypkg has no base dependencies.
        assert lock.base_dependencies == {}

    def test_emitted_as_pep751_dependencies(self) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {"foo": _index_pin("foo"), "bar": _index_pin("bar")},
                    {"foo": ("bar",)},
                )
            )
        )
        data = tomllib.loads(text)
        by_name = {p["name"]: p for p in data["packages"]}
        assert by_name["foo"]["dependencies"] == [{"name": "bar"}]
        assert "dependencies" not in by_name["bar"]

    def test_local_pin_carries_dependencies(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                targets=_one(
                    {
                        "foo": LocalPin(
                            name="foo", version="1.0", path=str(tmp_path / "foo")
                        ),
                        "bar": _index_pin("bar"),
                    },
                    {"foo": ("bar",)},
                )
            ),
            output_path=tmp_path / "pylock.toml",
        )
        data = tomllib.loads(text)
        by_name = {p["name"]: p for p in data["packages"]}
        assert by_name["foo"]["dependencies"] == [{"name": "bar"}]

    def test_entry_covering_two_targets_unions_edges(self) -> None:
        # One entry covers both targets, and they disagree on what ``foo``
        # depends on (a marker-gated dep answered differently), so the
        # emitted edges are the union over the targets the entry covers.
        pins = {
            "foo": _index_pin("foo"),
            "bar": _index_pin("bar"),
            "baz": _index_pin("baz"),
        }
        py310 = _target("3.10")
        py311 = _target("3.11")
        text = write_lock(
            LockInput(
                targets={
                    py310.label: TargetLock(
                        target=py310, pins=pins, dependencies={"foo": ("bar",)}
                    ),
                    py311.label: TargetLock(
                        target=py311, pins=pins, dependencies={"foo": ("baz",)}
                    ),
                }
            )
        )
        data = tomllib.loads(text)
        by_name = {p["name"]: p for p in data["packages"]}
        assert by_name["foo"]["dependencies"] == [{"name": "bar"}, {"name": "baz"}]


class TestMembershipGates:
    """Only-an-extra / only-a-group packages carry a membership marker.

    The lock declares its ``extras`` and ``dependency-groups``, and PEP
    751 has an installer default to no extras and to ``default-groups``,
    so every package the selection alone reaches has to say so in its
    own ``packages.marker``.
    """

    @staticmethod
    def _provider(
        names: Sequence[str],
        deps_cache: dict[tuple[str, Version], dict[str, object]] | None = None,
        extra_deps_map: dict[tuple[str, Version], dict[str, dict[str, object]]]
        | None = None,
    ) -> _FakeProvider:
        return _FakeProvider(
            listings={name: [(Version("1.0"), _wheel_file(name))] for name in names},
            deps_cache=deps_cache,
            extra_deps_map=extra_deps_map,
        )

    def test_extra_only_package_and_its_transitive_dep_are_gated(self) -> None:
        provider = self._provider(
            ("core", "mytool", "subtool"),
            deps_cache={
                ("core", Version("1.0")): {},
                ("mytool", Version("1.0")): dict.fromkeys(["subtool"]),
                ("subtool", Version("1.0")): {},
            },
        )
        lock = build_target_lock(
            provider,
            _HOST,
            dict.fromkeys(("core", "mytool", "subtool"), Version("1.0")),
            base_roots=frozenset({"core"}),
            selector_roots={("extra", "cli"): frozenset({"mytool"})},
        )
        assert lock.package_gates == {
            "mytool": (("extra", "cli"),),
            "subtool": (("extra", "cli"),),
        }

    def test_group_only_package_gates_on_dependency_groups(self) -> None:
        provider = self._provider(
            ("core", "mydev"),
            deps_cache={
                ("core", Version("1.0")): {},
                ("mydev", Version("1.0")): {},
            },
        )
        lock = build_target_lock(
            provider,
            _HOST,
            dict.fromkeys(("core", "mydev"), Version("1.0")),
            base_roots=frozenset({"core"}),
            selector_roots={("group", "dev"): frozenset({"mydev"})},
        )
        pylock = build_pylock(_lock_from(lock))
        assert {
            str(pkg.name): str(pkg.marker) if pkg.marker else None
            for pkg in pylock.packages
        } == {"core": None, "mydev": '"dev" in dependency_groups'}

    def test_package_two_selectors_reach_disjoins_both(self) -> None:
        """Reached by an extra and a group: either selection installs it."""
        provider = self._provider(
            ("shared",), deps_cache={("shared", Version("1.0")): {}}
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"shared": Version("1.0")},
            base_roots=frozenset(),
            selector_roots={
                ("extra", "cli"): frozenset({"shared"}),
                ("group", "dev"): frozenset({"shared"}),
            },
        )
        pylock = build_pylock(_lock_from(lock))
        (package,) = pylock.packages
        assert str(package.marker) == ('"dev" in dependency_groups or "cli" in extras')

    def test_extras_proxy_gates_only_what_the_extra_adds(self) -> None:
        """The project requires ``foo``; the extra requires ``foo[fancy]``.

        ``foo`` itself installs unconditionally; only what ``fancy``
        adds on top of it is gated.
        """
        provider = self._provider(
            ("foo", "fancy-lib"),
            deps_cache={
                ("foo", Version("1.0")): {},
                ("fancy-lib", Version("1.0")): {},
            },
            extra_deps_map={
                ("foo", Version("1.0")): {"fancy": dict.fromkeys(["fancy-lib"])}
            },
        )
        lock = build_target_lock(
            provider,
            _HOST,
            dict.fromkeys(("foo", "fancy-lib"), Version("1.0")),
            base_roots=frozenset({"foo"}),
            selector_roots={("extra", "cli"): frozenset({"foo", "foo[fancy]"})},
        )
        assert lock.package_gates == {"fancy-lib": (("extra", "cli"),)}

    def test_base_dependency_reached_through_a_cycle_is_not_gated(self) -> None:
        """A dependency cycle in the base closure terminates the walk."""
        provider = self._provider(
            ("core", "loop", "mytool"),
            deps_cache={
                ("core", Version("1.0")): dict.fromkeys(["loop"]),
                ("loop", Version("1.0")): dict.fromkeys(["core"]),
                ("mytool", Version("1.0")): dict.fromkeys(["loop"]),
            },
        )
        lock = build_target_lock(
            provider,
            _HOST,
            dict.fromkeys(("core", "loop", "mytool"), Version("1.0")),
            base_roots=frozenset({"core"}),
            selector_roots={("extra", "cli"): frozenset({"mytool"})},
        )
        assert lock.package_gates == {"mytool": (("extra", "cli"),)}

    def test_unpinned_dependency_is_skipped(self) -> None:
        """A dep name the resolve did not pin cannot be gated."""
        provider = self._provider(
            ("mytool",),
            deps_cache={("mytool", Version("1.0")): dict.fromkeys(["ghost"])},
        )
        lock = build_target_lock(
            provider,
            _HOST,
            {"mytool": Version("1.0")},
            base_roots=frozenset(),
            selector_roots={("extra", "cli"): frozenset({"mytool"})},
        )
        assert set(lock.package_gates) == {"mytool"}

    def test_no_selection_leaves_the_map_empty(self) -> None:
        provider = self._provider(("core",), deps_cache={("core", Version("1.0")): {}})
        lock = build_target_lock(
            provider, _HOST, {"core": Version("1.0")}, base_roots=frozenset({"core"})
        )
        assert lock.package_gates == {}

    def test_selector_roots_without_base_roots_are_refused(self) -> None:
        """Omitting ``base_roots`` would gate the base packages too.

        An empty ``base_roots`` is a project with no dependencies of its
        own, so it cannot double as "the caller did not say".
        """
        provider = self._provider(("core",), deps_cache={("core", Version("1.0")): {}})
        with pytest.raises(ValueError, match="need base_roots"):
            build_target_lock(
                provider,
                _HOST,
                {"core": Version("1.0")},
                selector_roots={("extra", "cli"): frozenset({"core"})},
            )

    def test_gate_stands_alone_when_every_target_agrees(self) -> None:
        """The selection, not the platform, is what selects the package."""
        provider = self._provider(
            ("core", "mytool"),
            deps_cache={
                ("core", Version("1.0")): {},
                ("mytool", Version("1.0")): {},
            },
        )
        pins = dict.fromkeys(("core", "mytool"), Version("1.0"))
        roots: dict[tuple[str, str], frozenset[str]] = {
            ("extra", "cli"): frozenset({"mytool"})
        }
        py310 = _target("3.10")
        py311 = _target("3.11")
        lock_input = LockInput(
            targets={
                t.label: build_target_lock(
                    provider,
                    t,
                    pins,
                    base_roots=frozenset({"core"}),
                    selector_roots=roots,
                )
                for t in (py310, py311)
            },
            extras=("cli",),
        )
        pylock = build_pylock(lock_input)
        assert {
            str(pkg.name): str(pkg.marker) if pkg.marker else None
            for pkg in pylock.packages
        } == {"core": None, "mytool": '"cli" in extras'}

    def test_target_specific_gate_keeps_its_environment_clause(self) -> None:
        """Only one target's extra reaches the package, so the env stays.

        The gate is joined by and onto that target's own marker, so an installer
        on the other target leaves the package out however it selects.
        """
        provider = self._provider(
            ("core", "mytool"),
            deps_cache={
                ("core", Version("1.0")): {},
                ("mytool", Version("1.0")): {},
            },
        )
        pins = dict.fromkeys(("core", "mytool"), Version("1.0"))
        py310 = _target("3.10")
        py311 = _target("3.11")
        lock_input = LockInput(
            targets={
                py310.label: build_target_lock(
                    provider,
                    py310,
                    pins,
                    base_roots=frozenset({"core"}),
                    selector_roots={("extra", "cli"): frozenset({"mytool"})},
                ),
                py311.label: build_target_lock(
                    provider,
                    py311,
                    pins,
                    base_roots=frozenset({"core", "mytool"}),
                    selector_roots={("extra", "cli"): frozenset()},
                ),
            },
            extras=("cli",),
        )
        pylock = build_pylock(lock_input)
        markers = {
            str(pkg.name): str(pkg.marker) if pkg.marker else None
            for pkg in pylock.packages
        }
        assert markers["core"] is None
        assert markers["mytool"] == (
            'platform_machine == "x86_64" and sys_platform == "linux"'
            ' and (("cli" in extras and python_version == "3.10")'
            ' or python_version == "3.11")'
        )


# Wheel tags spanning OS and arch, python levels, abi3, a free-threaded
# build, several manylinux glibc levels, and musllinux.  A pure-python wheel
# leads so the version survives on every declared target.
_WHEEL_TAG_CATALOG = (
    "py3-none-any",
    "py2.py3-none-any",
    "cp310-cp310-manylinux_2_17_x86_64",
    "cp311-cp311-manylinux_2_17_x86_64",
    "cp311-cp311-manylinux_2_28_x86_64",
    "cp311-cp311-manylinux_2_34_x86_64",
    "cp312-cp312-manylinux_2_17_x86_64",
    "cp311-cp311-musllinux_1_2_x86_64",
    "cp311-cp311-manylinux_2_17_aarch64",
    "cp311-abi3-manylinux_2_17_x86_64",
    "cp39-abi3-manylinux_2_17_x86_64",
    "cp313-cp313t-manylinux_2_17_x86_64",
    "cp311-cp311-macosx_11_0_arm64",
    "cp311-cp311-macosx_14_0_arm64",
    "cp39-abi3-macosx_11_0_arm64",
    "cp311-cp311-macosx_10_9_x86_64",
    "cp311-cp311-win_amd64",
)


def _tag_wheel(tag: str, version: str = "1.0", package: str = "pkg") -> WheelFile:
    """An index-listing wheel carrying an explicit tag, with a hash for the lock."""
    filename = f"{package}-{version}-{tag}.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=(("sha256", "a" * 64),),
    )


def _tag_sdist(version: str = "1.0", package: str = "pkg") -> SdistFile:
    filename = f"{package}-{version}.tar.gz"
    return SdistFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=None,
        upload_time=None,
        hashes=(("sha256", "b" * 64),),
    )


_PRUNE_LINUX = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)
_PRUNE_MACOS = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("macos_arm64")
)


def _pkg_entry(pylock: object) -> object:
    """The single ``pkg`` package entry of a built pylock."""
    entries = [p for p in pylock.packages if str(p.name) == "pkg"]  # type: ignore[attr-defined]
    assert len(entries) == 1
    return entries[0]


def _emitted_wheel_names(pkg_entry: object) -> set[str]:
    return {w.name for w in (pkg_entry.wheels or [])}  # type: ignore[attr-defined]


class TestLockWheelPrunePredicate:
    """The lock keeps a wheel iff a contributing faithful target accepts it."""

    def _resolve_matrix(
        self, tags: Sequence[str], targets: Sequence[ResolveTarget]
    ) -> ResolveResult:
        coordinator = make_coordinator(
            listings={"pkg": [_tag_wheel(t) for t in tags]}, auto_metadata=True
        )
        return resolve_with_coordinator(
            coordinator,
            list(targets),
            [Requirement("pkg")],
            config=NabProjectConfig(build_policy=BuildPolicy.NEVER),
        )

    def test_property_union_over_seeded_subsets(self) -> None:
        """Over seeded random subsets the union is exactly the accepted wheels."""
        targets = (_PRUNE_LINUX, _PRUNE_MACOS)
        rng = random.Random(20260722)  # noqa: S311
        for _ in range(40):
            present = [t for t in _WHEEL_TAG_CATALOG[1:] if rng.random() < 0.5]
            # Keep the pure-python wheel so every target holds the version.
            tags = ["py3-none-any", *present]
            filenames = [f"pkg-1.0-{t}.whl" for t in tags]

            result = self._resolve_matrix(tags, targets)
            assert result.success

            accepted_by_some = {
                fn for fn in filenames if any(t.tags.accepts(fn) for t in targets)
            }
            rejected_by_all = {
                fn
                for fn in filenames
                if all(t.tags_faithful and not t.tags.accepts(fn) for t in targets)
            }

            emitted = _emitted_wheel_names(
                _pkg_entry(build_pylock(build_lock_input(result)))
            )
            assert emitted == accepted_by_some
            assert emitted.isdisjoint(rejected_by_all)

            # Each target's own pin carries exactly the wheels it accepts.
            for tr in result.target_results:
                assert tr.lock is not None
                pin = tr.lock.pins["pkg"]
                assert isinstance(pin, IndexPin)
                pin_names = {w.filename for w in pin.wheels}
                assert pin_names == {
                    fn for fn in filenames if tr.target.tags.accepts(fn)
                }

    def test_union_keeps_both_families_and_drops_windows(self) -> None:
        """A linux+macos union keeps both platforms and drops the never-declared one."""
        result = self._resolve_matrix(
            [
                "py3-none-any",
                "cp311-cp311-manylinux_2_17_x86_64",
                "cp311-cp311-macosx_11_0_arm64",
                "cp311-cp311-win_amd64",
            ],
            (_PRUNE_LINUX, _PRUNE_MACOS),
        )
        assert result.success
        emitted = _emitted_wheel_names(
            _pkg_entry(build_pylock(build_lock_input(result)))
        )
        assert emitted == {
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
            "pkg-1.0-cp311-cp311-macosx_11_0_arm64.whl",
        }

    def test_non_faithful_overlay_keeps_every_wheel(self) -> None:
        """An overlay moves the markers off the tag axis, so no wheel is pruned."""
        overlay = _PRUNE_LINUX.with_marker_overrides({"sys_platform": "win32"})
        assert not overlay.tags_faithful
        result = self._resolve_matrix(
            [
                "cp311-cp311-manylinux_2_17_x86_64",
                "cp311-cp311-macosx_11_0_arm64",
                "cp311-cp311-win_amd64",
            ],
            (overlay,),
        )
        assert result.success
        lock = result.target_results[0].lock
        assert lock is not None
        pin = lock.pins["pkg"]
        assert isinstance(pin, IndexPin)
        assert {w.filename for w in pin.wheels} == {
            "pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
            "pkg-1.0-cp311-cp311-macosx_11_0_arm64.whl",
            "pkg-1.0-cp311-cp311-win_amd64.whl",
        }

    def test_no_compatible_wheel_and_no_sdist_fails_loudly(self) -> None:
        """A version pruned to nothing fails via the existing no-candidate error."""
        result = self._resolve_matrix(["cp311-cp311-win_amd64"], (_PRUNE_LINUX,))
        assert not result.success
        error = result.target_results[0].error
        assert error is not None
        assert "none of the wheel's tags are compatible" in str(error)

    def test_sdist_survives_when_every_wheel_is_pruned(self) -> None:
        """The same pruned version keeps its sdist and pins the sdist alone."""
        coordinator = make_coordinator(
            listings={"pkg": [_tag_wheel("cp311-cp311-win_amd64"), _tag_sdist()]},
            auto_metadata=True,
            sdist_pkg_info="Metadata-Version: 2.4\nName: pkg\nVersion: 1.0\n\n",
        )
        result = resolve_with_coordinator(
            coordinator,
            [_PRUNE_LINUX],
            [Requirement("pkg")],
            config=NabProjectConfig(build_policy=BuildPolicy.NEVER),
        )
        assert result.success
        lock = result.target_results[0].lock
        assert lock is not None
        pin = lock.pins["pkg"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels == ()
        assert pin.sdist is not None
        assert pin.sdist.filename == "pkg-1.0.tar.gz"


_BUILDER_LOGGER = "nab_python._lockfile.builder"


class TestLockPruneObservability:
    """The builder reports, per pinned package, how many wheels it omitted."""

    def test_debug_line_only_for_a_pruning_package(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A real resolve logs one line for the pruned package, none for the clean one."""
        coordinator = make_coordinator(
            listings={
                "pruner": [
                    _tag_wheel("py3-none-any", package="pruner"),
                    _tag_wheel("cp311-cp311-win_amd64", package="pruner"),
                ],
                "clean": [_tag_wheel("py3-none-any", package="clean")],
            },
            auto_metadata=True,
        )
        with caplog.at_level(logging.DEBUG, logger=_BUILDER_LOGGER):
            result = resolve_with_coordinator(
                coordinator,
                [_PRUNE_LINUX],
                [Requirement("pruner"), Requirement("clean")],
                config=NabProjectConfig(build_policy=BuildPolicy.NEVER),
            )
        assert result.success
        messages = [r.getMessage() for r in caplog.records if r.name == _BUILDER_LOGGER]
        pruner_lines = [m for m in messages if "pruner" in m]
        assert len(pruner_lines) == 1
        assert "1" in pruner_lines[0]
        assert not [m for m in messages if "clean" in m]

    def test_builder_reads_the_provider_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The line reflects the provider's ``tag_excluded_wheel_count`` reading."""
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            tag_excluded_counts={("foo", Version("1.0")): 3},
        )
        with caplog.at_level(logging.DEBUG, logger=_BUILDER_LOGGER):
            build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        messages = [r.getMessage() for r in caplog.records if r.name == _BUILDER_LOGGER]
        assert len(messages) == 1
        assert "foo" in messages[0]
        assert "3" in messages[0]

    def test_no_line_when_nothing_pruned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A package with a zero count emits no debug line."""
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
        )
        with caplog.at_level(logging.DEBUG, logger=_BUILDER_LOGGER):
            build_target_lock(provider, _HOST, {"foo": Version("1.0")})
        assert not [r for r in caplog.records if r.name == _BUILDER_LOGGER]


def _selection_pin(name: str, version: str) -> IndexPin:
    # PEP 427 escapes ``-`` to ``_`` in the wheel filename's name field.
    escaped = canonicalize_name(name).replace("-", "_")
    return IndexPin(
        name=name,
        version=version,
        index="pypi",
        wheels=(
            WheelArtifact(
                filename=f"{escaped}-{version}-py3-none-any.whl",
                url=f"https://example.com/{escaped}-{version}-py3-none-any.whl",
                hashes=(("sha256", "a" * 64),),
            ),
        ),
    )


class TestConflictForkNegatedEmission:
    """A conflict fork's per-package marker negates its conflict co-members.

    nab emits ``"cpu" in extras and "gpu" not in extras`` for the cpu
    fork, not the bare positive clause, so a PEP 751 consumer that never
    reads ``[tool.nab].conflicts`` still installs at most one fork.  Built
    and emitted through nab's own ``write_lock``, then read back by
    packaging's reference consumer ``Pylock.select``.
    """

    _BASE: ClassVar[ResolveTarget] = _target(python_version="3.12")
    _ENV: ClassVar[dict[str, str]] = dict(_target(python_version="3.12").marker_env)
    _CPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "cpu"),)
    _GPU: ClassVar[tuple[tuple[str, str], ...]] = (("extra", "gpu"),)

    def _lock(self) -> Pylock:
        cpu = self._BASE.with_selection(self._CPU)
        gpu = self._BASE.with_selection(self._GPU)

        targets = {
            cpu.label: TargetLock(
                target=cpu,
                pins={
                    "onnxruntime": _selection_pin("onnxruntime", "1.20.0"),
                    "torch": _selection_pin("torch", "2.5.0"),
                },
            ),
            gpu.label: TargetLock(
                target=gpu,
                pins={
                    "onnxruntime-gpu": _selection_pin("onnxruntime-gpu", "1.20.0"),
                    "torch": _selection_pin("torch", "2.6.0"),
                },
            ),
        }

        conflicts = (
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "cpu"),
                    ConflictMember(ConflictKind.EXTRA, "gpu"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
        )

        text = write_lock(
            LockInput(
                targets=targets,
                env_base_names={_env_signature(cpu): frozenset()},
                extras=("cpu", "gpu"),
                conflicts=conflicts,
            )
        )
        return Pylock.from_dict(tomllib.loads(text))

    def _select(self, pylock: Pylock, extras: list[str]) -> set[str]:
        return {
            str(pkg.name)
            for pkg, _ in pylock.select(
                environment=self._ENV,  # type: ignore[arg-type]
                extras=extras,
                dependency_groups=(),
            )
        }

    def test_emitted_markers_carry_the_negation(self) -> None:
        pylock = self._lock()
        by_name = {str(p.name): str(p.marker) for p in pylock.packages}
        assert '"cpu" in extras and "gpu" not in extras' in by_name["onnxruntime"]
        assert '"gpu" in extras and "cpu" not in extras' in by_name["onnxruntime-gpu"]

    def test_select_both_extras_installs_neither_conflicting_name(self) -> None:
        # {cpu, gpu} together matches neither fork's pin.
        pylock = self._lock()
        assert self._select(pylock, ["cpu", "gpu"]) == set()

    def test_single_extra_selection_unchanged(self) -> None:
        pylock = self._lock()
        assert self._select(pylock, ["cpu"]) == {"onnxruntime", "torch"}
        assert self._select(pylock, ["gpu"]) == {"onnxruntime-gpu", "torch"}

    def test_empty_extras_selects_nothing(self) -> None:
        pylock = self._lock()
        assert self._select(pylock, []) == set()

    def test_same_name_fork_no_double_select(self) -> None:
        # Both torch entries share a name; {cpu, gpu} selects neither.
        pylock = self._lock()
        assert "torch" not in self._select(pylock, ["cpu", "gpu"])

    def test_partitions_are_disjoint_without_the_conflict_universe(self) -> None:
        # The emitted markers are disjoint with no conflict-declaration
        # universe; the constraint lives in the marker.
        pylock = self._lock()
        by_name = {str(p.name): p.marker for p in pylock.packages}
        left = MarkerSet.from_marker(str(by_name["onnxruntime"]))
        right = MarkerSet.from_marker(str(by_name["onnxruntime-gpu"]))
        assert left.is_disjoint(right)

        torch_markers = [
            MarkerSet.from_marker(str(p.marker))
            for p in pylock.packages
            if str(p.name) == "torch"
        ]
        assert len(torch_markers) == 2
        assert torch_markers[0].is_disjoint(torch_markers[1])

    def test_gate_accepts_partitions_without_the_conflict_universe(self) -> None:
        # The torch pair passes the gate with empty ``exclusive_groups``;
        # the negation in the markers makes the entries disjoint.
        pylock = self._lock()
        validate_marker_disjointness(
            pylock.packages,
            environments={"env": self._ENV},
            extras=("cpu", "gpu"),
            groups=(),
            exclusive_groups=(),
        )

    def test_two_conflict_sets_negate_only_within_set(self) -> None:
        # A member is negated only within its own conflict set: cpu
        # excludes gpu, mkl excludes openblas, never across sets.
        cpu_mkl = self._BASE.with_selection((("extra", "cpu"), ("extra", "mkl")))
        gpu_blas = self._BASE.with_selection((("extra", "gpu"), ("extra", "openblas")))

        targets = {
            cpu_mkl.label: TargetLock(
                target=cpu_mkl, pins={"fast": _selection_pin("fast", "1.0")}
            ),
            gpu_blas.label: TargetLock(
                target=gpu_blas, pins={"slow": _selection_pin("slow", "1.0")}
            ),
        }

        conflicts = (
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "cpu"),
                    ConflictMember(ConflictKind.EXTRA, "gpu"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "mkl"),
                    ConflictMember(ConflictKind.EXTRA, "openblas"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
        )

        text = write_lock(
            LockInput(
                targets=targets,
                env_base_names={_env_signature(cpu_mkl): frozenset()},
                extras=("cpu", "gpu", "mkl", "openblas"),
                conflicts=conflicts,
            )
        )
        pylock = Pylock.from_dict(tomllib.loads(text))

        fast = next(str(p.marker) for p in pylock.packages if str(p.name) == "fast")
        assert '"cpu" in extras' in fast
        assert '"mkl" in extras' in fast
        assert '"gpu" not in extras' in fast
        assert '"openblas" not in extras' in fast

    def test_single_set_draw_negates_only_that_set(self) -> None:
        # Drawing from one of two sets negates that set's co-members
        # alone; the other set stays open, so {cpu, mkl} installs cpu.
        cpu = self._BASE.with_selection((("extra", "cpu"),))
        gpu = self._BASE.with_selection((("extra", "gpu"),))

        targets = {
            cpu.label: TargetLock(
                target=cpu,
                pins={"onnxruntime": _selection_pin("onnxruntime", "1.20.0")},
            ),
            gpu.label: TargetLock(
                target=gpu,
                pins={"onnxruntime-gpu": _selection_pin("onnxruntime-gpu", "1.20.0")},
            ),
        }

        conflicts = (
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "cpu"),
                    ConflictMember(ConflictKind.EXTRA, "gpu"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "mkl"),
                    ConflictMember(ConflictKind.EXTRA, "openblas"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
        )

        text = write_lock(
            LockInput(
                targets=targets,
                env_base_names={_env_signature(cpu): frozenset()},
                extras=("cpu", "gpu", "mkl", "openblas"),
                conflicts=conflicts,
            )
        )
        pylock = Pylock.from_dict(tomllib.loads(text))

        cpu_marker = next(
            str(p.marker) for p in pylock.packages if str(p.name) == "onnxruntime"
        )
        assert '"gpu" not in extras' in cpu_marker
        assert '"mkl" not in extras' not in cpu_marker
        assert '"openblas" not in extras' not in cpu_marker
        assert self._select(pylock, ["cpu", "mkl"]) == {"onnxruntime"}

    def test_three_large_sets_write_lock_within_budget(self) -> None:
        # Three declared 5-member at-most-one sets, each fork drawing one
        # member per set, so every per-package marker negates 12
        # co-members.  The disjointness gate binds them through the
        # selection instead of the full membership powerset, so the lock
        # is written rather than raising IntractableMarkerSet.
        sets = [tuple(f"{letter}{i}" for i in range(5)) for letter in ("a", "b", "c")]
        all_names = tuple(name for members in sets for name in members)

        fork_one = self._BASE.with_selection(
            (("extra", "a0"), ("extra", "b0"), ("extra", "c0"))
        )
        fork_two = self._BASE.with_selection(
            (("extra", "a1"), ("extra", "b1"), ("extra", "c1"))
        )

        targets = {
            fork_one.label: TargetLock(
                target=fork_one, pins={"torch": _selection_pin("torch", "2.5.0")}
            ),
            fork_two.label: TargetLock(
                target=fork_two, pins={"torch": _selection_pin("torch", "2.6.0")}
            ),
        }

        conflicts = tuple(
            ConflictSet(
                members=tuple(
                    ConflictMember(ConflictKind.EXTRA, name) for name in members
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            )
            for members in sets
        )

        text = write_lock(
            LockInput(
                targets=targets,
                env_base_names={_env_signature(fork_one): frozenset()},
                extras=all_names,
                conflicts=conflicts,
            )
        )
        pylock = Pylock.from_dict(tomllib.loads(text))

        torch_markers = {
            str(p.version): str(p.marker)
            for p in pylock.packages
            if str(p.name) == "torch"
        }
        assert set(torch_markers) == {"2.5.0", "2.6.0"}

        # simplify overruns the cell budget on this fork, so the emitter
        # passes the raw marker through unchanged: base env, the fork's three
        # drawn members, then the 12 negated co-members by set in declaration
        # order.
        base = (
            'python_version == "3.12" and sys_platform == "linux"'
            ' and platform_machine == "x86_64"'
        )

        def raw_marker(drawn: tuple[str, str, str]) -> str:
            positives = " and ".join(f'"{name}" in extras' for name in drawn)
            negatives = " and ".join(
                f'"{name}" not in extras'
                for members in sets
                for name in members
                if name not in drawn
            )
            return f"{base} and {positives} and {negatives}"

        assert torch_markers["2.5.0"] == raw_marker(("a0", "b0", "c0"))
        assert torch_markers["2.6.0"] == raw_marker(("a1", "b1", "c1"))


class TestConflictSetCrossGating:
    """Two engaged conflict sets gate a package only along the set it varies over.

    ``--all-groups`` over two declared sets forks into their cartesian
    product.  A package one member of one set pulls in is the same in
    every fork of the other set, so its marker names only its own set;
    conjoining a member of both leaves a selection naming a member of one
    set alone matching no entry, which under-installs silently.  The
    projection stops wherever a dependency cannot follow it.

    The fixture mirrors what a resolve produces for a project with
    ``a1 = [six==1.16.0]``, ``a2 = [six==1.15.0]``,
    ``b1/b2/b3 = [idna==3.7/3.6/3.4]``, a base dependency on
    ``packaging``, and ``conflicts = [[a1, a2], [b1, b2, b3]]``.
    """

    _BASE: ClassVar[ResolveTarget] = _target(python_version="3.12")
    _ENV: ClassVar[dict[str, str]] = dict(_target(python_version="3.12").marker_env)
    _A: ClassVar[dict[str, str]] = {"a1": "1.16.0", "a2": "1.15.0"}
    _B: ClassVar[dict[str, str]] = {"b1": "3.7", "b2": "3.6", "b3": "3.4"}

    @property
    def _conflicts(self) -> tuple[ConflictSet, ...]:
        return tuple(
            ConflictSet(
                members=tuple(ConflictMember(ConflictKind.GROUP, m) for m in members),
                policy=ConflictPolicy.AT_MOST_ONE,
            )
            for members in (self._A, self._B)
        )

    def _forked_lock(
        self,
        contribution: Callable[
            [str, str],
            tuple[dict[str, PinShape], dict[str, tuple[tuple[str, str], ...]]],
        ],
        base_names: frozenset[str] = frozenset(),
        edges: Callable[[str, str], dict[str, tuple[str, ...]]] | None = None,
    ) -> Pylock:
        """Emit the 2x3 product of the a and b sets, one fork per point."""
        targets: dict[str, TargetLock] = {}
        for a, b in itertools.product(self._A, self._B):
            fork = self._BASE.with_selection((("group", a), ("group", b)))
            pins, gates = contribution(a, b)
            targets[fork.label] = TargetLock(
                target=fork,
                pins=pins,
                dependencies=edges(a, b) if edges is not None else {},
                package_gates=gates,
            )
        text = write_lock(
            LockInput(
                targets=targets,
                env_base_names={_env_signature(self._BASE): base_names},
                dependency_groups=(*self._A, *self._B),
                conflicts=self._conflicts,
            )
        )
        return Pylock.from_dict(tomllib.loads(text))

    def _lock(self) -> Pylock:
        def contribution(
            a: str, b: str
        ) -> tuple[dict[str, PinShape], dict[str, tuple[tuple[str, str], ...]]]:
            return (
                {
                    "packaging": _selection_pin("packaging", "24.0"),
                    "six": _selection_pin("six", self._A[a]),
                    "idna": _selection_pin("idna", self._B[b]),
                },
                # What ``_membership_gates`` derives: the group whose
                # requirements reach the package, and nothing for the
                # package the project requires itself.
                {"six": (("group", a),), "idna": (("group", b),)},
            )

        return self._forked_lock(contribution, base_names=frozenset({"packaging"}))

    def _select(self, pylock: Pylock, groups: Sequence[str]) -> set[str]:
        return {
            f"{pkg.name} {pkg.version}"
            for pkg, _ in pylock.select(
                environment=self._ENV,  # type: ignore[arg-type]
                extras=(),
                dependency_groups=groups,
            )
        }

    def _dangling(self, pylock: Pylock) -> list[tuple[tuple[str, ...], str, list[str]]]:
        """Selections where a selected entry declares a dep the selection misses."""
        out = []
        for a, b in itertools.product([None, *self._A], [None, *self._B]):
            groups = tuple(name for name in (a, b) if name is not None)
            chosen = [
                pkg
                for pkg, _ in pylock.select(
                    environment=self._ENV,  # type: ignore[arg-type]
                    extras=(),
                    dependency_groups=groups,
                )
            ]
            names = {str(pkg.name) for pkg in chosen}
            for pkg in chosen:
                missing = sorted(
                    {str(dep["name"]) for dep in (pkg.dependencies or ())} - names
                )
                if missing:
                    out.append((groups, str(pkg.name), missing))
        return out

    def test_marker_names_only_the_set_the_package_varies_over(self) -> None:
        by_key = {
            (str(p.name), str(p.version)): str(p.marker) for p in self._lock().packages
        }

        six = by_key["six", "1.16.0"]
        assert '"a1" in dependency_groups' in six
        assert '"a2" not in dependency_groups' in six
        assert not any(member in six for member in self._B)

        idna = by_key["idna", "3.7"]
        assert '"b1" in dependency_groups' in idna
        assert '"b2" not in dependency_groups' in idna
        assert '"b3" not in dependency_groups' in idna
        assert not any(member in idna for member in self._A)

    def test_base_dependency_keeps_its_unconditional_entry(self) -> None:
        packaging = next(p for p in self._lock().packages if str(p.name) == "packaging")
        assert packaging.marker is None

    @pytest.mark.parametrize("a", [None, "a1", "a2"])
    @pytest.mark.parametrize("b", [None, "b1", "b2", "b3"])
    def test_every_legal_selection_installs_what_it_asked_for(
        self, a: str | None, b: str | None
    ) -> None:
        # at-most-one permits selecting none of a set, so the legal
        # selections are the product of each set plus its empty case.
        groups = [name for name in (a, b) if name is not None]
        expected = {"packaging 24.0"}
        if a is not None:
            expected.add(f"six {self._A[a]}")
        if b is not None:
            expected.add(f"idna {self._B[b]}")
        assert self._select(self._lock(), groups) == expected

    def test_entries_of_one_name_stay_disjoint(self) -> None:
        # The forks stay mutually exclusive in the markers alone, with no
        # conflict-declaration universe to lean on.
        pylock = self._lock()
        for name in ("six", "idna"):
            markers = [
                MarkerSet.from_marker(str(p.marker))
                for p in pylock.packages
                if str(p.name) == name
            ]
            for left, right in itertools.combinations(markers, 2):
                assert left.is_disjoint(right)

    def test_disjointness_gate_accepts_the_projected_markers(self) -> None:
        validate_marker_disjointness(
            self._lock().packages,
            environments={"env": self._ENV},
            extras=(),
            groups=(*self._A, *self._B),
            exclusive_groups=conflict_exclusion_groups(self._conflicts),
        )

    def test_a_package_varying_over_both_sets_keeps_both(self) -> None:
        # attrs is pinned by the a-member and the b-member together, so
        # neither set is flat and the marker keeps the full conjunction.
        pylock = self._forked_lock(
            lambda a, b: (
                {"attrs": _selection_pin("attrs", f"{a[-1]}.{b[-1]}")},
                {"attrs": (("group", a), ("group", b))},
            )
        )
        marker = next(str(p.marker) for p in pylock.packages if str(p.version) == "1.1")
        assert '"a1" in dependency_groups' in marker
        assert '"b1" in dependency_groups' in marker
        assert '"a2" not in dependency_groups' in marker
        assert '"b2" not in dependency_groups' in marker
        assert '"b3" not in dependency_groups' in marker

    def test_member_only_package_flat_everywhere_keeps_the_set_that_reaches_it(
        self,
    ) -> None:
        # attrs is the same in all six forks, and its gate says the
        # a-members are what reach it, so the b-set drops and the a-set
        # stays: a1 alone installs it, no member at all does not.
        pylock = self._forked_lock(
            lambda a, _b: (
                {"attrs": _selection_pin("attrs", "24.0")},
                {"attrs": (("group", a),)},
            )
        )
        assert self._select(pylock, ["a1"]) == {"attrs 24.0"}
        assert self._select(pylock, ["b1"]) == set()
        assert self._select(pylock, []) == set()

    def test_a_dependency_either_set_reaches_installs_for_either_alone(self) -> None:
        # attrs is the same in every fork and a member of each set
        # requires it, so neither set is what selects it and the gate
        # stands alone.
        pylock = self._forked_lock(
            lambda a, b: (
                {"attrs": _selection_pin("attrs", "24.0")},
                {"attrs": (("group", a), ("group", b))},
            )
        )
        marker = next(str(p.marker) for p in pylock.packages)
        assert "not in dependency_groups" not in marker
        for member in (*self._A, *self._B):
            assert self._select(pylock, [member]) == {"attrs 24.0"}
        assert self._select(pylock, []) == set()

    def test_a_dependency_two_members_share_installs_for_either_alone(self) -> None:
        # a1 and b1 both name attrs, so the forks that select neither do
        # not carry it at all; a1 alone and b1 alone still install it.
        def contribution(
            a: str, b: str
        ) -> tuple[dict[str, PinShape], dict[str, tuple[tuple[str, str], ...]]]:
            named = tuple(("group", m) for m in (a, b) if m in {"a1", "b1"})
            if not named:
                return ({}, {})
            return ({"attrs": _selection_pin("attrs", "24.0")}, {"attrs": named})

        pylock = self._forked_lock(contribution)
        assert self._select(pylock, ["a1"]) == {"attrs 24.0"}
        assert self._select(pylock, ["b1"]) == {"attrs 24.0"}
        assert self._select(pylock, ["a2", "b2"]) == set()
        assert self._select(pylock, []) == set()

    def test_a_drop_a_dependency_cannot_follow_is_refused(self) -> None:
        # attrs is the same in every fork of the b-set but the idna it
        # requires is not, so attrs keeps the b-clauses rather than
        # installing into a selection idna cannot reach.
        pylock = self._forked_lock(
            lambda a, b: (
                {
                    "attrs": _selection_pin("attrs", "24.0"),
                    "idna": _selection_pin("idna", self._B[b]),
                },
                {"attrs": (("group", a),), "idna": (("group", a),)},
            ),
            edges=lambda _a, _b: {"attrs": ("idna",)},
        )
        assert self._dangling(pylock) == []
        assert self._select(pylock, ["a1"]) == set()
        assert self._select(pylock, ["a1", "b1"]) == {"attrs 24.0", "idna 3.7"}

    def test_an_edge_only_one_fork_carries_holds_the_whole_entry(self) -> None:
        # The entry's edges are the union over its forks, so the idna
        # only the b1 forks require rides on attrs everywhere; a fork
        # that does not carry idna at all projects nothing.
        def contribution(
            a: str, b: str
        ) -> tuple[dict[str, PinShape], dict[str, tuple[tuple[str, str], ...]]]:
            pins: dict[str, PinShape] = {"attrs": _selection_pin("attrs", "24.0")}
            gates = {"attrs": (("group", a),)}
            if b == "b1":
                pins["idna"] = _selection_pin("idna", "3.7")
                gates["idna"] = (("group", a),)
            return (pins, gates)

        pylock = self._forked_lock(
            contribution,
            edges=lambda _a, b: {"attrs": ("idna",)} if b == "b1" else {},
        )
        assert self._select(pylock, ["a1", "b1"]) == {"attrs 24.0", "idna 3.7"}
        assert self._select(pylock, ["a1"]) == set()

    def test_a_fork_the_base_reaches_blocks_the_drop(self) -> None:
        # attrs is ungated in the b1 forks, so those install it whichever
        # a-member is selected while the others need their own; no one
        # set of clauses covers both, and the b-set stays named.
        pylock = self._forked_lock(
            lambda _a, b: (
                {"attrs": _selection_pin("attrs", "24.0")},
                {} if b == "b1" else {"attrs": (("group", b),)},
            )
        )
        assert self._select(pylock, ["b1"]) == {"attrs 24.0"}
        assert self._select(pylock, ["b2"]) == {"attrs 24.0"}
        assert self._select(pylock, ["a1"]) == set()
        assert self._select(pylock, []) == set()

    def test_reach_that_differs_across_the_dropped_set_blocks_the_drop(self) -> None:
        # attrs is reached through the a-member in the b1 forks and
        # through the b-member elsewhere, so the b-set is not something
        # attrs is indifferent to and its clauses stay.
        pylock = self._forked_lock(
            lambda a, b: (
                {"attrs": _selection_pin("attrs", "24.0")},
                {"attrs": ((("group", a),) if b == "b1" else (("group", b),))},
            )
        )
        assert self._select(pylock, ["b2"]) == {"attrs 24.0"}
        assert self._select(pylock, ["a1", "b1"]) == {"attrs 24.0"}
        assert self._select(pylock, ["b1"]) == set()
        assert self._select(pylock, ["a1"]) == set()
