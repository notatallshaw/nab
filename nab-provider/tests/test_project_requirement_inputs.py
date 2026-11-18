"""Dependency-only imports retain local extra selections and their conditions."""

import pytest

from nab_provider.pep508 import parse_requirement
from nab_provider.requirements_file import (
    expand_extra_requirements,
    expand_project_requirements,
)


def test_cross_project_extras_keep_marker_gates_and_terminate_cycles() -> None:
    roots = [parse_requirement('frontend[test]; python_version >= "3.13"')]
    projects = {
        "frontend": {"test": ['backend[tests]; sys_platform == "linux"']},
        "backend": {"tests": ["frontend[test]", "pytest", 'wrong; extra == "other"']},
    }
    result = expand_project_requirements(roots, projects)
    assert len(result) == 1
    (requirement,) = result
    assert requirement.name == "pytest"
    assert requirement.marker is not None
    for python, platform, expected in [
        ("3.14", "linux", True),
        ("3.12", "linux", False),
        ("3.14", "win32", False),
    ]:
        assert (
            requirement.marker.evaluate(
                {"python_version": python, "sys_platform": platform}
            )
            is expected
        )
    assert str(roots[0]) == 'frontend[test]; python_version >= "3.13"'


def test_local_runtime_reference_is_omitted_but_external_requirements_remain() -> None:
    result = expand_project_requirements(
        [parse_requirement("local>=1"), parse_requirement("remote[cli]>=2")],
        {"local": {}},
    )
    assert [str(requirement) for requirement in result] == ["remote[cli]>=2"]


def test_unknown_extra_is_refused() -> None:
    with pytest.raises(LookupError, match="has no extra"):
        expand_project_requirements(
            [parse_requirement("local[missing]")], {"local": {}}
        )


def test_project_and_extra_aliases_use_normalized_names() -> None:
    result = expand_project_requirements(
        [parse_requirement("My.Project[Py_Test,py-test]")],
        {"my_project": {"py.test": ["pytest"]}},
    )
    assert [str(requirement) for requirement in result] == ["pytest"]


def test_leaf_extra_markers_are_evaluated_in_the_selected_extra_context() -> None:
    result = expand_project_requirements(
        [parse_requirement("local[test]")],
        {"local": {"test": ["leaf; extra == 'test'"]}},
    )
    assert [str(requirement) for requirement in result] == ["leaf"]


def test_direct_url_cannot_silently_select_a_different_local_source() -> None:
    with pytest.raises(ValueError, match="direct URL"):
        expand_project_requirements(
            [parse_requirement("local @ https://example.org/local.whl")], {"local": {}}
        )


@pytest.mark.parametrize("name", ["app", None])
def test_bound_extras_reduce_leaf_markers_without_losing_environment_conditions(
    name: str | None,
) -> None:
    optional = {
        "test": [
            "pytest; extra == 'test'",
            "wrong; extra != 'test'",
            "linux-tool; extra == 'test' and sys_platform == 'linux'",
        ]
    }
    result = expand_extra_requirements(
        optional, name, ["test"], bind_extra_markers=True
    )
    assert {requirement.name for requirement in result} == {"pytest", "linux-tool"}
    for platform in ["linux", "win32"]:
        selected = {
            r.name
            for r in result
            if r.marker is None or r.marker.evaluate({"sys_platform": platform})
        }
        assert selected == (
            {"pytest", "linux-tool"} if platform == "linux" else {"pytest"}
        )
    assert optional["test"][0] == "pytest; extra == 'test'"


def test_bound_extras_follow_conditional_self_references_and_cycles() -> None:
    result = expand_extra_requirements(
        {
            "all": ["app[test]; extra == 'all' and sys_platform == 'linux'"],
            "test": ["app[all]", "pytest; extra == 'test'"],
        },
        "app",
        ["all"],
        bind_extra_markers=True,
    )
    assert len(result) == 1
    requirement = result[0]
    assert requirement.name == "pytest"
    assert requirement.marker is not None
    assert requirement.marker.evaluate({"sys_platform": "linux"})
    assert not requirement.marker.evaluate({"sys_platform": "win32"})
