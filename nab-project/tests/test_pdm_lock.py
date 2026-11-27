"""PDM consumes one file record per artifact and one marker per group set."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import tomli
from packaging.markers import Marker
from packaging.tags import Tag

from nab_project.lockfile import (
    BASE_MEMBER,
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
from nab_project.native_lock import write_pdm_lock
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

if TYPE_CHECKING:
    from pathlib import Path

_DIGEST = (("sha256", "ab" * 32),)


def test_source_files_and_groups(tmp_path: Path) -> None:
    target = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("windows_amd64")
    )
    wheel = WheelArtifact(
        "demo-1-py3-none-any.whl",
        "unused",
        _DIGEST,
        local_path=tmp_path / "demo-1-py3-none-any.whl",
    )
    sdist = SdistArtifact("demo-1.tar.gz", "https://example.org/demo-1.tar.gz", _DIGEST)
    pins = {
        "demo": IndexPin(
            "demo",
            "1",
            "https://example.org/simple",
            sdist=sdist,
            wheels=(wheel,),
            requires_python=">=3.10",
        ),
        "plain": IndexPin(
            "plain",
            "1",
            "https://example.org/simple",
            sdist=SdistArtifact(
                "plain-1.tar.gz", "https://example.org/plain-1.tar.gz", _DIGEST
            ),
        ),
        "app": LocalPin(
            "app", "1", str(tmp_path / "app"), editable=True, subdirectory="pkg"
        ),
        "source": VcsPin(
            "source",
            "2",
            "git+https://example.org/repo",
            "https://example.org/repo",
            "a" * 40,
            None,
            "v2",
        ),
        "archive": ArchivePin(
            "archive", "3", "https://example.org/archive.zip", _DIGEST
        ),
    }
    lock = TargetLock(
        target,
        pins,
        {"app": ("demo",)},
        package_gates={
            "app": (BASE_MEMBER,),
            "demo": (BASE_MEMBER, ("group", "test")),
            "source": (("extra", "docs"),),
            "archive": (("group", "test"),),
        },
    )
    inputs = LockInput(
        targets={target.label: lock}, dependency_groups=("test",), extras=("docs",)
    )
    destination = tmp_path / "pdm.lock"
    write_pdm_lock(
        inputs,
        {
            "tool": {"pdm": {"dev-dependencies": {"Test": ["archive"]}}},
            "dependency-groups": {"test": ["demo"]},
        },
        project_dir=tmp_path,
        output_path=destination,
    )

    doc = tomli.loads(destination.read_text())
    rows = {row["name"]: row for row in doc["package"]}
    assert "requires_python" not in rows["plain"]
    assert rows["app"]["path"] == "app"
    assert rows["app"]["editable"]
    assert rows["app"]["subdirectory"] == "pkg"
    assert rows["app"]["dependencies"] == ["demo"]
    assert rows["app"]["version"] == "1"
    assert rows["demo"]["groups"] == ["default", "test"]
    assert rows["demo"]["requires_python"] == ">=3.10"
    assert {f["url"] for f in rows["demo"]["files"]} == {
        (tmp_path / wheel.filename).as_uri(),
        sdist.url,
    }
    assert rows["source"]["groups"] == ["docs"]
    assert rows["source"]["git"] == "https://example.org/repo"
    assert rows["source"]["ref"] == "v2"
    assert rows["source"]["revision"] == "a" * 40
    assert rows["archive"]["files"] == [
        {"url": "https://example.org/archive.zip", "hash": "sha256:" + "ab" * 32}
    ]
    assert doc["metadata"]["targets"] == [
        {
            "requires_python": "==3.12.*",
            "platform": "windows_amd64",
            "implementation": "cpython",
            "gil_disabled": False,
        }
    ]


def test_groups_and_versions_follow_the_resolved_platform(tmp_path: Path) -> None:
    targets = {}
    envs = []
    for platform, version in (("linux_x86_64", "1"), ("macos_arm64", "2")):
        target = ResolveTarget.for_declared(
            python_version="3.13", spec=PlatformSpec(platform)
        )
        pin = ArchivePin(
            "choice", version, f"https://example.org/choice-{version}.zip", _DIGEST
        )
        targets[target.label] = TargetLock(
            target, {"choice": pin}, package_gates={"choice": (("group", "dev"),)}
        )
        envs.append(Marker(target.environment_marker_string))
    lock = LockInput(targets=targets, environments=envs, dependency_groups=("dev",))
    rows = tomli.loads(write_pdm_lock(lock, {}, project_dir=tmp_path))["package"]
    for value in targets.values():
        active = [
            row
            for row in rows
            if Marker(row["marker"]).evaluate(value.target.marker_env)
        ]
        assert [row["version"] for row in active] == [value.pins["choice"].version]
        assert active[0]["groups"] == ["dev"]


def test_pdm_refuses_colliding_groups_and_unsupported_digests(tmp_path: Path) -> None:
    with pytest.raises(LockValidationError, match="distinct names"):
        write_pdm_lock(
            LockInput(extras=("dev",), dependency_groups=("dev",)),
            {},
            project_dir=tmp_path,
        )
    target = ResolveTarget.for_host()
    pin = ArchivePin(
        "source", "1", "https://example.org/source.zip", (("md5", "ab" * 16),)
    )
    with pytest.raises(LockValidationError, match="digest"):
        write_pdm_lock(
            LockInput(targets={target.label: TargetLock(target, {"source": pin})}),
            {},
            project_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("platforms", "expected"),
    [
        (["linux_x86_64", "manylinux_2_17_x86_64"], "manylinux_2_17_x86_64"),
        (["win32"], "windows_x86"),
        (["macosx_14_0_arm64"], "macos_14_0_arm64"),
    ],
)
def test_host_platform_uses_native_installer_names(
    platforms: list[str], expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ResolveTarget.for_host()
    tags = (Tag("py3", "none", value) for value in platforms)
    monkeypatch.setattr("nab_project._lockfile.pdm.sys_tags", lambda: tags)
    pin = VcsPin(
        "demo",
        "1",
        "git+https://example.com/demo",
        "https://example.com/demo",
        "a" * 40,
    )
    lock = LockInput(targets={target.label: TargetLock(target, {"demo": pin})})
    result = tomli.loads(write_pdm_lock(lock, {}, project_dir=tmp_path))
    assert result["metadata"]["targets"][0]["platform"] == expected
    assert result["package"][0]["groups"] == ["default"]


def test_declared_macos_and_patch_target(tmp_path: Path) -> None:
    target = ResolveTarget.for_declared(
        python_version="3.13",
        python_full_version="3.13.1",
        spec=PlatformSpec("macos_arm64", runs_on_macos=(12, 0)),
        multi_implementation=True,
    )
    lock = LockInput(targets={target.label: TargetLock(target, {})})
    result = tomli.loads(write_pdm_lock(lock, {}, project_dir=tmp_path))
    assert result["metadata"]["targets"] == [
        {"requires_python": "==3.13.1", "platform": "macos_12_0_arm64"}
    ]


def test_refuse_group_specific_environment_disagreement(tmp_path: Path) -> None:
    targets = {}
    envs = []
    archive = ArchivePin("foo", "1", "https://example.org/foo.zip", _DIGEST)
    for platform, selector in (("linux_x86_64", "one"), ("windows_amd64", "two")):
        target = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec(platform)
        )
        targets[target.label] = TargetLock(
            target, {"foo": archive}, package_gates={"foo": (("group", selector),)}
        )
        envs.append(Marker(target.environment_marker_string))
    lock = LockInput(
        targets=targets, environments=envs, dependency_groups=("one", "two")
    )
    with pytest.raises(
        LockValidationError, match="distinct group environments for foo"
    ):
        write_pdm_lock(lock, {}, project_dir=tmp_path)


def test_index_file_must_have_a_download_location(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    wheel = WheelArtifact("demo-1-py3-none-any.whl", "", _DIGEST)
    package = IndexPin("demo", "1", "https://example.org/simple", wheels=(wheel,))
    lock = LockInput(targets={target.label: TargetLock(target, {"demo": package})})
    with pytest.raises(LockValidationError, match="no download URL"):
        write_pdm_lock(lock, {}, project_dir=tmp_path)
