"""Validate native locks and reuse only choices proven for the requested target."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.version import Version

from nab_project import toml_io, toml_writer
from nab_project._lockfile.native_saved import NativeSolution
from nab_project.inputs import ResolveInputs
from nab_project.lock_check import (
    LockConsistencyError,
    can_reuse_saved_lock,
    check_saved_lock,
)
from nab_project.lock_preferences import read_lock_preferences
from nab_project.lock_validation import snapshot_inputs
from nab_project.lockfile import (
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    LockValidationError,
    Provenance,
    TargetLock,
    WheelArtifact,
)
from nab_project.native_lock import write_pdm_lock, write_poetry_lock, write_uv_lock
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

_HASH = (("sha256", "ab" * 32),)


@pytest.mark.parametrize("format_name", ["pdm", "poetry", "uv"])
def test_remote_saved_lock_checks_bounds_and_consumer_freshness(
    tmp_path: Path, format_name: str
) -> None:
    writer = {"pdm": write_pdm_lock, "poetry": write_poetry_lock, "uv": write_uv_lock}[
        format_name
    ]
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname="app"\nversion="1"\nrequires-python=">=3.12"\ndependencies=["demo"]\n'
    )
    doc = toml_io.load_path(project)
    target = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )
    inputs = ResolveInputs(requires_python=">=3.12", constraints=("demo<2",))
    current = snapshot_inputs(project, inputs, [target])
    wheel = WheelArtifact(
        "demo-1-py3-none-any.whl",
        "https://files.example.org/demo-1-py3-none-any.whl",
        _HASH,
    )
    pin = IndexPin("demo", "1", "https://pypi.org/simple", wheels=(wheel,))
    value = LockInput(
        targets={target.label: TargetLock(target, {"demo": pin})},
        requires_python=">=3.12",
        validation_inputs=current,
    )
    dest = tmp_path / (format_name + ".lock")
    writer(value, doc, project_dir=tmp_path, output_path=dest)
    previous = dest.read_bytes()

    check_saved_lock(
        dest, project, current, inputs, [target], native_format=format_name
    )
    assert can_reuse_saved_lock(
        dest, project, current, inputs, [target], native_format=format_name
    )
    compatible = ResolveInputs(requires_python=">=3.12", constraints=("demo>=1",))
    check_saved_lock(
        dest,
        project,
        snapshot_inputs(project, compatible, [target]),
        compatible,
        [target],
        native_format=format_name,
    )
    incompatible = ResolveInputs(requires_python=">=3.12", constraints=("demo>=2",))
    with pytest.raises(LockConsistencyError, match="does not satisfy"):
        check_saved_lock(
            dest,
            project,
            snapshot_inputs(project, incompatible, [target]),
            incompatible,
            [target],
            native_format=format_name,
        )

    prior = read_lock_preferences(dest, native_format=format_name)
    assert prior is not None
    assert prior.summary_versions() == {"demo": Version("1")}
    assert prior.versions_for(target) == {"demo": Version("1")}
    assert (
        prior.versions_for(
            ResolveTarget.for_declared(
                python_version="3.13", spec=PlatformSpec("linux_x86_64")
            )
        )
        == {}
    )
    assert dest.read_bytes() == previous

    project.write_text(
        project.read_text().replace('dependencies=["demo"]', 'dependencies=["demo>=1"]')
    )
    with pytest.raises(LockConsistencyError, match="project inputs"):
        check_saved_lock(
            dest, project, current, inputs, [target], native_format=format_name
        )
    assert not can_reuse_saved_lock(
        dest, project, current, inputs, [target], native_format=format_name
    )


def test_local_file_hashes_and_nonindex_hints(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname="app"\nversion="1"\n')
    target = ResolveTarget.for_host()
    current = snapshot_inputs(project, ResolveInputs(), [target])
    local = tmp_path / "demo-1-py3-none-any.whl"
    local.write_bytes(b"wheel")
    hashes = (("sha256", hashlib.sha256(local.read_bytes()).hexdigest()),)
    archive = tmp_path / "remote.zip"
    archive.write_bytes(b"archive")
    ah = (("sha256", hashlib.sha256(archive.read_bytes()).hexdigest()),)
    pins = {
        "demo": IndexPin(
            "demo",
            "1",
            tmp_path.as_uri(),
            wheels=(
                WheelArtifact(local.name, local.as_uri(), hashes, local_path=local),
            ),
        ),
        "unpack": ArchivePin("unpack", "3", archive.as_uri(), ah),
        "directory": LocalPin("directory", "2", str(tmp_path)),
    }
    value = LockInput(
        targets={target.label: TargetLock(target, pins)}, validation_inputs=current
    )
    dest = tmp_path / "pdm.lock"
    write_pdm_lock(
        value, toml_io.load_path(project), project_dir=tmp_path, output_path=dest
    )
    assert not can_reuse_saved_lock(
        dest, project, current, ResolveInputs(), [target], native_format="pdm"
    )
    check_saved_lock(
        dest, project, current, ResolveInputs(), [target], native_format="pdm"
    )
    prior = read_lock_preferences(dest, native_format="pdm", exclude={"Demo"})
    assert prior is not None
    assert prior.known_packages == {"demo", "unpack"}
    assert prior.versions_for(target) == {}
    assert prior.summary_versions() == {"demo": Version("1"), "unpack": Version("3")}
    local.write_bytes(b"changed")
    with pytest.raises(ValueError, match="local artifact changed"):
        check_saved_lock(
            dest, project, current, ResolveInputs(), [target], native_format="pdm"
        )


def test_native_file_reproduction_reads_without_writing(tmp_path: Path) -> None:
    dest = tmp_path / "pdm.lock"
    target = ResolveTarget.for_host()
    inputs = LockInput(targets={target.label: TargetLock(target, {})})
    with pytest.raises(LockValidationError, match="requires a lockfile path"):
        write_pdm_lock(inputs, {}, project_dir=tmp_path, reproduce=True)
    with pytest.raises(LockValidationError, match="cannot read"):
        write_pdm_lock(
            inputs, {}, project_dir=tmp_path, output_path=dest, reproduce=True
        )

    write_pdm_lock(inputs, {}, project_dir=tmp_path, output_path=dest)
    saved = dest.read_bytes()
    write_pdm_lock(inputs, {}, project_dir=tmp_path, output_path=dest, reproduce=True)
    assert dest.read_bytes() == saved
    with pytest.raises(LockValidationError, match="out of date"):
        write_pdm_lock(
            inputs,
            {"project": {"dependencies": ["added"]}},
            project_dir=tmp_path,
            output_path=dest,
            reproduce=True,
        )
    assert dest.read_bytes() == saved
    assert read_lock_preferences(dest, native_format="pdm") is None


def test_target_specific_preference_and_altered_evidence(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname="app"\nversion="1"\ndependencies=["demo"]\n')
    linux = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )
    windows = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("windows_amd64")
    )
    path = tmp_path / "shared-1-py3-none-any.whl"
    path.write_bytes(b"shared")
    remote = IndexPin(
        "shared",
        "1",
        tmp_path.as_uri(),
        wheels=(
            WheelArtifact(
                path.name,
                path.as_uri(),
                (("sha256", hashlib.sha256(path.read_bytes()).hexdigest()),),
            ),
        ),
    )
    targets = {}
    for target, version in ((linux, "1"), (windows, "2")):
        w = WheelArtifact(
            f"demo-{version}-py3-none-any.whl",
            f"https://example.org/demo-{version}.whl",
            _HASH,
        )
        demo = IndexPin("demo", version, "https://pypi.org/simple", wheels=(w,))
        targets[target.label] = TargetLock(target, {"demo": demo, "shared": remote})
    inputs = ResolveInputs()
    snapshot = snapshot_inputs(project, inputs, [linux, windows])
    value = LockInput(
        targets=targets,
        environments=[Marker(t.environment_marker_string) for t in (linux, windows)],
        validation_inputs=snapshot,
    )
    output = tmp_path / "pdm.lock"
    write_pdm_lock(
        value, toml_io.load_path(project), project_dir=tmp_path, output_path=output
    )

    preferences = read_lock_preferences(output, native_format="pdm")
    assert preferences is not None
    assert preferences.summary_versions() is None
    assert preferences.versions_for(windows)["demo"] == Version("2")
    assert preferences.versions_for(linux)["demo"] == Version("1")
    solution = NativeSolution(toml_io.load_path(output), "pdm")
    other = ResolveTarget.for_declared(
        python_version="3.13", spec=PlatformSpec("linux_x86_64")
    )
    with pytest.raises(ValueError, match="cannot verify saved pins"):
        solution.fixed_versions(other, ())

    document = toml_io.load_path(output)
    document["tool"]["nab"]["solution"]["local-files"].clear()
    output.write_text(toml_writer.dumps(document))
    assert read_lock_preferences(output, native_format="pdm") is None
    with pytest.raises(ValueError, match="saved selections changed"):
        check_saved_lock(
            output, project, snapshot, inputs, [linux, windows], native_format="pdm"
        )


def test_native_provenance_only_output(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    lock = LockInput(
        targets={target.label: TargetLock(target, {})},
        provenance=Provenance(
            "1",
            datetime.now(timezone.utc),
            ("nab", "lock"),
            "pyproject.toml",
            "specific",
        ),
    )
    output = tmp_path / "pdm.lock"
    write_pdm_lock(lock, {}, project_dir=tmp_path, output_path=output)
    assert toml_io.load_path(output)["tool"]["nab"]["command-line"] == ["nab", "lock"]
    assert read_lock_preferences(output, native_format="pdm") is None
