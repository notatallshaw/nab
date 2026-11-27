"""uv lock graph, source identity and target-dependent edge selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import tomli
from packaging.markers import Marker

from nab_project.conflicts import ConflictKind, ConflictMember, ConflictSet
from nab_project.declarations import DependencyDeclaration
from nab_project.lockfile import (
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    LockValidationError,
    SdistArtifact,
    TargetLock,
    VcsPin,
    WheelArtifact,
)
from nab_project.native_lock import write_uv_lock
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

if TYPE_CHECKING:
    from pathlib import Path

_HASH = (("sha256", "ab" * 32),)


def test_uv_sources_and_root_inputs(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    wheel_dir = tmp_path / "files"
    git = VcsPin(
        "vcs",
        "2",
        "git+https://example.org/vcs",
        "https://example.org/vcs",
        "a" * 40,
        "pkg",
        "release/2",
    )
    editable = LocalPin(
        "local", "1", str(tmp_path / "member"), editable=True, subdirectory="sub"
    )
    archive = ArchivePin(
        "archive", "1", "https://example.org/archive.zip", _HASH, "pkg"
    )
    path = wheel_dir / "localwheel-1-py3-none-any.whl"
    wheel = ArchivePin("localwheel", "1", path.as_uri(), _HASH)
    sdist = SdistArtifact(
        "lib-1.tar.gz", "https://example.org/lib-1.tar.gz", _HASH, size=123
    )
    lib = IndexPin(
        "lib",
        "1",
        "https://example.org/simple",
        sdist=sdist,
        wheels=(
            WheelArtifact(
                "lib-1-py3-none-any.whl", "https://example.org/lib.whl", _HASH
            ),
        ),
    )
    pins = {p.name: p for p in (git, editable, archive, wheel, lib)}
    locks = LockInput(
        targets={target.label: TargetLock(target, pins, {"local": ("lib",)})},
        extras=("fast",),
        dependency_groups=("test",),
    )
    project = {
        "project": {
            "name": "example",
            "version": "2",
            "dependencies": ["local", f"localwheel @ {path.as_uri()}"],
            "optional-dependencies": {
                "fast": ['lib[speed]>=1; python_version >= "3.11"']
            },
        },
        "dependency-groups": {
            "test": ["vcs", "archive @ https://example.org/archive.zip"]
        },
        "build-system": {"requires": ["lib"]},
    }
    destination = tmp_path / "uv.lock"
    write_uv_lock(locks, project, project_dir=tmp_path, output_path=destination)
    rows = {p["name"]: p for p in tomli.loads(destination.read_text())["package"]}
    assert rows["local"]["source"] == {"editable": "member/sub"}
    assert rows["local"]["dependencies"] == [{"name": "lib"}]
    assert rows["localwheel"]["source"] == {"path": "files/" + path.name}
    assert rows["localwheel"]["wheels"][0]["filename"] == path.name
    assert rows["archive"]["source"]["subdirectory"] == "pkg"
    assert (
        rows["vcs"]["source"]["git"]
        == "https://example.org/vcs?rev=release%2F2&subdirectory=pkg#" + "a" * 40
    )
    assert rows["lib"]["sdist"]["size"] == 123
    root = rows["example"]
    assert root["source"] == {"editable": "."}
    assert root["optional-dependencies"]["fast"][0]["extra"] == ["speed"]
    assert [edge["name"] for edge in root["dev-dependencies"]["test"]] == [
        "vcs",
        "archive",
    ]
    assert root["metadata"]["provides-extras"] == ["fast"]


def test_uv_forked_edges_keep_exact_child_identity(tmp_path: Path) -> None:
    targets = {}
    envs = []
    for platform, version in (("linux_x86_64", "1"), ("windows_amd64", "2")):
        target = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec(platform)
        )
        envs.append(Marker(target.environment_marker_string))
        root = LocalPin("driver", "1", str(tmp_path / "driver"))
        leaf = ArchivePin(
            "leaf", version, f"https://example.org/leaf-{version}.zip", _HASH
        )
        declaration = DependencyDeclaration(
            "leaf[fast]",
            "",
            parent_name="driver",
            dependency_name="leaf",
            dependency_extras=("fast",),
            required_for_parent_extras=("speed",),
        )
        targets[target.label] = TargetLock(
            target,
            {"driver": root, "leaf": leaf},
            {"driver": ("leaf",)},
            dependency_requirements={"driver": {"leaf": (declaration,)}},
        )
    inputs = LockInput(targets=targets, environments=envs)
    document = {
        "project": {
            "name": "app",
            "dependencies": [
                "driver[speed]",
                'leaf==2; sys_platform == "win32"',
                'leaf==1; sys_platform == "win32"',
            ],
        }
    }
    rows = tomli.loads(write_uv_lock(inputs, document, project_dir=tmp_path))["package"]
    edges = next(row for row in rows if row["name"] == "driver")[
        "optional-dependencies"
    ]["speed"]
    for lock in targets.values():
        chosen = [
            edge
            for edge in edges
            if Marker(edge["marker"]).evaluate(lock.target.marker_env)
        ]
        assert [(e["name"], e["version"], e["extra"]) for e in chosen] == [
            ("leaf", lock.pins["leaf"].version, ["fast"])
        ]


def test_uv_named_root_and_build_groups(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    runtime = LocalPin("runtime", "1", str(tmp_path / "runtime"))
    backend = LocalPin("backend", "1", str(tmp_path / "backend"))
    inputs = LockInput(
        targets={
            target.label: TargetLock(target, {"runtime": runtime, "backend": backend})
        },
        base_group="runtime",
        build_group="build",
        conflicts=(
            ConflictSet(
                (
                    ConflictMember(ConflictKind.GROUP, "runtime"),
                    ConflictMember(ConflictKind.GROUP, "build"),
                )
            ),
        ),
    )
    project = {
        "project": {"name": "app", "dependencies": ["runtime"]},
        "build-system": {"requires": ["backend"]},
    }
    doc = tomli.loads(write_uv_lock(inputs, project, project_dir=tmp_path))
    root = next(row for row in doc["package"] if row["name"] == "app")
    assert root["dev-dependencies"] == {
        "runtime": [{"name": "runtime"}],
        "build": [{"name": "backend"}],
    }
    assert doc["conflicts"] == [
        [{"package": "app", "group": "runtime"}, {"package": "app", "group": "build"}]
    ]
    script = tomli.loads(
        write_uv_lock(
            inputs, project, project_dir=tmp_path, build_requirements=True, script=True
        )
    )
    assert script["manifest"]["requirements"] == [{"name": "backend"}]


def test_uv_rejects_unsupported_vcs(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    svn = VcsPin(
        "source",
        "1",
        "svn+https://example.org/repo",
        "https://example.org/repo",
        "1",
        None,
        vcs_type="svn",
    )
    inputs = LockInput(targets={target.label: TargetLock(target, {"source": svn})})
    with pytest.raises(LockValidationError, match="from svn"):
        write_uv_lock(inputs, {"project": {}}, project_dir=tmp_path)


def test_uv_empty_project_and_minimal_registry_rows(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    lock = LockInput(targets={target.label: TargetLock(target, {})}, extras=("empty",))
    project = {"project": {"name": "app", "optional-dependencies": {"empty": []}}}
    root = tomli.loads(write_uv_lock(lock, project, project_dir=tmp_path))["package"][0]
    assert root["optional-dependencies"] == {"empty": []}
    assert "requires-dist" not in root["metadata"]

    lock = LockInput(targets={target.label: TargetLock(target, {})})
    root = tomli.loads(
        write_uv_lock(lock, {"project": {"name": "app"}}, project_dir=tmp_path)
    )["package"][0]
    assert root == {"name": "app", "version": "0", "source": {"virtual": "."}}


def test_uv_registry_paths_use_the_serving_index(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    index_dir = tmp_path / "index"
    path = index_dir / "foo" / "foo-1-py3-none-any.whl"
    wheel = WheelArtifact(path.name, path.as_uri(), _HASH, local_path=path)
    # The provider also accepts local find-links files alongside an HTTP index.
    archive = tmp_path / "bar-1.tar.gz"
    sdist = SdistArtifact(archive.name, archive.as_uri(), _HASH, local_path=archive)
    pins = {
        "foo": IndexPin("foo", "1", index_dir.as_uri(), wheels=(wheel,)),
        "bar": IndexPin("bar", "1", "https://example.org/simple", sdist=sdist),
        "repo": VcsPin(
            "repo",
            "1",
            "git+https://example.org/repo",
            "https://example.org/repo",
            "b" * 40,
            None,
        ),
    }
    lock = LockInput(targets={target.label: TargetLock(target, pins)})
    project = {
        "project": {"name": "app", "dependencies": ["foo", "foo>=1", "bar", "repo"]}
    }
    rows = {
        r["name"]: r
        for r in tomli.loads(write_uv_lock(lock, project, project_dir=tmp_path))[
            "package"
        ]
    }
    assert rows["foo"]["source"] == {"registry": "index"}
    assert rows["foo"]["wheels"][0]["path"] == "foo/" + path.name
    assert rows["bar"]["sdist"]["url"] == archive.as_uri()
    assert rows["repo"]["source"]["git"] == "https://example.org/repo#" + "b" * 40
    assert rows["app"]["dependencies"].count({"name": "foo"}) == 1


def test_uv_encodes_conflict_members_in_forked_edges(tmp_path: Path) -> None:
    targets = {}
    base = ResolveTarget.for_host()
    for extra, version in (("cpu", "1"), ("gpu", "2")):
        target = base.with_selection((("extra", extra),))
        pin = ArchivePin(
            "choice", version, f"https://example.org/choice-{version}.zip", _HASH
        )
        targets[target.label] = TargetLock(
            target, {"choice": pin}, package_gates={"choice": (("extra", extra),)}
        )
    conflict = ConflictSet(
        (
            ConflictMember(ConflictKind.EXTRA, "cpu"),
            ConflictMember(ConflictKind.EXTRA, "gpu"),
        )
    )
    lock = LockInput(targets=targets, extras=("cpu", "gpu"), conflicts=(conflict,))
    project = {
        "project": {
            "name": "app",
            "optional-dependencies": {"cpu": ["choice==1"], "gpu": ["choice==2"]},
        }
    }
    rows = tomli.loads(write_uv_lock(lock, project, project_dir=tmp_path))["package"]
    root = next(row for row in rows if row["name"] == "app")
    for name, version in (("cpu", "1"), ("gpu", "2")):
        (edge,) = root["optional-dependencies"][name]
        assert edge["version"] == version
        assert "extra-3-app-" + name in edge["marker"]
