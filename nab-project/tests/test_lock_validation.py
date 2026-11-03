"""Producer evidence preserves dependency semantics without changing installation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nab_project import toml_io
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.lock_validation import (
    _value,
    capture_metadata,
    content_digest,
    snapshot_inputs,
)
from nab_project.lockfile import build_pylock, render_lock
from nab_project.resolve import build_lock_input, resolve_with_coordinator
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import PackageOverride
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import DecisionOrder, LocalSource, ResolutionStrategy
from nab_provider.records import IndexConfig
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, ResolveTarget
from nab_provider.testing import make_metadata, make_wheel


def _project(tmp_path: Path, text: str = "") -> Path:
    """Write a root whose irrelevant fields can change independently of its requirements."""
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname="app"\nversion="1"\ndependencies=["demo >= 1"]\n' + text
    )
    return path


def _targets() -> list[ResolveTarget]:
    return Matrix(
        python="==3.14",
        platforms=(PlatformSpec("linux_x86_64"),),
        include_free_threaded=True,
    ).expand()


def test_input_snapshot_ignores_formatting_and_search_preferences(
    tmp_path: Path,
) -> None:
    path = _project(tmp_path)
    first = snapshot_inputs(path, ResolveInputs(), _targets())
    path.write_text(
        '[project]\nname = "app"\nversion = "2"\ndescription="updated"\n# comment\ndependencies = [ "Demo>=1" ]\n'
    )
    second = snapshot_inputs(
        path,
        ResolveInputs(
            decision_order=DecisionOrder.STABLE, resolution=ResolutionStrategy.LOWEST
        ),
        _targets(),
    )
    assert first == second
    path.write_text(path.read_text().replace("Demo>=1", "Demo>=2"))
    assert snapshot_inputs(path, ResolveInputs(), _targets()) != first


def test_groups_extras_build_inputs_and_constraints_are_normalized(
    tmp_path: Path,
) -> None:
    path = _project(
        tmp_path,
        '[project.optional-dependencies]\nfeature=["Extra_Pkg>=2"]\n[dependency-groups]\nchecks=["Tool_Pkg>=3"]\ndev=[{include-group="checks"}]\n[build-system]\nrequires=["hatchling"]\n',
    )
    inputs = ResolveInputs(
        default_groups=("dev",),
        base_group="runtime",
        build_group="build",
        constraints=("DEMO<3",),
    )
    snapshot = snapshot_inputs(
        path, inputs, _targets(), extras=["feature"], exclude=frozenset({"app"})
    )
    assert snapshot["roots"] == {
        "base": ["demo>=1"],
        "extra:feature": ["extra-pkg>=2"],
        "group:dev": ["tool-pkg>=3"],
        "build": ["hatchling"],
    }
    assert snapshot["constraints"] == ["demo<3"]
    assert snapshot["excluded"] == ["app"]
    built = snapshot_inputs(path, inputs, _targets(), build_requirements=True)
    assert built["roots"] == {"build": ["hatchling"]}


def test_local_metadata_is_read_fresh_and_credentials_are_removed(
    tmp_path: Path,
) -> None:
    path = _project(tmp_path)
    source = tmp_path / "local"
    source.mkdir()
    local = _project(source)
    inputs = ResolveInputs(
        local_sources=(LocalSource("local", str(source)),),
        indexes=(IndexConfig("private", "https://user:secret@example.com/simple"),),
    )
    first = snapshot_inputs(path, inputs, _targets())
    assert "secret" not in json.dumps(first)
    local.write_text(local.read_text().replace("demo >= 1", "demo >= 2"))
    assert snapshot_inputs(path, inputs, _targets()) != first
    local.unlink()
    assert snapshot_inputs(path, inputs, _targets())["local"]["local"]["dynamic"] == [
        "metadata"
    ]


def test_dynamic_local_metadata_does_not_claim_static_inputs(tmp_path: Path) -> None:
    path = _project(tmp_path)
    source = tmp_path / "local"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="local"\ndynamic=["version","dependencies"]\n'
    )
    result = snapshot_inputs(
        path,
        ResolveInputs(
            local_sources=(LocalSource("local", str(tmp_path), subdirectory="local"),)
        ),
        _targets(),
    )
    assert result["local"]["local"]["dynamic"] == ["dependencies", "version"]


def test_metadata_capture_is_an_independent_snapshot() -> None:
    item = WheelMetadata(
        "demo",
        Version("1"),
        requires_python=SpecifierSet(">=3.10"),
        requires_dist=[parse_requirement("dep>=2")],
        provides_extra=["Feature_Name"],
    )
    captured = capture_metadata({("demo", Version("1")): item}, {"demo": Version("1")})
    item.requires_dist.clear()
    assert captured["demo"]["requires-dist"] == ["dep>=2"]
    assert captured["demo"]["requires-python"] == ">=3.10"
    assert captured["demo"]["provides-extra"] == ["feature-name"]


def test_real_target_metadata_roundtrips_without_affecting_selection(
    tmp_path: Path,
) -> None:
    path = _project(tmp_path)
    targets = _targets()
    wheel = make_wheel("1", package="demo", hashes=(("sha256", "a" * 64),))
    port = make_coordinator(
        listings={"demo": [wheel]}, metadata_text=make_metadata("demo", "1")
    )
    result = resolve_with_coordinator(
        port,
        targets,
        [parse_requirement("demo>=1")],
        include_dependency_requirements=True,
    )
    assert result.success
    lock_input = build_lock_input(result)
    lock_input.validation_inputs = snapshot_inputs(path, ResolveInputs(), targets)
    document = toml_io.loads(render_lock(lock_input, lock_dir=tmp_path))
    record = document["tool"]["nab"]["validation"]
    assert record["contents-sha256"] == content_digest(document)
    assert len(record["metadata"]) == 1
    assert len(record["targets"]) == 2
    assert record["targets"][0]["platform"]["free_threaded"] is False
    assert record["targets"][1]["platform"]["free_threaded"] is True
    parsed = Pylock.from_dict(document)
    parsed.validate()
    original = build_pylock(lock_input)
    for target in targets:
        actual = list(
            parsed.select(environment=target.marker_env, tags=target.tags.ordered)
        )
        expected = list(
            original.select(environment=target.marker_env, tags=target.tags.ordered)
        )
        assert [(pkg.name, pkg.version, dist) for pkg, dist in actual] == [
            (pkg.name, pkg.version, dist) for pkg, dist in expected
        ]
    assert record["cacheable"] is True
    assert len(record["metadata-sets"]) == 1
    lock_input.validation_inputs["local"]["app"] = {"dynamic": ["version"]}
    dynamic = toml_io.loads(render_lock(lock_input, lock_dir=tmp_path))
    assert dynamic["tool"]["nab"]["validation"]["cacheable"] is False
    before = record["contents-sha256"]
    document["packages"][0]["version"] = "2"
    assert content_digest(document) != before


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        3,
        2.5,
        "plain",
        Path("/tmp/example"),
        Version("1"),
        SpecifierSet(">=1"),
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        {"set": frozenset({"b", "a"})},
        parse_requirement("Demo @ https://user:secret@example.com/demo.whl"),
        "Demo @ https://user:secret@example.com/demo.whl",
        "https://example.com/unchanged",
        "file:///tmp/archive.whl",
        "file://localhost/tmp/archive.whl",
    ],
)
def test_known_input_types_are_serializable(value: object, tmp_path: Path) -> None:
    result = _value(value, tmp_path)
    encoded = json.dumps(result)
    assert "secret" not in encoded


def test_override_records_keep_semantic_fields(tmp_path: Path) -> None:
    override = PackageOverride(
        requirement=parse_requirement("demo"),
        name="demo",
        version_range=VersionRange.full(),
        dependencies=("dep>=2",),
    )
    result = _value(override, tmp_path)
    assert result["requirement"] == "demo"
    assert "version_range" not in result
    assert result["dependencies"] == ["dep>=2"]


def test_unknown_input_type_is_not_silently_stringified(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="unsupported validation input type"):
        _value(object(), tmp_path)


def test_unreadable_or_malformed_local_metadata_is_not_ignored(tmp_path: Path) -> None:
    source = tmp_path / "local"
    source.mkdir()
    _project(source).write_text('project="invalid"')
    root = _project(tmp_path)
    with pytest.raises(TypeError, match="project must be a table"):
        snapshot_inputs(
            root,
            ResolveInputs(local_sources=(LocalSource("app", str(source)),)),
            _targets(),
        )


def test_legacy_local_metadata_requires_backend_validation(tmp_path: Path) -> None:
    root = _project(tmp_path)
    local = tmp_path / "legacy"
    local.mkdir()
    (local / "pyproject.toml").write_text('[build-system]\nrequires=["setuptools"]\n')
    inputs = ResolveInputs(local_sources=(LocalSource("legacy", str(local)),))
    assert snapshot_inputs(root, inputs, _targets())["local"]["legacy"]["dynamic"] == [
        "metadata"
    ]


def test_cross_drive_sources_retain_their_absolute_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_relative_path(path: Path, base: Path) -> str:
        raise ValueError("different drives")

    monkeypatch.setattr("nab_project.lock_validation.os.path.relpath", no_relative_path)
    assert _value(Path("D:/source"), tmp_path) == "D:/source"


def test_workspace_root_without_project_can_record_groups(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[dependency-groups]\ndev=["pytest>=9"]\n')
    snapshot = snapshot_inputs(path, ResolveInputs(), _targets(), groups=["dev"])
    assert snapshot["roots"] == {"base": [], "group:dev": ["pytest>=9"]}
