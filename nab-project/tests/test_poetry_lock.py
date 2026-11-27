"""Poetry lock inputs, conditional dependencies and installer source fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import tomli

from nab_project.lockfile import (
    BASE_MEMBER,
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    LockValidationError,
    SdistArtifact,
    TargetLock,
    VcsPin,
    WheelArtifact,
)
from nab_project.native_lock import write_poetry_lock
from nab_provider.target import ResolveTarget

if TYPE_CHECKING:
    from pathlib import Path

_DIGEST = (("sha256", "ab" * 32),)


def test_poetry_qualified_sources_and_optional_selections(tmp_path: Path) -> None:
    archive = ArchivePin(
        "archive", "1", "https://example.org/archive.zip", _DIGEST, subdirectory="lib"
    )
    git = VcsPin(
        "gitdep",
        "1",
        "git+https://example.org/repo",
        "https://example.org/repo",
        "b" * 40,
        "pkg",
        "stable",
    )
    directory = LocalPin(
        "app", "1", str(tmp_path / "src"), editable=True, subdirectory="app"
    )
    wheel = WheelArtifact(
        "dep-1-py3-none-any.whl", "https://example.org/dep-1-py3-none-any.whl", _DIGEST
    )
    dep = IndexPin(
        "dep",
        "1",
        "https://example.org/simple",
        wheels=(wheel,),
        requires_python=">=3.11",
    )

    target = ResolveTarget.for_host()
    pins = {p.name: p for p in (archive, git, directory, dep)}

    selectors = {
        "dep": (BASE_MEMBER, ("extra", "fast")),
        "archive": (("extra", "fast"),),
        "gitdep": (("group", "dev"),),
        "app": (BASE_MEMBER,),
    }
    lock = TargetLock(target, pins, {"app": ("dep",)}, package_gates=selectors)
    inputs = LockInput(
        targets={target.label: lock}, extras=("fast",), dependency_groups=("dev",)
    )

    project = {
        "project": {"name": "app", "optional-dependencies": {"fast": ["archive"]}},
        "dependency-groups": {"dev": ["gitdep"]},
        "tool": {
            "poetry": {
                "source": [{"name": "private", "url": "https://example.org/simple/"}]
            }
        },
    }

    output = tmp_path / "poetry.lock"
    write_poetry_lock(inputs, project, project_dir=tmp_path, output_path=output)
    document = tomli.loads(output.read_text())
    packages = {p["name"]: p for p in document["package"]}

    assert packages["dep"]["source"] == {
        "type": "legacy",
        "reference": "private",
        "url": "https://example.org/simple",
    }
    assert packages["dep"]["python-versions"] == ">=3.11"
    assert not packages["dep"]["optional"]

    assert packages["archive"]["optional"]
    assert packages["archive"]["markers"]["main"] == 'extra == "fast"'
    assert packages["archive"]["source"]["subdirectory"] == "lib"
    assert document["extras"] == {"fast": ["archive"]}

    assert packages["gitdep"]["source"]["resolved_reference"] == "b" * 40
    assert packages["gitdep"]["source"]["subdirectory"] == "pkg"
    assert packages["gitdep"]["groups"] == ["dev"]

    assert packages["app"]["source"] == {
        "type": "directory",
        "url": "src",
        "subdirectory": "app",
    }
    assert packages["app"]["develop"]
    assert packages["app"]["dependencies"] == {"dep": "*"}


def test_poetry_registry_and_direct_files(tmp_path: Path) -> None:
    wheel = WheelArtifact(
        "demo-1-py3-none-any.whl",
        "https://files.example.org/demo-1-py3-none-any.whl",
        _DIGEST,
    )
    target = ResolveTarget.for_host()

    def lock(pin: IndexPin) -> dict:
        """Render one release against the project's configured package sources."""
        inputs = LockInput(targets={target.label: TargetLock(target, {pin.name: pin})})
        return tomli.loads(write_poetry_lock(inputs, {}, project_dir=tmp_path))[
            "package"
        ][0]

    assert "source" not in lock(
        IndexPin("demo", "1", "https://pypi.org/simple/", wheels=(wheel,))
    )
    assert lock(IndexPin("demo", "1", "https://example.org/simple", wheels=(wheel,)))[
        "source"
    ] == {"type": "url", "url": wheel.url}
    local = WheelArtifact(
        wheel.filename, wheel.url, _DIGEST, local_path=tmp_path / wheel.filename
    )
    assert lock(IndexPin("demo", "1", "local", wheels=(local,)))["source"] == {
        "type": "file",
        "url": wheel.filename,
    }
    sdist = SdistArtifact("demo-1.tar.gz", "https://example.org/demo-1.tar.gz", _DIGEST)
    with pytest.raises(LockValidationError, match="tool.poetry.source"):
        lock(
            IndexPin(
                "demo", "1", "https://example.org/simple", sdist=sdist, wheels=(wheel,)
            )
        )


@pytest.mark.parametrize(
    ("kind", "url", "problem"),
    [
        ("hg", "https://example.org/repo", "from hg"),
        ("git", "", "Git repository URL"),
    ],
)
def test_invalid_poetry_vcs_sources(
    kind: str, url: str, problem: str, tmp_path: Path
) -> None:
    target = ResolveTarget.for_host()
    vcs = VcsPin("dep", "1", f"{kind}+{url}", url, "b" * 40, None, vcs_type=kind)
    inputs = LockInput(targets={target.label: TargetLock(target, {"dep": vcs})})
    with pytest.raises(LockValidationError, match=problem):
        write_poetry_lock(inputs, {}, project_dir=tmp_path)


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        (
            {"project": {"dependencies": []}},
            "1c4b7d03605c20e6f96a02bca2daeba973554885fab37a188c1e5effbd8e352c",
        ),
        (
            {"dependency-groups": {"docs": ["sphinx"]}},
            "cb9a70d40b5d5ab249584b17e5e6d5294e62ed991aa78ceb437050ad8e684476",
        ),
        (
            {"tool": {"poetry": {"dependencies": {"python": "^3.13"}}}},
            "f01b553f3895e558c34b4f10542e05acdef39bf0527c8090bd136d914dc73f94",
        ),
    ],
)
def test_freshness_matches_poetry(project: dict, expected: str, tmp_path: Path) -> None:
    # Poetry 2.5.2 uses a different hash input when only legacy declarations exist.
    target = ResolveTarget.for_host()
    pin = LocalPin("demo", "1", str(tmp_path / "demo"))
    value = LockInput(targets={target.label: TargetLock(target, {"demo": pin})})
    doc = tomli.loads(write_poetry_lock(value, project, project_dir=tmp_path))
    assert doc["metadata"]["content-hash"] == expected
    assert doc["package"][0]["source"] == {"type": "directory", "url": "demo"}
