"""Read selected dependency categories from source projects and requirement lists."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_project.dependency_inputs import dependency_input_requirements, input_table
from nab_provider.requirements_file import InvalidProjectRequirementError


def test_monorepo_groups_expand_local_extras_without_pulling_published_projects(
    tmp_path: Path,
) -> None:
    files = {
        "pyproject.toml": (
            "[project]\n"
            'name="front"\n'
            'dependencies=["backend>=1"]\n'
            "[project.optional-dependencies]\n"
            'test=["front[pytest]"]\n'
            'pytest=["pytest"]\n'
            "[build-system]\n"
            'requires=["hatchling"]\n'
            "[dependency-groups]\n"
            'test=["front[test]", "coverage"]\n'
        ),
        "backend/pyproject.toml": (
            "[project]\n"
            'name="backend"\n'
            'dependencies=["editables"]\n'
            "[build-system]\n"
            'requires=["flit_core"]\n'
        ),
    }
    declaration = {
        "projects": [
            {"groups": ["test"], "dependencies": True, "build-requirements": True},
            {"path": "backend", "build-requirements": True, "dependencies": True},
        ]
    }
    assert dependency_input_requirements(
        declaration, files.__getitem__, root=tmp_path
    ) == ["coverage", "editables", "flit_core", "hatchling", "pytest"]


def test_group_only_import_excludes_runtime_and_build_tools(tmp_path: Path) -> None:
    text = (
        "[project]\n"
        'name="pip"\n'
        'dependencies=["runtime"]\n'
        "[build-system]\n"
        'requires=["backend"]\n'
        "[dependency-groups]\n"
        'wheels=["wheel-fixture==1", "pip==26"]\n'
    )
    declaration = {
        "projects": [
            {"groups": ["wheels"], "dependencies": False, "build-requirements": False}
        ]
    }
    assert dependency_input_requirements(
        declaration, lambda path: text, root=tmp_path
    ) == ["pip==26", "wheel-fixture==1"]


def test_group_only_input_needs_no_project_table(tmp_path: Path) -> None:
    text = '[dependency-groups]\ntest=["pytest"]\n'
    declaration = {
        "projects": [
            {"groups": ["test"], "dependencies": False, "build-requirements": False}
        ]
    }
    assert dependency_input_requirements(
        declaration, lambda path: text, root=tmp_path
    ) == ["pytest"]


@pytest.mark.parametrize("directive", ["-r", "--requirement"])
@pytest.mark.parametrize("separator", [" ", "\t"], ids=["space", "tab"])
def test_requirements_file_includes_are_relative_and_comments_do_not_truncate_urls(
    tmp_path: Path,
    directive: str,
    separator: str,
) -> None:
    files = {
        "tests/requirements.txt": (
            f"# comment\n{directive}{separator}nested requirements.txt\npytest{separator}# comment"
        ),
        "tests/nested requirements.txt": "remote @ https://example.org/a.whl#sha256=abc",
    }
    declaration = {"projects": [], "requirements-files": ["tests/requirements.txt"]}
    assert dependency_input_requirements(
        declaration, files.__getitem__, root=tmp_path
    ) == ["pytest", "remote @ https://example.org/a.whl#sha256=abc"]


def test_dynamic_dependencies_are_not_silently_treated_as_empty(tmp_path: Path) -> None:
    text = '[project]\nname="project"\ndynamic=["dependencies"]\n'
    with pytest.raises(ValueError, match="dynamic"):
        dependency_input_requirements({}, lambda path: text, root=tmp_path)


def test_include_without_a_file_identifies_the_directive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requirements.txt: -r needs a file name"):
        dependency_input_requirements(
            {"requirements-files": ["requirements.txt"]},
            lambda path: "-r  # add a filename here",
            root=tmp_path,
        )


def test_relative_requirements_includes_can_use_a_parent_inside_the_source(
    tmp_path: Path,
) -> None:
    files = {"tests/requirements.txt": "-r ../shared.txt", "shared.txt": "pytest"}
    declaration = {"projects": [], "requirements-files": ["tests/requirements.txt"]}
    assert dependency_input_requirements(
        declaration, files.__getitem__, root=tmp_path
    ) == ["pytest"]


@pytest.mark.parametrize("included", ["../../outside.txt", "/outside.txt"])
def test_requirements_includes_cannot_escape_the_source(
    tmp_path: Path, included: str
) -> None:
    declaration = {"projects": [], "requirements-files": ["tests/requirements.txt"]}
    with pytest.raises(ValueError, match="unsafe source path"):
        dependency_input_requirements(
            declaration, lambda path: "-r " + included, root=tmp_path
        )


def test_cyclic_requirement_includes_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cyclic requirements"):
        dependency_input_requirements(
            {"projects": [], "requirements-files": ["requirements.txt"]},
            lambda path: "-r requirements.txt",
            root=tmp_path,
        )


def test_relative_direct_dependencies_are_not_rebound_to_the_consuming_project(
    tmp_path: Path,
) -> None:
    text = (
        "[project]\n"
        'name="remote"\n'
        'dependencies=["child @ ./child"]\n'
        "[build-system]\n"
        "requires=[]\n"
    )
    with pytest.raises(ValueError, match="cannot rebind"):
        dependency_input_requirements({}, lambda path: text, root=tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="missing-table"),
        pytest.param([], id="array-instead-of-table"),
        pytest.param({1: "not a TOML key"}, id="non-string-key"),
    ],
)
def test_input_tables_require_string_keys(value: object) -> None:
    with pytest.raises(ValueError, match="must be a table"):
        input_table(value, "input")


@pytest.mark.parametrize(
    ("projects", "message"),
    [
        pytest.param(None, "must be an array", id="missing-array"),
        pytest.param([None], "must be a table", id="non-table-project"),
        pytest.param([{"unknown": True}], "unknown.*keys", id="unknown-key"),
        pytest.param([{"path": 1}], "path must be a string", id="non-string-path"),
        pytest.param(
            [{"dependencies": "false"}],
            "must be booleans",
            id="non-boolean-dependencies",
        ),
        pytest.param(
            [{"build-requirements": 1}],
            "must be booleans",
            id="non-boolean-build-requirements",
        ),
    ],
)
def test_malformed_project_selection_is_refused(
    tmp_path: Path, projects: object, message: str
) -> None:
    with pytest.raises((ValueError, TypeError), match=message):
        dependency_input_requirements(
            {"projects": projects}, lambda path: "", root=tmp_path
        )


def test_missing_project_identity_is_refused_for_runtime_import(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="project name"):
        dependency_input_requirements(
            {}, lambda path: "[project]\ndependencies=[]", root=tmp_path
        )


def test_duplicate_project_identities_are_compared_after_normalization(
    tmp_path: Path,
) -> None:
    text = '[project]\nname="demo"\ndependencies=[]\n[build-system]\nrequires=[]\n'
    files = {
        "pyproject.toml": text.replace('name="demo"', 'name="Demo.Core"'),
        "backend/pyproject.toml": text.replace('name="demo"', 'name="demo-core"'),
    }
    with pytest.raises(ValueError, match="duplicate imported project"):
        dependency_input_requirements(
            {"projects": [{"path": "."}, {"path": "backend"}]},
            files.__getitem__,
            root=tmp_path,
        )


def test_selected_extras_can_be_imported_without_runtime_or_build_requirements(
    tmp_path: Path,
) -> None:
    text = (
        "[project]\n"
        'name="demo"\n'
        'dependencies=["runtime"]\n'
        "[project.optional-dependencies]\n"
        'cli=["rich"]\n'
    )
    declaration = {
        "projects": [
            {"extras": ["cli"], "dependencies": False, "build-requirements": False}
        ]
    }
    assert dependency_input_requirements(
        declaration, lambda path: text, root=tmp_path
    ) == ["rich"]


def test_foreign_configuration_and_backend_names_do_not_run(tmp_path: Path) -> None:
    text = (
        "[project]\n"
        'name="demo"\n'
        'dependencies=["needed"]\n'
        "[build-system]\n"
        "requires=[]\n"
        'build-backend="unavailable_backend"\n'
        "[tool.nab]\n"
        'mode="invalid-foreign-policy"\n'
    )
    assert dependency_input_requirements({}, lambda path: text, root=tmp_path) == [
        "needed"
    ]


def test_requirements_files_cannot_change_the_callers_index_policy(
    tmp_path: Path,
) -> None:
    declaration = {"projects": [], "requirements-files": ["requirements.txt"]}
    with pytest.raises(InvalidProjectRequirementError):
        dependency_input_requirements(
            declaration,
            lambda path: "--index-url https://example.org/simple",
            root=tmp_path,
        )


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        pytest.param({}, ["runtime"], id="runtime-default"),
        pytest.param({"groups": ["docs"]}, ["sphinx"], id="group-only"),
        pytest.param({"extras": ["web"]}, ["httpx"], id="extra-only"),
        pytest.param({"build-requirements": True}, ["backend"], id="build-only"),
        pytest.param(
            {
                "dependencies": True,
                "extras": ["web"],
                "groups": ["docs"],
                "build-requirements": True,
            },
            ["backend", "httpx", "runtime", "sphinx"],
            id="combined-selections",
        ),
    ],
)
def test_dependency_categories_are_selected_independently(
    tmp_path: Path,
    selection: dict[str, object],
    expected: list[str],
) -> None:
    text = (
        '[project]\nname="service"\ndependencies=["runtime"]\n'
        '[project.optional-dependencies]\nweb=["httpx"]\n'
        '[build-system]\nrequires=["backend"]\n'
        '[dependency-groups]\ndocs=["sphinx"]\n'
    )
    assert (
        dependency_input_requirements(
            {"projects": [selection]}, lambda path: text, root=tmp_path
        )
        == expected
    )


def test_group_import_does_not_require_dynamic_runtime_or_build_metadata(
    tmp_path: Path,
) -> None:
    text = (
        '[project]\nname="service"\ndynamic=["dependencies"]\n'
        '[build-system]\nrequires="invalid unselected build metadata"\n'
        '[dependency-groups]\ndocs=["sphinx"]\n'
    )
    assert dependency_input_requirements(
        {"projects": [{"groups": ["docs"]}]},
        lambda path: text,
        root=tmp_path,
    ) == ["sphinx"]


def test_requirements_file_selection_needs_no_project_metadata(tmp_path: Path) -> None:
    files = {
        "requirements.txt": "httpx\n-r config/extra.txt",
        "config/extra.txt": "rich",
    }
    assert dependency_input_requirements(
        {"requirements-files": ["requirements.txt"]},
        files.__getitem__,
        root=tmp_path,
    ) == ["httpx", "rich"]
