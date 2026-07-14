"""Tests for nab_python.lockfile (PEP 751 emission)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, cast

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import IndexConfig
from nab_python._lockfile.builder import _common_requires_python
from nab_python._lockfile.disjointness import (
    _restrict_to_referenced,
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
from nab_python._vendor.packaging.pylock import Package, PackageWheel, Pylock
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
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
        assert data["packages"][0]["marker"] == str(Marker(py310.marker_string))

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
        assert v1_marker == str(Marker(py310.marker_string))
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

    def test_powerset_pruned_when_no_marker_uses_extras(self) -> None:
        # Airflow declares ~120 extras; materialising 2**120 subsets
        # OOMs the validator.  When no candidate marker references
        # ``extras``, the powerset must collapse to ``{()}`` and the
        # validator must finish without enumerating subsets.
        envs = {
            "linux": {
                "python_version": "3.11",
                "sys_platform": "linux",
                "platform_machine": "x86_64",
            },
        }
        many_extras = tuple(f"e{i}" for i in range(120))
        # Markers that only constrain the environment must succeed
        # quickly even with 120 declared extras: 2**120 powerset is
        # untenable; pruning to 1 subset is what makes this finish.
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
        # case and separator, so the powerset pruning must normalize
        # both sides; otherwise both extras drop out of the universe
        # and the {cpu, fast-io} collision is silently missed.
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

    def test_powerset_pruned_when_no_marker_uses_groups(self) -> None:
        # Symmetric pruning for ``dependency_groups``: a project with
        # many declared groups whose markers do not reference the
        # variable must not enumerate ``2**N`` subsets.
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

    def test_powerset_falls_back_when_bare_token_without_named_literal(self) -> None:
        # Defensive path: a marker that mentions the bare ``extras``
        # token but does not match the literal-extraction regex must
        # fall back to the full declared list so a real collision
        # still surfaces.  Construct a minimal stand-in marker that
        # exercises this branch through the public ``Marker``
        # interface without subclassing the frozen Package dataclass.
        class _BareTokenMarker:
            def __str__(self) -> str:
                # Bare ``extras`` token in a context the literal regex
                # does not match (a ``not (...)`` wrapper round a
                # comparison the regex does not anticipate).
                return "extras and python_version >= '3.10'"

        relevant = _restrict_to_referenced(
            ("a", "b", "c"),
            [cast("Marker", _BareTokenMarker())],
            "extras",
        )
        # No literal extracted, but the bare token appears -> safe
        # over-approximation falls back to the full declared list.
        assert relevant == ("a", "b", "c")

    def test_powerset_pruned_to_referenced_extras_only(self) -> None:
        # Of 50 declared extras, only ``"cpu"`` and ``"gpu"`` appear
        # in markers; the powerset must restrict to those two so we
        # iterate 4 subsets, not 2**50.  The collision on
        # ``{cpu, gpu}`` is still reported.
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
        :class:`Marker`, so the empty-extras point in
        :func:`_enumerate_valid_points` never makes two membership-gated
        entries collide.  Asserts the marker-eval primitive in isolation
        so a later switch to a different Marker library cannot regress
        this without breaking a focused test."""
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


def test_vcs_config_unused_in_lockfile_path() -> None:
    """VcsConfig is consumed by the provider; lockfile builder ignores it."""
    cfg = VcsConfig(policy=VcsPolicy.BLOCK)
    assert cfg.policy is VcsPolicy.BLOCK


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
        # locked dependencies, so they are absent from the graph.
        assert lock.dependencies == {"foo": ("bar",)}

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
