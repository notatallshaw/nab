"""Library imports defer work without changing the interpreter's import policy."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("module", "dependency", "operation"),
    [
        (
            "nab_resolver.decide",
            "nab_resolver.incompat_index",
            "assert callable(module.add_incompatibility)",
        ),
        (
            "nab_markersets.python_versions",
            "nab_markersets._packaging",
            'module.validate_python_version("3.12")',
        ),
        (
            "nab_provider.extra_keys",
            "nab_provider._vendor.packaging.utils",
            'assert module.join_extra("pkg", "SOME_Extra") == "pkg[some-extra]"',
        ),
        ("nab_index._json_decode", "json", 'assert module.decode_json(b"[]") == []'),
        (
            "nab_project.pyproject_files",
            "nab_provider.requirements_file",
            'assert module.project_dependencies({"project": {"dependencies": ["foo==1"]}})[0].name == "foo"',
        ),
        (
            "nab._lock",
            "nab_project.lockfile",
            "assert callable(module.RootRequirement)",
        ),
    ],
)
def test_library_imports_defer_dependencies_until_first_use(
    module: str, dependency: str, operation: str
) -> None:
    probe = f"""
import importlib
import sys

if sys.version_info >= (3, 15):
    assert sys.get_lazy_imports() == "normal"
    previous_filter = sys.get_lazy_imports_filter()
module = importlib.import_module({module!r})
assert ({dependency!r} not in sys.modules) == (sys.version_info >= (3, 15))
{operation}
assert {dependency!r} in sys.modules
if sys.version_info >= (3, 15):
    assert sys.get_lazy_imports() == "normal"
    assert sys.get_lazy_imports_filter() is previous_filter
"""
    environment = dict(os.environ)
    environment.pop("PYTHON_LAZY_IMPORTS", None)
    subprocess.run(  # noqa: S603 - The probe uses fixed module names and operations above.
        [sys.executable, "-c", probe], env=environment, check=True
    )


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
def test_process_entry_reports_an_absent_optional_backend(
    hermetic_roots: Path, backend: str
) -> None:
    (hermetic_roots / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0"\ndependencies = []\n'
    )
    probe = """
import sys
backend = sys.argv[1]
sys.modules[backend] = None
sys.argv = ["nab", "--no-progress", "lock", "--http-backend", backend]
from nab._entry import console_entry
console_entry()
"""
    result = subprocess.run(  # noqa: S603 - The probe and backend choices are fixed test data.
        [sys.executable, "-c", probe, backend],
        cwd=hermetic_roots,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"{backend} is not installed" in result.stderr
    assert f"pip install nab[{backend}]" in result.stderr
