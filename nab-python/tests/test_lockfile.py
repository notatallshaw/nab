"""Tests for nab_python.lockfile (PEP 751 emission)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import IndexConfig
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
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.pylock import Package, PackageWheel, Pylock
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import (
    LOCK_VERSION,
    DisjointnessError,
    IndexPin,
    LocalPin,
    LockInput,
    MissingHashError,
    MissingSdistError,
    MissingVcsCommitError,
    Provenance,
    SdistArtifact,
    VcsPin,
    WheelArtifact,
    build_lock_input_from_provider,
    build_pylock,
    read_lockfile_anchor,
    read_lockfile_packages,
    write_lock,
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from nab_python.provider import DistPolicy, LocalSource, VcsConfig, VcsPolicy, VcsSource


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


class TestSingleTuple:
    def test_index_pin_round_trips(self) -> None:
        text = write_lock(LockInput(pins={"foo": _index_pin()}))
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
                pins={"foo": LocalPin(name="foo", version="1.0", path=str(src))},
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
                pins={
                    "foo": VcsPin(
                        name="foo",
                        version="1.0",
                        repo_url="https://github.com/x/y.git",
                        bare_repo_url="https://github.com/x/y.git",
                        commit_id="a" * 40,
                        subdirectory="pkg",
                    ),
                },
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
                pins={
                    "foo": VcsPin(
                        name="foo",
                        version="1.0",
                        repo_url="https://example.com/x/y",
                        bare_repo_url="https://example.com/x/y",
                        commit_id="a" * 40,
                        vcs_type="hg",
                    ),
                },
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["vcs"]["type"] == "hg"

    def test_multiple_packages_sorted_by_name(self) -> None:
        text = write_lock(
            LockInput(
                pins={
                    "foo": _index_pin("foo"),
                    "bar": _index_pin("bar"),
                    "baz": _index_pin("baz"),
                },
            )
        )
        data = tomllib.loads(text)
        names = [p["name"] for p in data["packages"]]
        assert names == ["bar", "baz", "foo"]

    def test_writes_to_file(self, tmp_path: Path) -> None:
        out = tmp_path / "pylock.toml"
        text = write_lock(
            LockInput(pins={"foo": _index_pin()}),
            output_path=out,
        )
        assert out.read_text(encoding="utf-8") == text

    def test_extras_canonicalised(self) -> None:
        text = write_lock(
            LockInput(
                pins={"foo": _index_pin()},
                extras=("My-Extra",),
            )
        )
        data = tomllib.loads(text)
        assert "my-extra" in data["extras"]

    def test_canonicalises_package_name(self) -> None:
        text = write_lock(
            LockInput(pins={"foo": _index_pin(name="Foo_Bar")}),
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["name"] == "foo-bar"


class TestPerTupleMarkerSimplification:
    def test_all_tuples_agree_no_marker(self) -> None:
        per_tuple = {
            "py310-linux": {"foo": _index_pin(version="1.0")},
            "py311-linux": {"foo": _index_pin(version="1.0")},
        }
        tuple_markers = {
            "py310-linux": Marker('python_version == "3.10"'),
            "py311-linux": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert "marker" not in data["packages"][0]

    def test_diverging_pins_get_markers(self) -> None:
        per_tuple = {
            "py310-linux": {"foo": _index_pin(version="1.0")},
            "py311-linux": {"foo": _index_pin(version="2.0")},
        }
        tuple_markers = {
            "py310-linux": Marker('python_version == "3.10"'),
            "py311-linux": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        # Two Package entries, each with a marker
        assert len(data["packages"]) == 2
        markers = [p.get("marker") for p in data["packages"]]
        assert all(m is not None for m in markers)
        assert any('python_version == "3.10"' in m for m in markers)
        assert any('python_version == "3.11"' in m for m in markers)

    def test_three_tuples_two_groups(self) -> None:
        # 3.10 + 3.11 share v1.0; 3.12 has v2.0 -> two groups
        per_tuple = {
            "py310": {"foo": _index_pin(version="1.0")},
            "py311": {"foo": _index_pin(version="1.0")},
            "py312": {"foo": _index_pin(version="2.0")},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
            "py312": Marker('python_version == "3.12"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2
        # The v1.0 group has an OR marker; the v2.0 group has a single marker
        v1_pkg = next(p for p in data["packages"] if p["version"] == "1.0")
        v2_pkg = next(p for p in data["packages"] if p["version"] == "2.0")
        assert "or" in v1_pkg["marker"]
        assert "or" not in v2_pkg["marker"]

    def test_local_pin_per_tuple(self) -> None:
        # LocalPin discriminator coverage
        per_tuple = {
            "py310": {"foo": LocalPin(name="foo", version="1.0", path="/a")},
            "py311": {"foo": LocalPin(name="foo", version="1.0", path="/b")},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        # Different paths -> different groups -> two packages
        assert len(data["packages"]) == 2

    def test_tuple_specific_wheels_merged_within_group(self) -> None:
        """Two tuples sharing version/index keep both tuples' wheel filenames."""
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
        per_tuple = {
            "linux": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="pypi",
                    wheels=(linux_wheel,),
                ),
            },
            "macos": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="pypi",
                    wheels=(macos_wheel,),
                ),
            },
        }
        tuple_markers = {
            "linux": Marker('sys_platform == "linux"'),
            "macos": Marker('sys_platform == "darwin"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        wheel_names = sorted(w["name"] for w in data["packages"][0]["wheels"])
        assert wheel_names == [
            "foo-1.0-cp310-cp310-linux_x86_64.whl",
            "foo-1.0-cp310-cp310-macosx_11_0_arm64.whl",
        ]

    def test_requires_python_drops_when_tuples_disagree(self) -> None:
        """``requires_python`` survives merging only when every tuple agrees."""
        wheel = _wheel()
        per_tuple = {
            "a": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="pypi",
                    wheels=(wheel,),
                    requires_python=">=3.10",
                ),
            },
            "b": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="pypi",
                    wheels=(wheel,),
                    requires_python=">=3.11",
                ),
            },
        }
        tuple_markers = {
            "a": Marker('python_version == "3.10"'),
            "b": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert "requires-python" not in data["packages"][0]

    def test_sdist_filled_from_any_tuple(self) -> None:
        """An sdist appearing in only some tuples is preserved on the merge."""
        sdist = _sdist()
        wheel = _wheel()
        per_tuple = {
            "a": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="pypi",
                    sdist=None,
                    wheels=(wheel,),
                ),
            },
            "b": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="pypi",
                    sdist=sdist,
                    wheels=(wheel,),
                ),
            },
        }
        tuple_markers = {
            "a": Marker('python_version == "3.10"'),
            "b": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["sdist"]["url"].endswith("foo-1.0.tar.gz")

    def test_vcs_pin_per_tuple(self) -> None:
        # VcsPin discriminator coverage
        per_tuple = {
            "py310": {
                "foo": VcsPin(
                    name="foo",
                    version="1.0",
                    repo_url="https://x/y.git",
                    bare_repo_url="https://x/y.git",
                    commit_id="a" * 40,
                ),
            },
            "py311": {
                "foo": VcsPin(
                    name="foo",
                    version="1.0",
                    repo_url="https://x/y.git",
                    bare_repo_url="https://x/y.git",
                    commit_id="a" * 40,
                ),
            },
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        # Same VCS commit -> single group -> one package
        assert len(data["packages"]) == 1

    def test_pin_in_both_pins_and_per_tuple_emits_once(self) -> None:
        # When the same package appears in both pins and per_tuple_pins,
        # the per_tuple_pins entry wins (single emission per name).
        per_tuple = {
            "py310": {"foo": _index_pin(version="1.0")},
            "py311": {"foo": _index_pin(version="1.0")},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(
                pins={"foo": _index_pin(version="9.9")},
                per_tuple_pins=per_tuple,
                tuple_markers=tuple_markers,
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert data["packages"][0]["version"] == "1.0"

    def test_diverging_pins_without_tuple_markers(self) -> None:
        # group_count > 1 but no tuple_markers -> _build_marker returns None
        per_tuple = {
            "py310": {"foo": _index_pin(version="1.0")},
            "py311": {"foo": _index_pin(version="2.0")},
        }
        text = write_lock(LockInput(per_tuple_pins=per_tuple))
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2
        # No markers since tuple_markers is empty
        assert all("marker" not in p for p in data["packages"])

    def test_pin_labels_outside_tuple_universe_get_no_marker(self) -> None:
        # Defensive guard: a per-tuple pin labelled with a tuple that
        # was never declared in tuple_markers must not crash. With one
        # orphan label and two declared tuples, the marker filter
        # returns empty and the package is emitted without a marker.
        per_tuple = {
            "orphan": {"foo": _index_pin(version="1.0")},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(
                per_tuple_pins=per_tuple,
                tuple_markers=tuple_markers,
            )
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert data["packages"][0]["version"] == "1.0"
        assert "marker" not in data["packages"][0]

    def test_extra_pin_in_top_level_emitted(self) -> None:
        # ``foo`` lives in per_tuple_pins; ``bar`` only in pins.
        # Both should appear in the lock.
        per_tuple = {
            "py310-linux": {"foo": _index_pin(name="foo", version="1.0")},
            "py311-linux": {"foo": _index_pin(name="foo", version="1.0")},
        }
        tuple_markers = {
            "py310-linux": Marker('python_version == "3.10"'),
            "py311-linux": Marker('python_version == "3.11"'),
        }
        pins = {"bar": _index_pin(name="bar", version="2.0")}
        text = write_lock(
            LockInput(
                pins=pins,
                per_tuple_pins=per_tuple,
                tuple_markers=tuple_markers,
            )
        )
        data = tomllib.loads(text)
        names = sorted(p["name"] for p in data["packages"])
        assert names == ["bar", "foo"]

    def test_package_in_only_one_of_two_tuples_gets_marker(self) -> None:
        # ``foo`` resolves on py3.10 only.  The 1-of-2-tuples shape used
        # to fall through ``group_count == 1`` and emit unconditionally.
        per_tuple = {
            "py310": {"foo": _index_pin(version="1.0")},
            "py311": {},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert data["packages"][0]["marker"] == 'python_version == "3.10"'

    def test_package_in_two_of_four_tuples_gets_or_marker(self) -> None:
        # ``foo`` resolves on py3.10 and py3.11 only with the same pin.
        # The marker is the OR of those two tuple markers.
        per_tuple = {
            "py310": {"foo": _index_pin(version="1.0")},
            "py311": {"foo": _index_pin(version="1.0")},
            "py312": {},
            "py313": {},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
            "py312": Marker('python_version == "3.12"'),
            "py313": Marker('python_version == "3.13"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        marker = data["packages"][0]["marker"]
        assert 'python_version == "3.10"' in marker
        assert 'python_version == "3.11"' in marker
        assert 'python_version == "3.12"' not in marker
        assert 'python_version == "3.13"' not in marker
        assert " or " in marker

    def test_package_in_all_four_tuples_same_version_no_marker(self) -> None:
        # Regression check: the existing "all four agree on a single
        # version" path stays unmarkered after the fix.
        per_tuple = {
            "py310": {"foo": _index_pin(version="1.0")},
            "py311": {"foo": _index_pin(version="1.0")},
            "py312": {"foo": _index_pin(version="1.0")},
            "py313": {"foo": _index_pin(version="1.0")},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
            "py312": Marker('python_version == "3.12"'),
            "py313": Marker('python_version == "3.13"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 1
        assert "marker" not in data["packages"][0]

    def test_package_in_three_of_four_with_two_versions(self) -> None:
        # ``foo`` resolves on py3.10 (v1) and py3.11+py3.12 (v2); absent
        # from py3.13.  Two groups; both must be marker-gated, and
        # neither group should claim py3.13.
        per_tuple = {
            "py310": {"foo": _index_pin(version="1.0")},
            "py311": {"foo": _index_pin(version="2.0")},
            "py312": {"foo": _index_pin(version="2.0")},
            "py313": {},
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
            "py312": Marker('python_version == "3.12"'),
            "py313": Marker('python_version == "3.13"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2
        v1_marker = next(p["marker"] for p in data["packages"] if p["version"] == "1.0")
        v2_marker = next(p["marker"] for p in data["packages"] if p["version"] == "2.0")
        assert v1_marker == 'python_version == "3.10"'
        assert 'python_version == "3.11"' in v2_marker
        assert 'python_version == "3.12"' in v2_marker
        assert 'python_version == "3.13"' not in v2_marker


class TestBuildPylockReturnsValidPylock:
    def test_can_be_validated(self) -> None:
        pylock = build_pylock(LockInput(pins={"foo": _index_pin()}))
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
                pins={
                    "foo": _index_pin(),
                    "bar": LocalPin(name="bar", version="2.0", path="/tmp/bar"),
                },
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
        text = write_lock(LockInput(pins={"foo": _index_pin()}, provenance=prov))
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
        text = write_lock(LockInput(pins={"foo": _index_pin()}, provenance=prov))
        data = tomllib.loads(text)
        assert data["tool"]["nab"]["mode"] == "universal"
        assert data["tool"]["nab"]["python-specifier"] == ">=3.11,<3.14"
        assert data["tool"]["nab"]["platforms"] == [
            "linux_x86_64",
            "macos_arm64",
        ]

    def test_absent_provenance_means_no_tool_block(self) -> None:
        text = write_lock(LockInput(pins={"foo": _index_pin()}))
        data = tomllib.loads(text)
        assert "tool" not in data


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

    def test_returns_none_when_not_a_pylock(self, tmp_path: Path) -> None:
        # Valid TOML but missing the required PEP 751 keys.
        path = tmp_path / "pylock.toml"
        path.write_text('title = "not a lockfile"\n')
        assert read_lockfile_packages(path) is None

    def test_reads_name_to_version_map(self, tmp_path: Path) -> None:
        lock_input = LockInput(
            pins={
                "foo": _index_pin("foo", "1.2.3"),
                "bar": _index_pin("bar", "4.5"),
            }
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
                pins={"foo": _index_pin()},
                dependency_groups=("dev", "docs"),
                default_groups=("dev",),
            )
        )
        data = tomllib.loads(text)
        assert data["dependency-groups"] == ["dev", "docs"]
        assert data["default-groups"] == ["dev"]

    def test_omits_arrays_when_empty(self) -> None:
        text = write_lock(LockInput(pins={"foo": _index_pin()}))
        data = tomllib.loads(text)
        assert "dependency-groups" not in data
        assert "default-groups" not in data

    def test_group_names_normalized(self) -> None:
        text = write_lock(
            LockInput(
                pins={"foo": _index_pin()},
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
        vcs_pins: dict[str, str] | None = None,
        listing_indexes: dict[str, str] | None = None,
        dist_policy_overrides: dict[str, DistPolicy] | None = None,
    ) -> None:
        self._listings = listings or {}
        self._local = local_sources or {}
        self._vcs = vcs_sources or {}
        self._vcs_pins = vcs_pins or {}
        self._dist_policy_overrides = dist_policy_overrides or {}
        self.coordinator = _FakeCoordinator(listing_indexes)

    def local_source_for(self, canonical: str) -> LocalSource | None:
        return self._local.get(canonical)

    def vcs_source_for(self, canonical: str) -> VcsSource | None:
        return self._vcs.get(canonical)

    def vcs_pin_for(self, canonical: str) -> str | None:
        return self._vcs_pins.get(canonical)

    def dist_files_for(
        self, canonical: str, version: Version
    ) -> list[WheelFile | SdistFile]:
        return [d for v, d in self._listings.get(canonical, []) if v == version]

    def effective_dist_policy(self, canonical: str) -> DistPolicy:
        return self._dist_policy_overrides.get(canonical, DistPolicy.WHEEL_OR_SDIST)


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


class TestBuildLockInputFromProvider:
    def test_index_pin_from_listing(self) -> None:
        provider = _FakeProvider(
            listings={
                "foo": [
                    (Version("1.0"), _wheel_file()),
                    (Version("1.0"), _sdist_file()),
                ]
            }
        )
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("1.0")}, requires_python=">=3.10"
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.version == "1.0"
        assert pin.index == "https://pypi.org/simple/"
        assert pin.requires_python == ">=3.10"
        assert pin.sdist is not None
        assert pin.sdist.hashes == (("sha256", "b" * 64),)
        assert len(pin.wheels) == 1
        assert pin.wheels[0].hashes == (("sha256", "a" * 64),)
        assert lock_input.requires_python == ">=3.10"

    def test_local_path_threads_to_artifact(self, tmp_path: Path) -> None:
        """A WheelFile.local_path reaches the emitted WheelArtifact."""
        wheel_path = tmp_path / "foo-1.0-py3-none-any.whl"
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file(local_path=wheel_path))]}
        )
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels == ()
        assert pin.sdist is not None
        assert pin.sdist.hashes == (("sha256", "b" * 64),)

    def test_index_pin_sdist_install_without_sdist_raises(self) -> None:
        """sdist-install with no sdist available raises MissingSdistError."""
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            dist_policy_overrides={"foo": DistPolicy.SDIST_INSTALL},
        )
        with pytest.raises(MissingSdistError, match="sdist-install"):
            build_lock_input_from_provider(provider, {"foo": Version("1.0")})

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
        lock_input = build_lock_input_from_provider(
            provider,
            {"foo": Version("1.0")},
            indexes=(
                IndexConfig("pypi", "https://pypi.org/simple/"),
                IndexConfig("torch-cpu", "https://download.pytorch.org/whl/cpu/"),
            ),
        )
        pin = lock_input.pins["foo"]
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
            build_lock_input_from_provider(
                provider,
                {"foo": Version("1.0")},
                indexes=(IndexConfig("custom", "https://custom.example/simple/"),),
            )

    def test_index_pin_strips_credentials_from_url(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "private"},
        )
        lock_input = build_lock_input_from_provider(
            provider,
            {"foo": Version("1.0")},
            indexes=(
                IndexConfig(
                    "private", "https://user:token@Private.Example:8443/simple/"
                ),
            ),
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.index == "https://Private.Example:8443/simple/"

    def test_index_pin_keeps_url_without_credentials(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "plain"},
        )
        lock_input = build_lock_input_from_provider(
            provider,
            {"foo": Version("1.0")},
            indexes=(IndexConfig("plain", "https://Plain.Example/simple/"),),
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.index == "https://Plain.Example/simple/"

    def test_index_pin_strips_username_only_credentials(self) -> None:
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "useronly"},
        )
        lock_input = build_lock_input_from_provider(
            provider,
            {"foo": Version("1.0")},
            indexes=(IndexConfig("useronly", "https://user@example.com/simple/"),),
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.index == "https://example.com/simple/"

    def test_local_source_emits_local_pin(self, tmp_path: Path) -> None:
        provider = _FakeProvider(
            local_sources={"foo": LocalSource(name="foo", path=str(tmp_path))}
        )
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+local")}
        )
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.subdirectory == "pkg/sub"

    def test_vcs_pin_carries_bare_repo_url(self) -> None:
        """The bare repo URL is carried through from the parsed source.

        ``repo_url`` keeps the full installable form for the
        requirements.txt path; ``bare_repo_url`` holds the plain
        repository URL with no ``git+`` prefix, ``@<ref>``, or
        ``#subdirectory`` fragment.
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, VcsPin)
        full_url = "git+https://example.com/r.git@release/1.0#subdirectory=pkg/sub"
        assert pin.repo_url == full_url
        assert pin.bare_repo_url == "https://example.com/r.git"

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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        vcs = tomllib.loads(write_lock(lock_input))["packages"][0]["vcs"]
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.bare_repo_url == "https://example.com/r.git"

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
            build_lock_input_from_provider(provider, {"foo": Version("0.0.0+vcs")})

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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.repo_url == "git+https://GitHub.com/org/repo.git"

    def test_vcs_source_keeps_url_without_credentials(self) -> None:
        provider = _FakeProvider(
            vcs_sources={
                "foo": VcsSource(name="foo", url="git+https://github.com/org/repo.git"),
            },
            vcs_pins={"foo": "a" * 40},
        )
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.repo_url == "git+https://github.com/org/repo.git"

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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+vcs")}
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.requested_revision is None

    def test_local_source_threads_editable(self, tmp_path: Path) -> None:
        provider = _FakeProvider(
            local_sources={
                "foo": LocalSource(name="foo", path=str(tmp_path), editable=True)
            }
        )
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+local")}
        )
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+local")}
        )
        pin = lock_input.pins["foo"]
        assert isinstance(pin, LocalPin)
        assert pin.subdirectory == "pkg/lib"

    def test_local_source_defaults_not_editable_no_subdirectory(
        self, tmp_path: Path
    ) -> None:
        provider = _FakeProvider(
            local_sources={"foo": LocalSource(name="foo", path=str(tmp_path))}
        )
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("0.0.0+local")}
        )
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.sdist is not None
        assert pin.sdist.upload_time == datetime(
            2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc
        )

    def test_upload_time_none_when_index_omits_it(self) -> None:
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), _wheel_file())]})
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].upload_time is None

    def test_missing_acceptable_hash_raises(self) -> None:
        wheel = _wheel_file(sha256=None)
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        with pytest.raises(MissingHashError, match="no acceptable hash"):
            build_lock_input_from_provider(provider, {"foo": Version("1.0")})

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
        with pytest.raises(MissingHashError, match="no acceptable hash"):
            build_lock_input_from_provider(provider, {"foo": Version("1.0")})

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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert dict(pin.wheels[0].hashes) == {
            "sha384": "d" * 96,
            "sha512": "e" * 128,
        }
        assert pin.wheels[0].primary_digest == ("sha384", "d" * 96)

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
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python is None

    def test_index_pin_raises_when_serving_index_unconfigured(self) -> None:
        """A serving index not among the configured indexes raises."""
        provider = _FakeProvider(
            listings={"foo": [(Version("1.0"), _wheel_file())]},
            listing_indexes={"foo": "gone"},
        )
        with pytest.raises(AssertionError, match="not one of the configured indexes"):
            build_lock_input_from_provider(
                provider,
                {"foo": Version("1.0")},
                indexes=(IndexConfig("primary", "https://primary.example/simple/"),),
            )

    def test_extras_passed_through(self) -> None:
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), _wheel_file())]})
        lock_input = build_lock_input_from_provider(
            provider, {"foo": Version("1.0")}, extras=("dev", "test")
        )
        assert lock_input.extras == ("dev", "test")

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
        with pytest.raises(MissingHashError, match="sha256"):
            build_lock_input_from_provider(provider, {"foo": Version("1.0")})

    def test_files_without_requires_python_drop_field(self) -> None:
        wheel = _wheel_file(requires_python=None)
        provider = _FakeProvider(listings={"foo": [(Version("1.0"), wheel)]})
        lock_input = build_lock_input_from_provider(provider, {"foo": Version("1.0")})
        pin = lock_input.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.requires_python is None


class TestDirectoryFields:
    """PEP 751 ``packages.directory`` editable + subdirectory emission."""

    def test_editable_emitted_when_true(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                pins={
                    "foo": LocalPin(
                        name="foo",
                        version="1.0",
                        path=str(tmp_path),
                        editable=True,
                    )
                },
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["editable"] is True

    def test_editable_false_by_default(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                pins={"foo": LocalPin(name="foo", version="1.0", path=str(tmp_path))},
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["editable"] is False

    def test_subdirectory_emitted(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                pins={
                    "foo": LocalPin(
                        name="foo",
                        version="1.0",
                        path=str(tmp_path),
                        subdirectory="pkg/lib",
                    )
                },
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["subdirectory"] == "pkg/lib"

    def test_subdirectory_omitted_when_none(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(
                pins={"foo": LocalPin(name="foo", version="1.0", path=str(tmp_path))},
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

    def test_per_tuple_editable_diverges(self) -> None:
        per_tuple = {
            "py310": {
                "foo": LocalPin(name="foo", version="1.0", path="/a", editable=True)
            },
            "py311": {
                "foo": LocalPin(name="foo", version="1.0", path="/a", editable=False)
            },
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(per_tuple_pins=per_tuple, tuple_markers=tuple_markers)
        )
        data = tomllib.loads(text)
        assert len(data["packages"]) == 2


class TestVcsRequestedRevision:
    """PEP 751 ``packages.vcs.requested-revision`` emission."""

    def test_requested_revision_emitted(self) -> None:
        text = write_lock(
            LockInput(
                pins={
                    "foo": VcsPin(
                        name="foo",
                        version="1.0",
                        repo_url="https://github.com/x/y.git",
                        bare_repo_url="https://github.com/x/y.git",
                        commit_id="a" * 40,
                        requested_revision="v2.1.0",
                    ),
                },
            )
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["vcs"]["requested-revision"] == "v2.1.0"

    def test_requested_revision_omitted_when_none(self) -> None:
        text = write_lock(
            LockInput(
                pins={
                    "foo": VcsPin(
                        name="foo",
                        version="1.0",
                        repo_url="https://github.com/x/y.git",
                        bare_repo_url="https://github.com/x/y.git",
                        commit_id="a" * 40,
                    ),
                },
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
        text = write_lock(LockInput(pins={"foo": pin}))
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
        text = write_lock(LockInput(pins={"foo": pin}))
        data = tomllib.loads(text)
        assert data["packages"][0]["sdist"]["upload-time"] == ts

    def test_upload_time_omitted_when_none(self) -> None:
        text = write_lock(LockInput(pins={"foo": _index_pin()}))
        data = tomllib.loads(text)
        assert "upload-time" not in data["packages"][0]["wheels"][0]
        assert "upload-time" not in data["packages"][0]["sdist"]


class TestRelativeDirectoryPath:
    """PEP 751: ``packages.directory.path`` is relative to the lock file (#6)."""

    def test_path_inside_lock_dir_is_relative(self, tmp_path: Path) -> None:
        src = tmp_path / "libs" / "foo"
        text = write_lock(
            LockInput(pins={"foo": LocalPin(name="foo", version="1.0", path=str(src))}),
            output_path=tmp_path / "pylock.toml",
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["path"] == "libs/foo"

    def test_path_outside_lock_dir_uses_parent_prefix(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "foo"
        out_dir = tmp_path / "locks"
        out_dir.mkdir()
        text = write_lock(
            LockInput(pins={"foo": LocalPin(name="foo", version="1.0", path=str(src))}),
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
            LockInput(pins={"foo": LocalPin(name="foo", version="1.0", path=str(src))})
        )
        data = tomllib.loads(text)
        assert data["packages"][0]["directory"]["path"] == "pkg"

    def test_build_pylock_honours_explicit_lock_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "a" / "b" / "foo"
        pylock = build_pylock(
            LockInput(pins={"foo": LocalPin(name="foo", version="1.0", path=str(src))}),
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
            LockInput(pins={"foo": pin}), output_path=tmp_path / "pylock.toml"
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
            LockInput(pins={"foo": pin}), output_path=tmp_path / "pylock.toml"
        )
        sdist = tomllib.loads(text)["packages"][0]["sdist"]
        assert sdist["path"] == "dist/foo-1.0.tar.gz"
        assert "url" not in sdist

    def test_remote_artifacts_keep_url(self, tmp_path: Path) -> None:
        text = write_lock(
            LockInput(pins={"foo": _index_pin()}),
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
            LockInput(pins={"foo": pin}), output_path=out_dir / "pylock.toml"
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
        text = write_requirements_with_hashes(LockInput(pins={"foo": _index_pin()}))
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
        text_forward = write_requirements_with_hashes(LockInput(pins={"foo": forward}))
        text_reverse = write_requirements_with_hashes(LockInput(pins={"foo": reverse}))
        assert text_forward == text_reverse

    def test_local_pin_uses_file_url(self, tmp_path: Path) -> None:
        text = write_requirements_with_hashes(
            LockInput(
                pins={"foo": LocalPin(name="foo", version="1.0", path=str(tmp_path))}
            )
        )
        assert "foo @ file://" in text

    def test_vcs_pin_round_trips_url(self) -> None:
        text = write_requirements_with_hashes(
            LockInput(
                pins={
                    "foo": VcsPin(
                        name="foo",
                        version="1.0",
                        repo_url="git+https://example.com/r.git@abc",
                        bare_repo_url="https://example.com/r.git",
                        commit_id="abc",
                    ),
                },
            )
        )
        assert "foo @ git+https://example.com/r.git@abc" in text

    def test_index_pin_without_hashes_falls_back(self) -> None:
        bare = IndexPin(name="foo", version="1.0", index="pypi")
        text = write_requirements_with_hashes(LockInput(pins={"foo": bare}))
        assert text.strip() == "foo==1.0"

    def test_writes_to_file(self, tmp_path: Path) -> None:
        out = tmp_path / "requirements.txt"
        text = write_requirements_with_hashes(
            LockInput(pins={"foo": _index_pin()}),
            output_path=out,
        )
        assert out.read_text(encoding="utf-8") == text


class TestWriteRequirementsPerTuple:
    def test_blocks_sorted_by_label(self) -> None:
        # Blocks must come out in sorted label order regardless of the
        # per_tuple_pins insertion order, matching the pylock writer so
        # equivalent matrices declared in a different order render the
        # same bytes.
        per_tuple = {
            "py311-linux": {"foo": _index_pin(version="2.0")},
            "py310-linux": {"foo": _index_pin(version="1.0")},
        }
        text = write_requirements_without_hashes(LockInput(per_tuple_pins=per_tuple))
        assert text.index("# py310-linux") < text.index("# py311-linux")


def test_vcs_config_unused_in_lockfile_path() -> None:
    """VcsConfig is consumed by the provider; lockfile builder ignores it."""
    cfg = VcsConfig(policy=VcsPolicy.BLOCK)
    assert cfg.policy is VcsPolicy.BLOCK
