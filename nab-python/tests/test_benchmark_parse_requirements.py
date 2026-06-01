"""The benchmark harness intersects repeated same-name requirements.

A scenario may list a package both pinned and with a bare extra, e.g.
``inmanta-core==6.0.0`` followed by ``inmanta-core[pytest-inmanta-extensions]``.
The harness must keep the pin (as pip and uv do), not let the later bare
requirement widen the range back to any version.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.version import Version

_BENCH = Path(__file__).resolve().parents[1] / "benchmarks"


def _harness(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_bench_{name}", _BENCH / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("harness", ["scenarios", "canary"])
def test_repeated_name_keeps_pin(harness: str) -> None:
    reqs = _harness(harness).parse_requirements(
        ["inmanta-core==6.0.0", "inmanta-core[pytest-inmanta-extensions]"]
    )
    assert Version("6.0.0") in reqs["inmanta-core"]
    assert Version("4.0.0") not in reqs["inmanta-core"]
    assert reqs["inmanta-core[pytest-inmanta-extensions]"] == VersionRange.full()
