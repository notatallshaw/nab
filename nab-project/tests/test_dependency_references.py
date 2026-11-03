"""Informational dependency references must identify existing package rows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nab_project import toml_io
from nab_project.lockfile import (
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    LockValidationError,
    TargetLock,
    VcsPin,
    WheelArtifact,
    build_pylock,
    render_lock,
)
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget


def _index(
    name: str, version: str = "1", index: str = "https://example.com/simple/"
) -> IndexPin:
    return IndexPin(
        name=name,
        version=version,
        index=index,
        wheels=(
            WheelArtifact(
                filename=f"{name}-{version}-py3-none-any.whl",
                url=f"{index}{name}-{version}-py3-none-any.whl",
                hashes=(("sha256", "a" * 64),),
            ),
        ),
    )


def _document(lock: LockInput, path: Path, *, rendered: bool) -> dict[str, Any]:
    """Exercise both native models and the document emission path."""
    if rendered:
        return toml_io.loads(render_lock(lock, lock_dir=path))
    return dict(build_pylock(lock, lock_dir=path).to_dict())


@pytest.mark.parametrize("rendered", [False, True])
@pytest.mark.parametrize("kind", ["version", "index", "directory", "git", "archive"])
def test_references_identify_each_emitted_child(
    tmp_path: Path, rendered: bool, kind: str
) -> None:
    targets = [
        ResolveTarget.for_declared(
            python_version=python, spec=PlatformSpec("linux_x86_64")
        )
        for python in ("3.10", "3.11")
    ]
    locks = {}
    for ordinal, target in enumerate(targets, start=1):
        child: IndexPin | LocalPin | VcsPin | ArchivePin
        if kind == "version":
            child = _index("child", str(ordinal))
        elif kind == "index":
            child = _index("child", index=f"https://example.com/{ordinal}/simple/")
        elif kind == "directory":
            child = LocalPin("child", "1", str(tmp_path / str(ordinal)))
        elif kind == "git":
            commit = str(ordinal) * 40
            repository = "https://example.com/child.git"
            child = VcsPin(
                "child", "1", f"git+{repository}@{commit}", repository, commit
            )
        else:
            child = ArchivePin(
                "child",
                str(ordinal),
                f"https://example.com/child-{ordinal}.tar.gz",
                (("sha256", "b" * 64),),
            )
        locks[target.label] = TargetLock(
            target=target,
            pins={"parent": _index("parent"), "child": child},
            dependencies={"parent": frozenset({"child"})},
        )
    document = _document(LockInput(targets=locks), tmp_path, rendered=rendered)
    parent = next(
        package for package in document["packages"] if package["name"] == "parent"
    )
    assert len(parent["dependencies"]) == 2
    for reference in parent["dependencies"]:
        matches = [
            package
            for package in document["packages"]
            if all(package.get(key) == value for key, value in reference.items())
        ]
        assert len(matches) == 1
        assert reference["name"] == "child"
        if kind in {"version", "archive"}:
            assert set(reference) == {"name", "version"}
        elif kind == "index":
            assert set(reference) == {"name", "version", "marker"}
        else:
            assert set(reference) == {"name", "marker"}
    assert all(
        "dependencies" not in package
        for package in document["packages"]
        if package["name"] == "child"
    )


@pytest.mark.parametrize("rendered", [False, True])
def test_parent_references_only_children_it_reached(
    tmp_path: Path, rendered: bool
) -> None:
    first = ResolveTarget.for_declared(
        python_version="3.10", spec=PlatformSpec("linux_x86_64")
    )
    second = ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    )
    lock = LockInput(
        targets={
            first.label: TargetLock(
                target=first,
                pins={
                    "parent": _index("parent"),
                    "child": _index("child", "1"),
                    "unique": _index("unique"),
                },
                dependencies={"parent": frozenset({"child", "unique"})},
            ),
            second.label: TargetLock(
                target=second, pins={"child": _index("child", "2")}
            ),
        }
    )
    document = _document(lock, tmp_path, rendered=rendered)
    parent = next(
        package for package in document["packages"] if package["name"] == "parent"
    )
    assert parent["dependencies"] == [
        {"name": "child", "version": "1"},
        {"name": "unique"},
    ]


def test_unresolved_dependency_reference_is_refused(tmp_path: Path) -> None:
    target = ResolveTarget.for_host()
    lock = LockInput(
        targets={
            target.label: TargetLock(
                target=target,
                pins={"parent": _index("parent")},
                dependencies={"parent": frozenset({"missing"})},
            )
        }
    )
    with pytest.raises(LockValidationError, match="depends on missing package"):
        render_lock(lock, lock_dir=tmp_path)
