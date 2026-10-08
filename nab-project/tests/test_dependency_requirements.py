"""Successful resolves expose only active declarations for their final edges."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import pytest

from nab_index.multi_index import IndexConfig
from nab_index.transport import HttpResponse
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import LockInput, drop_workspace_pins
from nab_project.resolve import resolve_for_targets
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import LocalSource
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import pkg_override

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_project.resolve import TargetResult

DECLARATIONS = (
    'Child_Name ( <2 ) ; python_version < "3.10"',
    'child-name ( ~=2.0 ) ; python_version >= "3.10"',
    'child-name<2.5 ; extra == "feature" or extra == "other"',
    'parent[feature] ; extra == "all"',
)


class _LocalTransport:
    """Refuse HTTP so fixtures exercise only the real local-index path."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        message = f"unexpected HTTP request: {url} {headers}"
        raise AssertionError(message)

    async def aclose(self) -> None:
        pass


def _write_wheel(
    directory: Path, name: str, version: str, declarations: Sequence[str]
) -> None:
    """Write a wheel archive carrying the fixture's dependency metadata."""
    package = directory
    package.mkdir(parents=True, exist_ok=True)
    wheel_name = name.replace("-", "_")
    metadata = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if name == "parent":
        metadata.extend(
            f"Provides-Extra: {extra}" for extra in ("feature", "other", "all")
        )
    metadata.extend(f"Requires-Dist: {declaration}" for declaration in declarations)
    with ZipFile(package / f"{wheel_name}-{version}-py3-none-any.whl", "w") as archive:
        archive.writestr(
            f"{wheel_name}-{version}.dist-info/METADATA", "\n".join(metadata) + "\n\n"
        )


def _resolve(
    directory: Path,
    requirements: Sequence[str] = ("parent",),
    declarations: Sequence[str] = DECLARATIONS,
    *,
    enabled: bool = True,
    python: str = "3.11",
    inputs: ResolveInputs | None = None,
) -> TargetResult:
    """Resolve fixture wheels through the project's public entry point."""
    _write_wheel(directory, "parent", "1.0", declarations)
    for version in ("1.9", "2.4", "2.8", "3.0"):
        _write_wheel(directory, "child-name", version, ())
    project = directory / "pyproject.toml"
    project.write_text(
        '[project]\nname="root"\nversion="0"\ndependencies='
        + json.dumps(list(requirements))
        + "\n"
    )
    inputs = (ResolveInputs() if inputs is None else inputs).replace(
        indexes=(IndexConfig("local", directory.as_uri()),)
    )
    target = ResolveTarget.for_declared(
        python_version=python, spec=PlatformSpec("linux_x86_64")
    )
    result = resolve_for_targets(
        project,
        _LocalTransport(),
        targets=[target],
        inputs=inputs,
        include_dependency_requirements=enabled,
    )
    return result.target_results[0]


@pytest.mark.parametrize(
    ("root", "python", "version", "count"),
    [
        ("parent", "3.11", "2.8", 1),
        ("parent[feature]", "3.11", "2.4", 2),
        ("parent[all]", "3.11", "2.4", 2),
        ("parent[feature,other]", "3.11", "2.4", 2),
        ("parent", "3.9", "1.9", 1),
    ],
)
def test_only_active_declarations(
    tmp_path: Path, root: str, python: str, version: str, count: int
) -> None:
    selected = _resolve(tmp_path, (root,), python=python)
    assert selected.success
    lock = selected.lock
    assert lock is not None
    assert lock.pins["child-name"].version == version
    assert lock.dependencies == {"parent": ("child-name",)}
    declarations = lock.dependency_requirements
    assert declarations is not None
    assert set(declarations) == {"parent"}
    assert set(declarations["parent"]) == {"child-name"}
    values = declarations["parent"]["child-name"]
    assert len(values) == count
    assert all(parse_requirement(value).specifier.contains(version) for value in values)
    if python == "3.11":
        assert any("~=" in value for value in values)


def test_collection_is_optional(tmp_path: Path) -> None:
    omitted = _resolve(tmp_path, enabled=False)
    selected = _resolve(tmp_path)
    assert omitted.success
    assert selected.success
    assert omitted.lock is not None
    assert omitted.lock.dependency_requirements is None
    assert omitted.pins == selected.pins
    assert omitted.consulted == selected.consulted
    assert (omitted.decisions, omitted.conflicts, omitted.rounds) == (
        selected.decisions,
        selected.conflicts,
        selected.rounds,
    )


def test_leaf_and_empty_resolves_have_collected_empty_maps(tmp_path: Path) -> None:
    for requirements in (("parent",), ()):
        selected = _resolve(tmp_path, requirements, declarations=())
        assert selected.success
        assert selected.lock is not None
        assert selected.lock.dependency_requirements == {}


def test_repeated_declarations_remain_separate(tmp_path: Path) -> None:
    selected = _resolve(tmp_path, declarations=("child-name>=2", "child-name>=2"))
    assert selected.success
    assert selected.lock is not None
    declarations = selected.lock.dependency_requirements
    assert declarations is not None
    expected = ("child-name>=2", "child-name>=2")
    assert declarations == {"parent": {"child-name": expected}}


