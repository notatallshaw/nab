"""Python compatibility of wheels built from a source distribution."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import IncompatiblePythonError, UnsupportedSdistError
from nab_provider.metadata import parse_metadata
from nab_provider.provider import BuildPolicy, Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    make_coordinator,
    make_metadata,
    make_sdist,
    make_wheel,
    pkg_override,
)

_VERSION = Version("1.0")
_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)


@pytest.mark.parametrize(
    "project",
    [
        None,
        {},
        {"name": "other"},
        {"name": "pkg", "dynamic": ["requires-python"]},
        {"name": "pkg", "dynamic": "requires-python"},
        {"name": "pkg", "dynamic": [None]},
    ],
)
def test_wheel_cannot_fill_in_a_dynamic_sdist_python_requirement(
    project: dict[str, object] | None,
) -> None:
    foreign_wheel = make_wheel("1.0", tag="cp311-cp311-win_amd64", has_metadata=True)
    port = make_coordinator(
        [make_sdist(), foreign_wheel],
        sdist_pkg_info=make_metadata("pkg", "1.0", metadata_version="2.2")
        + "Dynamic: Requires-Python\n",
        sdist_pyproject={"project": project},
        metadata_text=make_metadata("pkg", "1.0", requires_python=">=3"),
    )
    provider = Provider(port, _TARGET, assume_universal_deps=True)
    with pytest.raises(UnsupportedSdistError, match="Requires-Python"):
        provider.get_dependencies("pkg", _VERSION)
    assert port.calls_to("build_sdist_metadata") == []


@pytest.mark.parametrize("fields", [{}, {"requires-python": ">=3.10"}])
def test_project_python_requirement_replaces_the_sdist_placeholder(
    fields: dict[str, str],
) -> None:
    port = make_coordinator(
        [make_sdist()],
        sdist_pkg_info=make_metadata(
            "pkg", "1.0", "dep", requires_python=">=3.12", metadata_version="2.2"
        )
        + "Dynamic: Requires-Python\n",
        sdist_pyproject={"project": {"name": "Pkg", **fields}},
    )
    provider = Provider(port, _TARGET)
    assert set(provider.get_dependencies("pkg", _VERSION)) == {"dep"}
    found = provider.metadata_cache[("pkg", _VERSION)].requires_python
    assert (str(found) if found is not None else None) == fields.get("requires-python")
    assert port.calls_to("build_sdist_metadata") == []


def test_cached_dependencies_do_not_replace_the_next_targets_python_check() -> None:
    port = make_coordinator(
        [make_sdist()],
        sdist_pkg_info=make_metadata("pkg", "1.0", metadata_version="2.2")
        + "Dynamic: Requires-Dist\nDynamic: Requires-Python\n",
        sdist_pyproject={
            "project": {
                "name": "pkg",
                "dependencies": ["dep"],
                "requires-python": ">=3.12",
            }
        },
    )
    overridden = Provider(
        port, _TARGET, package_overrides=(pkg_override("pkg", requires_python=""),)
    )
    assert set(overridden.get_dependencies("pkg", _VERSION)) == {"dep"}
    with pytest.raises(IncompatiblePythonError):
        Provider(port, _TARGET).get_dependencies("pkg", _VERSION)


def test_build_supplies_the_wheels_python_requirement() -> None:
    built = parse_metadata(make_metadata("pkg", "1.0", requires_python=">=3.12"))
    port = make_coordinator(
        [make_sdist()],
        sdist_pkg_info=make_metadata("pkg", "1.0", metadata_version="2.2")
        + "Dynamic: Requires-Python\n",
        build_sdist=lambda *_args: built,
    )
    provider = Provider(
        port, _TARGET, assume_universal_deps=True, build_policy=BuildPolicy.BUILD_REMOTE
    )
    with pytest.raises(
        UnsupportedSdistError, match=r"built sdist requires Python >=3\.12"
    ):
        provider.get_dependencies("pkg", _VERSION)
    assert len(port.calls_to("build_sdist_metadata")) == 1
