"""The benchmark harness intersects repeated same-name requirements.

A scenario may list a package both pinned and with a bare extra, e.g.
``inmanta-core==6.0.0`` followed by ``inmanta-core[pytest-inmanta-extensions]``.
The harness must keep the pin, as pip and uv do, not let the bare requirement
widen it back.

It must also enter a requirement's extras in a fixed order, so a multi-extra
root seeds the resolver the same way in every process.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Iterator

# scenarios.py and canary.py import their siblings by bare name.
pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.usefixtures("benchmark_import_path"),
]

_BENCH = Path(__file__).resolve().parents[2] / "benchmarks"


def _harness(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_bench_{name}", _BENCH / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    benchmark_dir = str(_BENCH)
    sys.path.insert(0, benchmark_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(benchmark_dir)
    return module


@pytest.mark.parametrize("harness", ["scenarios", "canary"])
def test_repeated_name_keeps_pin(harness: str) -> None:
    reqs = _harness(harness).parse_requirements(
        ["inmanta-core==6.0.0", "inmanta-core[pytest-inmanta-extensions]"]
    )
    assert Version("6.0.0") in reqs["inmanta-core"]
    assert Version("4.0.0") not in reqs["inmanta-core"]
    assert reqs["inmanta-core[pytest-inmanta-extensions]"] == VersionRange.full(
        admit_arbitrary=False
    )


class _ReverseOrderedExtras(set[str]):
    """A ``set`` that iterates in reverse-sorted order."""

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(super().__iter__(), reverse=True))


class _ReverseExtrasRequirement(Requirement):
    """A ``Requirement`` whose extras iterate in reverse-sorted order."""

    def __init__(self, requirement_string: str) -> None:
        super().__init__(requirement_string)
        self.extras = _ReverseOrderedExtras(self.extras)


@pytest.mark.parametrize("harness", ["scenarios", "canary"])
def test_extras_enter_in_sorted_order(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _harness(harness)
    monkeypatch.setattr(module, "Requirement", _ReverseExtrasRequirement)
    reqs = module.parse_requirements(["pkg[web,api,dev,cli]"])
    assert [name for name in reqs if "[" in name] == [
        "pkg[api]",
        "pkg[cli]",
        "pkg[dev]",
        "pkg[web]",
    ]
