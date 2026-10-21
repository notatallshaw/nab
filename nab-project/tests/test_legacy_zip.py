"""Resolve, lock and download ZIP sdists through a real local index."""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from typing import TYPE_CHECKING, NoReturn

import pytest

from nab_project.download import DownloadError, download_lock
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import IndexPin, build_pylock
from nab_project.resolve import build_lock_input, resolve_for_targets
from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import WheelMetadata
from nab_provider.policy import BuildPolicy
from nab_provider.records import IndexConfig
from nab_provider.target import ResolveTarget
from nab_provider.testing import pkg_override

if TYPE_CHECKING:
    from pathlib import Path


class _NoNetwork:
    """Refuse network requests from tests backed entirely by local archives."""

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> NoReturn:
        msg = f"unexpected request: {url}"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        pass


def _sdist(
    directory: Path,
    name: str,
    suffix: str,
    *,
    legacy: bool = False,
    dependency: str = "",
) -> Path:
    """Write an sdist with a dependency declaration and an extractable source file."""
    metadata = (
        f"Metadata-Version: {'1.0' if legacy else '2.2'}\nName: {name}\nVersion: 1\n"
        + (f"Requires-Dist: {dependency}\n" if dependency else "")
    ).encode()
    members = [(f"{name}-1/PKG-INFO", metadata), (f"{name}-1/setup.py", b"# source")]
    output = io.BytesIO()
    if suffix == ".zip":
        with zipfile.ZipFile(output, "w") as archive:
            for filename, data in members:
                archive.writestr(filename, data)
    else:
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for filename, data in members:
                member = tarfile.TarInfo(filename)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    path = directory / f"{name}-1{suffix}"
    path.write_bytes(output.getvalue())
    listing = directory / name / "index.html"
    listing.parent.mkdir(exist_ok=True)
    with listing.open("a", encoding="utf-8") as page:
        digest = hashlib.sha256(output.getvalue()).hexdigest()
        page.write(f'<a href="../{path.name}#sha256={digest}">{path.name}</a>\n')
    return path


@pytest.fixture
def local_project(tmp_path: Path) -> tuple[Path, Path, IndexConfig]:
    """A root depending on a ZIP-only package with a normal sdist dependency."""
    index = tmp_path / "index"
    index.mkdir()
    _sdist(index, "child", ".tar.gz")
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "root"\nversion = "1"\ndependencies = ["pkg"]\n'
    )
    return project, index, IndexConfig("local", index.as_uri())


@pytest.mark.parametrize("universal", [False, True])
def test_zip_resolves_locks_and_downloads(
    tmp_path: Path, local_project: tuple[Path, Path, IndexConfig], *, universal: bool
) -> None:
    project, directory, index = local_project
    archive = _sdist(directory, "pkg", ".zip", dependency="child==1")
    inputs = ResolveInputs(
        indexes=(index,), allow_zip_sdists=True, build_policy=BuildPolicy.NEVER
    )
    targets = (
        (ResolveTarget.for_host_python("3.10"), ResolveTarget.for_host_python("3.13"))
        if universal
        else (ResolveTarget.for_host(),)
    )
    result = resolve_for_targets(project, _NoNetwork(), targets=targets, inputs=inputs)
    result.raise_for_failure()
    lock_input = build_lock_input(result, inputs=inputs)
    pylock = build_pylock(lock_input)
    assert {package.name for package in pylock.packages} == {"pkg", "child"}
    for target in lock_input.targets.values():
        pin = target.pins["pkg"]
        assert isinstance(pin, IndexPin)
        assert pin.sdist is not None
        assert pin.sdist.filename == archive.name
        assert pin.sdist.primary_digest == (
            "sha256",
            hashlib.sha256(archive.read_bytes()).hexdigest(),
        )

    output = tmp_path / "download"
    download_lock(lock_input, _NoNetwork(), output, offline=True)
    assert (output / archive.name).read_bytes() == archive.read_bytes()

    archive.write_bytes(b"changed after resolve")
    (output / archive.name).unlink()
    with pytest.raises(DownloadError, match="sha256 mismatch"):
        download_lock(lock_input, _NoNetwork(), output, offline=True)


def test_zip_rejection_suggests_the_scoped_option(
    local_project: tuple[Path, Path, IndexConfig],
) -> None:
    project, directory, index = local_project
    _sdist(directory, "pkg", ".zip")
    result = resolve_for_targets(
        project,
        _NoNetwork(),
        targets=(ResolveTarget.for_host(),),
        inputs=ResolveInputs(indexes=(index,), build_policy=BuildPolicy.NEVER),
    )
    assert not result.success
    error = result.target_results[0].error
    assert error is not None
    assert 'packages."pkg".allow-zip-sdists = true' in str(error)


@pytest.mark.parametrize("trust", [False, True])
def test_zip_permission_preserves_legacy_metadata_trust(
    local_project: tuple[Path, Path, IndexConfig], *, trust: bool
) -> None:
    project, directory, index = local_project
    _sdist(directory, "pkg", ".zip", legacy=True, dependency="child==1")
    result = resolve_for_targets(
        project,
        _NoNetwork(),
        targets=(ResolveTarget.for_host(),),
        inputs=ResolveInputs(
            indexes=(index,),
            allow_zip_sdists=True,
            build_policy=BuildPolicy.NEVER,
            trust_unverified_sdist_deps=trust,
        ),
    )
    assert result.success is trust
    if trust:
        lock = result.target_results[0].lock
        assert lock is not None
        assert set(lock.pins) == {"pkg", "child"}


def test_zip_legacy_metadata_can_use_the_build_backend(
    local_project: tuple[Path, Path, IndexConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, directory, index = local_project
    _sdist(directory, "pkg", ".zip", legacy=True)
    seen: list[bytes] = []

    def build(source_dir: Path, **_kwargs: object) -> WheelMetadata:
        seen.append((source_dir / "setup.py").read_bytes())
        return WheelMetadata(name="pkg", version=Version("1"))

    monkeypatch.setattr("nab_project._build.runner.run_build_backend", build)
    result = resolve_for_targets(
        project,
        _NoNetwork(),
        targets=(ResolveTarget.for_host(),),
        inputs=ResolveInputs(
            indexes=(index,),
            allow_zip_sdists=True,
            build_policy=BuildPolicy.BUILD_REMOTE,
        ),
    )
    result.raise_for_failure()
    assert seen == [b"# source"]


def test_scoped_zip_exception_and_tar_preference(
    local_project: tuple[Path, Path, IndexConfig],
) -> None:
    project, directory, index = local_project
    _sdist(directory, "pkg", ".zip", dependency="absent")
    tarball = _sdist(directory, "pkg", ".tar.gz", dependency="child==1")
    inputs = ResolveInputs(
        indexes=(index,),
        build_policy=BuildPolicy.NEVER,
        package_overrides=(pkg_override("pkg==1", allow_zip_sdists=True),),
    )
    result = resolve_for_targets(
        project, _NoNetwork(), targets=(ResolveTarget.for_host(),), inputs=inputs
    )
    result.raise_for_failure()
    lock_input = build_lock_input(result, inputs=inputs)
    for target in lock_input.targets.values():
        assert set(target.pins) == {"pkg", "child"}
        pin = target.pins["pkg"]
        assert isinstance(pin, IndexPin)
        assert pin.sdist is not None
        assert pin.sdist.filename == tarball.name