def test_inactive_url_is_excluded(tmp_path: Path) -> None:
    selected = _resolve(
        tmp_path,
        declarations=(
            "child-name",
            'remote @ https://example.test/remote.whl ; python_version < "3"',
        ),
    )
    assert selected.success
    assert selected.lock is not None
    assert selected.lock.dependency_requirements == {
        "parent": {"child-name": ("child-name",)}
    }


def test_constraints_are_separate_from_parent_declarations(tmp_path: Path) -> None:
    selected = _resolve(tmp_path, inputs=ResolveInputs(constraints=("child-name<2.5",)))
    assert selected.success
    assert selected.lock is not None
    assert selected.lock.pins["child-name"].version == "2.4"
    declarations = selected.lock.dependency_requirements
    assert declarations is not None
    assert all("<2.5" not in value for value in declarations["parent"]["child-name"])


def test_metadata_override_supplies_effective_declarations(tmp_path: Path) -> None:
    inputs = ResolveInputs(
        package_overrides=(
            pkg_override("parent", dependencies=(parse_requirement("child-name>=3"),)),
        )
    )
    selected = _resolve(tmp_path, inputs=inputs)
    assert selected.success
    assert selected.lock is not None
    assert selected.lock.dependency_requirements == {
        "parent": {"child-name": ("child-name>=3",)}
    }


@pytest.mark.parametrize("excluded", ["parent", "child-name", "unrelated"])
def test_dropped_workspace_edges_lose_their_declarations(
    tmp_path: Path, excluded: str
) -> None:
    selected = _resolve(tmp_path)
    assert selected.success
    assert selected.lock is not None
    lock = selected.lock
    filtered = drop_workspace_pins(
        LockInput(targets={lock.target.label: lock}), frozenset({excluded})
    )
    expected_edges = lock.dependencies if excluded == "unrelated" else {}
    expected_declarations = (
        lock.dependency_requirements if excluded == "unrelated" else {}
    )
    assert filtered.targets[lock.target.label].dependencies == expected_edges
    assert (
        filtered.targets[lock.target.label].dependency_requirements
        == expected_declarations
    )
    uncollected = replace(lock, dependency_requirements=None)
    filtered = drop_workspace_pins(
        LockInput(targets={lock.target.label: uncollected}), frozenset({excluded})
    )
    assert filtered.targets[lock.target.label].dependency_requirements is None


def test_failed_resolve_does_not_collect_declarations(tmp_path: Path) -> None:
    selected = _resolve(tmp_path, ("parent", "child-name<1"))
    assert not selected.success
    assert selected.lock is None


def test_selected_extra_with_an_excluded_environment_marker(tmp_path: Path) -> None:
    declarations = (
        "child-name~=2.0",
        'child-name<2.5 ; extra == "feature" and python_version < "3.10"',
    )
    selected = _resolve(tmp_path, ("parent[feature]",), declarations=declarations)
    assert selected.success
    assert selected.lock is not None
    data = selected.lock.dependency_requirements
    assert data is not None
    assert selected.lock.pins["child-name"].version == "2.8"
    assert len(data["parent"]["child-name"]) == 1


def test_matrix_keeps_each_targets_declarations(tmp_path: Path) -> None:
    _resolve(tmp_path)
    targets = [
        ResolveTarget.for_declared(
            python_version=python, spec=PlatformSpec("linux_x86_64")
        )
        for python in ("3.9", "3.11")
    ]
    result = resolve_for_targets(
        tmp_path / "pyproject.toml",
        _LocalTransport(),
        targets=targets,
        inputs=ResolveInputs(indexes=(IndexConfig("local", tmp_path.as_uri()),)),
        include_dependency_requirements=True,
    )
    assert result.success
    for selected, version in zip(result.target_results, ("1.9", "2.8"), strict=True):
        assert selected.lock is not None
        assert selected.lock.pins["child-name"].version == version
        data = selected.lock.dependency_requirements
        assert data is not None
        values = data["parent"]["child-name"]
        assert len(values) == 1
        assert parse_requirement(values[0]).specifier.contains(version)


def test_rejected_parent_keeps_only_the_selected_declarations(
    tmp_path: Path,
) -> None:
    _write_wheel(tmp_path, "parent", "2.0", ("child-name<2",))
    selected = _resolve(
        tmp_path, ("parent", "child-name>=2"), declarations=("child-name~=2.0",)
    )
    assert selected.success
    assert selected.lock is not None
    assert selected.lock.pins["parent"].version == "1.0"
    assert selected.metadata_fetched > len(selected.pins)
    data = selected.lock.dependency_requirements
    assert data is not None
    values = data["parent"]["child-name"]
    assert len(values) == 1
    assert "<2" not in values[0]


def test_local_source_keeps_its_effective_declarations(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="parent"\nversion="1.0"\ndependencies=["child-name>=2"]\n'
    )
    inputs = ResolveInputs(local_sources=(LocalSource("parent", str(source)),))
    selected = _resolve(tmp_path, inputs=inputs)
    assert selected.success
    assert selected.lock is not None
    assert selected.lock.dependency_requirements == {
        "parent": {"child-name": ("child-name>=2",)}
    }
