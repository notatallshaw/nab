"""Informational dependency references must identify existing package rows."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nab_project import toml_io
from nab_project.lockfile import (
    ArchivePin,
    IndexPin,
    InvalidLockfileError,
    LocalPin,
    LockInput,
    LockValidationError,
    TargetLock,
    VcsPin,
    WheelArtifact,
    build_pylock,
    render_lock,
    resolve_dependency_references,
)
from nab_provider._vendor.packaging.pylock import Pylock
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
            assert set(reference) == {"name", "marker"}
        else:
            assert set(reference) == {"name", "marker"}
    resolved = resolve_dependency_references(Pylock.from_dict(document))
    assert sum(bool(children) for children in resolved.values()) == 1
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


def _one_parent_lock(tmp_path: Path) -> Pylock:
    """Build a schema-valid parent/child pair for reference validation."""
    target = ResolveTarget.for_host()
    return build_pylock(
        LockInput(
            targets={
                target.label: TargetLock(
                    target=target,
                    pins={"parent": _index("parent"), "child": _index("child")},
                    dependencies={"parent": frozenset({"child"})},
                )
            }
        ),
        lock_dir=tmp_path,
    )


@pytest.mark.parametrize("field", ["version", "index", "wheels"])
def test_reference_validation_accepts_standard_fields(
    tmp_path: Path, field: str
) -> None:
    lock = _one_parent_lock(tmp_path)
    child_index = next(
        index for index, pkg in enumerate(lock.packages) if pkg.name == "child"
    )
    parent_index = next(
        index for index, pkg in enumerate(lock.packages) if pkg.name == "parent"
    )
    child = lock.to_dict()["packages"][child_index]
    reference = (
        {field: child[field]}
        if field == "wheels"
        else {"name": "child", field: child[field]}
    )
    packages = list(lock.packages)
    packages[parent_index] = replace(packages[parent_index], dependencies=(reference,))
    qualified = replace(lock, packages=packages)
    qualified.validate()
    assert resolve_dependency_references(qualified) == {
        child_index: frozenset(),
        parent_index: frozenset({child_index}),
    }


@pytest.mark.parametrize(
    "reference",
    [
        {"name": "child", "version": "999"},
        {"name": "child", "unknown": True},
        {"name": ["child"]},
        {"name": "missing"},
        {"name": "child", "wheels": []},
        {"name": "child", "wheels": {}},
    ],
)
def test_reference_validation_rejects_missing_identities(
    tmp_path: Path, reference: dict[str, object]
) -> None:
    lock = _one_parent_lock(tmp_path)
    packages = [
        replace(pkg, dependencies=(reference,)) if pkg.name == "parent" else pkg
        for pkg in lock.packages
    ]
    invalid = replace(lock, packages=packages)
    invalid.validate()
    with pytest.raises(InvalidLockfileError, match="identify exactly one package"):
        resolve_dependency_references(invalid)


def test_reference_validation_rejects_ambiguous_identities(tmp_path: Path) -> None:
    locks = {}
    for python, version in [("3.10", "1"), ("3.11", "2")]:
        target = ResolveTarget.for_declared(
            python_version=python, spec=PlatformSpec("linux_x86_64")
        )
        locks[target.label] = TargetLock(
            target=target,
            pins={"parent": _index("parent"), "child": _index("child", version)},
            dependencies={"parent": frozenset({"child"})},
        )
    lock = build_pylock(LockInput(targets=locks), lock_dir=tmp_path)
    packages = [
        replace(pkg, dependencies=({"name": "child"},)) if pkg.name == "parent" else pkg
        for pkg in lock.packages
    ]
    invalid = replace(lock, packages=packages)
    invalid.validate()
    with pytest.raises(InvalidLockfileError, match="identify exactly one package"):
        resolve_dependency_references(invalid)


@pytest.mark.parametrize("kind", ["directory", "git", "archive"])
def test_reference_validation_accepts_partial_source_tables(
    tmp_path: Path, kind: str
) -> None:
    if kind == "directory":
        child = LocalPin("child", "1", str(tmp_path / "child"), editable=True)
        field = "path"
    elif kind == "git":
        repository = "https://example.com/child.git"
        child = VcsPin(
            "child", "1", f"git+{repository}@" + "a" * 40, repository, "a" * 40
        )
        field = "url"
    else:
        child = ArchivePin(
            "child", "1", "https://example.com/child.tar.gz", (("sha256", "a" * 64),)
        )
        field = "url"
    target = ResolveTarget.for_host()
    lock = build_pylock(
        LockInput(
            targets={
                target.label: TargetLock(
                    target=target,
                    pins={"parent": _index("parent"), "child": child},
                    dependencies={"parent": frozenset({"child"})},
                )
            }
        ),
        lock_dir=tmp_path,
    )
    document = dict(lock.to_dict())
    parent = next(pkg for pkg in document["packages"] if pkg["name"] == "parent")
    source_key = "vcs" if kind == "git" else kind
    source = next(pkg for pkg in document["packages"] if pkg["name"] == "child")[
        source_key
    ]
    parent["dependencies"] = [{source_key: {field: source[field]}}]
    qualified = Pylock.from_dict(document)
    resolved = resolve_dependency_references(qualified)
    assert sum(len(children) for children in resolved.values()) == 1
    if kind == "directory":
        parent["dependencies"] = [{source_key: {"path": source["path"], "editable": 1}}]
        with pytest.raises(InvalidLockfileError, match="identify exactly one package"):
            resolve_dependency_references(Pylock.from_dict(document))
