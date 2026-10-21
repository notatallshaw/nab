"""Resolve dynamic sdists for a declared matrix through the offline CLI."""

from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_project.toml_io import loads
from nab_provider._vendor.packaging.pylock import PackageSdist, Pylock
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata

from ._local_artifacts import make_wheel_archive, publish_artifacts

if TYPE_CHECKING:
    from pathlib import Path


def _dynamic_sdist(version: str = "6.0.0") -> bytes:
    """Publish dependency fields that require reconciliation with a wheel."""
    metadata = make_metadata(
        "watchdog",
        version,
        "stale-dependency",
        metadata_version="2.2",
        requires_python=">=3.10",
    )
    members = {
        "PKG-INFO": metadata.rstrip() + "\nDynamic: Requires-Dist\n\n",
        "pyproject.toml": (
            f'[project]\nname = "watchdog"\nversion = "{version}"\n'
            'requires-python = ">=3.10"\ndynamic = ["dependencies"]\n'
        ),
    }
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        for name, text in members.items():
            payload = text.encode("utf-8")
            member = tarfile.TarInfo(f"watchdog-{version}/{name}")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return content.getvalue()


@pytest.fixture
def release_project(hermetic_roots: Path) -> Path:
    """Offer a dynamic sdist and one wheel that cannot install on Python 3.14."""
    index = hermetic_roots / "index"
    tag = "cp313-cp313-macosx_11_0_arm64"
    publish_artifacts(
        index,
        "watchdog",
        {
            "watchdog-6.0.0.tar.gz": _dynamic_sdist(),
            f"watchdog-6.0.0-{tag}.whl": make_wheel_archive(
                "watchdog",
                "6.0.0",
                make_metadata(
                    "watchdog",
                    "6.0.0",
                    'macos-dep; sys_platform == "darwin"',
                    'linux-dep; sys_platform == "linux"',
                    'old-python; python_version < "3.14"',
                    metadata_version="2.2",
                    requires_python=">=3.10",
                ),
                tag,
            ),
        },
    )
    for name in ("macos-dep", "linux-dep"):
        wheel_name = name.replace("-", "_")
        publish_artifacts(
            index,
            name,
            {
                f"{wheel_name}-1-py3-none-any.whl": make_wheel_archive(
                    wheel_name, "1", make_metadata(name, "1"), "py3-none-any"
                )
            },
        )
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1"\n'
        'requires-python = ">=3.10"\ndependencies = ["watchdog==6.0.0"]\n'
        '[tool.nab]\nmode = "universal"\n'
        '[tool.nab.matrix]\npython = "==3.14"\n'
        'platforms = ["macos_arm64", "linux_x86_64"]\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{index.as_uri()}"\n',
        encoding="utf-8",
    )
    return project


def _lock(project: Path, *flags: str) -> int:
    """Resolve to stdout without reading or populating a remote cache."""
    return run(
        ["lock", str(project), "--offline", "--no-cache", "--output", "-", *flags]
    )


def _fallback_release(project: Path) -> None:
    """Offer three dynamic releases before an older readable wheel."""
    artifacts = {
        f"watchdog-{version}.tar.gz": _dynamic_sdist(version)
        for version in ("6.0.0", "6.1.0", "6.2.0")
    }
    artifacts["watchdog-5.0.3-py3-none-any.whl"] = make_wheel_archive(
        "watchdog",
        "5.0.3",
        make_metadata("watchdog", "5.0.3"),
        "py3-none-any",
    )
    publish_artifacts(project.parent / "index", "watchdog", artifacts)


@pytest.fixture
def wider_project(release_project: Path) -> Path:
    """Resolve a transitive watchdog dependency alongside two other packages."""
    _fallback_release(release_project)
    publish_artifacts(
        release_project.parent / "index",
        "application",
        {
            "application-1-py3-none-any.whl": make_wheel_archive(
                "application",
                "1",
                make_metadata("application", "1", "watchdog>=5"),
                "py3-none-any",
            )
        },
    )
    release_project.write_text(
        release_project.read_text(encoding="utf-8").replace(
            'dependencies = ["watchdog==6.0.0"]',
            'dependencies = ["application", "macos-dep", "linux-dep"]',
        ),
        encoding="utf-8",
    )
    return release_project


