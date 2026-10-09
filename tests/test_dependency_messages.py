"""Debug output states active declarations without changing command data."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from nab._resolve import _report_dependency_requirements
from nab.cli import main
from nab.output import Printer, Verbosity
from nab_project.declarations import DependencyDeclaration
from nab_project.lockfile import TargetLock
from nab_project.resolve import ResolveResult, TargetResult
from nab_provider._vendor.packaging.version import Version
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_resolver.errors import ResolutionError

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture


def _project(directory: Path) -> Path:
    """Create metadata archives and a project for the real local-index resolve."""
    for name, version, requirements in (
        ("parent", "1.0", ("child~=2.0", 'child<2.5 ; extra == "feature"')),
        ("child", "2.4", ()),
        ("child", "2.8", ()),
    ):
        fields = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
        if name == "parent":
            fields.append("Provides-Extra: feature")
        fields.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
        with ZipFile(directory / f"{name}-{version}-py3-none-any.whl", "w") as archive:
            archive.writestr(
                f"{name}-{version}.dist-info/METADATA", "\n".join(fields) + "\n\n"
            )
    path = directory / "pyproject.toml"
    path.write_text(
        '[project]\nname="root"\nversion="0"\ndependencies=["parent[feature]"]\n'
        '\n[[tool.nab.indexes]]\nname="local"\nurl='
        + json.dumps(directory.as_uri())
        + "\n"
    )
    return path


@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"], ["-q"], ["-qq"]])
def test_only_debug_output_shows_active_requirements(
    tmp_path: Path, capsys: CaptureFixture[str], verbosity: list[str]
) -> None:
    path = _project(tmp_path)
    result = main(
        [
            "--color",
            "never",
            *verbosity,
            "lock",
            str(path),
            "--no-cache",
            "--format",
            "requirements-without-hashes",
            "--output",
            "-",
        ]
    )
    assert result in (None, 0)
    output = capsys.readouterr()
    assert "child==2.4" in output.out
    assert "parent==1.0" in output.out
    assert " requires " not in output.out

    declarations = [line for line in output.err.splitlines() if " requires " in line]
    if verbosity == ["-vv"]:
        assert len(declarations) == 2
        assert declarations[0].endswith("parent==1.0 requires child~=2.0")
        assert declarations[1].endswith(
            'parent==1.0 requires child<2.5; extra == "feature" (via parent[feature])'
        )
    else:
        assert declarations == []


def test_incomplete_and_uncollected_results_have_no_declaration_messages() -> None:
    target = ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    )
    result = ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target, success=False, error=ResolutionError("no matches")
            ),
            TargetResult(
                target=target,
                success=True,
                lock=TargetLock(target=target, pins={}),
            ),
        ],
    )
    stderr = io.StringIO()
    writer = Printer(verbosity=Verbosity.DEBUG, stderr=stderr)
    with patch("nab._resolve.printer", return_value=writer):
        _report_dependency_requirements(result)
    assert stderr.getvalue() == ""


def test_messages_keep_base_requirements_separate_from_extra_sources() -> None:
    target = ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    )
    records = (
        DependencyDeclaration(
            "child~=2.0", "~=2.0", parent_name="parent", dependency_name="child"
        ),
        DependencyDeclaration(
            'child[other]<2.5; extra == "feature" or extra == "other"',
            "<2.5",
            parent_name="parent",
            dependency_name="child",
            dependency_extras=("other",),
            required_for_parent_extras=("feature", "other"),
        ),
    )
    result = ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target,
                success=True,
                pins={"parent": Version("1.0")},
                lock=TargetLock(
                    target=target,
                    pins={},
                    dependency_requirements={"parent": {"child": records}},
                ),
            )
        ],
    )
    stderr = io.StringIO()
    writer = Printer(verbosity=Verbosity.DEBUG, stderr=stderr)
    with patch("nab._resolve.printer", return_value=writer):
        _report_dependency_requirements(result)
    lines = stderr.getvalue().splitlines()
    assert lines[0].endswith("parent==1.0 requires child~=2.0")
    assert lines[1].endswith("(via parent[feature], parent[other])")
    assert "child[other]<2.5" in lines[1]
