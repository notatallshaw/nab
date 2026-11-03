"""Check saved solutions through real resolution and lock emission."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from nab_project import toml_io
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.lock_check import (
    LockConsistencyError,
    _case_targets,
    _check_local_artifacts,
    _local_metadata,
    _validate_graph,
    check_saved_lock,
)
from nab_project.lock_validation import (
    content_digest,
    snapshot_inputs,
    validation_digest,
)
from nab_project.lockfile import render_lock
from nab_project.resolve import (
    build_lock_input,
    plan_project_inputs,
    resolve_with_coordinator,
)
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider.overrides import PackageOverride
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import LocalSource
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, ResolveTarget
from nab_provider.testing import make_metadata, make_wheel


def _saved(
    tmp_path: Path, *, text: str = "", inputs: ResolveInputs | None = None
) -> tuple[Path, Path, ResolveInputs, list[ResolveTarget]]:
    """Create an artifact-bound record with the production planner and resolver."""
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname="app"\nversion="1"\ndependencies=["demo>=1"]\n' + text
    )
    inputs = ResolveInputs() if inputs is None else inputs
    targets = Matrix(
        python="==3.14",
        platforms=(PlatformSpec("linux_x86_64"),),
        include_free_threaded=True,
    ).expand()
    forks, base = plan_project_inputs(path, inputs, targets)
    wheel = make_wheel("1", package="demo", hashes=(("sha256", "a" * 64),))
    port = make_coordinator(
        listings={"demo": [wheel]}, metadata_text=make_metadata("demo", "1")
    )
    result = resolve_with_coordinator(
        port,
        targets,
        inputs=inputs,
        forks=forks,
        base_requirements=base,
        include_dependency_requirements=True,
    )
    assert result.success
    lock_input = build_lock_input(result, inputs=inputs)
    lock_input.validation_inputs = snapshot_inputs(path, inputs, targets)
    lock = tmp_path / "pylock.toml"
    lock.write_text(render_lock(lock_input, lock_dir=tmp_path))
    return lock, path, inputs, targets


def _check(case: tuple[Path, Path, ResolveInputs, list[ResolveTarget]]) -> None:
    lock, path, inputs, targets = case
    check_saved_lock(
        lock, path, snapshot_inputs(path, inputs, targets), inputs, targets
    )


def test_unchanged_inputs_skip_graph_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _saved(tmp_path)
    before = case[0].read_bytes()

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("unchanged inputs must not plan a graph or resolve")

    monkeypatch.setattr("nab_project.lock_check.plan_project_inputs", unexpected)
    _check(case)
    assert case[0].read_bytes() == before


@pytest.mark.parametrize("bound", [">=0", "==1", "<2", ">=2"])
def test_bounds_are_checked_against_saved_versions(tmp_path: Path, bound: str) -> None:
    case = _saved(tmp_path)
    case[1].write_text(case[1].read_text().replace("demo>=1", "demo" + bound))
    if bound == ">=2":
        with pytest.raises(LockConsistencyError, match="does not satisfy"):
            _check(case)
    else:
        _check(case)


@pytest.mark.parametrize(
    "constraint",
    [
        "demo<2",
        "demo>=2",
        "absent>=2",
        'demo>=2; sys_platform == "win32"',
        'demo>=2; python_full_version >= "3.14.5"',
    ],
)
def test_constraints_cover_active_packages_and_micro_slices(
    tmp_path: Path, constraint: str
) -> None:
    lock, path, _, targets = _saved(tmp_path)
    inputs = ResolveInputs(constraints=(constraint,))
    if constraint.startswith("demo>=2") and "win32" not in constraint:
        with pytest.raises(LockConsistencyError, match="constraint"):
            _check((lock, path, inputs, targets))
    else:
        _check((lock, path, inputs, targets))


def _replace_record(lock: Path, document: dict[str, Any]) -> None:
    """Reseal a deliberately edited record to exercise its inner checks."""
    record = document["tool"]["nab"]["validation"]
    record["record-sha256"] = validation_digest(
        {key: value for key, value in record.items() if key != "record-sha256"}
    )
    lock.write_text(tomli_w.dumps(document))


@pytest.mark.parametrize(
    "change",
    [
        "record",
        "contents",
        "inputs",
        "metadata",
        "missing",
        "version",
        "tool",
        "selection",
        "target",
    ],
)
def test_invalid_evidence_is_refused(tmp_path: Path, change: str) -> None:
    case = _saved(tmp_path)
    lock = case[0]
    document = toml_io.load_path(lock)
    record = document["tool"]["nab"]["validation"]
    if change == "record":
        record["cacheable"] = False
        lock.write_text(
            lock.read_text().replace("cacheable = true", "cacheable = false")
        )
    elif change == "contents":
        lock.write_text(
            lock.read_text().replace('"' + "a" * 64 + '"', '"' + "b" * 64 + '"')
        )
    elif change == "missing":
        lock.write_text(lock.read_text().split("[tool.nab.validation]")[0])
    else:
        if change == "inputs":
            record["inputs-sha256"] = "invalid"
        elif change == "metadata":
            next(iter(record["metadata"].values()))["version"] = "2"
        elif change == "version":
            record["version"] = 999
        elif change == "tool":
            document["tool"]["nab"]["validation"] = []
            lock.write_text(tomli_w.dumps(document))
        elif change == "selection":
            record["targets"][0]["selection"] = [["group", "absent"]]
        elif change == "target":
            record["targets"][0]["environment"]["python_version"] = "3.99"
        if change != "tool":
            _replace_record(lock, document)
        case[1].write_text(case[1].read_text().replace("demo>=1", "demo>=0"))
    with pytest.raises(LockConsistencyError):
        _check(case)


@pytest.mark.parametrize(
    "change",
    ["roots", "policy-sha256", "targets", "excluded", "build-requirements", "local"],
)
def test_selection_and_admission_changes_need_update(
    tmp_path: Path, change: str
) -> None:
    case = _saved(tmp_path)
    lock, path, inputs, targets = case
    current = snapshot_inputs(path, inputs, targets)
    if change == "roots":
        current["roots"]["base"] = ["other"]
    elif change == "local":
        current["local"]["local"] = {"name": "local"}
    else:
        current[change] = "changed"
    with pytest.raises(LockConsistencyError, match="changed"):
        check_saved_lock(lock, path, current, inputs, targets)


def test_missing_and_malformed_locks(tmp_path: Path) -> None:
    case = _saved(tmp_path)
    case[0].unlink()
    with pytest.raises(LockConsistencyError, match="no lockfile"):
        _check(case)
    case[0].write_text('lock-version = "1.0"\n')
    with pytest.raises(LockConsistencyError, match="cannot verify"):
        _check(case)


def test_transitive_versions_extras_and_cycles_are_checked() -> None:
    metadata = {
        "demo": {
            "version": "1",
            "provides-extra": ["feature"],
            "requires-dist": [
                'dep>=2; extra == "feature"',
                'absent; sys_platform == "win32"',
            ],
        },
        "dep": {
            "version": "2",
            "provides-extra": [],
            "requires-dist": ["demo[feature]>=1"],
        },
    }
    environment = ResolveTarget.for_host().marker_env | {"sys_platform": "linux"}
    _validate_graph(
        metadata,
        [parse_requirement("demo[feature]"), parse_requirement("demo[feature]")],
        ["dep<3"],
        environment,
    )
    _validate_graph(
        metadata,
        [parse_requirement('missing; sys_platform == "win32"')],
        [],
        environment,
    )
    with pytest.raises(LockConsistencyError, match="missing missing"):
        _validate_graph(metadata, [parse_requirement("missing")], [], environment)
    metadata["dep"]["version"] = "1"
    with pytest.raises(LockConsistencyError, match="dep==1 does not satisfy"):
        _validate_graph(metadata, [parse_requirement("demo[feature]")], [], environment)
    with pytest.raises(LockConsistencyError, match="URL constraints"):
        _validate_graph(
            metadata,
            [parse_requirement("demo")],
            ["demo @ https://example.com/demo.whl"],
            environment,
        )


@pytest.mark.parametrize(
    "change",
    ["name", "dynamic", "requires-python", "dependencies", "optional-dependencies"],
)
def test_local_topology_changes_need_update(tmp_path: Path, change: str) -> None:
    local = tmp_path / "local"
    local.mkdir()
    source = local / "pyproject.toml"
    source.write_text('[project]\nname="local"\nversion="1"\ndependencies=[]\n')
    case = _saved(
        tmp_path,
        inputs=ResolveInputs(local_sources=(LocalSource("local", str(local)),)),
    )
    lock, path, inputs, targets = case
    current = snapshot_inputs(path, inputs, targets)
    current["local"]["local"][change] = (
        {"feature": ["other"]}
        if change == "optional-dependencies"
        else ["other"]
        if change in {"dynamic", "dependencies"}
        else "changed"
    )
    with pytest.raises(LockConsistencyError, match="local"):
        check_saved_lock(lock, path, current, inputs, targets)


def test_static_local_metadata_is_refreshed_without_hiding_dynamic_metadata() -> None:
    original = {"local": {"name": "local", "version": "1", "dependencies": ["demo>=1"]}}
    current = {
        "local": {
            "local": {"name": "local", "version": "2", "dependencies": ["demo<2"]}
        }
    }
    metadata = {
        "local": {
            "name": "local",
            "version": "1",
            "requires-dist": ["demo>=1"],
            "provides-extra": [],
        }
    }
    result = _local_metadata(metadata, current, {"local": original}, ResolveInputs())
    assert result["local"]["version"] == "2"
    assert result["local"]["requires-dist"] == ["demo<2"]
    assert metadata["local"]["version"] == "1"
    assert (
        _local_metadata(
            metadata, {"local": original}, {"local": original}, ResolveInputs()
        )
        == metadata
    )
    assert _local_metadata({}, current, {"local": original}, ResolveInputs()) == {}
    override = ResolveInputs(
        package_overrides=(
            PackageOverride(
                name="local",
                requirement=parse_requirement("local"),
                version_range=VersionRange.full(),
                dependencies=(parse_requirement("demo>=1"),),
            ),
        )
    )
    with pytest.raises(LockConsistencyError, match="overrides"):
        _local_metadata(metadata, current, {"local": original}, override)
    current["local"]["local"]["dynamic"] = ["version"]
    with pytest.raises(LockConsistencyError, match="dynamic"):
        _local_metadata(metadata, current, current, ResolveInputs())
    current["local"]["local"] = {"name": "local"}
    with pytest.raises(LockConsistencyError, match="cannot read static"):
        _local_metadata(metadata, current, {"local": original}, ResolveInputs())


@pytest.mark.parametrize("location", ["path", "url"])
@pytest.mark.parametrize("damage", [False, True])
def test_local_artifact_bytes_are_checked(
    tmp_path: Path, location: str, damage: bool
) -> None:
    case = _saved(tmp_path)
    lock = case[0]
    document = toml_io.load_path(lock)
    wheel = document["packages"][0]["wheels"][0]
    artifact = tmp_path / "demo-1-py3-none-any.whl"
    artifact.write_bytes(b"locked artifact bytes")
    wheel.pop("url")
    wheel[location] = artifact.name if location == "path" else artifact.as_uri()
    wheel["hashes"] = {"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    document["tool"]["nab"]["validation"]["contents-sha256"] = content_digest(document)
    _replace_record(lock, document)
    if damage:
        artifact.write_bytes(b"changed artifact bytes")
        with pytest.raises(LockConsistencyError, match="local artifact changed"):
            _check(case)
    else:
        _check(case)


def test_micro_slices_keep_the_recorded_interval(tmp_path: Path) -> None:
    lock, _, _, targets = _saved(tmp_path)
    record = toml_io.load_path(lock)["tool"]["nab"]["validation"]["targets"][0]
    record["micro-clauses"] = ['python_full_version >= "3.14.5"']
    record["environment"]["python_full_version"] = "3.14.5"
    assert [
        target.marker_env["python_full_version"]
        for target in _case_targets(record, targets, [])
    ] == ["3.14.5"]
    record["concrete-micro"] = True
    assert _case_targets(record, targets, []) == [targets[0]]


def test_local_extra_bounds_can_change_without_changing_selection(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    source = local / "pyproject.toml"
    source.write_text(
        '[project]\nname="local"\nversion="1"\n[project.optional-dependencies]\nfeature=["demo>=1"]\n'
    )
    case = _saved(
        tmp_path,
        inputs=ResolveInputs(local_sources=(LocalSource("local", str(local)),)),
    )
    source.write_text(source.read_text().replace("demo>=1", "demo<2"))
    _check(case)
    source.write_text(source.read_text().replace("demo<2", "other"))
    with pytest.raises(LockConsistencyError, match="local extras"):
        _check(case)


def test_local_artifact_requires_a_supported_hash(tmp_path: Path) -> None:
    case = _saved(tmp_path)
    document = toml_io.load_path(case[0])
    wheel = document["packages"][0]["wheels"][0]
    wheel.pop("url")
    wheel["path"] = "demo-1-py3-none-any.whl"
    wheel["hashes"] = {"md5": "a" * 32}
    with pytest.raises(LockConsistencyError, match="no supported hash"):
        _check_local_artifacts(Pylock.from_dict(document), tmp_path)


@pytest.mark.parametrize("same_hash", [True, False])
def test_repeated_paths_check_every_declared_hash(
    tmp_path: Path, same_hash: bool
) -> None:
    case = _saved(tmp_path)
    document = toml_io.load_path(case[0])
    wheel = document["packages"][0]["wheels"][0]
    artifact = tmp_path / "demo-1-py3-none-any.whl"
    artifact.write_bytes(b"locked bytes")
    wheel.pop("url")
    wheel["path"] = artifact.name
    wheel["hashes"] = {"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    duplicate = dict(wheel)
    if not same_hash:
        duplicate["hashes"] = {"sha256": "b" * 64}
    document["packages"][0]["wheels"].append(duplicate)
    parsed = Pylock.from_dict(document)
    if same_hash:
        _check_local_artifacts(parsed, tmp_path)
    else:
        with pytest.raises(LockConsistencyError, match="local artifact changed"):
            _check_local_artifacts(parsed, tmp_path)


def test_graph_preserves_base_dependencies_and_individual_extra_semantics() -> None:
    metadata = {
        "demo": {
            "version": "1",
            "provides-extra": ["feature", "other"],
            "requires-dist": ['absent; extra == "feature" and extra == "other"'],
        },
        "dep": {"version": "1", "provides-extra": [], "requires-dist": []},
    }
    roots = [parse_requirement("demo[feature,other]")]
    environment = ResolveTarget.for_host().marker_env
    _validate_graph(metadata, roots, [], environment)
    metadata["demo"]["requires-dist"] = ['dep>=2; extra != "feature"']
    with pytest.raises(LockConsistencyError, match="dep==1 does not satisfy"):
        _validate_graph(metadata, roots, [], environment)
