"""Prepared declarations remain local to one operation and its original files."""

from pathlib import Path

import pytest

from nab_project import toml_io
from nab_project.input_source import InputChangedError, InputFormat, read_input_document
from nab_project.prepared_input import PreparedProjectInput


def test_views_are_independent_and_do_not_change_normal_source_reads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="original"\ndependencies=[]\n')
    original = toml_io.load_path(path)
    document = {"project": {"name": "derived", "dependencies": ["dep"]}}
    prepared = PreparedProjectInput(
        path, original, document, identity={"lock": "test"}, local_inputs={}
    )
    document["project"]["dependencies"].append("later")
    first = read_input_document(path, project_input=prepared)
    first["project"]["dependencies"].append("caller-mutation")
    assert read_input_document(path, project_input=prepared)["project"][
        "dependencies"
    ] == ["dep"]
    assert read_input_document(path)["project"]["name"] == "original"
    with pytest.raises(InputChangedError):
        prepared.read(tmp_path / "different.toml")
    with pytest.raises(ValueError, match="script"):
        read_input_document(
            path, project_input=prepared, input_format=InputFormat.SCRIPT
        )


def test_semantic_root_changes_are_refused_but_comments_are_not(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="original"\n')
    original = toml_io.load_path(path)
    prepared = PreparedProjectInput(
        path, original, original, identity={}, local_inputs={}
    )
    with path.open("a") as file:
        file.write("# annotation\n")
    assert prepared.read(path) == original
    path.write_text('[project]\nname="changed"\n')
    with pytest.raises(InputChangedError, match="inputs changed"):
        prepared.read(path)


def test_unchanged_nan_survives_comments_and_table_reordering(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="root"\n[tool.example]\nvalues=[{limit=nan}]\n')
    original = toml_io.load_path(path)
    prepared = PreparedProjectInput(
        path, original, original, identity={}, local_inputs={}
    )

    assert prepared.read(path)["project"] == {"name": "root"}
    path.write_text(
        '[tool.example]\nvalues=[{limit=nan}] # unlimited\n[project]\nname="root"\n'
    )
    assert prepared.read(path)["project"] == {"name": "root"}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("true", "1"),
        ("1", "1.0"),
        ("nan", "inf"),
        ("{low=1}", "{low=1, high=2}"),
        ("{low=1, high=2}", "{low=1}"),
    ],
)
def test_changed_toml_values_are_refused(
    tmp_path: Path, before: str, after: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(f"[tool.example]\nvalues=[{{limit={before}}}]\n")
    original = toml_io.load_path(path)
    prepared = PreparedProjectInput(
        path, original, original, identity={}, local_inputs={}
    )

    path.write_text(f"[tool.example]\nvalues=[{{limit={after}}}]\n")
    with pytest.raises(InputChangedError, match="project inputs changed"):
        prepared.read(path)


@pytest.mark.parametrize("change", ["edit", "delete", "create"])
def test_watched_local_files_and_configuration_presence_are_checked(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="root"\n')
    local = tmp_path / "local.toml"
    if change != "create":
        local.write_text("original")
    expected = None if change == "create" else "original"
    original = toml_io.load_path(path)
    prepared = PreparedProjectInput(
        path, original, original, identity={}, local_inputs={local: expected}
    )
    assert prepared.read(path) == original
    if change == "delete":
        local.unlink()
    else:
        local.write_text("changed")
    with pytest.raises(InputChangedError, match="inputs changed"):
        prepared.read(path)


def test_prepared_identity_and_roots_are_part_of_saved_validation(
    tmp_path: Path,
) -> None:
    from nab_project.inputs import ResolveInputs
    from nab_project.lock_validation import snapshot_inputs
    from nab_provider.target import ResolveTarget

    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="root"\ndependencies=[]\n')
    original = toml_io.load_path(path)
    prepared = PreparedProjectInput(
        path,
        original,
        {"project": {"dependencies": ["dep"]}},
        identity={"lock": "dev"},
        local_inputs={},
    )
    snapshot = snapshot_inputs(
        path, ResolveInputs(), [ResolveTarget.for_host()], project_input=prepared
    )
    assert snapshot["prepared-inputs"] == {"lock": "dev"}
    assert snapshot["roots"]["base"] == ["dep"]


def test_complete_requirements_bind_extras_and_keep_environment_conditions(
    tmp_path: Path,
) -> None:
    from nab_project.inputs import ResolveInputs
    from nab_project.resolve import complete_project_requirements
    from nab_provider.target import ResolveTarget

    document = {
        "project": {
            "name": "app",
            "dependencies": ["runtime"],
            "optional-dependencies": {
                "fast": ["app[speed]"],
                "speed": [
                    "extension; extra == 'speed' and python_version < '3.14'",
                    "missing; extra == 'unselected'",
                ],
            },
        },
        "build-system": {"requires": ["backend"]},
        "dependency-groups": {"test": ["pytest"]},
    }
    result = complete_project_requirements(
        tmp_path / "pyproject.toml",
        document,
        ResolveInputs(),
        [ResolveTarget.for_host()],
        groups=("test",),
        extras=("fast",),
        dependencies=False,
        build_requirements=True,
    )
    assert {req.name for req in result} == {"pytest", "extension", "backend"}
    marker = next(req.marker for req in result if req.name == "extension")
    assert marker is not None
    assert marker.evaluate({"python_version": "3.13"})
    assert not marker.evaluate({"python_version": "3.14"})


def test_complete_requirements_refuse_declared_alternatives(tmp_path: Path) -> None:
    from nab_project.conflicts import ConflictKind, ConflictMember, ConflictSet
    from nab_project.inputs import ResolveInputs
    from nab_project.resolve import complete_project_requirements
    from nab_provider.target import ResolveTarget

    document = {"dependency-groups": {"test": ["pytest"], "release": ["twine"]}}
    inputs = ResolveInputs(
        conflicts=(
            ConflictSet(
                (
                    ConflictMember(ConflictKind.GROUP, "test"),
                    ConflictMember(ConflictKind.GROUP, "release"),
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        complete_project_requirements(
            tmp_path / "pyproject.toml",
            document,
            inputs,
            [ResolveTarget.for_host()],
            groups=("test", "release"),
            extras=(),
            dependencies=True,
            build_requirements=False,
        )
