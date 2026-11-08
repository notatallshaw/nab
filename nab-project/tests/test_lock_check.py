"""Check saved solutions through real resolution and lock emission."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from nab_project import toml_io, toml_writer
from nab_project._resolve.engine import InstallContexts
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.lock_check import (
    LockConsistencyError,
    _check_fixed_solution,
    _check_local_artifacts,
    _check_target_slices,
    _current_local_metadata,
    _fork_selections,
    can_reuse_saved_lock,
    check_saved_lock,
)
from nab_project.lock_policy import LockPolicy
from nab_project.lock_validation import content_digest, snapshot_inputs
from nab_project.lockfile import ArchivePin, LockInput, TargetLock, render_lock
from nab_project.resolve import (
    ResolveFork,
    build_lock_input,
    plan_project_inputs,
    resolve_with_coordinator,
)
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import parse_metadata
from nab_provider.overrides import PackageOverride
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import ArchiveSource, LocalSource, VcsSource
from nab_provider.records import IndexConfig
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, MatrixInclude, ResolveTarget
from nab_provider.testing import make_metadata, make_wheel, pkg_override


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
        include=(MatrixInclude(free_threaded=True),),
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


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX filesystem byte paths")
def test_local_index_changes_keep_distinct_filesystem_bytes(tmp_path: Path) -> None:
    """A different file-URL byte path changes the saved selection policy."""
    first = tmp_path / os.fsdecode(b"index-\xff")
    second = tmp_path / os.fsdecode(b"index-\xfe")
    inputs = ResolveInputs(indexes=(IndexConfig("local", first.as_uri()),))
    case = _saved(tmp_path, inputs=inputs)
    _check(case)

    lock, path, _, targets = case
    changed = ResolveInputs(indexes=(IndexConfig("local", second.as_uri()),))
    with pytest.raises(LockConsistencyError, match="selections or policy changed"):
        _check((lock, path, changed, targets))


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


@pytest.mark.parametrize(
    ("first_suffix", "second_suffix"),
    [
        ("%FF", "%FE"),
        ("%FF", "%EF%BF%BD"),
        ("%C3%A9", "%C3%A8"),
        ("%3Fselector", "?selector"),
        ("%23selector", "#selector"),
        ("%253Fselector", "%3Fselector"),
    ],
    ids=[
        "undecodable-bytes",
        "replacement-character",
        "utf8",
        "query",
        "fragment",
        "percent",
    ],
)
def test_file_index_changes_need_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_suffix: str,
    second_suffix: str,
) -> None:
    # Exercise POSIX filename decoding on every test platform.
    monkeypatch.setattr(sys, "platform", "linux")
    index_url = tmp_path.as_uri() + "/index-"
    case = _saved(
        tmp_path,
        inputs=ResolveInputs(indexes=(IndexConfig("local", index_url + first_suffix),)),
    )
    lock, path, _, targets = case
    before = lock.read_bytes()
    _check(case)
    assert lock.read_bytes() == before

    changed = ResolveInputs(indexes=(IndexConfig("local", index_url + second_suffix),))
    with pytest.raises(LockConsistencyError, match="policy changed"):
        _check((lock, path, changed, targets))
    assert lock.read_bytes() == before


@pytest.mark.parametrize("url_kind", ["file", "localhost", "https"])
def test_archive_subdirectory_changes_need_update(
    tmp_path: Path, url_kind: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="app"\nversion="1"\ndependencies=["demo"]\n')

    archive = tmp_path / "source archive.tar.gz"
    archive.write_bytes(b"reviewed archive bytes")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    url = archive.as_uri()
    if url_kind == "localhost":
        url = url.replace("file:///", "file://localhost/")
    elif url_kind == "https":
        url = "https://example.com/archive.tar.gz"

    source = ArchiveSource("demo", f"{url}#sha256={digest}&subdirectory=first")
    inputs = ResolveInputs(archive_sources=(source,))
    targets = [ResolveTarget.for_host()]
    pin = ArchivePin("demo", "1.0", url, (("sha256", digest),), "first")
    lock_input = LockInput(
        targets={"host": TargetLock(targets[0], {"demo": pin})},
        validation_inputs=snapshot_inputs(path, inputs, targets),
    )
    lock = tmp_path / "pylock.toml"
    lock.write_text(render_lock(lock_input, lock_dir=tmp_path))

    before = lock.read_bytes()
    _check((lock, path, inputs, targets))

    changed = inputs.replace(
        archive_sources=(ArchiveSource("demo", source.url.replace("first", "second")),)
    )
    with pytest.raises(LockConsistencyError, match="selections or policy changed"):
        _check((lock, path, changed, targets))

    assert lock.read_bytes() == before


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
    """Write edited machine data to exercise standard field checks."""
    lock.write_text(toml_writer.dumps(document))


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
    with pytest.raises(LockConsistencyError, match="changed|dynamic"):
        check_saved_lock(lock, path, current, inputs, targets)


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
    with pytest.raises(LockConsistencyError, match="changed"):
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


@pytest.mark.parametrize(
    "change",
    [
        "contents",
        "missing",
        "version",
        "previous-version",
        "legacy-schema",
        "tool",
        "missing-field",
    ],
)
def test_invalid_evidence_is_refused(tmp_path: Path, change: str) -> None:
    case = _saved(tmp_path)
    document = toml_io.load_path(case[0])
    record = document["tool"]["nab"]["validation"]
    if change == "contents":
        document["packages"][0]["wheels"][0]["hashes"]["sha256"] = "b" * 64
    elif change == "missing":
        del document["tool"]["nab"]["validation"]
    elif change == "version":
        record["version"] = 999
    elif change == "previous-version":
        record["version"] = 3
    elif change == "legacy-schema":
        record["version"] = 2
        record["inputs-sha256"] = record.pop("all-inputs-sha256")
        record["shape-sha256"] = record.pop("selection-inputs-sha256")
    elif change == "tool":
        document["tool"]["nab"]["validation"] = []
    else:
        del record["all-inputs-sha256"]
    _replace_record(case[0], document)
    with pytest.raises(LockConsistencyError):
        _check(case)


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_validation_version_requires_an_integer(
    tmp_path: Path, version: object
) -> None:
    case = _saved(tmp_path)
    document = toml_io.load_path(case[0])
    document["tool"]["nab"]["validation"]["version"] = version
    _replace_record(case[0], document)
    with pytest.raises(LockConsistencyError, match="no supported validation record"):
        _check(case)


def test_previous_datetime_fingerprint_requires_regeneration(tmp_path: Path) -> None:
    case = _saved(tmp_path)
    document = toml_io.load_path(case[0])
    document["packages"][0]["wheels"][0]["upload-time"] = datetime(
        2026, 1, 1, tzinfo=timezone.utc
    )
    standard = {key: value for key, value in document.items() if key != "tool"}
    legacy = json.dumps(
        standard,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode()
    document["tool"]["nab"]["validation"]["contents-sha256"] = hashlib.sha256(
        legacy
    ).hexdigest()
    Pylock.from_dict(document).validate()
    _replace_record(case[0], document)
    with pytest.raises(LockConsistencyError, match="does not match the lock contents"):
        _check(case)


def test_current_local_metadata_is_static_and_not_overridden() -> None:
    current = {
        "local": {
            "local": {"name": "local", "version": "2", "dependencies": ["demo<2"]}
        }
    }
    parsed = _current_local_metadata(current, ResolveInputs())
    assert parsed["local"].version == Version("2")
    assert [str(req) for req in parsed["local"].requires_dist] == ["demo<2"]
    override = PackageOverride(
        name="local",
        requirement=parse_requirement("local"),
        version_range=VersionRange.full(),
        dependencies=(parse_requirement("demo>=1"),),
    )
    with pytest.raises(LockConsistencyError, match="overrides"):
        _current_local_metadata(current, ResolveInputs(package_overrides=(override,)))
    current["local"]["local"] = {"name": "local"}
    with pytest.raises(LockConsistencyError, match="cannot read static"):
        _current_local_metadata(current, ResolveInputs())


def test_fixed_solution_checks_local_cycles_extras_and_omitted_members() -> None:
    local = {
        "local": parse_metadata(
            make_metadata(
                "local",
                "2",
                "demo>=1",
                'other>=2; extra == "feature"',
                provides_extra=["feature"],
            )
        ),
        "other": parse_metadata(make_metadata("other", "2", "local[feature]>=2")),
    }
    environment = ResolveTarget.for_host().marker_env
    _check_fixed_solution(
        {"demo": Version("1"), "other": None},
        local,
        [parse_requirement("local[feature]>=2")],
        [parse_requirement("other<3")],
        ["local"],
        environment,
    )
    with pytest.raises(LockConsistencyError, match="missing"):
        _check_fixed_solution(
            {"demo": Version("1")},
            local,
            [parse_requirement("local")],
            [],
            [],
            environment,
        )
    with pytest.raises(LockConsistencyError, match="does not satisfy"):
        _check_fixed_solution(
            {"demo": Version("1")},
            local,
            [parse_requirement("local[feature]>=3")],
            [],
            ["local"],
            environment,
        )
    _check_fixed_solution(
        {},
        {},
        [parse_requirement('missing; python_version < "0"')],
        [],
        [],
        environment,
    )


def test_opaque_versions_are_not_guessed() -> None:
    environment = ResolveTarget.for_host().marker_env
    with pytest.raises(LockConsistencyError, match="cannot verify the version"):
        _check_fixed_solution(
            {"demo": None}, {}, [parse_requirement("demo>=1")], [], [], environment
        )
    with pytest.raises(LockConsistencyError, match="URL constraints"):
        _check_fixed_solution(
            {"demo": Version("1")},
            {},
            [],
            [parse_requirement("demo @ https://example.com/demo.whl")],
            [],
            environment,
        )


def test_local_extra_conditions_match_provider_semantics() -> None:
    environment = ResolveTarget.for_host().marker_env
    local = {
        "local": parse_metadata(
            make_metadata(
                "local",
                "1",
                'missing; extra == "a" and extra == "b"',
                provides_extra=["a", "b"],
            )
        )
    }
    roots = [parse_requirement("local[a,b]"), parse_requirement("local[a,b]")]
    _check_fixed_solution({"local": None}, local, roots, [], [], environment)
    local["local"] = parse_metadata(
        make_metadata("local", "1", 'demo>=2; extra != "a"', provides_extra=["a", "b"])
    )
    with pytest.raises(LockConsistencyError, match="does not satisfy"):
        _check_fixed_solution(
            {"local": None, "demo": Version("1")}, local, roots, [], [], environment
        )


def test_fork_selections_preserve_named_base_and_joint_contexts() -> None:
    contexts = InstallContexts(
        project=(parse_requirement("demo"),),
        selectors={("extra", "feature"): (), ("group", "dev"): ()},
        name_project=True,
    )
    fork = ResolveFork((), (parse_requirement("demo"),), contexts)
    assert _fork_selections(fork, ResolveInputs(base_group="runtime")) == (
        {"feature"},
        {"runtime", "dev"},
    )
    assert _fork_selections(ResolveFork((("group", "dev"),), ()), ResolveInputs()) == (
        set(),
        {"dev"},
    )


def test_standard_markers_supply_micro_boundaries(tmp_path: Path) -> None:
    case = _saved(tmp_path)
    lock = Pylock.from_dict(toml_io.load_path(case[0]))
    slices = list(
        _check_target_slices(
            lock,
            case[3][0],
            [parse_requirement('demo; python_full_version >= "3.14.5"')],
        )
    )
    assert [target.marker_env["python_full_version"] for target in slices] == [
        "3.14.0",
        "3.14.5",
    ]
    concrete = ResolveTarget.for_host()
    assert list(_check_target_slices(lock, concrete, [])) == [concrete]


def test_standard_artifact_size_is_checked(tmp_path: Path) -> None:
    case = _saved(tmp_path)
    document = toml_io.load_path(case[0])
    artifact = tmp_path / "demo-1-py3-none-any.whl"
    artifact.write_bytes(b"local artifact")
    wheel = document["packages"][0]["wheels"][0]
    wheel.pop("url")
    wheel["path"] = artifact.name
    wheel["size"] = artifact.stat().st_size
    wheel["hashes"] = {"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    _check_local_artifacts(Pylock.from_dict(document), tmp_path)
    wheel["size"] += 1
    with pytest.raises(LockConsistencyError, match="unexpected size"):
        _check_local_artifacts(Pylock.from_dict(document), tmp_path)


def test_standard_requires_python_keeps_every_admitted_micro_slice(
    tmp_path: Path,
) -> None:
    case = _saved(tmp_path)
    document = toml_io.load_path(case[0])
    document["requires-python"] = ">=3.14.3"
    lock = Pylock.from_dict(document)
    requirements = [parse_requirement('demo; python_full_version >= "3.14.5"')]
    assert [
        target.marker_env["python_full_version"]
        for target in _check_target_slices(lock, case[3][0], requirements)
    ] == ["3.14.3", "3.14.5"]


def test_reuse_policy_preserves_the_standard_saved_check(tmp_path: Path) -> None:
    lock, path, inputs, targets = _saved(tmp_path)
    current = snapshot_inputs(path, inputs, targets)

    assert can_reuse_saved_lock(lock, path, current, inputs, targets)

    path.write_text(path.read_text().replace("demo>=1", "demo>=2"))
    current = snapshot_inputs(path, inputs, targets)

    assert not can_reuse_saved_lock(lock, path, current, inputs, targets)


@pytest.mark.parametrize(
    "context",
    [
        "local",
        "vcs",
        "archive",
        "local-snapshot",
        "file-index",
        "root-url",
        "constraint-url",
        "override-url",
    ],
)
def test_reuse_policy_refuses_unqualified_source_contexts(
    tmp_path: Path, context: str
) -> None:
    lock, path, inputs, targets = _saved(tmp_path)
    current = snapshot_inputs(path, inputs, targets)
    if context == "local":
        inputs = inputs.replace(local_sources=(LocalSource("source", str(tmp_path)),))
    elif context == "vcs":
        inputs = inputs.replace(
            vcs_sources=(VcsSource("source", "git+https://example.test/repo"),)
        )
    elif context == "archive":
        inputs = inputs.replace(
            archive_sources=(
                ArchiveSource("source", "https://example.test/source.tar.gz"),
            )
        )
    elif context == "local-snapshot":
        current["local"]["source"] = {"name": "source", "version": "1"}
    elif context == "file-index":
        inputs = inputs.replace(indexes=(IndexConfig("local", tmp_path.as_uri()),))
    elif context == "root-url":
        current["roots"]["base"] = ["demo @ https://example.test/demo.whl"]
    elif context == "constraint-url":
        current["constraints"] = ["demo @ https://example.test/demo.whl"]
    else:
        inputs = inputs.replace(
            package_overrides=(
                pkg_override(
                    "demo",
                    dependencies=(
                        parse_requirement("dep @ https://example.test/dep.whl"),
                    ),
                ),
            )
        )

    assert not can_reuse_saved_lock(lock, path, current, inputs, targets)


@pytest.mark.parametrize("location", ["path", "url"])
def test_reuse_policy_declines_valid_local_artifact_records(
    tmp_path: Path, location: str
) -> None:
    case = _saved(tmp_path)
    lock, path, inputs, targets = case
    document = toml_io.load_path(lock)
    artifact = tmp_path / "demo-1-py3-none-any.whl"
    artifact.write_bytes(b"reviewed local artifact")
    wheel = document["packages"][0]["wheels"][0]
    wheel.pop("url")
    wheel[location] = artifact.name if location == "path" else artifact.as_uri()
    wheel["hashes"] = {"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    document["tool"]["nab"]["validation"]["contents-sha256"] = content_digest(document)
    _replace_record(lock, document)
    _check(case)

    assert not can_reuse_saved_lock(
        lock, path, snapshot_inputs(path, inputs, targets), inputs, targets
    )


@pytest.mark.parametrize(
    "source", ["directory", "vcs", "archive", "sdist-http", "sdist-file"]
)
def test_reuse_policy_limits_saved_source_kinds(tmp_path: Path, source: str) -> None:
    lock, path, inputs, targets = _saved(tmp_path)
    document = toml_io.load_path(lock)
    package = document["packages"][0]
    artifact = package.pop("wheels")[0]
    if source == "directory":
        package["directory"] = {"path": "source", "editable": False}
    elif source == "vcs":
        package["vcs"] = {
            "type": "git",
            "url": "https://example.test/repo",
            "commit-id": "a" * 40,
        }
    elif source == "archive":
        package["archive"] = {
            "url": "https://example.test/source.tar.gz",
            "hashes": artifact["hashes"],
        }
    else:
        artifact["url"] = (
            "https://example.test/demo-1.tar.gz"
            if source == "sdist-http"
            else (tmp_path / "demo-1.tar.gz").as_uri()
        )
        artifact["name"] = "demo-1.tar.gz"
        package["sdist"] = artifact

    Pylock.from_dict(document)
    document["tool"]["nab"]["validation"]["contents-sha256"] = content_digest(document)
    _replace_record(lock, document)

    assert can_reuse_saved_lock(
        lock, path, snapshot_inputs(path, inputs, targets), inputs, targets
    ) is (source == "sdist-http")


def test_unknown_lock_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported-policy"):
        LockPolicy("unsupported-policy")