def test_rejected_dynamic_releases_are_quiet_when_an_older_version_resolves(
    wider_project: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _lock(wider_project) == 0

    captured = capsys.readouterr()
    pylock = Pylock.from_dict(loads(captured.out))

    assert {str(package.name) for package in pylock.packages} == {
        "application",
        "watchdog",
        "macos-dep",
        "linux-dep",
    }

    watchdog = next(
        package for package in pylock.packages if package.name == "watchdog"
    )

    assert str(watchdog.version) == "5.0.3"
    assert "assume-universal-deps" not in captured.err + caplog.text


def test_rejected_dynamic_releases_do_not_add_hints_to_an_unrelated_failure(
    wider_project: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    wider_project.write_text(
        wider_project.read_text(encoding="utf-8").replace(
            '"application", "macos-dep", "linux-dep"',
            '"application", "macos-dep", "linux-dep", "absent==1"',
        ),
        encoding="utf-8",
    )

    assert _lock(wider_project) == 1

    captured = capsys.readouterr()

    assert "absent" in captured.err
    assert "assume-universal-deps" not in captured.err + caplog.text


def test_many_dynamic_rejections_produce_one_hint_per_command(
    wider_project: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    wider_project.write_text(
        wider_project.read_text(encoding="utf-8").replace(
            '"application", "macos-dep", "linux-dep"',
            '"application", "macos-dep", "linux-dep", "watchdog>=6"',
        ),
        encoding="utf-8",
    )

    assert _lock(wider_project) == 1

    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.count("try:") == 1
    assert captured.err.count("assume-universal-deps = true") == 1
    assert "assume-universal-deps" not in caplog.text


@pytest.mark.parametrize("requirement", ["watchdog==6.0.0", "watchdog[events]==6.0.0"])
def test_dynamic_sdist_failure_suggests_a_release_scoped_assertion(
    release_project: Path, capsys: pytest.CaptureFixture[str], requirement: str
) -> None:
    release_project.write_text(
        release_project.read_text(encoding="utf-8").replace(
            '"watchdog==6.0.0"', f'"{requirement}"'
        ),
        encoding="utf-8",
    )

    assert _lock(release_project) == 1

    captured = capsys.readouterr()

    assert captured.out == ""
    assert "every version in range was rejected on its metadata" in captured.err
    assert '[tool.nab.packages."watchdog==6.0.0"]' in captured.err
    assert "assume-universal-deps = true" in captured.err


@pytest.mark.parametrize("surface", ["package", "cli"])
def test_assertion_locks_the_sdist_with_target_conditioned_dependencies(
    release_project: Path, capsys: pytest.CaptureFixture[str], surface: str
) -> None:
    flags = []
    if surface == "package":
        with release_project.open("a", encoding="utf-8") as stream:
            stream.write(
                '[tool.nab.packages."watchdog==6.0.0"]\nassume-universal-deps = true\n'
            )
    else:
        flags.append("--project-assume-universal-deps")

    assert _lock(release_project, *flags) == 0

    pylock = Pylock.from_dict(loads(capsys.readouterr().out))
    pylock.validate()
    packages = {str(package.name): package for package in pylock.packages}

    assert set(packages) == {"watchdog", "macos-dep", "linux-dep"}

    watchdog = packages["watchdog"]

    assert str(watchdog.version) == "6.0.0"
    assert watchdog.sdist is not None
    assert watchdog.sdist.filename == "watchdog-6.0.0.tar.gz"
    assert not watchdog.wheels

    for platform, dependency in (
        ("macos_arm64", "macos-dep"),
        ("linux_x86_64", "linux-dep"),
    ):
        target = ResolveTarget.for_declared(
            python_version="3.14", spec=PlatformSpec(platform)
        )
        selected = {
            str(package.name): artifact
            for package, artifact in pylock.select(
                environment=target.marker_env, tags=[Tag("py3", "none", "any")]
            )
        }
        assert set(selected) == {"watchdog", dependency}
        assert isinstance(selected["watchdog"], PackageSdist)
