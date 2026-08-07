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


def _wheel(
    package: str,
    version: str,
    *,
    requires_python: str | None = None,
) -> WheelFile:
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.invalid/{package}-{version}.whl",
        version=version,
        requires_python=requires_python,
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
        listings={
            "a": [
                _wheel("a", version, requires_python=">=3.12") for version in versions
            ]
        },
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


def test_transitive_prerelease_admission_stays_in_its_fork() -> None:
    """Keep explicit prerelease admission local to the matching target."""
    a = _wheel("a", "1.0.0")
    c_stable = _wheel("c", "1.0.0")
    c_beta = _wheel("c", "2.0.0b1")
    assert a.metadata_url is not None
    assert c_stable.metadata_url is not None
    assert c_beta.metadata_url is not None
    metadata = {
        a.metadata_url: (
            "Metadata-Version: 2.1\n"
            "Name: a\n"
            "Version: 1.0.0\n"
            "Requires-Dist: c>=2.0.0b1 ; sys_platform == 'linux'\n"
            "Requires-Dist: c==1.0.0 ; sys_platform != 'linux'\n\n"
        ),
        c_stable.metadata_url: ("Metadata-Version: 2.1\nName: c\nVersion: 1.0.0\n\n"),
        c_beta.metadata_url: ("Metadata-Version: 2.1\nName: c\nVersion: 2.0.0b1\n\n"),
    }
    coordinator = make_coordinator(
        listings={"a": [a], "c": [c_stable, c_beta]},
        metadata_by_url=metadata,
    )
    project_python = ">=3.12"
    witness_python = ">=3.12,<3.13"
    targets = Matrix(
        python=witness_python,
        platforms=(
            PlatformSpec("linux_x86_64"),
            PlatformSpec("windows_amd64"),
        ),
    ).expand()
    config = NabProjectConfig(requires_python=project_python)

    result = resolve_with_coordinator(
        coordinator,
        targets,
        [Requirement("a")],
        config=config,
    )

    assert result.success
    assert {
        target_result.target.label: target_result.pins
        for target_result in result.target_results
    } == {
        "py312-linux_x86_64": {
            "a": Version("1.0.0"),
            "c": Version("2.0.0b1"),
        },
        "py312-windows_amd64": {
            "a": Version("1.0.0"),
            "c": Version("1.0.0"),
        },
    }

    pylock = build_pylock(build_lock_input(result, config=config))
    pylock.validate()
    assert len(pylock.environments) == len(targets) == 2
    for target in targets:
        assert (
            sum(marker.evaluate(target.marker_env) for marker in pylock.environments)
            == 1
        )

    a_packages = [package for package in pylock.packages if str(package.name) == "a"]
    assert [(str(package.version), package.marker) for package in a_packages] == [
        ("1.0.0", None)
    ]
    assert list(a_packages[0].dependencies or ()) == [{"name": "c"}]
    c_packages = [package for package in pylock.packages if str(package.name) == "c"]
    assert len(c_packages) == 2
    assert {str(package.version) for package in c_packages} == {"1.0.0", "2.0.0b1"}
    expected_c = {
        "linux_x86_64": "2.0.0b1",
        "windows_amd64": "1.0.0",
    }
    for target in targets:
        matching_c = [
            package
            for package in c_packages
            if package.marker is None or package.marker.evaluate(target.marker_env)
        ]
        assert len(matching_c) == 1
        assert str(matching_c[0].version) == expected_c[target.platform_id]


def test_conditional_dependency_stays_in_its_package_fork() -> None:
    a_old = _wheel("a", "1.0.0")
    a_new = _wheel("a", "2.0.0")
    b = _wheel("b", "1.0.0")
    assert a_old.metadata_url is not None
    assert a_new.metadata_url is not None
    assert b.metadata_url is not None
    metadata = {
        a_old.metadata_url: (
            "Metadata-Version: 2.1\n"
            "Name: a\n"
            "Version: 1.0.0\n"
            "Requires-Dist: b ; sys_platform == 'linux'\n\n"
        ),
        a_new.metadata_url: (
            "Metadata-Version: 2.1\n"
            "Name: a\n"
            "Version: 2.0.0\n"
            "Requires-Dist: b ; sys_platform == 'linux'\n\n"
        ),
        b.metadata_url: "Metadata-Version: 2.1\nName: b\nVersion: 1.0.0\n\n",
    }
    coordinator = make_coordinator(
        listings={"a": [a_old, a_new], "b": [b]},
        metadata_by_url=metadata,
    )
    project_python = ">=3.12"
    witness_python = ">=3.12,<3.13"
    targets = Matrix(
        python=witness_python,
        platforms=(
            PlatformSpec("linux_x86_64"),
            PlatformSpec("macos_arm64"),
        ),
    ).expand()
    config = NabProjectConfig(requires_python=project_python)

    result = resolve_with_coordinator(
        coordinator,
        targets,
        [
            Requirement("a>=2 ; sys_platform == 'linux'"),
            Requirement("a<2 ; sys_platform == 'darwin'"),
        ],
        config=config,
    )

    assert result.success
    assert {
        target_result.target.label: target_result.pins
        for target_result in result.target_results
    } == {
        "py312-linux_x86_64": {
            "a": Version("2.0.0"),
            "b": Version("1.0.0"),
        },
        "py312-macos_arm64": {"a": Version("1.0.0")},
    }

    pylock = build_pylock(build_lock_input(result, config=config))
    pylock.validate()
    assert len(pylock.environments) == len(targets) == 2
    for target in targets:
        assert (
            sum(marker.evaluate(target.marker_env) for marker in pylock.environments)
            == 1
        )

    a_packages = [package for package in pylock.packages if str(package.name) == "a"]
    assert len(a_packages) == 2
    assert {str(package.version) for package in a_packages} == {"1.0.0", "2.0.0"}
    a_by_version = {str(package.version): package for package in a_packages}
    assert list(a_by_version["1.0.0"].dependencies or ()) == []
    assert list(a_by_version["2.0.0"].dependencies or ()) == [{"name": "b"}]

    b_packages = [package for package in pylock.packages if str(package.name) == "b"]
    assert len(b_packages) == 1
    assert str(b_packages[0].version) == "1.0.0"
    assert b_packages[0].marker is not None

    expected_packages = {
        "linux_x86_64": [("a", "2.0.0"), ("b", "1.0.0")],
        "macos_arm64": [("a", "1.0.0")],
    }
    for target in targets:
        matching_packages = sorted(
            (str(package.name), str(package.version))
            for package in pylock.packages
            if package.marker is None or package.marker.evaluate(target.marker_env)
        )
        assert matching_packages == expected_packages[target.platform_id]
