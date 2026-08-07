"""Deterministic universal-lock regression scenarios."""

from __future__ import annotations

from nab_index.client import WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.config import NabProjectConfig
from nab_python.lockfile import build_pylock
from nab_python.resolve import build_lock_input, resolve_with_coordinator
from nab_python.tags import PlatformSpec
from nab_python.target import Matrix


def _wheel(version: str) -> WheelFile:
    return WheelFile(
        filename=f"a-{version}-py3-none-any.whl",
        url=f"https://example.invalid/a-{version}.whl",
        version=version,
        requires_python=">=3.12",
        has_metadata=True,
        upload_time=None,
        hashes=(("sha256", "a" * 64),),
    )


def test_overlapping_root_markers_cover_each_python_partition() -> None:
    """Exercise every root-marker partition with a finite Python witness.

    Python 3.12, 3.13, and 3.14 cover the three marker partitions. The package
    retains its ``Requires-Python >=3.12`` gate and uses wheels only because
    artifact choice is outside the marker invariant.
    """
    versions = ("1.0.0", "1.1.0", "1.2.0")
    coordinator = make_coordinator(
        listings={"a": [_wheel(version) for version in versions]},
        auto_metadata=True,
    )
    python = ">=3.12,<3.15"
    targets = Matrix(
        python=python,
        platforms=(PlatformSpec("linux_x86_64"),),
    ).expand()
    requirements = [
        Requirement("a>=1.0.0; python_version < '3.13'"),
        Requirement("a>=1.1.0; python_version >= '3.13'"),
        Requirement("a>=1.2.0; python_version >= '3.14'"),
    ]
    config = NabProjectConfig(requires_python=python)

    result = resolve_with_coordinator(
        coordinator,
        targets,
        requirements,
        config=config,
    )

    assert result.success
    assert {
        target_result.target.label: target_result.pins
        for target_result in result.target_results
    } == {
        "py312-linux_x86_64": {"a": Version("1.2.0")},
        "py313-linux_x86_64": {"a": Version("1.2.0")},
        "py314-linux_x86_64": {"a": Version("1.2.0")},
    }

    pylock = build_pylock(build_lock_input(result, config=config))
    pylock.validate()
    assert len(pylock.environments) == len(targets) == 3
    for target in targets:
        assert (
            sum(marker.evaluate(target.marker_env) for marker in pylock.environments)
            == 1
        )
    assert [
        (
            str(package.name),
            str(package.version),
            str(package.requires_python),
            package.marker,
        )
        for package in pylock.packages
    ] == [
        ("a", "1.2.0", ">=3.12", None),
    ]
