"""Tests for the dynamic-metadata path through ``nab_project._build``.

The unit tests use a hand-rolled in-tree backend (an
``nab_test_backend.py`` file plus
``[build-system].backend-path = ["."]``).  ``[build-system].requires``
is empty so the venv install step is a no-op; everything stays
offline, no network, no PyPI roundtrip.

The end-to-end test against a real source distribution is marked
``network`` and is deselected by default; run with
``pytest -m network`` to exercise it against a real PyPI.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timezone
from importlib.util import cache_from_source, module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import build
import pyproject_hooks
import pytest
import tomli
from installer.utils import SCHEME_NAMES, Scheme

from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import IndexConfig
from nab_project._build import env as env_mod
from nab_project._build import runner as runner_mod
from nab_project._build.env import (
    BuildEnvError,
    NabBuildEnv,
    _FastSchemeDictionaryDestination,
    _PendingBuild,
    _remove_files,
    _venv_scheme_paths,
)
from nab_project._build.runner import (
    BuildBackendError,
    _build_wheel_and_extract,
    _validate_backend_path,
    build_wheel_for_install,
    run_build_backend,
)
from nab_project.download import DownloadError, DownloadResult, iter_artifacts
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import (
    IndexPin,
    LocalPin,
    LockInput,
    PinShape,
    SdistArtifact,
    TargetLock,
    WheelArtifact,
)
from nab_project.resolve import ResolveResult, TargetResult
from nab_provider._provider.metadata_resolver import pick_dist
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.provider import (
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    LocalSource,
    MissingExtraError,
)
from nab_provider.tags import PlatformSpec, TagSet
from nab_provider.target import ResolveTarget
from nab_provider.testing import pkg_override
from nab_resolver.errors import ResolutionError


def _unpack_fixture_sdist(data: bytes, target_dir: Path) -> Path:
    """Stand in for ``extract_sdist_archive`` on any supported Python.

    The real one refuses to run without the tar data filter (:pep:`706`),
    which 3.10 and 3.11 before their .12 and .4 releases lack, and every
    archive here is one a fixture just wrote.  Returns the lone top-level
    directory, which is the source root the real one would return.
    """
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target_dir, filter="data")
        else:
            tar.extractall(target_dir)  # noqa: S202

    entries = list(target_dir.iterdir())
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else target_dir


# A minimal, in-tree PEP 517 backend.  Implements
# ``prepare_metadata_for_build_wheel`` only, enough to exercise
# the happy path through ``BuildBackendHookCaller`` ->
# ``build.ProjectBuilder.from_isolated_env`` -> our ``NabBuildEnv``.
_FAKE_BACKEND_SRC = '''\
"""Minimal PEP 517 backend used by nab_project._build tests."""
import os


def _write_metadata(metadata_directory, name, version, requires_dist):
    distinfo = f"{name.replace('-', '_')}-{version}.dist-info"
    target = os.path.join(metadata_directory, distinfo)
    os.makedirs(target, exist_ok=True)
    metadata_path = os.path.join(target, "METADATA")
    body_lines = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
        "Requires-Python: >=3.10",
    ]
    for r in requires_dist:
        body_lines.append(f"Requires-Dist: {r}")
    body_lines.append("Provides-Extra: cli")
    body_lines.append("")
    with open(metadata_path, "w", encoding="utf-8") as f:
        f.write("\\n".join(body_lines))
    return distinfo


def get_requires_for_build_wheel(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _write_metadata(
        metadata_directory,
        name="fake-pkg",
        version="1.2.3",
        requires_dist=["click>=8", "rich>=13; extra == \\"cli\\""],
    )


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    """Stub: build a tiny zip with just dist-info/METADATA.

    Used by the hatchling-quirk-skip test, which exercises the
    fallback path that reads METADATA out of a built wheel.
    """
    import os
    import zipfile

    name = "fake-dyn"
    version = "9.9.9"
    escaped = name.replace("-", "_")
    distinfo = f"{escaped}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\\n"
        f"Name: {name}\\n"
        f"Version: {version}\\n"
        "Requires-Python: >=3.10\\n"
        "Requires-Dist: click>=8\\n"
    )
    wheel_name = f"{escaped}-{version}-py3-none-any.whl"
    target = os.path.join(wheel_directory, wheel_name)
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr(f"{distinfo}/METADATA", metadata)
        zf.writestr(
            f"{distinfo}/WHEEL",
            "Wheel-Version: 1.0\\nGenerator: nab-test\\n"
            "Root-Is-Purelib: true\\nTag: py3-none-any\\n",
        )
        zf.writestr(f"{distinfo}/RECORD", "")
    return wheel_name
'''


# The backend shipped inside a buildable sdist fixture.  Writes a real,
# installer-valid wheel, since the point is that the build env installs it.
_SDIST_BACKEND_SRC = '''\
"""In-tree PEP 517 backend for a nab_project._build sdist fixture."""
import base64
import hashlib
import os
import zipfile

NAME = "{name}"
VERSION = "{version}"


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    escaped = NAME.replace("-", "_")
    distinfo = escaped + "-" + VERSION + ".dist-info"
    members = {{
        escaped + "/__init__.py": b"",
        distinfo + "/METADATA": (
            "Metadata-Version: 2.1\\nName: " + NAME + "\\nVersion: " + VERSION + "\\n"
        ).encode(),
        distinfo + "/WHEEL": (
            b"Wheel-Version: 1.0\\nGenerator: nab-test\\n"
            b"Root-Is-Purelib: true\\nTag: py3-none-any\\n"
        ),
    }}
    records = []
    for member, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        records.append(
            member + ",sha256=" + digest.decode() + "," + str(len(data))
        )
    records.append(distinfo + "/RECORD,,")

    wheel_name = escaped + "-" + VERSION + "-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, wheel_name), "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
        zf.writestr(distinfo + "/RECORD", "\\n".join(records) + "\\n")
    return wheel_name
'''


def _wheel_record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode()


def _make_installable_wheel(
    path: Path,
    name: str,
    version: str,
    data_files: dict[str, bytes] | None = None,
    package_files: dict[str, bytes] | None = None,
) -> None:
    """Write a minimal but installer-valid pure-Python wheel.

    ``data_files`` are placed under the PEP 427 ``<dist>.data``
    directory, keyed by their path within it (``headers/foo.h``).
    ``package_files`` are placed inside the ``<name>/`` package.
    """
    dist = f"{name}-{version}"
    files = {
        f"{name}/__init__.py": b"",
        f"{dist}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
        ),
        f"{dist}.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: nab-test\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    for module, data in (package_files or {}).items():
        files[f"{name}/{module}"] = data
    for data_path, data in (data_files or {}).items():
        files[f"{dist}.data/{data_path}"] = data
    record = "".join(
        f"{p},{_wheel_record_hash(d)},{len(d)}\n" for p, d in files.items()
    )
    record += f"{dist}.dist-info/RECORD,,\n"
    files[f"{dist}.dist-info/RECORD"] = record.encode()
    with zipfile.ZipFile(path, "w") as zf:
        for member, data in files.items():
            zf.writestr(member, data)


def _make_pep643_sdist(path: Path, name: str, version: str) -> None:
    """Write an sdist whose PKG-INFO is static (PEP 643), so no build is needed."""
    pkg_info = f"Metadata-Version: 2.2\nName: {name}\nVersion: {version}\n".encode()
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(f"{name}-{version}/PKG-INFO")
        info.size = len(pkg_info)
        tf.addfile(info, io.BytesIO(pkg_info))


def _make_buildable_sdist(
    path: Path, name: str, version: str, requires: tuple[str, ...] = ()
) -> None:
    """Write an sdist that builds into an installable wheel.

    Static PKG-INFO so the inner resolve can read it without a build,
    and an in-tree backend reached through ``backend-path`` so the
    build needs nothing from an index beyond ``requires``.  Passing a
    non-empty ``requires`` is how a fixture reaches a second level of
    nesting.
    """
    escaped = name.replace("-", "_")
    root = f"{name}-{version}"
    requires_block = ", ".join(f'"{req}"' for req in requires)
    members = {
        "PKG-INFO": f"Metadata-Version: 2.2\nName: {name}\nVersion: {version}\n",
        "pyproject.toml": (
            "[build-system]\n"
            f"requires = [{requires_block}]\n"
            f'build-backend = "{escaped}_backend"\n'
            'backend-path = ["."]\n'
        ),
        f"{escaped}_backend.py": _SDIST_BACKEND_SRC.format(name=name, version=version),
    }
    with tarfile.open(path, "w:gz") as tf:
        for member, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{root}/{member}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _make_local_index(
    root: Path,
    name: str,
    version: str,
    *,
    sdist_only: bool = False,
    requires: tuple[str, ...] = (),
) -> None:
    """Create a PEP 503 ``file://`` index serving ``name``.

    Serves a wheel beside the sdist by default.  ``sdist_only`` serves
    an sdist alone, and that sdist is one a build can turn into a
    wheel, declaring ``requires`` as its own build requirements.
    """
    pkg_dir = root / name
    pkg_dir.mkdir(parents=True)
    sdist = pkg_dir / f"{name}-{version}.tar.gz"
    files = [sdist]
    if sdist_only:
        _make_buildable_sdist(sdist, name, version, requires)
    else:
        wheel = pkg_dir / f"{name}-{version}-py3-none-any.whl"
        _make_installable_wheel(wheel, name, version)
        _make_pep643_sdist(sdist, name, version)
        files.append(wheel)

    def _digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    links = "".join(
        f'<a href="{p.name}#sha256={_digest(p)}">{p.name}</a>\n' for p in files
    )
    (pkg_dir / "index.html").write_text(
        f"<!DOCTYPE html><html><body>{links}</body></html>", encoding="utf-8"
    )


def _write_fake_backend_project(
    tmp_path: Path,
    *,
    extra_pyproject: str = "",
) -> Path:
    """Write a source tree using the in-tree fake backend."""
    (tmp_path / "nab_test_backend.py").write_text(_FAKE_BACKEND_SRC, encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[build-system]\n"
        "requires = []\n"
        'build-backend = "nab_test_backend"\n'
        'backend-path = ["."]\n' + extra_pyproject,
        encoding="utf-8",
    )
    return tmp_path


def _tempdir_denying(denied: str) -> Callable[..., tempfile.TemporaryDirectory[str]]:
    """Return a ``TemporaryDirectory`` stand-in that refuses one prefix.

    A full temp filesystem raises ``OSError`` out of ``mkdtemp``.  Calls
    with any other prefix are delegated, so the rest of the build still
    gets scratch space.
    """
    real = tempfile.TemporaryDirectory

    def factory(*args: Any, **kwargs: Any) -> tempfile.TemporaryDirectory[str]:
        if kwargs.get("prefix") == denied:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    return factory


@pytest.fixture
def config() -> ResolveInputs:
    """A minimal :class:`ResolveInputs` for the tests."""
    return ResolveInputs()


class TestRunBuildBackend:
    def test_prepare_metadata_happy_path(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """The fake backend's ``prepare_metadata_for_build_wheel`` runs,
        produces a valid METADATA, and we parse it into
        :class:`WheelMetadata`.
        """
        _write_fake_backend_project(tmp_path)
        metadata = run_build_backend(tmp_path, config=config)
        assert metadata.name == "fake-pkg"
        assert str(metadata.version) == "1.2.3"
        assert metadata.requires_python is not None
        assert "cli" in metadata.provides_extra
        names = sorted(r.name for r in metadata.requires_dist)
        assert names == ["click", "rich"]

    def test_missing_pyproject_and_setup_py(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        with pytest.raises(BuildBackendError, match="no pyproject.toml or setup.py"):
            run_build_backend(tmp_path, config=config)

    def test_oversized_integer_pyproject(
        self, tmp_path: Path, config: ResolveInputs, oversized_integer: str
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            f"[tool.other]\ncount = {oversized_integer}\n", encoding="utf-8"
        )
        with pytest.raises(BuildBackendError, match="could not read pyproject.toml"):
            run_build_backend(tmp_path, config=config)

    def test_unsearchable_pyproject_reports_the_errno(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        # The presence check must not read EACCES as absence: the file is
        # there, so the tree is not the no-pyproject.toml-or-setup.py case.
        (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
        with (
            deny_access(tmp_path / "pyproject.toml"),
            pytest.raises(BuildBackendError, match="could not read.*Permission denied"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_directory_pyproject_reports_not_a_regular_file(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        (tmp_path / "pyproject.toml").mkdir()

        # setup.py is here so a fall-through would take the legacy branch.
        (tmp_path / "setup.py").write_text("from setuptools import setup\n")

        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)

        with (
            patch("nab_project._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="not a regular file"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_unsearchable_setup_py_takes_the_legacy_branch(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        # Same for the setup.py fallback: an unreadable one is a legacy
        # project whose build fails, not a tree with no build inputs.
        (tmp_path / "setup.py").write_text("from setuptools import setup\n")
        with (
            deny_access(tmp_path / "setup.py"),
            patch(
                "nab_project._build.runner.NabBuildEnv",
                side_effect=BuildEnvError("no venv"),
            ),
            pytest.raises(BuildBackendError, match="build env setup"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_legacy_setup_py_returns_parsed_metadata(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """A tree with only ``setup.py`` builds through to parsed metadata.

        ``NabBuildEnv`` and ``ProjectBuilder`` are stubbed to keep the test offline.
        """
        from nab_provider.metadata import WheelMetadata

        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")

        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)
        project = MagicMock()
        project.get_requires_for_build.return_value = []

        metadata_text = "Metadata-Version: 2.1\nName: legacy-pkg\nVersion: 0.1\n"

        def fake_prepare(_dist: str, out_dir: str) -> str:
            target = Path(out_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / "METADATA").write_text(metadata_text, encoding="utf-8")
            return str(target)

        project.prepare.side_effect = fake_prepare

        with (
            patch("nab_project._build.runner.NabBuildEnv", return_value=env),
            patch(
                "nab_project._build.runner.build.ProjectBuilder.from_isolated_env",
                return_value=project,
            ),
        ):
            metadata = run_build_backend(tmp_path, config=config)

        assert isinstance(metadata, WheelMetadata)
        assert metadata.name == "legacy-pkg"
        assert str(metadata.version) == "0.1"

    def test_malformed_pyproject(self, tmp_path: Path, config: ResolveInputs) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "not = valid = toml = [", encoding="utf-8"
        )
        with pytest.raises(BuildBackendError, match="could not read"):
            run_build_backend(tmp_path, config=config)

    def test_non_utf8_pyproject(self, tmp_path: Path, config: ResolveInputs) -> None:
        (tmp_path / "pyproject.toml").write_bytes(
            b"[build-system]\nrequires = []\n# \xe9\n"
        )
        with pytest.raises(BuildBackendError, match="could not read"):
            run_build_backend(tmp_path, config=config)

    def test_backend_metadata_missing_version_raises(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """A backend that returns a METADATA without ``Version`` triggers a
        clean :class:`BuildBackendError`, not an obscure parse traceback.
        """
        broken_backend = _FAKE_BACKEND_SRC.replace(
            'f"Version: {version}",',
            "# Version intentionally omitted to trigger the error path",
        )
        (tmp_path / "nab_test_backend.py").write_text(broken_backend, encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "nab_test_backend"\n'
            'backend-path = ["."]\n',
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="missing Name or Version"):
            run_build_backend(tmp_path, config=config)

    def test_hatchling_with_dynamic_deps_skips_prepare(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When a hatchling-based project has ``dynamic =
        ["dependencies"]``, the runner skips
        ``prepare_metadata_for_build_wheel`` and goes straight to
        ``build_wheel``, mirroring uv's documented quirk.

        The build_wheel hook returns a wheel with name "fake-dyn";
        if prepare had been used we would get "fake-pkg".  The skip
        gate is a runtime predicate, monkeypatched here to fire
        unconditionally so the fake backend does not need to live
        under the literal ``hatchling.`` namespace.
        """
        from nab_project._build import runner as runner_mod

        _write_fake_backend_project(
            tmp_path,
            extra_pyproject=(
                "\n[project]\n"
                'name = "fake-dyn"\n'
                'version = "9.9.9"\n'
                'dynamic = ["dependencies"]\n'
            ),
        )
        monkeypatch.setattr(runner_mod, "_should_skip_prepare", lambda *_a: True)
        metadata = run_build_backend(tmp_path, config=config)
        assert metadata.name == "fake-dyn"
        assert str(metadata.version) == "9.9.9"

    def test_build_env_setup_error_wrapped(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """BuildEnvError during env setup is wrapped as BuildBackendError."""
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n'
            'build-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        env = MagicMock()
        env.__enter__ = MagicMock(side_effect=BuildEnvError("sdist-only build dep"))
        env.__exit__ = MagicMock(return_value=None)
        with (
            patch("nab_project._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="build env setup"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_build_env_resolution_error_wrapped(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """ResolutionError from build deps is wrapped as BuildBackendError."""
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["does-not-exist"]\n'
            'build-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        env = MagicMock()
        env.__enter__ = MagicMock(
            side_effect=ResolutionError("no solution for build deps")
        )
        env.__exit__ = MagicMock(return_value=None)
        with (
            patch("nab_project._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="build env setup"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_build_system_table_rejected_wrapped(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """build.BuildSystemTableValidationError is wrapped as BuildBackendError.

        An unknown ``[build-system]`` key is build's rule rather than one
        nab reads, so the table passes nab's own checks and fails in build.
        """
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "setuptools.build_meta"\n'
            "unknown-key = 1\n",
            encoding="utf-8",
        )
        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)
        with (
            patch("nab_project._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="setuptools.build_meta"),
        ):
            run_build_backend(tmp_path, config=config)

    @pytest.mark.parametrize(
        ("table", "message"),
        [
            ('build-backend = "not-a-backend"\n', "requires is required by PEP 518"),
            ('requires = "hatchling"\n', "requires.*array of strings"),
            ('requires = ["hatchling", 42]\n', "requires.*array of strings"),
            ('requires = {a = "b"}\n', "requires.*array of strings"),
        ],
    )
    def test_bad_requires_fails_before_the_build_env(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
        table: str,
        message: str,
    ) -> None:
        """An absent or malformed ``requires`` fails before an env is opened."""
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n" + table, encoding="utf-8"
        )

        def _no_env(**_kwargs: object) -> NabBuildEnv:
            raise AssertionError("the build env must not be opened")

        monkeypatch.setattr(runner_mod, "NabBuildEnv", _no_env)
        with pytest.raises(BuildBackendError, match=message):
            run_build_backend(tmp_path, config=config)

    def test_build_system_not_a_table(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """A scalar build-system key fails the build."""
        (tmp_path / "pyproject.toml").write_text(
            'build-system = "hatchling.build"\n', encoding="utf-8"
        )
        with pytest.raises(BuildBackendError, match="must be a table"):
            run_build_backend(tmp_path, config=config)

    def test_backend_path_outside_source_tree_rejected(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """A backend-path that leaves the source tree fails the build."""
        source = tmp_path / "member"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "shared_backend"\n'
            'backend-path = ["../shared"]\n',
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="outside the source tree"):
            run_build_backend(source, config=config)

    def test_absolute_backend_path_rejected(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """backend-path entries are relative to the project root."""
        source = tmp_path / "member"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "shared_backend"\n'
            f'backend-path = ["{tmp_path.as_posix()}"]\n',
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="must be relative"):
            run_build_backend(source, config=config)

    def test_backend_path_to_sibling_sharing_a_prefix_accepted(
        self, tmp_path: Path
    ) -> None:
        """A sibling whose name starts with the source directory's is allowed.

        pyproject_hooks compares the two paths as strings and accepts
        this entry, so rejecting it here would refuse a tree other
        frontends build.
        """
        (tmp_path / "member-shared").mkdir()
        source = tmp_path / "member"
        source.mkdir()
        _validate_backend_path(source, ("../member-shared",))

    def test_venv_creation_oserror_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError building the venv is wrapped as BuildBackendError."""
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n'
            'build-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )

        class _Builder:
            def __init__(self, **_kw: object) -> None:
                pass

            def create(self, _path: Path) -> None:
                raise OSError(28, "No space left on device")

        import venv as venv_mod

        monkeypatch.setattr(venv_mod, "EnvBuilder", _Builder)
        with pytest.raises(BuildBackendError, match="build env setup"):
            run_build_backend(tmp_path, config=config)

    def test_temp_root_oserror_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError creating the env's temp root is wrapped as BuildBackendError."""
        _write_fake_backend_project(tmp_path)
        monkeypatch.setattr(
            env_mod.tempfile,
            "TemporaryDirectory",
            _tempdir_denying("nab-build-env-"),
        )
        with pytest.raises(
            BuildBackendError,
            match="build env setup.*could not create a temporary build directory",
        ):
            run_build_backend(tmp_path, config=config)

    def test_metadata_directory_oserror_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError creating the metadata directory is wrapped the same way."""
        _write_fake_backend_project(tmp_path)
        monkeypatch.setattr(
            runner_mod.tempfile,
            "TemporaryDirectory",
            _tempdir_denying("nab-build-meta-"),
        )
        with pytest.raises(
            BuildBackendError,
            match="temporary metadata directory for build backend 'nab_test_backend'",
        ):
            run_build_backend(tmp_path, config=config)

    def test_non_string_build_requirement_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-string build requirement is wrapped as BuildBackendError."""
        from nab_project._build import env as env_mod

        class _Builder:
            def __init__(self, **_kw: object) -> None:
                pass

            def create(self, path: Path) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "bin").mkdir(exist_ok=True)
                (path / "bin" / "python").touch()

        import venv as venv_mod

        monkeypatch.setattr(venv_mod, "EnvBuilder", _Builder)
        monkeypatch.setattr(
            env_mod, "_venv_scheme_paths", lambda _python: {"purelib": str(tmp_path)}
        )

        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )

        project = MagicMock()
        project.get_requires_for_build.return_value = ["setuptools", None]

        with (
            patch(
                "nab_project._build.runner.build.ProjectBuilder.from_isolated_env",
                return_value=project,
            ),
            pytest.raises(BuildBackendError, match="non-string"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_unencodable_build_requirement_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A build requirement with no UTF-8 encoding is wrapped.

        A backend that derives a requirement from a filesystem name passes
        on what ``os.fsdecode`` gave it, so a name that is not valid UTF-8
        comes back as a lone surrogate.
        """
        monkeypatch.setattr("venv.EnvBuilder", _StubEnvBuilder)
        monkeypatch.setattr(
            "nab_project._build.env._venv_scheme_paths",
            lambda _python: {"purelib": str(tmp_path)},
        )

        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )

        project = MagicMock()
        project.get_requires_for_build.return_value = [
            "setuptools",
            "pkg @ file:///vendor/p\udce9kg-1.0-py3-none-any.whl",
        ]

        with (
            patch(
                "nab_project._build.runner.build.ProjectBuilder.from_isolated_env",
                return_value=project,
            ),
            pytest.raises(BuildBackendError) as excinfo,
        ):
            run_build_backend(tmp_path, config=config)

        message = str(excinfo.value)
        assert "cannot be encoded as UTF-8" in message

        # The message carries the repr, so the surrogate appears escaped.
        assert r"pkg @ file:///vendor/p\udce9kg-1.0-py3-none-any.whl" in message


class TestShouldSkipPrepare:
    """Unit tests for the hatchling+dynamic-deps detection predicate."""

    def test_non_hatchling_backend_does_not_skip(self) -> None:
        from nab_project._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["dependencies"]}}
        assert _should_skip_prepare("setuptools.build_meta", data) is False
        assert _should_skip_prepare("flit_core.buildapi", data) is False

    def test_hatchling_without_dynamic_deps_does_not_skip(self) -> None:
        from nab_project._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["version"]}}
        assert _should_skip_prepare("hatchling.build", data) is False

    def test_hatchling_with_dynamic_deps_skips(self) -> None:
        from nab_project._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["version", "dependencies"]}}
        assert _should_skip_prepare("hatchling.build", data) is True

    def test_hatchling_with_dynamic_optional_dependencies_skips(self) -> None:
        from nab_project._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["optional-dependencies"]}}
        assert _should_skip_prepare("hatchling.build", data) is True

    def test_no_project_table_does_not_skip(self) -> None:
        from nab_project._build.runner import _should_skip_prepare

        assert _should_skip_prepare("hatchling.build", {}) is False

    def test_dynamic_not_a_list_does_not_skip(self) -> None:
        """``dynamic`` value of the wrong type is treated as no dynamic
        fields; the prepare hook is allowed to run."""
        from nab_project._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": "wrong-shape"}}
        assert _should_skip_prepare("hatchling.build", data) is False


class TestReadBuildSystem:
    """Defaults for absent ``[build-system]`` fields, errors for malformed ones."""

    def test_no_build_system_table_returns_defaults(self) -> None:
        """The defaults are spelled out here, so changing one fails this test."""
        from nab_project._build.runner import _read_build_system

        assert _read_build_system({}) == (
            "setuptools.build_meta:__legacy__",
            ("setuptools >= 40.8.0",),
            None,
        )

    @pytest.mark.parametrize("value", ["hatchling.build", ["setuptools>=61"], 1])
    def test_build_system_not_a_table_raises(self, value: object) -> None:
        """PEP 518 defines build-system as a table."""
        from nab_project._build.runner import _read_build_system

        with pytest.raises(BuildBackendError, match="must be a table"):
            _read_build_system({"build-system": value})

    def test_absent_optional_keys_take_their_defaults(self) -> None:
        """Only ``requires`` is mandatory; PEP 517 supplies the backend."""
        from nab_project._build.runner import _read_build_system

        assert _read_build_system({"build-system": {"requires": ["hatchling"]}}) == (
            "setuptools.build_meta:__legacy__",
            ("hatchling",),
            None,
        )

    def test_build_backend_wrong_type_raises(self) -> None:
        from nab_project._build.runner import _read_build_system

        with pytest.raises(BuildBackendError, match="build-backend.*must be a string"):
            _read_build_system(
                {"build-system": {"build-backend": 1234, "requires": []}}
            )

    def test_requires_absent_raises(self) -> None:
        """PEP 518 makes ``requires`` mandatory once the table is there."""
        from nab_project._build.runner import _read_build_system

        with pytest.raises(BuildBackendError, match="requires is required by PEP 518"):
            _read_build_system({"build-system": {"build-backend": "x"}})

    @pytest.mark.parametrize(
        "value", ["not-a-list", ["hatchling", 42], {"a": "b"}, 1234]
    )
    def test_requires_wrong_type_raises(self, value: object) -> None:
        from nab_project._build.runner import _read_build_system

        with pytest.raises(BuildBackendError, match="requires.*array of strings"):
            _read_build_system(
                {"build-system": {"build-backend": "x", "requires": value}}
            )

    def test_backend_path_returns_tuple_when_strings(self) -> None:
        from nab_project._build.runner import _read_build_system

        _, _, backend_path = _read_build_system(
            {
                "build-system": {
                    "build-backend": "x",
                    "requires": [],
                    "backend-path": ["src", "vendor"],
                }
            }
        )
        assert backend_path == ("src", "vendor")

    def test_backend_path_wrong_type_raises(self) -> None:
        from nab_project._build.runner import _read_build_system

        with pytest.raises(BuildBackendError, match="backend-path.*array of strings"):
            _read_build_system(
                {
                    "build-system": {
                        "build-backend": "x",
                        "requires": [],
                        "backend-path": "src",
                    }
                }
            )


class TestParseMetadata:
    """Direct unit tests for :func:`_parse_metadata` cover error paths
    that are awkward to reach through ``run_build_backend`` end-to-end.
    """

    def test_missing_metadata_file_raises(self, tmp_path: Path) -> None:
        from nab_project._build.runner import _parse_metadata

        with pytest.raises(BuildBackendError, match="no METADATA file"):
            _parse_metadata(tmp_path / "DOES-NOT-EXIST")

    def test_unreadable_metadata_reports_the_errno(
        self,
        tmp_path: Path,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        """A METADATA that cannot be read is a read failure, not an absent file."""
        from nab_project._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n", encoding="utf-8"
        )

        with (
            deny_access(path),
            pytest.raises(
                BuildBackendError, match="could not be read.*Permission denied"
            ),
        ):
            _parse_metadata(path)

    def test_non_utf8_metadata_raises_build_backend_error(self, tmp_path: Path) -> None:
        from nab_project._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_bytes(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nAuthor: café\n".encode(
                "latin-1"
            )
        )
        with pytest.raises(BuildBackendError, match="not valid UTF-8"):
            _parse_metadata(path)

    def test_invalid_version_raises(self, tmp_path: Path) -> None:
        from nab_project._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: not-a-version\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Version"):
            _parse_metadata(path)

    def test_oversized_version_raises(self, tmp_path: Path) -> None:
        """A release segment past the int-from-string limit is corrupt too."""
        from nab_project._build.runner import _parse_metadata

        oversized = "1" * (sys.get_int_max_str_digits() + 1)
        path = tmp_path / "METADATA"
        path.write_text(
            f"Metadata-Version: 2.1\nName: foo\nVersion: {oversized}\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Version"):
            _parse_metadata(path)

    def test_invalid_requires_python_raises(self, tmp_path: Path) -> None:
        from nab_project._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            "Requires-Python: not-a-specifier\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Requires-Python"):
            _parse_metadata(path)

    def test_unparseable_requires_dist_raises(self, tmp_path: Path) -> None:
        """A malformed Requires-Dist line is invalid metadata, so parsing raises."""
        from nab_project._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            "Requires-Dist: bad junk @@@\n"
            "Requires-Dist: click>=8\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Requires-Dist"):
            _parse_metadata(path)

    def test_over_nested_requires_dist_marker_raises(
        self, tmp_path: Path, over_nested_marker: str
    ) -> None:
        """A marker the parser cannot recurse through is malformed METADATA."""
        from nab_project._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            f"Requires-Dist: click ; {over_nested_marker}\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Requires-Dist"):
            _parse_metadata(path)

    def test_oversized_requires_python_raises(self, tmp_path: Path) -> None:
        """A specifier parses fine and only fails when something compares it."""
        from nab_project._build.runner import _parse_metadata

        oversized = "1" * (sys.get_int_max_str_digits() + 1)
        path = tmp_path / "METADATA"
        path.write_text(
            f"Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            f"Requires-Python: >={oversized}\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Requires-Python"):
            _parse_metadata(path)

    def test_oversized_requires_dist_version_raises(self, tmp_path: Path) -> None:
        """The same deferred conversion applies to a Requires-Dist specifier."""
        from nab_project._build.runner import _parse_metadata

        oversized = "1" * (sys.get_int_max_str_digits() + 1)
        path = tmp_path / "METADATA"
        path.write_text(
            f"Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            f"Requires-Dist: click>={oversized}\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Requires-Dist"):
            _parse_metadata(path)

    def test_provides_extra_whitespace_stripped(self, tmp_path: Path) -> None:
        """Surrounding whitespace on a Provides-Extra value is insignificant
        per RFC 822; canonicalize_name does not strip it, so strip first."""
        from nab_project._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            "Provides-Extra: security \nProvides-Extra: docs\t\n",
            encoding="utf-8",
        )
        meta = _parse_metadata(path)
        assert meta.provides_extra == ["docs", "security"]


class TestBuildWheelExtraction:
    """``_build_wheel_and_extract`` reads the dist-info directory the built
    wheel's own filename names, and refuses one that does not match.
    """

    def _builder(self, wheel_path: Path) -> build.ProjectBuilder:
        class _Builder:
            def build(self, _kind: str, _outdir: str) -> str:
                return str(wheel_path)

        return _Builder()  # type: ignore[return-value]

    def _metadata(self, name: str, version: str, requires: str) -> str:
        return (
            "Metadata-Version: 2.1\n"
            f"Name: {name}\n"
            f"Version: {version}\n"
            f"Requires-Dist: {requires}\n"
        )

    def test_wheel_without_dist_info_raises(self, tmp_path: Path) -> None:
        wheel_path = tmp_path / "fake-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr("loose-file.txt", "hi")

        with pytest.raises(BuildBackendError, match="no .dist-info"):
            _build_wheel_and_extract(self._builder(wheel_path), tmp_path)

    def test_leftover_dist_info_from_another_release_raises(
        self, tmp_path: Path
    ) -> None:
        """A stale ``<name>-<oldver>.dist-info/`` left in the source tree can
        get swept into the built wheel alongside the real one.
        """
        wheel_path = tmp_path / "bar-2.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr(
                "bar-1.9.dist-info/METADATA", self._metadata("bar", "1.9", "old-dep==1")
            )
            zf.writestr(
                "bar-2.0.dist-info/METADATA", self._metadata("bar", "2.0", "new-dep>=2")
            )
            zf.writestr("bar/__init__.py", "")

        with pytest.raises(BuildBackendError, match="multiple .dist-info"):
            _build_wheel_and_extract(self._builder(wheel_path), tmp_path)

    def test_dist_info_for_another_distribution_raises(self, tmp_path: Path) -> None:
        wheel_path = tmp_path / "bar-2.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr(
                "aaa-1.0.dist-info/METADATA", self._metadata("aaa", "1.0", "old-dep==1")
            )
            zf.writestr("bar/__init__.py", "")

        with pytest.raises(BuildBackendError, match="different distribution"):
            _build_wheel_and_extract(self._builder(wheel_path), tmp_path)

    def test_extracts_dist_info_named_by_filename(self, tmp_path: Path) -> None:
        """Members ahead of the dist-info do not hide it, and an escaped name
        (``zope.interface`` as ``zope_interface``) still matches.
        """
        wheel_path = tmp_path / "zope_interface-5.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr("zope/interface/__init__.py", "")
            zf.writestr(
                "zope_interface-5.0.dist-info/METADATA",
                self._metadata("zope.interface", "5.0", "new-dep>=2"),
            )
            zf.writestr("zope_interface-5.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = _build_wheel_and_extract(self._builder(wheel_path), out_dir)

        assert result == out_dir / "zope_interface-5.0.dist-info"
        assert (result / "WHEEL").is_file()
        assert "Version: 5.0" in (result / "METADATA").read_text(encoding="utf-8")


_UNREADABLE_DIST_INFO = "foo-1.0.dist-info"


def _wheel_zip(compression: int) -> bytes:
    """A two-member wheel archive with METADATA written first."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        zf.writestr(
            f"{_UNREADABLE_DIST_INFO}/METADATA",
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n" * 40,
        )
        zf.writestr(f"{_UNREADABLE_DIST_INFO}/WHEEL", "Wheel-Version: 1.0\n")
    return buf.getvalue()


def _blank_metadata_payload(data: bytes, keep: int) -> bytes:
    """Overwrite METADATA's compressed bytes, keeping the first ``keep`` of them.

    LZMA needs its 9-byte properties header kept, or the member fails as a bad
    CRC before the decompressor sees the corruption.
    """
    out = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo(f"{_UNREADABLE_DIST_INFO}/METADATA")

    # the local header is 30 bytes plus its own name and extra fields
    name_len, extra_len = struct.unpack_from("<HH", out, info.header_offset + 26)
    start = info.header_offset + 30 + name_len + extra_len

    out[start + keep : start + info.compress_size] = b"\xff" * (
        info.compress_size - keep
    )
    return bytes(out)


def _relabel_compression(data: bytes, method: int) -> bytes:
    """Set the first member's compression method in both of its headers."""
    out = bytearray(data)
    struct.pack_into("<H", out, out.index(b"PK\x03\x04") + 8, method)
    struct.pack_into("<H", out, out.index(b"PK\x01\x02") + 10, method)
    return bytes(out)


def _unreadable_metadata_wheel(kind: str) -> bytes:
    """A valid zip whose METADATA member cannot be decompressed."""
    if kind == "corrupt-deflate":
        return _blank_metadata_payload(_wheel_zip(zipfile.ZIP_DEFLATED), 0)
    if kind == "corrupt-lzma":
        return _blank_metadata_payload(_wheel_zip(zipfile.ZIP_LZMA), 9)

    # method 9 is deflate64, which zipfile has no decompressor for
    return _relabel_compression(_wheel_zip(zipfile.ZIP_STORED), 9)


class TestRunBuildBackendCorruptBuiltWheel:
    """A ``build_wheel`` hook that succeeds but emits an unreadable wheel must
    normalize to ``BuildBackendError``, whether the wheel is not a zip, its name
    will not parse, or its dist-info member cannot be decompressed. The read-back
    sits outside the hook-error wrapper on both the no-prepare-hook fallback and
    the runner's own skip path.
    """

    def _pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "nab_test_backend"\n'
            'backend-path = ["."]\n',
            encoding="utf-8",
        )

    def _mock_env(self) -> MagicMock:
        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)
        return env

    def _corrupt_building_project(
        self, wheel_name: str, data: bytes = b"not a zip"
    ) -> MagicMock:
        """A project whose ``build_wheel`` writes ``data`` and returns
        ``wheel_name``. ``prepare`` returns None, so the runner falls back to
        building a wheel, whose read-back hits the real ``parse_wheel_filename``
        and ``zipfile.ZipFile``.
        """
        project = MagicMock()
        project.get_requires_for_build.return_value = []
        project.prepare.return_value = None

        def fake_build(_dist: str, outdir: str, *_a: object, **_k: object) -> str:
            path = Path(outdir) / wheel_name
            path.write_bytes(data)
            return str(path)

        project.build.side_effect = fake_build
        return project

    def _run(self, tmp_path: Path, config: ResolveInputs, project: MagicMock) -> None:
        with (
            patch(
                "nab_project._build.runner.NabBuildEnv",
                return_value=self._mock_env(),
            ),
            patch(
                "nab_project._build.runner.build.ProjectBuilder.from_isolated_env",
                return_value=project,
            ),
            pytest.raises(BuildBackendError, match="unreadable wheel"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_default_path_corrupt_wheel_wrapped(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """The backend has no prepare hook, so the runner builds a wheel and
        reads it with ``zipfile.ZipFile`` after the hook wrapper; a corrupt
        wheel raises ``BadZipFile`` there, which the runner normalizes.
        """
        self._pyproject(tmp_path)
        self._run(
            tmp_path, config, self._corrupt_building_project("foo-1.0-py3-none-any.whl")
        )

    def test_invalid_wheel_name_wrapped(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """``parse_wheel_filename`` raises a bare ``ValueError`` when the built
        wheel's name does not parse; the runner normalizes it.
        """
        self._pyproject(tmp_path)
        self._run(tmp_path, config, self._corrupt_building_project("garbage.whl"))

    def test_skip_prepare_corrupt_wheel_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On the skip-prepare path the runner's own ``_build_wheel_and_extract``
        reads the built wheel; a corrupt wheel there normalizes as well.
        """
        self._pyproject(tmp_path)
        monkeypatch.setattr(runner_mod, "_should_skip_prepare", lambda *_a: True)
        self._run(
            tmp_path, config, self._corrupt_building_project("foo-1.0-py3-none-any.whl")
        )

    def test_skip_prepare_invalid_wheel_name_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The skip-prepare path parses the wheel filename too, so a readable
        zip under a name that does not parse is refused there as well.
        """
        self._pyproject(tmp_path)
        monkeypatch.setattr(runner_mod, "_should_skip_prepare", lambda *_a: True)
        self._run(
            tmp_path,
            config,
            self._corrupt_building_project(
                "garbage.whl", _wheel_zip(zipfile.ZIP_DEFLATED)
            ),
        )

    @pytest.mark.parametrize("skip_prepare", [False, True])
    @pytest.mark.parametrize(
        "kind", ["corrupt-deflate", "corrupt-lzma", "unsupported-method"]
    )
    def test_unreadable_dist_info_member_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
        skip_prepare: bool,
    ) -> None:
        """The wheel opens and lists cleanly, so the failure lands on the
        dist-info member's own read: ``zlib.error`` for a corrupt deflate
        payload, ``lzma.LZMAError`` for a corrupt LZMA one, and
        ``NotImplementedError`` for a method zipfile cannot decompress.
        """
        self._pyproject(tmp_path)
        monkeypatch.setattr(
            runner_mod, "_should_skip_prepare", lambda *_a: skip_prepare
        )
        self._run(
            tmp_path,
            config,
            self._corrupt_building_project(
                "foo-1.0-py3-none-any.whl", _unreadable_metadata_wheel(kind)
            ),
        )


class TestRunBuildBackendBuildTaggedWheel:
    """A wheel filename may carry a build tag, which never appears in the
    dist-info directory name.  Both metadata routes read METADATA out of the
    built wheel, so neither may derive the dist-info name from the filename.
    """

    _METADATA = (
        "Metadata-Version: 2.1\nName: probepkg\nVersion: 1.0\nRequires-Dist: attrs>=1\n"
    )

    def _pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "nab_test_backend"\n'
            'backend-path = ["."]\n',
            encoding="utf-8",
        )

    def _stub_hooks(self, monkeypatch: pytest.MonkeyPatch, wheel_name: str) -> None:
        """Answer the wheel hooks in-process; no backend subprocess runs.

        The backend implements ``build_wheel`` but not
        ``prepare_metadata_for_build_wheel``.
        """

        def _prepare(*_a: object, **_k: object) -> object:
            raise pyproject_hooks.HookMissing("prepare_metadata_for_build_wheel")

        def _build_wheel(
            _self: object,
            wheel_directory: str,
            config_settings: object = None,
            metadata_directory: object = None,
        ) -> str:
            with zipfile.ZipFile(Path(wheel_directory) / wheel_name, "w") as zf:
                zf.writestr("probepkg/__init__.py", "")
                zf.writestr("probepkg-1.0.dist-info/METADATA", self._METADATA)
                zf.writestr("probepkg-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
            return wheel_name

        monkeypatch.setattr(
            pyproject_hooks.BuildBackendHookCaller,
            "get_requires_for_build_wheel",
            lambda *_a, **_k: [],
        )
        monkeypatch.setattr(
            pyproject_hooks.BuildBackendHookCaller,
            "prepare_metadata_for_build_wheel",
            _prepare,
        )
        monkeypatch.setattr(
            pyproject_hooks.BuildBackendHookCaller, "build_wheel", _build_wheel
        )

    @pytest.mark.parametrize("skip_prepare", [False, True])
    @pytest.mark.parametrize(
        "wheel_name",
        [
            "probepkg-1.0-py3-none-any.whl",
            "probepkg-1.0-1-py3-none-any.whl",
            "probepkg-1.0-42abc-cp312-cp312-manylinux_2_17_x86_64.whl",
        ],
    )
    def test_build_tag_does_not_hide_the_dist_info(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
        wheel_name: str,
        skip_prepare: bool,
    ) -> None:
        self._pyproject(tmp_path)
        self._stub_hooks(monkeypatch, wheel_name)
        monkeypatch.setattr(
            runner_mod, "_should_skip_prepare", lambda *_a: skip_prepare
        )

        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)

        with patch("nab_project._build.runner.NabBuildEnv", return_value=env):
            metadata = run_build_backend(tmp_path, config=config)

        assert metadata.name == "probepkg"
        assert str(metadata.version) == "1.0"
        assert [str(req) for req in metadata.requires_dist] == ["attrs>=1"]


_HOOK_MISSING = object()


class TestRunBuildBackendNonStringHookPath:
    """PEP 517's path-returning hooks return the basename of what they wrote.
    ``build`` joins that onto the output directory outside its own hook-error
    wrapper, so a non-string basename arrives as a bare ``TypeError`` from
    ``os.path.join`` rather than a ``BuildBackendException``.
    """

    def _pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "nab_test_backend"\n'
            'backend-path = ["."]\n',
            encoding="utf-8",
        )

    def _stub_hooks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        prepare: object = _HOOK_MISSING,
        build_wheel: object = "foo-1.0-py3-none-any.whl",
    ) -> None:
        """Answer the wheel hooks in-process, so no backend subprocess runs.

        A ``prepare`` of ``_HOOK_MISSING`` raises ``HookMissing``, which is what
        sends the runner down its ``build_wheel`` fallback.
        """

        def _prepare(*_a: object, **_k: object) -> object:
            if prepare is _HOOK_MISSING:
                raise pyproject_hooks.HookMissing("prepare_metadata_for_build_wheel")
            return prepare

        monkeypatch.setattr(
            pyproject_hooks.BuildBackendHookCaller,
            "get_requires_for_build_wheel",
            lambda *_a, **_k: [],
        )
        monkeypatch.setattr(
            pyproject_hooks.BuildBackendHookCaller,
            "prepare_metadata_for_build_wheel",
            _prepare,
        )
        monkeypatch.setattr(
            pyproject_hooks.BuildBackendHookCaller,
            "build_wheel",
            lambda *_a, **_k: build_wheel,
        )

    def _run(self, tmp_path: Path, config: ResolveInputs) -> None:
        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)
        with (
            patch("nab_project._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="non-string path"),
        ):
            run_build_backend(tmp_path, config=config)

    @pytest.mark.parametrize("value", [None, 1, ["foo-1.0.dist-info"]])
    def test_prepare_hook_non_string_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
        value: object,
    ) -> None:
        self._pyproject(tmp_path)
        self._stub_hooks(monkeypatch, prepare=value)
        self._run(tmp_path, config)

    def test_build_wheel_fallback_non_string_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no prepare hook, the runner falls back to ``build_wheel``,
        whose return hits the same join.
        """
        self._pyproject(tmp_path)
        self._stub_hooks(monkeypatch, build_wheel=1)
        self._run(tmp_path, config)

    def test_skip_prepare_non_string_wrapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The skip-prepare path calls ``build_wheel`` from the runner itself."""
        self._pyproject(tmp_path)
        monkeypatch.setattr(runner_mod, "_should_skip_prepare", lambda *_a: True)
        self._stub_hooks(monkeypatch, build_wheel=None)
        self._run(tmp_path, config)


class TestRunBuildBackendDefaults:
    """End-to-end coverage for the ``[build-system]`` defaults branch
    plus the ``BuildBackendException`` re-raise.
    """

    def test_extra_build_requires_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-empty get_requires_for_build_wheel resolves and installs it.

        The requirement is resolved from a local ``file://`` index; the
        download step (HTTP-only, so it cannot fetch ``file://``) is
        stubbed to write the wheel locally, so the whole flow runs
        offline.
        """
        index_dir = tmp_path / "index"
        _make_local_index(index_dir, "buildstub", "1.0")
        config = ResolveInputs(indexes=(IndexConfig("local", index_dir.as_uri()),))

        def fake_download_lock(
            lock_input: LockInput, _transport: object, wheel_dir: Path, *_a: object
        ) -> DownloadResult:
            written = []
            for lock in lock_input.targets.values():
                for name, pin in lock.pins.items():
                    wheel = wheel_dir / f"{name}-{pin.version}-py3-none-any.whl"
                    _make_installable_wheel(wheel, name, pin.version)
                    written.append(wheel)
            return DownloadResult(written=tuple(written), skipped=())

        monkeypatch.setattr("nab_project._build.env.download_lock", fake_download_lock)
        backend_src = _FAKE_BACKEND_SRC.replace(
            "def get_requires_for_build_wheel(config_settings=None):\n    return []",
            "def get_requires_for_build_wheel(config_settings=None):\n"
            "    return ['buildstub']",
        )
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "nab_test_backend.py").write_text(backend_src, encoding="utf-8")
        (proj / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = []\n"
            'build-backend = "nab_test_backend"\n'
            'backend-path = ["."]\n',
            encoding="utf-8",
        )
        metadata = run_build_backend(proj, config=config)
        assert metadata.name == "fake-pkg"

    def test_backend_exception_remapped(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any ``build.BuildBackendException`` from the inner call is
        re-raised as our own ``BuildBackendError`` with a contextful
        message naming the backend.
        """
        import build as build_mod

        _write_fake_backend_project(tmp_path)

        class _Boom(build_mod.BuildBackendException):
            pass

        def _raise(*_a: object, **_k: object) -> None:
            raise _Boom(RuntimeError("hook crashed"))

        monkeypatch.setattr(build_mod.ProjectBuilder, "from_isolated_env", _raise)
        with pytest.raises(BuildBackendError, match="nab_test_backend"):
            run_build_backend(tmp_path, config=config)


class TestFastSchemeDictionaryDestination:
    """``_FastSchemeDictionaryDestination`` short-circuits empty-level
    bytecode compile so it skips the parent's filesystem path-resolve.
    """

    def _destination(self, levels: tuple[int, ...]) -> _FastSchemeDictionaryDestination:
        return _FastSchemeDictionaryDestination(
            scheme_dict={"purelib": "/tmp/none"},
            interpreter="/tmp/none/bin/python",
            script_kind="posix",
            bytecode_optimization_levels=levels,
            overwrite_existing=True,
        )

    def test_compile_bytecode_skips_when_levels_empty(self) -> None:
        destination = self._destination(())
        with patch.object(
            type(destination).__mro__[1],
            "_compile_bytecode",
        ) as parent:
            destination._compile_bytecode(Scheme("purelib"), MagicMock())
        parent.assert_not_called()

    def test_compile_bytecode_delegates_when_levels_nonempty(self) -> None:
        destination = self._destination((0,))
        with patch.object(
            type(destination).__mro__[1],
            "_compile_bytecode",
        ) as parent:
            destination._compile_bytecode(Scheme("purelib"), MagicMock())
        parent.assert_called_once()


class TestDeclaredInstallerFloor:
    """``_install_wheels`` passes ``overwrite_existing``, added in installer 1.0."""

    _LAST_RELEASE_WITHOUT_OVERWRITE_EXISTING = "0.7.0"

    def test_floor_excludes_releases_without_overwrite_existing(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = tomli.loads(pyproject.read_text(encoding="utf-8"))["project"][
            "dependencies"
        ]

        found = [
            requirement
            for requirement in map(Requirement, declared)
            if canonicalize_name(requirement.name) == "installer"
        ]
        assert len(found) == 1, f"expected one installer requirement, got {found}"

        assert not found[0].specifier.contains(
            self._LAST_RELEASE_WITHOUT_OVERWRITE_EXISTING
        )


class TestLauncherKind:
    """``_LAUNCHER_KIND`` names the launcher stub for the interpreter's architecture."""

    _MAXSIZE_64BIT = 2**63 - 1
    _MAXSIZE_32BIT = 2**31 - 1

    def _launcher_kind_on(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        os_name: str,
        platform: str,
        version: str,
        maxsize: int,
    ) -> str:
        """Return the ``_LAUNCHER_KIND`` a copy of ``env`` computes on a simulated host.

        Reloading ``env`` instead would leave the shared module holding the simulated
        value and rebind ``BuildEnvError`` out from under the tests that imported it.
        """
        probe_name = f"{env_mod.__name__}_launcher_probe"
        spec = spec_from_file_location(probe_name, env_mod.__file__)
        assert spec is not None
        assert spec.loader is not None

        probe = module_from_spec(spec)
        monkeypatch.setitem(sys.modules, probe_name, probe)

        monkeypatch.setattr(os, "name", os_name)
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(sys, "version", version)
        monkeypatch.setattr(sys, "maxsize", maxsize)

        spec.loader.exec_module(probe)
        return probe._LAUNCHER_KIND

    @pytest.mark.parametrize(
        ("version", "maxsize", "expected"),
        [
            ("3.13.5 [MSC v.1943 64 bit (AMD64)]", _MAXSIZE_64BIT, "win-amd64"),
            ("3.13.5 [MSC v.1943 64 bit (ARM64)]", _MAXSIZE_64BIT, "win-arm64"),
            ("3.13.5 [MSC v.1943 32 bit (Intel)]", _MAXSIZE_32BIT, "win-ia32"),
            ("3.13.5 [MSC v.1943 32 bit (ARM)]", _MAXSIZE_32BIT, "win-arm"),
        ],
        ids=["amd64", "arm64", "ia32", "arm"],
    )
    def test_windows_kind_names_the_architecture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        version: str,
        maxsize: int,
        expected: str,
    ) -> None:
        kind = self._launcher_kind_on(
            monkeypatch,
            os_name="nt",
            platform="win32",
            version=version,
            maxsize=maxsize,
        )
        assert kind == expected

    @pytest.mark.parametrize(
        "version",
        [
            "3.13.5 (main, Jun  1 2025, 09:00:00) [GCC 14.2.0]",
            "3.13.5 (tags/v3.13.5) [MSC v.1943 64 bit (Unknown)]",
        ],
        ids=["mingw", "msvc-unknown-architecture"],
    )
    def test_64bit_windows_kind_without_an_architecture_is_amd64(
        self, monkeypatch: pytest.MonkeyPatch, version: str
    ) -> None:
        """CPython leaves the architecture out of ``sys.version`` on two Windows builds.

        mingw-w64 gives ``[GCC ...]`` and an MSVC target that is neither x86-64 nor
        ARM64 gives ``64 bit (Unknown)``.  A 32-bit launcher would run emulated on
        both.
        """
        kind = self._launcher_kind_on(
            monkeypatch,
            os_name="nt",
            platform="win32",
            version=version,
            maxsize=self._MAXSIZE_64BIT,
        )
        assert kind == "win-amd64"

    def test_non_windows_kind_is_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kind = self._launcher_kind_on(
            monkeypatch,
            os_name="posix",
            platform="linux",
            version="3.13.5 [GCC 14.2.0]",
            maxsize=self._MAXSIZE_64BIT,
        )
        assert kind == "posix"


class TestNabBuildEnvOutsideContext:
    """Accessors and ``install`` raise when used outside ``with`` scope."""

    def _env(self) -> NabBuildEnv:
        return NabBuildEnv(requires=[], config=ResolveInputs())

    def test_python_executable_outside_context(self) -> None:
        env = self._env()
        with pytest.raises(BuildEnvError, match="outside its context-manager"):
            _ = env.python_executable

    def test_make_extra_environ_outside_context(self) -> None:
        env = self._env()
        with pytest.raises(BuildEnvError, match="outside its context-manager"):
            env.make_extra_environ()

    def test_install_outside_context(self) -> None:
        env = self._env()
        with pytest.raises(BuildEnvError, match="outside its context-manager"):
            env.install(["foo"])


class TestNabBuildEnvInstall:
    """The ``install`` shortcut: empty list and the inner re-resolve path."""

    def test_install_empty_returns_immediately(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty requirements list is a no-op (does not even touch
        the venv state).  We assert by checking the resolve helper is
        not called.
        """
        env = NabBuildEnv(requires=[], config=ResolveInputs())
        # Avoid real venv creation by short-circuiting __enter__.
        env._venv_path = tmp_path  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "python"  # type: ignore[attr-defined]
        called = MagicMock()
        monkeypatch.setattr(env, "_resolve_and_download", called)
        env.install([])
        called.assert_not_called()

    def test_install_runs_inner_resolve_and_installs_wheels(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The install shortcut: write a wheel to disk, mock the
        resolver so it "produces" that wheel, and verify ``installer``
        is invoked once per wheel.  Stays offline.
        """
        from installer.sources import WheelFile

        env = NabBuildEnv(requires=[], config=ResolveInputs())
        venv_path = tmp_path / "venv"
        venv_path.mkdir()
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        env._venv_path = venv_path  # type: ignore[attr-defined]
        env._python_executable = venv_path / "python"  # type: ignore[attr-defined]
        fake_wheel = wheel_dir / "_extra_0" / "fake-1.0-py3-none-any.whl"
        monkeypatch.setattr(
            env, "_resolve_and_download", lambda *_a, **_k: [fake_wheel]
        )
        monkeypatch.setattr(
            "nab_project._build.env._venv_scheme_paths",
            lambda _python: {
                "purelib": str(tmp_path / "site"),
                "headers": str(tmp_path / "include"),
            },
        )
        opened = MagicMock()
        opened.__enter__.return_value = MagicMock(distribution="fake")
        opened.__exit__.return_value = False
        monkeypatch.setattr(WheelFile, "open", lambda _path: opened)
        installer_calls: list[object] = []
        monkeypatch.setattr(
            "nab_project._build.env.installer_install",
            lambda **kwargs: installer_calls.append(kwargs),
        )
        env.install(["pip"])
        assert len(installer_calls) == 1

    def test_install_wraps_unlistable_wheel_dir(self, tmp_path: Path) -> None:
        """An OSError listing the wheel directory surfaces as BuildEnvError."""
        env = NabBuildEnv(requires=[], config=ResolveInputs())
        venv_path = tmp_path / "venv"
        venv_path.mkdir()
        env._venv_path = venv_path  # type: ignore[attr-defined]
        env._python_executable = venv_path / "python"  # type: ignore[attr-defined]

        with pytest.raises(
            BuildEnvError, match="could not install extra build requirements"
        ):
            env.install(["pip"])


class TestResolveAndDownload:
    """What the inner build-requires resolve is allowed to see and do."""

    @staticmethod
    def _capture_inner_inputs(
        env: NabBuildEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> ResolveInputs:
        """Run the inner resolve against stubs and return the settings it got."""
        from nab_project.resolve import ResolveResult, TargetResult

        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]
        target = ResolveTarget.for_host()
        fake_result = ResolveResult(
            targets=(target,),
            target_results=[
                TargetResult(
                    target=target,
                    success=True,
                    pins={},
                    lock=TargetLock(target=target, pins={}),
                )
            ],
        )
        captured: dict[str, object] = {}

        def fake_resolve(
            _path: Path, _transport: object, **kwargs: object
        ) -> ResolveResult:
            captured.update(kwargs)
            return fake_result

        monkeypatch.setattr("nab_project.resolve.resolve_for_targets", fake_resolve)
        monkeypatch.setattr(
            "nab_project._build.env.download_lock",
            lambda *_a, **_k: DownloadResult(written=(), skipped=()),
        )
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()

        env._resolve_and_download(wheel_dir)
        inner = captured["inputs"]
        assert isinstance(inner, ResolveInputs)
        return inner

    def test_inner_resolve_admits_both_artifact_kinds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Candidacy is not narrowed to wheels.

        Hiding the wheel-less versions from the resolver would settle
        on an older build backend than the requirement asked for and
        say nothing about it; whether the one it settles on can be
        installed is decided after the resolve, by ``_plan_install``.
        """
        env = NabBuildEnv(requires=["foo"], config=ResolveInputs())

        inner = self._capture_inner_inputs(env, tmp_path, monkeypatch)

        assert inner.dist_policy is DistPolicy.WHEEL_OR_SDIST

    def test_inner_resolve_reads_metadata_statically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The build env's own resolve never invokes a backend.

        A build the depth budget does not count is one it cannot bound,
        so an inherited override may keep its refusal and loses its
        permission.
        """
        env = NabBuildEnv(
            requires=["foo"],
            config=ResolveInputs(
                build_policy=BuildPolicy.BUILD_REMOTE,
                package_overrides=(
                    pkg_override("foo", build_policy=BuildPolicy.BUILD_REMOTE),
                    pkg_override("bar", build_policy=BuildPolicy.NEVER),
                ),
                index_overrides={
                    "pypi": IndexOverride(build_policy=BuildPolicy.BUILD_REMOTE)
                },
            ),
        )

        inner = self._capture_inner_inputs(env, tmp_path, monkeypatch)

        assert inner.build_policy is BuildPolicy.NEVER
        assert [o.build_policy for o in inner.package_overrides] == [
            None,
            BuildPolicy.NEVER,
        ]
        assert inner.index_overrides["pypi"].build_policy is None

    def test_inner_resolve_runs_for_the_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inner resolve gets no Python override.

        The venv is built from the host interpreter, so a target
        Python would pick wheels for another ABI and evaluate the
        build requirements' markers against the wrong interpreter.
        """
        from nab_project.lockfile import TargetLock
        from nab_project.resolve import ResolveResult, TargetResult
        from nab_provider.target import ResolveTarget

        env = NabBuildEnv(requires=["foo"], config=ResolveInputs())
        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]
        target = ResolveTarget.for_host()
        fake_result = ResolveResult(
            targets=(target,),
            target_results=[
                TargetResult(
                    target=target,
                    success=True,
                    pins={},
                    lock=TargetLock(target=target, pins={}),
                )
            ],
        )
        captured: dict[str, object] = {}

        def fake_resolve(
            _path: Path, _transport: object, **kwargs: object
        ) -> ResolveResult:
            captured.update(kwargs)
            return fake_result

        monkeypatch.setattr("nab_project.resolve.resolve_for_targets", fake_resolve)
        monkeypatch.setattr(
            "nab_project._build.env.download_lock",
            lambda *_a, **_k: DownloadResult(written=(), skipped=()),
        )
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()

        assert env._resolve_and_download(wheel_dir) == []
        assert "python_version" not in captured

    def test_inner_resolve_takes_indexes_cutoff_order_and_overrides_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inner settings forward the indexes, cutoff, order and overrides.

        Build deps come from the configured indexes alone, so the outer
        run's constraints, dist policy and local sources do not reach it.
        """
        cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
        index = IndexConfig("internal", "https://example.invalid/simple/")
        package_override = pkg_override("hatchling", uploaded_prior_to=cutoff)
        index_override = IndexOverride(uploaded_prior_to=cutoff)
        env = NabBuildEnv(
            requires=["hatchling"],
            config=ResolveInputs(
                indexes=(index,),
                uploaded_prior_to=cutoff,
                decision_order=DecisionOrder.STABLE,
                package_overrides=(package_override,),
                index_overrides={"internal": index_override},
                constraints=("setuptools<70",),
                dist_policy=DistPolicy.SDIST_ONLY,
                local_sources=(LocalSource("plugin", str(tmp_path / "plugin")),),
            ),
        )

        inner = self._capture_inner_inputs(env, tmp_path, monkeypatch)

        assert inner.indexes == (index,)
        assert inner.uploaded_prior_to == cutoff
        assert inner.decision_order is DecisionOrder.STABLE
        assert inner.package_overrides == (package_override,)
        assert inner.index_overrides == {"internal": index_override}

        assert inner.constraints == ()
        assert inner.dist_policy is DistPolicy.WHEEL_OR_SDIST
        assert inner.local_sources == ()

    def test_inner_resolve_keeps_the_default_decision_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project that did not ask for ``stable`` does not get it inside."""
        env = NabBuildEnv(requires=["hatchling"], config=ResolveInputs())

        inner = self._capture_inner_inputs(env, tmp_path, monkeypatch)

        assert inner.decision_order is DecisionOrder.ARRIVAL

    def test_url_build_requirement_wrapped(self, tmp_path: Path) -> None:
        """A direct-URL build requirement the inner resolve refuses is
        wrapped as BuildEnvError, so the outer resolve skips the
        unbuildable sdist instead of aborting on the raw
        UnsupportedVcsError.
        """
        env = NabBuildEnv(
            requires=["plugin @ https://example.com/plugin-1.0.tar.gz"],
            config=ResolveInputs(),
        )
        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        with pytest.raises(BuildEnvError, match="build env resolve"):
            env._resolve_and_download(wheel_dir)

    def test_control_char_build_requirement_wrapped(self, tmp_path: Path) -> None:
        """A control character makes a build requirement invalid PEP 508, so
        _resolve_and_download raises BuildEnvError rather than a raw error.
        """
        env = NabBuildEnv(
            requires=["setuptools\n>=61"],
            config=ResolveInputs(),
        )
        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        with pytest.raises(BuildEnvError, match="build env resolve"):
            env._resolve_and_download(wheel_dir)

    def test_download_error_wrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DownloadError fetching a build-dep wheel is wrapped as
        BuildEnvError, so the outer resolve skips the unbuildable sdist
        instead of aborting on the raw DownloadError.
        """
        from nab_project.lockfile import TargetLock
        from nab_project.resolve import ResolveResult, TargetResult
        from nab_provider.target import ResolveTarget

        env = NabBuildEnv(requires=["foo"], config=ResolveInputs())
        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]
        target = ResolveTarget.for_host()
        fake_result = ResolveResult(
            targets=(target,),
            target_results=[
                TargetResult(
                    target=target,
                    success=True,
                    pins={},
                    lock=TargetLock(target=target, pins={}),
                )
            ],
        )
        monkeypatch.setattr(
            "nab_project.resolve.resolve_for_targets", lambda *_a, **_k: fake_result
        )

        def _boom(*_a: object, **_k: object) -> DownloadResult:
            msg = "foo==1.0: failed to fetch foo-1.0-py3-none-any.whl: GET x 404"
            raise DownloadError(msg)

        monkeypatch.setattr("nab_project._build.env.download_lock", _boom)
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        with pytest.raises(BuildEnvError, match="build env download"):
            env._resolve_and_download(wheel_dir)


def _no_network(*_a: object, **_k: object) -> object:
    """Stand in for anything an offline build env must not call."""
    raise AssertionError("offline must not reach the network")


def _raise_missing_extra(*_a: object, **_k: object) -> object:
    """Stand in for an inner resolve whose build dep lacks the named extra."""
    msg = "dummyreq==1.0 does not provide extra 'nope'"
    raise MissingExtraError(msg)


class _StubEnvBuilder:
    """Stand in for ``venv.EnvBuilder``: makes the directory, runs nothing."""

    def __init__(self, **_kw: object) -> None:
        pass

    def create(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)


class _PathsepRefusingEnvBuilder:
    """Stand in for ``venv.EnvBuilder`` on 3.11+: refuses an ``os.pathsep`` path."""

    def __init__(self, **_kw: object) -> None:
        pass

    def create(self, path: Path) -> None:
        msg = (
            f"Refusing to create a venv in {path} because it contains"
            f" the PATH separator {os.pathsep}."
        )
        raise ValueError(msg)


class TestBuildEnvMissingExtra:
    """A build requirement naming an undeclared extra fails the build env."""

    def test_wrapped_as_build_env_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "nab_project.resolve.resolve_for_targets", _raise_missing_extra
        )

        env = NabBuildEnv(requires=["dummyreq[nope]"], config=ResolveInputs())
        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()

        with pytest.raises(
            BuildEnvError,
            match=r"build env resolve failed: dummyreq==1\.0 does not provide"
            r" extra 'nope'",
        ):
            env._resolve_and_download(wheel_dir)

    def test_run_build_backend_names_the_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("venv.EnvBuilder", _StubEnvBuilder)
        monkeypatch.setattr(
            "nab_project._build.env._venv_scheme_paths",
            lambda _python: {"purelib": str(tmp_path)},
        )
        monkeypatch.setattr(
            "nab_project.resolve.resolve_for_targets", _raise_missing_extra
        )

        source = tmp_path / "src"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["dummyreq[nope]"]\n'
            'build-backend = "dummyreq.backend"\n',
            encoding="utf-8",
        )

        with pytest.raises(
            BuildBackendError,
            match=r"build env setup for 'dummyreq\.backend' failed: build env"
            r" resolve failed: dummyreq==1\.0 does not provide extra 'nope'",
        ):
            run_build_backend(source, config=ResolveInputs())


class TestBuildEnvOffline:
    """``offline`` bars the build env from fetching its requirements."""

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nab_project.resolve.resolve_for_targets", _no_network)
        monkeypatch.setattr("nab_project._build.env.download_lock", _no_network)

    def _offline_env(
        self, tmp_path: Path, requires: list[str]
    ) -> tuple[NabBuildEnv, Path]:
        """An offline env over ``requires``, and the wheel dir to fill."""
        env = NabBuildEnv(
            requires=requires,
            config=ResolveInputs(),
            offline=True,
            transport_factory=_no_network,  # type: ignore[arg-type]
        )
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        return env, wheel_dir

    def test_refuses_before_the_inner_resolve(self, tmp_path: Path) -> None:
        env, wheel_dir = self._offline_env(tmp_path, ["foo"])

        with pytest.raises(BuildEnvError, match=r"unavailable in offline mode: foo"):
            env._resolve_and_download(wheel_dir)

    def test_backend_with_build_requirements_is_refused(self, tmp_path: Path) -> None:
        source = _write_fake_backend_project(tmp_path)
        (source / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "nab_test_backend"\n'
            'backend-path = ["."]\n',
            encoding="utf-8",
        )
        with pytest.raises(
            BuildBackendError, match=r"unavailable in offline mode: hatchling"
        ):
            run_build_backend(source, config=ResolveInputs(), offline=True)

    def test_legacy_setup_py_refusal_names_both_defaults(self, tmp_path: Path) -> None:
        """The refusal names the default backend and the default requirement."""
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\nsetup()\n", encoding="utf-8"
        )

        with pytest.raises(
            BuildBackendError,
            match=r"build env setup for 'setuptools\.build_meta:__legacy__' failed:"
            r" build requirements unavailable in offline mode:"
            r" setuptools >= 40\.8\.0$",
        ):
            run_build_backend(tmp_path, config=ResolveInputs(), offline=True)

    def test_backend_with_no_build_requirements_still_builds(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """Nothing to fetch, so the offline run is served."""
        source = _write_fake_backend_project(tmp_path)
        metadata = run_build_backend(source, config=config, offline=True)
        assert metadata.name == "fake-pkg"

    def test_backend_whose_requirements_are_all_excluded_still_builds(
        self, tmp_path: Path, config: ResolveInputs
    ) -> None:
        """The one build requirement is marker-excluded, so nothing is fetched."""
        source = _write_fake_backend_project(tmp_path)
        (source / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = ['tomli; python_version < \"3.0\"']\n"
            'build-backend = "nab_test_backend"\n'
            'backend-path = ["."]\n',
            encoding="utf-8",
        )

        metadata = run_build_backend(source, config=config, offline=True)

        assert metadata.name == "fake-pkg"

    def test_marker_excluded_requirements_need_no_fetch(self, tmp_path: Path) -> None:
        """The host's markers exclude every entry, so nothing has to be fetched."""
        env, wheel_dir = self._offline_env(tmp_path, ['tomli; python_version < "3.0"'])

        assert env._resolve_and_download(wheel_dir) == []

    def test_refusal_names_only_the_requirements_that_apply(
        self, tmp_path: Path
    ) -> None:
        """A partly excluded list still refuses, naming what has to be fetched."""
        env, wheel_dir = self._offline_env(
            tmp_path, ['tomli; python_version < "3.0"', "hatchling"]
        )

        with pytest.raises(
            BuildEnvError, match=r"unavailable in offline mode: hatchling$"
        ):
            env._resolve_and_download(wheel_dir)

    def test_unparseable_requirement_is_refused(self, tmp_path: Path) -> None:
        """A string that is not PEP 508 is not an exclusion."""
        env, wheel_dir = self._offline_env(tmp_path, ["not a requirement!!"])

        with pytest.raises(
            BuildEnvError, match=r"unavailable in offline mode: not a requirement!!$"
        ):
            env._resolve_and_download(wheel_dir)

    def test_undecidable_marker_is_refused(self, tmp_path: Path) -> None:
        """A marker nothing decides is not an exclusion."""
        env, wheel_dir = self._offline_env(
            tmp_path, ['foo; python_full_version ~= "3"']
        )

        with pytest.raises(BuildEnvError, match=r"unavailable in offline mode: foo;"):
            env._resolve_and_download(wheel_dir)

    def test_hook_extras_named_in_a_stable_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal names the wheel hook's extras in sorted order.

        ``build.ProjectBuilder.get_requires_for_build`` returns a ``set``, so
        the order the extras arrive in varies with the hash seed. Both orders
        must give the one message.
        """
        monkeypatch.setattr("venv.EnvBuilder", _StubEnvBuilder)
        monkeypatch.setattr(
            "nab_project._build.env._venv_scheme_paths",
            lambda _python: {"purelib": str(tmp_path)},
        )

        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )

        messages = set()
        for arrival in (
            ["wheel", "setuptools>=61", "cython"],
            ["cython", "wheel", "setuptools>=61"],
        ):
            project = MagicMock()
            project.get_requires_for_build.return_value = arrival
            with (
                patch(
                    "nab_project._build.runner.build.ProjectBuilder.from_isolated_env",
                    return_value=project,
                ),
                pytest.raises(BuildBackendError) as excinfo,
            ):
                run_build_backend(tmp_path, config=ResolveInputs(), offline=True)
            messages.add(str(excinfo.value))

        assert messages == {
            (
                "build env setup for 'setuptools.build_meta' failed: build"
                " requirements unavailable in offline mode:"
                " cython, setuptools>=61, wheel"
            )
        }


class TestResolveAndDownloadSiblingWheels:
    """``_resolve_and_download`` narrows a pin to the one wheel PEP 425
    prefers, so the build venv never gets two wheels of a version.
    """

    MANYLINUX2014 = (
        "demo-1.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    )
    MANYLINUX1 = "demo-1.0-cp312-cp312-manylinux_2_5_x86_64.manylinux1_x86_64.whl"
    PURE = "demo-1.0-py3-none-any.whl"
    WINDOWS = "demo-1.0-cp312-cp312-win_amd64.whl"
    MACOS = "demo-1.0-cp312-cp312-macosx_11_0_arm64.whl"
    SDIST = "demo-1.0.tar.gz"

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        filenames: list[str],
        *,
        with_sdist: bool = False,
    ) -> tuple[list[str], list[Path]]:
        """Resolve a pin carrying ``filenames`` for a cp312 linux target.

        Returns the filenames handed to the downloader and the wheel
        paths the caller gets back.
        """
        sdist = (
            SdistArtifact(
                filename=self.SDIST,
                url=f"https://pypi.example/{self.SDIST}",
                hashes=(("sha256", "1" * 64),),
            )
            if with_sdist
            else None
        )
        pin = IndexPin(
            name="demo",
            version="1.0",
            index="https://pypi.org/simple/",
            sdist=sdist,
            wheels=tuple(
                WheelArtifact(
                    filename=name,
                    url=f"https://pypi.example/{name}",
                    hashes=(("sha256", "0" * 64),),
                )
                for name in filenames
            ),
        )

        target = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec(platform_id="linux_x86_64")
        )
        fake_result = ResolveResult(
            targets=(target,),
            target_results=[
                TargetResult(
                    target=target,
                    success=True,
                    pins={"demo": Version("1.0")},
                    lock=TargetLock(target=target, pins={"demo": pin}),
                )
            ],
        )
        monkeypatch.setattr(
            "nab_project.resolve.resolve_for_targets", lambda *_a, **_k: fake_result
        )

        requested: list[str] = []

        def _fake_download(
            lock_input: LockInput, _transport: object, output_dir: Path
        ) -> DownloadResult:
            written = []
            for entry in iter_artifacts(lock_input):
                requested.append(entry.filename)
                written.append(output_dir / entry.filename)
            return DownloadResult(written=tuple(written), skipped=())

        monkeypatch.setattr("nab_project._build.env.download_lock", _fake_download)

        env = NabBuildEnv(requires=["demo"], config=ResolveInputs())
        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]

        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()

        return requested, env._resolve_and_download(wheel_dir)

    @pytest.mark.parametrize(
        "filenames",
        [
            [MANYLINUX2014, MANYLINUX1],
            [MANYLINUX1, MANYLINUX2014],
            [PURE, MANYLINUX2014],
        ],
        ids=["preferred-first", "preferred-last", "pure-and-platform"],
    )
    def test_only_preferred_wheel_is_fetched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        filenames: list[str],
    ) -> None:
        """The preferred wheel wins whatever order the pin lists them in."""
        requested, wheels = self._run(tmp_path, monkeypatch, filenames)
        assert requested == [self.MANYLINUX2014]
        assert [p.name for p in wheels] == [self.MANYLINUX2014]

    def test_single_wheel_passes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requested, wheels = self._run(tmp_path, monkeypatch, [self.MANYLINUX1])
        assert requested == [self.MANYLINUX1]
        assert [p.name for p in wheels] == [self.MANYLINUX1]

    def test_sdist_goes_with_the_narrowed_wheels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sdist of a pin installed from a wheel is not even fetched.

        Nothing installs it, and a corrupt one would fail the download
        and take a build with it that the wheel alone would satisfy.
        """
        requested, wheels = self._run(
            tmp_path,
            monkeypatch,
            [self.MANYLINUX2014, self.MANYLINUX1],
            with_sdist=True,
        )
        assert requested == [self.MANYLINUX2014]
        assert [p.name for p in wheels] == [self.MANYLINUX2014]

    @pytest.mark.parametrize(
        "filenames",
        [[WINDOWS], [WINDOWS, MACOS]],
        ids=["one", "several"],
    )
    def test_no_wheel_the_host_can_install_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        filenames: list[str],
    ) -> None:
        """A pin the host cannot install is an error, not an empty venv.

        The provider's tag filter drops those wheels before a pin is
        built, so this holds a cross-module invariant rather than a
        case a real resolve reaches; hence the hand-built lock input.
        With no sdist there is not even a build to refuse.
        """
        with pytest.raises(BuildEnvError, match="no sdist to build"):
            self._run(tmp_path, monkeypatch, filenames)


class TestBuildEnvInstallsOnePreferredWheel:
    """The venv ends up with one wheel per build dependency, and with the
    one the host's tags rank highest.

    Real resolve, download and install against a ``file://`` index, since
    the wheels overwrite each other file by file and only the finished
    venv says which one's code is importable.
    """

    @staticmethod
    def _index(root: Path, order: tuple[str, ...]) -> None:
        """Serve demo 1.0 as two wheels the host accepts, in ``order``.

        One carries the host's most specific tag and one its most
        generic, and each records in its ``__init__`` which it is.
        """
        tags = ResolveTarget.for_host().tags
        by_mark = {"specific": tags.ordered[0], "generic": tags.ordered[-1]}
        pkg = root / "demo"
        pkg.mkdir(parents=True)
        links = []
        for mark in order:
            tag = by_mark[mark]
            filename = f"demo-1.0-{tag.interpreter}-{tag.abi}-{tag.platform}.whl"
            path = pkg / filename
            _make_installable_wheel(
                path,
                "demo",
                "1.0",
                package_files={"__init__.py": f"MARK = {mark!r}\n".encode()},
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            links.append(f'<a href="{filename}#sha256={digest}">{filename}</a>')
        (pkg / "index.html").write_text(
            "<!DOCTYPE html><html><body>" + "\n".join(links) + "</body></html>",
            encoding="utf-8",
        )

    @pytest.mark.parametrize(
        "order",
        [("specific", "generic"), ("generic", "specific")],
        ids=["preferred-first", "preferred-last"],
    )
    def test_the_preferred_wheel_is_the_installed_one(
        self, tmp_path: Path, order: tuple[str, ...]
    ) -> None:
        """The listing order does not decide which wheel's code wins."""
        index_dir = tmp_path / "index"
        self._index(index_dir, order)
        config = ResolveInputs(indexes=(IndexConfig("local", index_dir.as_uri()),))

        with NabBuildEnv(requires=["demo"], config=config) as env:
            venv_path = env._venv_path
            assert venv_path is not None
            installed = [
                p.read_text(encoding="utf-8")
                for p in venv_path.rglob("demo/__init__.py")
            ]
            dist_infos = list(venv_path.rglob("demo-1.0.dist-info"))

        assert installed == ["MARK = 'specific'\n"]
        assert len(dist_infos) == 1


class TestInstallPickIsNotTheMetadataPick:
    """The build env narrows with ``TagSet.pick``, not with ``pick_dist``.

    Both go through the one PEP 425 ranking, and they differ only in
    what they fall back to when the ranking does not decide.  Swapping
    one for the other is a behaviour change, so these pin the three
    inputs that separate them.
    """

    TIE_PLAIN = "demo-1.0-py2.py3-none-any.whl"
    TIE_SIDECAR = "demo-1.0-py3-none-any.whl"
    WINDOWS = "demo-1.0-cp312-cp312-win_amd64.whl"
    MACOS = "demo-1.0-cp312-cp312-macosx_11_0_arm64.whl"
    SDIST = "demo-1.0.tar.gz"
    SDIST_ZIP = "demo-1.0.zip"

    @staticmethod
    def _tags() -> TagSet:
        return ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec(platform_id="linux_x86_64")
        ).tags

    @staticmethod
    def _listed_sdist(filename: str) -> SdistFile:
        return SdistFile(
            filename=filename,
            url=f"https://pypi.example/{filename}",
            version="1.0",
            requires_python=None,
            upload_time=None,
        )

    @staticmethod
    def _listed(filename: str, *, has_metadata: bool) -> WheelFile:
        return WheelFile(
            filename=filename,
            url=f"https://pypi.example/{filename}",
            version="1.0",
            requires_python=None,
            has_metadata=has_metadata,
            upload_time=None,
        )

    @classmethod
    def _pin(cls, filenames: list[str]) -> IndexPin:
        return IndexPin(
            name="demo",
            version="1.0",
            index="https://pypi.org/simple/",
            wheels=tuple(
                WheelArtifact(
                    filename=name,
                    url=f"https://pypi.example/{name}",
                    hashes=(("sha256", "0" * 64),),
                )
                for name in filenames
            ),
        )

    @staticmethod
    def _env(depth: int = 0) -> NabBuildEnv:
        return NabBuildEnv(
            requires=[], config=ResolveInputs(build_requires_depth=depth)
        )

    @classmethod
    def _sdist_pin(cls) -> IndexPin:
        return IndexPin(
            name="demo",
            version="1.0",
            index="https://pypi.org/simple/",
            sdist=SdistArtifact(
                filename=cls.SDIST,
                url=f"https://pypi.example/{cls.SDIST}",
                hashes=(("sha256", "1" * 64),),
            ),
        )

    def test_tag_tie_goes_to_the_sidecar_only_for_metadata(self) -> None:
        """A tie breaks on the sidecar for metadata and on order for install.

        The lock records no ``has_metadata``, so the install side
        cannot express that tie-break even if it wanted to.
        """
        tags = self._tags()
        listed = [
            self._listed(self.TIE_PLAIN, has_metadata=False),
            self._listed(self.TIE_SIDECAR, has_metadata=True),
        ]
        assert tags.wheel_rank(self.TIE_PLAIN) == tags.wheel_rank(self.TIE_SIDECAR)
        assert pick_dist(listed, tags).filename == self.TIE_SIDECAR

        pin = self._env()._planned_pin(
            self._pin([self.TIE_PLAIN, self.TIE_SIDECAR]), tags, []
        )
        assert isinstance(pin, IndexPin)
        assert [w.filename for w in pin.wheels] == [self.TIE_PLAIN]

    def test_metadata_may_come_from_a_wheel_the_host_cannot_install(self) -> None:
        """Two off-target wheels, since ``pick_dist`` falls back only
        when the tags rank nothing at all.

        The install side has no such fallback, and with no sdist to
        build there is nothing left to try.
        """
        tags = self._tags()
        listed = [
            self._listed(self.WINDOWS, has_metadata=True),
            self._listed(self.MACOS, has_metadata=False),
        ]
        picked = pick_dist(listed, tags)
        assert picked.filename == self.WINDOWS
        assert not tags.accepts(picked.filename)

        with pytest.raises(BuildEnvError, match="no sdist to build"):
            self._env()._planned_pin(self._pin([self.WINDOWS, self.MACOS]), tags, [])

    def test_metadata_may_come_from_an_sdist(self) -> None:
        """A version publishing no wheel is read from its sdist.

        Two sdists, because ``pick_dist`` hands back a lone dist
        without ranking anything.  Installing that version means
        building the sdist, which the default depth refuses.
        """
        tags = self._tags()
        listed = [self._listed_sdist(self.SDIST), self._listed_sdist(self.SDIST_ZIP)]
        assert pick_dist(listed, tags) is listed[0]

        with pytest.raises(BuildEnvError, match="build-requires-depth is 0"):
            self._env()._planned_pin(self._sdist_pin(), tags, [])

    def test_sdist_pin_is_queued_for_a_build_when_the_depth_allows(self) -> None:
        """With budget the sdist stays, the wheels go, and a build is queued."""
        to_build: list[_PendingBuild] = []
        planned = self._env(depth=1)._planned_pin(
            self._sdist_pin(), self._tags(), to_build
        )

        assert isinstance(planned, IndexPin)
        assert planned.wheels == ()
        assert planned.sdist is not None
        assert [pending.label for pending in to_build] == ["demo 1.0"]

    @pytest.mark.parametrize("depth", [1, 3], ids=["budget-spent", "budget-left"])
    def test_a_pin_already_on_the_chain_is_a_cycle(self, depth: int) -> None:
        """A build requirement that needs itself built is reported as a cycle.

        At depth 1 the chain has also spent the budget, and the cycle
        is still what the message says: raising the depth to walk into
        a loop is the one piece of advice that cannot help.
        """
        env = NabBuildEnv(
            requires=[],
            config=ResolveInputs(build_requires_depth=depth),
            chain=("demo 1.0",),
        )

        with pytest.raises(BuildEnvError, match="cyclic build requirement: demo 1.0"):
            env._planned_pin(self._sdist_pin(), self._tags(), [])

    def test_a_pin_that_is_not_from_an_index_passes_through(self) -> None:
        """A local pin carries no artifacts to choose between."""
        pin = LocalPin(name="demo", version="1.0", path="/src/demo")

        assert self._env()._planned_pin(pin, self._tags(), []) is pin

    def test_a_refusal_names_the_builds_that_led_to_it(self) -> None:
        """Nested refusals are unreadable without the path that reached them."""
        env = NabBuildEnv(
            requires=[], config=ResolveInputs(), chain=("meson 1.4.2", "ninja 1.11")
        )

        with pytest.raises(BuildEnvError, match=r"chain: meson 1\.4\.2 -> ninja 1\.11"):
            env._planned_pin(self._sdist_pin(), self._tags(), [])


class TestRefusalNamesTheDistPolicyThatBarsTheWheels:
    """Why a pin has no wheel decides what the refusal is allowed to say.

    A per-package or per-index ``dist-policy`` governs the build env's
    own resolve, so ``sdist-only`` and ``sdist-install`` leave a pin
    carrying its sdist alone even where the index publishes a wheel this
    host installs.  Blaming the index there sends the reader after a
    wheel that is not missing.
    """

    NAME = "demo"
    VERSION = "1.0"
    INDEX_NAME = "local"
    INDEX_URL = "https://pypi.example/simple/"
    CREDENTIALED_URL = "https://user:token@pypi.example/simple/"
    OTHER_INDEX_NAME = "mirror"
    OTHER_INDEX_URL = "https://mirror.example/simple/"

    @staticmethod
    def _tags() -> TagSet:
        return ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec(platform_id="linux_x86_64")
        ).tags

    @classmethod
    def _pin(cls) -> IndexPin:
        """A pin the resolve settled on carrying its sdist alone."""
        filename = f"{cls.NAME}-{cls.VERSION}.tar.gz"
        return IndexPin(
            name=cls.NAME,
            version=cls.VERSION,
            index=cls.INDEX_URL,
            sdist=SdistArtifact(
                filename=filename,
                url=f"https://pypi.example/{filename}",
                hashes=(("sha256", "1" * 64),),
            ),
        )

    def _refusal(self, **fields: Any) -> str:
        """Return the message the env refuses ``_pin`` with.

        ``fields`` are the config fields the case sets.  The pin is
        served by ``INDEX_NAME`` unless a case declares its own
        ``indexes``.
        """
        fields.setdefault("indexes", (IndexConfig(self.INDEX_NAME, self.INDEX_URL),))
        config = ResolveInputs(**fields)
        env = NabBuildEnv(requires=[], config=config)

        with pytest.raises(BuildEnvError) as excinfo:
            env._planned_pin(self._pin(), self._tags(), [])
        return str(excinfo.value)

    @pytest.mark.parametrize(
        "policy",
        [DistPolicy.SDIST_ONLY, DistPolicy.SDIST_INSTALL],
        ids=["sdist-only", "sdist-install"],
    )
    def test_a_package_override_is_named_as_the_cause(self, policy: DistPolicy) -> None:
        """The key the user set is what the message points at."""
        message = self._refusal(
            package_overrides=(pkg_override(self.NAME, dist_policy=policy),)
        )

        assert (
            f"demo 1.0 has dist-policy '{policy.value}', which admits"
            " no wheel into the build env" in message
        )
        assert "publishes no wheel" not in message

        assert "build-requires-depth is 0" in message

    def test_an_index_override_is_named_as_the_cause(self) -> None:
        """The per-index surface reaches the build env the same way."""
        message = self._refusal(
            index_overrides={
                self.INDEX_NAME: IndexOverride(dist_policy=DistPolicy.SDIST_ONLY)
            }
        )

        assert "demo 1.0 has dist-policy 'sdist-only'" in message

    def test_an_index_override_reaches_a_pin_from_a_credentialed_index(self) -> None:
        """A pin records its index URL stripped of the credentials config has."""
        message = self._refusal(
            indexes=(IndexConfig(self.INDEX_NAME, self.CREDENTIALED_URL),),
            index_overrides={
                self.INDEX_NAME: IndexOverride(dist_policy=DistPolicy.SDIST_ONLY)
            },
        )

        assert "demo 1.0 has dist-policy 'sdist-only'" in message

    def test_indexes_differing_only_in_credentials_are_not_named(self) -> None:
        """Both strip to the pin's URL, so which one served it is unknown."""
        message = self._refusal(
            indexes=(
                IndexConfig(self.INDEX_NAME, self.INDEX_URL),
                IndexConfig(self.OTHER_INDEX_NAME, self.CREDENTIALED_URL),
            ),
            index_overrides={
                self.OTHER_INDEX_NAME: IndexOverride(dist_policy=DistPolicy.SDIST_ONLY)
            },
        )

        assert "demo 1.0 publishes no wheel this build host can install" in message
        assert "dist-policy" not in message

    def test_an_index_override_on_another_index_is_not_named(self) -> None:
        """Only the index that served the pin can have barred its wheels."""
        message = self._refusal(
            indexes=(
                IndexConfig(self.INDEX_NAME, self.INDEX_URL),
                IndexConfig(self.OTHER_INDEX_NAME, self.OTHER_INDEX_URL),
            ),
            index_overrides={
                self.OTHER_INDEX_NAME: IndexOverride(dist_policy=DistPolicy.SDIST_ONLY)
            },
        )

        assert "demo 1.0 publishes no wheel this build host can install" in message
        assert "dist-policy" not in message

    @pytest.mark.parametrize(
        "override",
        [
            pkg_override("other", dist_policy=DistPolicy.SDIST_ONLY),
            pkg_override("demo>2", dist_policy=DistPolicy.SDIST_ONLY),
            pkg_override("demo", dist_policy=DistPolicy.PREFER_WHEEL),
        ],
        ids=["another-package", "outside-the-range", "admits-wheels"],
    )
    def test_an_override_that_did_not_bar_the_wheels_is_not_named(
        self, override: PackageOverride
    ) -> None:
        """The name, the range, and the policy value all have to match."""
        message = self._refusal(package_overrides=(override,))

        assert "demo 1.0 publishes no wheel this build host can install" in message
        assert "dist-policy" not in message


class TestDistPolicyExcludesAWheelTheHostCanInstall:
    """``sdist-only`` on a build requirement, over a real inner resolve.

    The ``file://`` index publishes a wheel this host installs beside
    the sdist.  The first test pins that the env installs that wheel,
    so the refusal in the second can only be the override's doing.
    Only the download is stubbed; nothing here reaches the network.
    """

    NAME = "buildstub"
    VERSION = "1.0"
    WHEEL = "buildstub-1.0-py3-none-any.whl"

    def _config(self, tmp_path: Path, **fields: Any) -> ResolveInputs:
        """Config over an index serving a wheel beside a PEP 643 sdist."""
        index_dir = tmp_path / "index"
        _make_local_index(index_dir, self.NAME, self.VERSION)
        return ResolveInputs(
            indexes=(IndexConfig("local", index_dir.as_uri()),), **fields
        )

    @staticmethod
    def _planned_pins(monkeypatch: pytest.MonkeyPatch) -> list[PinShape]:
        """Return a list that fills with the pins the plan sends to download."""
        planned: list[PinShape] = []

        def fake_download_lock(
            lock_input: LockInput, _transport: object, _wheel_dir: Path, *_a: object
        ) -> DownloadResult:
            for lock in lock_input.targets.values():
                planned.extend(lock.pins.values())
            return DownloadResult(written=(), skipped=())

        monkeypatch.setattr("nab_project._build.env.download_lock", fake_download_lock)
        return planned

    def _resolve(self, config: ResolveInputs, tmp_path: Path) -> None:
        """Run the inner resolve and its install plan, nothing further."""
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        env = NabBuildEnv(requires=[self.NAME], config=config)
        env._resolve_and_download(wheel_dir)

    def test_the_published_wheel_is_what_the_env_installs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no override the plan narrows the pin to that wheel."""
        planned = self._planned_pins(monkeypatch)

        self._resolve(self._config(tmp_path), tmp_path)

        pin = planned[0]
        assert isinstance(pin, IndexPin)
        assert [wheel.filename for wheel in pin.wheels] == [self.WHEEL]
        assert pin.sdist is None

    def test_the_refusal_names_dist_policy_and_not_the_index(
        self, tmp_path: Path
    ) -> None:
        """The same wheel is published; the override is why it cannot be used."""
        config = self._config(
            tmp_path,
            package_overrides=(
                pkg_override(self.NAME, dist_policy=DistPolicy.SDIST_ONLY),
            ),
        )

        with pytest.raises(BuildEnvError) as excinfo:
            self._resolve(config, tmp_path)

        message = str(excinfo.value)
        assert f"{self.NAME} {self.VERSION} has dist-policy 'sdist-only'" in message
        assert "publishes no wheel" not in message


class TestDistPolicyOverAPackageThatPublishesNoWheel:
    """``sdist-only`` where the index had no wheel to bar.

    A pin arrives carrying its sdist alone whether the policy rejected
    a wheel or the index never served one, and the env cannot tell
    those apart.  So the clause says what the policy admits, not that
    the package has wheels somewhere.
    """

    NAME = "buildstub"
    VERSION = "1.0"

    def _refusal(self, tmp_path: Path) -> str:
        """Return the message an index-wide ``sdist-only`` refuses with."""
        index_dir = tmp_path / "index"
        _make_local_index(index_dir, self.NAME, self.VERSION, sdist_only=True)
        config = ResolveInputs(
            indexes=(IndexConfig("local", index_dir.as_uri()),),
            index_overrides={"local": IndexOverride(dist_policy=DistPolicy.SDIST_ONLY)},
        )
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        env = NabBuildEnv(requires=[self.NAME], config=config)

        with pytest.raises(BuildEnvError) as excinfo:
            env._resolve_and_download(wheel_dir)
        return str(excinfo.value)

    def test_the_refusal_does_not_say_the_package_has_wheels(
        self, tmp_path: Path
    ) -> None:
        """The policy is named; the package's own wheels are not claimed."""
        message = self._refusal(tmp_path)

        assert "its wheels" not in message
        assert (
            f"{self.NAME} {self.VERSION} has dist-policy 'sdist-only', which"
            " admits no wheel into the build env" in message
        )


class TestBuildRequirementNeedingItsOwnBuild:
    """A build requirement published as an sdist alone.

    The env installs wheels, so satisfying one of these means building
    it, which is what ``[tool.nab].build-requires-depth`` governs.  The
    index is a local ``file://`` tree and the download step is stubbed
    to copy from it, so the whole flow runs without network.
    """

    NAME = "buildstub"
    VERSION = "1.0"

    def _config(self, tmp_path: Path, depth: int) -> ResolveInputs:
        index_dir = tmp_path / "index"
        _make_local_index(index_dir, self.NAME, self.VERSION, sdist_only=True)
        return ResolveInputs(
            indexes=(IndexConfig("local", index_dir.as_uri()),),
            build_requires_depth=depth,
        )

    @staticmethod
    def _stub_download(monkeypatch: pytest.MonkeyPatch, index_dir: Path) -> None:
        """Copy from the local index in place of the HTTP download.

        ``download_lock`` speaks HTTP, so it cannot fetch the ``file://``
        index these tests serve.  Extraction is swapped at the same time
        for a fixture unpacker that does not need the tar data filter.
        """

        def fake_download_lock(
            lock_input: LockInput, _transport: object, wheel_dir: Path, *_a: object
        ) -> DownloadResult:
            written = []
            for lock in lock_input.targets.values():
                for name, pin in lock.pins.items():
                    assert isinstance(pin, IndexPin)
                    assert pin.sdist is not None
                    dest = wheel_dir / pin.sdist.filename
                    dest.write_bytes(
                        (index_dir / name / pin.sdist.filename).read_bytes()
                    )
                    written.append(dest)
            return DownloadResult(written=tuple(written), skipped=())

        monkeypatch.setattr("nab_project._build.env.download_lock", fake_download_lock)
        monkeypatch.setattr(
            "nab_project._build.env.extract_sdist_archive", _unpack_fixture_sdist
        )

    def test_built_and_installed_when_the_depth_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At depth 1 the sdist is built and the wheel lands in the venv."""
        config = self._config(tmp_path, depth=1)
        self._stub_download(monkeypatch, tmp_path / "index")

        with NabBuildEnv(requires=[self.NAME], config=config) as env:
            installed = [str(root / rel) for root, rel in env._installed_files]

        assert any(self.NAME in path for path in installed)

    def test_refused_at_the_default_depth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At depth 0 the sdist is not a candidate, and the error says why."""
        config = self._config(tmp_path, depth=0)
        self._stub_download(monkeypatch, tmp_path / "index")

        env = NabBuildEnv(requires=[self.NAME], config=config)
        with pytest.raises(BuildEnvError, match="build-requires-depth is"):
            env.__enter__()

    def test_the_nested_env_carries_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env a nested build opens knows what it is nested inside."""
        config = self._config(tmp_path, depth=1)
        self._stub_download(monkeypatch, tmp_path / "index")
        chains: list[tuple[str, ...]] = []
        original = NabBuildEnv.__init__

        def record(self: NabBuildEnv, *args: object, **kwargs: object) -> None:
            original(self, *args, **kwargs)  # type: ignore[arg-type]
            chains.append(self._chain)

        monkeypatch.setattr(NabBuildEnv, "__init__", record)

        with NabBuildEnv(requires=[self.NAME], config=config):
            pass

        assert chains == [(), (f"{self.NAME} {self.VERSION}",)]


class TestBuildRequirementTwoLevelsDown:
    """A build requirement whose own build requirement needs building.

    ``buildstub`` publishes an sdist alone and build-requires
    ``deepstub``, which publishes an sdist alone too.  Satisfying the
    first is one nested env, satisfying the second is a second one, so
    the pair pins that the budget is spent as the chain grows rather
    than reread at every level.
    """

    OUTER = "buildstub"
    INNER = "deepstub"
    VERSION = "1.0"

    def _config(self, tmp_path: Path, depth: int) -> ResolveInputs:
        index_dir = tmp_path / "index"
        _make_local_index(
            index_dir, self.OUTER, self.VERSION, sdist_only=True, requires=(self.INNER,)
        )
        _make_local_index(index_dir, self.INNER, self.VERSION, sdist_only=True)
        return ResolveInputs(
            indexes=(IndexConfig("local", index_dir.as_uri()),),
            build_requires_depth=depth,
        )

    def test_refused_one_level_short(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Depth 1 pays for the outer build and has nothing left for the inner."""
        config = self._config(tmp_path, depth=1)
        TestBuildRequirementNeedingItsOwnBuild._stub_download(
            monkeypatch, tmp_path / "index"
        )

        env = NabBuildEnv(requires=[self.OUTER], config=config)
        with pytest.raises(BuildEnvError) as excinfo:
            env.__enter__()

        message = str(excinfo.value)
        assert self.INNER in message
        assert f"chain: {self.OUTER} {self.VERSION}" in message

    def test_built_when_the_depth_reaches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Depth 2 pays for both, and the outer wheel lands in the venv."""
        config = self._config(tmp_path, depth=2)
        TestBuildRequirementNeedingItsOwnBuild._stub_download(
            monkeypatch, tmp_path / "index"
        )

        with NabBuildEnv(requires=[self.OUTER], config=config) as env:
            installed = [str(root / rel) for root, rel in env._installed_files]

        assert any(self.OUTER in path for path in installed)


class TestBuildRequirementBuildFailures:
    """``_build_requirement`` wraps every way the nested build can fail.

    Each surfaces as ``BuildEnvError`` naming the requirement, so the
    outer resolve skips the sdist it was building rather than aborting
    on a raw archive or backend error.
    """

    PENDING = _PendingBuild(
        name="demo",
        version="1.0",
        sdist=SdistArtifact(
            filename="demo-1.0.tar.gz",
            url="https://pypi.example/demo-1.0.tar.gz",
            hashes=(("sha256", "1" * 64),),
        ),
    )

    @staticmethod
    def _env() -> NabBuildEnv:
        return NabBuildEnv(requires=[], config=ResolveInputs(build_requires_depth=1))

    def test_missing_archive(self, tmp_path: Path) -> None:
        """The download step promised a file that is not there."""
        with pytest.raises(BuildEnvError, match="could not be read"):
            self._env()._build_requirement(self.PENDING, tmp_path)

    def test_unextractable_archive(self, tmp_path: Path) -> None:
        """A downloaded sdist that is not an archive at all."""
        (tmp_path / self.PENDING.sdist.filename).write_bytes(b"not a tarball")

        with pytest.raises(BuildEnvError, match="could not be extracted"):
            self._env()._build_requirement(self.PENDING, tmp_path)

    def test_unwritable_temp_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The directory the nested build unpacks into cannot be created."""
        (tmp_path / self.PENDING.sdist.filename).write_bytes(b"sdist")
        monkeypatch.setattr(
            env_mod.tempfile,
            "TemporaryDirectory",
            _tempdir_denying("nab-build-req-"),
        )

        with pytest.raises(
            BuildEnvError, match="could not create a temporary build directory"
        ):
            self._env()._build_requirement(self.PENDING, tmp_path)

    def test_backend_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sdist extracts but names a backend that cannot be imported."""
        archive = tmp_path / self.PENDING.sdist.filename
        members = {
            "PKG-INFO": "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n",
            "pyproject.toml": (
                "[build-system]\nrequires = []\n"
                'build-backend = "demo_missing_backend"\nbackend-path = ["."]\n'
            ),
        }
        with tarfile.open(archive, "w:gz") as tf:
            for member, text in members.items():
                data = text.encode()
                info = tarfile.TarInfo(f"demo-1.0/{member}")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        monkeypatch.setattr(
            "nab_project._build.env.extract_sdist_archive", _unpack_fixture_sdist
        )

        with pytest.raises(BuildEnvError, match="could not be built"):
            self._env()._build_requirement(self.PENDING, tmp_path)

    @pytest.mark.parametrize("cancel", [False, True], ids=["success", "cancel"])
    def test_cleanup_error_does_not_replace_result_or_cancellation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        cancel: bool,
    ) -> None:
        (tmp_path / self.PENDING.sdist.filename).write_bytes(b"sdist")
        wheel = tmp_path / "demo-1.0-py3-none-any.whl"

        monkeypatch.setattr(
            env_mod.tempfile,
            "TemporaryDirectory",
            _CleanupErrorTemporaryDirectory,
        )
        monkeypatch.setattr(
            env_mod, "extract_sdist_archive", lambda _data, target: target
        )

        build = MagicMock(
            return_value=wheel,
            side_effect=KeyboardInterrupt if cancel else None,
        )
        monkeypatch.setattr(runner_mod, "build_wheel_for_install", build)

        if cancel:
            with pytest.raises(KeyboardInterrupt):
                self._env()._build_requirement(self.PENDING, tmp_path)
        else:
            assert self._env()._build_requirement(self.PENDING, tmp_path) == wheel

    def test_non_string_wheel_path(self, tmp_path: Path) -> None:
        """A backend returning something other than a wheel's basename.

        ``build`` joins the hook's result onto the output directory
        without checking it, so the ``TypeError`` is nab's to report.
        """
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        (source_dir / "nab_test_backend.py").write_text(
            "def get_requires_for_build_wheel(config_settings=None):\n"
            "    return []\n\n"
            "def build_wheel(wheel_directory, config_settings=None,"
            " metadata_directory=None):\n"
            "    return 42\n",
            encoding="utf-8",
        )
        (source_dir / "pyproject.toml").write_text(
            "[build-system]\nrequires = []\n"
            'build-backend = "nab_test_backend"\nbackend-path = ["."]\n',
            encoding="utf-8",
        )

        with pytest.raises(BuildBackendError, match="non-string path"):
            build_wheel_for_install(
                source_dir, output_dir=tmp_path / "out", config=ResolveInputs()
            )


class TestBuiltWheelIdentity:
    """``_build_requirement`` refuses a wheel naming another release.

    The backend picks the name and version it emits, and the env's
    other wheels were resolved for the release the pin names.
    """

    PENDING = _PendingBuild(
        name="build-stub",
        version="1.0",
        sdist=SdistArtifact(
            filename="build_stub-1.0.tar.gz",
            url="https://pypi.example/build_stub-1.0.tar.gz",
            hashes=(("sha256", "1" * 64),),
        ),
    )

    def _built(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        filename: str,
        pending: _PendingBuild | None = None,
    ) -> Path:
        """Run ``_build_requirement`` over a build emitting ``filename``.

        ``pending`` defaults to :attr:`PENDING`, whose name is already
        canonical.
        """
        pending = pending or self.PENDING
        (tmp_path / pending.sdist.filename).write_bytes(b"sdist")

        monkeypatch.setattr(
            env_mod, "extract_sdist_archive", lambda _data, target: target
        )
        monkeypatch.setattr(
            runner_mod,
            "build_wheel_for_install",
            MagicMock(return_value=tmp_path / filename),
        )
        env = NabBuildEnv(requires=[], config=ResolveInputs(build_requires_depth=1))

        return env._build_requirement(pending, tmp_path)

    def test_another_version_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backend computing a version other than the one pinned."""
        with pytest.raises(BuildEnvError, match=r"names build-stub==0\.0\.0"):
            self._built(monkeypatch, tmp_path, "build_stub-0.0.0-py3-none-any.whl")

    def test_another_project_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wheel for another project, leaving the requirement unmet."""
        with pytest.raises(BuildEnvError, match=r"names otherpkg==9\.9"):
            self._built(monkeypatch, tmp_path, "otherpkg-9.9-py3-none-any.whl")

    def test_output_that_is_not_a_wheel_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``build`` joins the hook's result on without checking it."""
        with pytest.raises(BuildEnvError, match="not a wheel filename"):
            self._built(monkeypatch, tmp_path, "dist.zip")

    @pytest.mark.parametrize(
        ("pinned", "filename"),
        [
            ("build-stub", "build_stub-1.0-py3-none-any.whl"),
            ("Build.Stub", "build_stub-1.0-py3-none-any.whl"),
            ("build-stub", "build_stub-1.0.0-py3-none-any.whl"),
        ],
        ids=["as-pinned", "spelled-differently", "same-release"],
    )
    def test_the_pinned_release_is_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pinned: str,
        filename: str,
    ) -> None:
        """The comparison is by canonical name and PEP 440 version.

        A wheel filename escapes the project name, and ``1.0`` and
        ``1.0.0`` are one release.
        """
        pending = replace(self.PENDING, name=pinned)

        assert self._built(monkeypatch, tmp_path, filename, pending).name == filename


class _CleanupErrorTemporaryDirectory:
    """Raise ``PermissionError`` unless cleanup errors are ignored."""

    def __init__(self, *, prefix: str, ignore_cleanup_errors: bool = False) -> None:
        self.name = f"/tmp/{prefix}in-use"
        self._ignore_cleanup_errors = ignore_cleanup_errors

    def cleanup(self) -> None:
        if not self._ignore_cleanup_errors:
            raise PermissionError("build env is still in use")

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_args: object) -> None:
        self.cleanup()


def _build_env_with_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> NabBuildEnv:
    """Return an unprovisioned build env whose temp tree cannot be removed."""
    monkeypatch.setattr(
        env_mod.tempfile,
        "TemporaryDirectory",
        _CleanupErrorTemporaryDirectory,
    )
    monkeypatch.setattr(NabBuildEnv, "_provision", lambda self, _root: None)
    return NabBuildEnv(requires=[], config=ResolveInputs())


class TestNabBuildEnvLifecycle:
    """Edge cases of the context-manager lifecycle that fall outside
    the happy-path runner tests.
    """

    def test_exit_without_enter_is_a_noop(self) -> None:
        """``__exit__`` on a never-entered env is a quiet no-op so
        helpers that call ``with`` against an env construction failure
        do not double-fault.
        """
        env = NabBuildEnv(requires=[], config=ResolveInputs())
        env.__exit__(None, None, None)
        assert env._tmpdir is None  # type: ignore[attr-defined]

    def test_exit_ignores_cleanup_error_after_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _build_env_with_cleanup_error(monkeypatch):
            pass

    def test_exit_preserves_cancellation_when_cleanup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with (
            pytest.raises(KeyboardInterrupt),
            _build_env_with_cleanup_error(monkeypatch),
        ):
            raise KeyboardInterrupt

    @pytest.mark.parametrize("cancel", [False, True], ids=["success", "cancel"])
    def test_metadata_cleanup_error_does_not_replace_result_or_cancellation(
        self, monkeypatch: pytest.MonkeyPatch, *, cancel: bool
    ) -> None:
        metadata = MagicMock()
        prepared = MagicMock()
        prepared.__enter__.return_value = (MagicMock(), "backend")
        prepared.__exit__.return_value = False
        monkeypatch.setattr(
            runner_mod.tempfile,
            "TemporaryDirectory",
            _CleanupErrorTemporaryDirectory,
        )
        monkeypatch.setattr(runner_mod, "_read_pyproject", lambda _source: {})
        monkeypatch.setattr(
            runner_mod,
            "_prepared_project",
            lambda *_args, **_kwargs: prepared,
        )

        extract = MagicMock(
            return_value=Path("/tmp/metadata"),
            side_effect=KeyboardInterrupt if cancel else None,
        )
        monkeypatch.setattr(runner_mod, "_extract_metadata_dir", extract)
        monkeypatch.setattr(runner_mod, "_parse_metadata", lambda _path: metadata)

        if cancel:
            with pytest.raises(KeyboardInterrupt):
                run_build_backend(Path("/tmp/source"), config=ResolveInputs())
        else:
            assert (
                run_build_backend(Path("/tmp/source"), config=ResolveInputs())
                is metadata
            )

    def test_render_synthetic_pyproject_empty_requires(self) -> None:
        """The synthetic-pyproject helper renders an empty
        ``dependencies = []`` block when called with no requires.
        """
        from nab_project._build.env import _render_synthetic_pyproject

        text = _render_synthetic_pyproject([])
        assert "dependencies = []" in text

    def test_render_synthetic_pyproject_escapes_control_chars(self) -> None:
        """Control characters, quotes, and backslashes in a requirement
        round-trip through the synthetic pyproject as valid TOML.
        """
        import tomli

        from nab_project._build.env import _render_synthetic_pyproject

        requires = ["a\nb", 'c"d\\e', "f\x00g", "h\x7fi", "plain>=1"]
        rendered = _render_synthetic_pyproject(requires)
        assert tomli.loads(rendered)["project"]["dependencies"] == requires


class TestNabBuildEnvEnterInstall:
    """Coverage for the inner install path inside ``__enter__``.

    Real venv creation + wheel install is too expensive for a unit
    test; we mock the moving pieces so the loop still runs end to
    end and we can verify ``installer.install`` is called for each
    resolved wheel.
    """

    def test_enter_installs_resolved_wheels(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from installer.sources import WheelFile

        from nab_project._build import env as env_mod

        # Stub venv builder so no Python interpreter is spawned.
        class _Builder:
            def __init__(self, **_kw: object) -> None:
                pass

            def create(self, path: Path) -> None:
                path.mkdir(parents=True, exist_ok=True)
                # Pretend the interpreter exists.
                (path / "bin").mkdir(exist_ok=True)
                (path / "bin" / "python").touch()

        import venv as venv_mod

        monkeypatch.setattr(venv_mod, "EnvBuilder", _Builder)
        monkeypatch.setattr(
            env_mod,
            "_venv_scheme_paths",
            lambda _python: {
                "purelib": str(tmp_path),
                "headers": str(tmp_path / "include"),
            },
        )
        fake_wheel = tmp_path / "fake-1.0-py3-none-any.whl"
        fake_wheel.touch()
        monkeypatch.setattr(
            NabBuildEnv,
            "_resolve_and_download",
            lambda self, _wd: [fake_wheel],
        )
        opened = MagicMock()
        opened.__enter__.return_value = MagicMock(distribution="fake")
        opened.__exit__.return_value = False
        monkeypatch.setattr(WheelFile, "open", lambda _path: opened)
        installer_calls: list[object] = []
        monkeypatch.setattr(
            env_mod,
            "installer_install",
            lambda **kwargs: installer_calls.append(kwargs),
        )
        with NabBuildEnv(requires=["pip"], config=ResolveInputs()):
            pass
        assert len(installer_calls) == 1

    def test_enter_cleans_up_on_resolve_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the inner resolve raises during ``__enter__``, the temp
        directory is cleaned up and the exception propagates so the
        caller sees the real cause.
        """
        from nab_project._build import env as env_mod

        class _Builder:
            def __init__(self, **_kw: object) -> None:
                pass

            def create(self, path: Path) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "bin").mkdir(exist_ok=True)
                (path / "bin" / "python").touch()

        import venv as venv_mod

        monkeypatch.setattr(venv_mod, "EnvBuilder", _Builder)
        monkeypatch.setattr(
            env_mod, "_venv_scheme_paths", lambda _python: {"purelib": str(tmp_path)}
        )

        def _boom(self: NabBuildEnv, _wd: Path) -> list[Path]:
            msg = "resolve failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(NabBuildEnv, "_resolve_and_download", _boom)
        env = NabBuildEnv(requires=["pip"], config=ResolveInputs())
        with pytest.raises(RuntimeError, match="resolve failed"):
            env.__enter__()
        assert env._tmpdir is None  # type: ignore[attr-defined]

    def test_enter_wraps_venv_create_oserror(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError from ``venv.EnvBuilder.create`` is wrapped as
        BuildEnvError, and the temp directory is still cleaned up.
        """

        class _Builder:
            def __init__(self, **_kw: object) -> None:
                pass

            def create(self, _path: Path) -> None:
                raise OSError(28, "No space left on device")

        import venv as venv_mod

        monkeypatch.setattr(venv_mod, "EnvBuilder", _Builder)
        env = NabBuildEnv(requires=[], config=ResolveInputs())
        with pytest.raises(BuildEnvError, match="build venv"):
            env.__enter__()
        assert env._tmpdir is None  # type: ignore[attr-defined]

    def test_enter_wraps_venv_create_valueerror(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ValueError from ``venv.EnvBuilder.create`` is wrapped as
        BuildEnvError, and the temp directory is still cleaned up.

        From 3.11 ``venv`` refuses an env path containing ``os.pathsep``,
        which is where a TMPDIR named with one puts it.
        """
        monkeypatch.setattr("venv.EnvBuilder", _PathsepRefusingEnvBuilder)
        env = NabBuildEnv(requires=[], config=ResolveInputs())
        with pytest.raises(BuildEnvError, match="build venv"):
            env.__enter__()
        assert env._tmpdir is None  # type: ignore[attr-defined]

    def test_enter_wraps_inner_project_write_oserror(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError writing the inner resolve's project raises BuildEnvError.

        A full filesystem fails on a write, not on the ``mkdtemp`` that
        made the empty temp root.
        """

        class _Builder:
            def __init__(self, **_kw: object) -> None:
                pass

            def create(self, path: Path) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "bin").mkdir(exist_ok=True)
                (path / "bin" / "python").touch()

        import venv as venv_mod

        monkeypatch.setattr(venv_mod, "EnvBuilder", _Builder)
        monkeypatch.setattr(
            env_mod, "_venv_scheme_paths", lambda _python: {"purelib": str(tmp_path)}
        )

        real_write_text = Path.write_text

        def _deny_inner_project(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.parent.name == "_inner_project":
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _deny_inner_project)

        env = NabBuildEnv(requires=["pip"], config=ResolveInputs())
        with pytest.raises(BuildEnvError, match="could not populate the build env"):
            env.__enter__()
        assert env._tmpdir is None  # type: ignore[attr-defined]


class TestInstallWheelsCorruptArtifact:
    """A corrupt or malformed build-dependency wheel surfaces as
    ``BuildEnvError`` rather than a raw zip or installer error.

    A downloaded wheel has no hash to reject bad bytes, so corruption is
    only found when ``installer`` opens it.
    """

    def _env(self, tmp_path: Path) -> NabBuildEnv:
        env = NabBuildEnv(requires=[], config=ResolveInputs())
        venv_path = tmp_path / "venv"
        venv_path.mkdir()
        env._python_executable = venv_path / "bin" / "python"  # type: ignore[attr-defined]
        return env

    def _scheme(self, tmp_path: Path) -> dict[str, str]:
        return {
            "purelib": str(tmp_path / "site"),
            "headers": str(tmp_path / "include"),
        }

    def test_non_zip_wheel_raises_build_env_error(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)
        corrupt = tmp_path / "setuptools-70.0.0-py3-none-any.whl"
        corrupt.write_bytes(b"<html><body>404 Not Found</body></html>")
        with pytest.raises(BuildEnvError, match="setuptools-70.0.0-py3-none-any.whl"):
            env._install_wheels([corrupt], self._scheme(tmp_path))

    def test_truncated_wheel_raises_build_env_error(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)
        good = tmp_path / "good-1.0-py3-none-any.whl"
        _make_installable_wheel(good, "good", "1.0")
        truncated = tmp_path / "setuptools-70.0.0-py3-none-any.whl"
        data = good.read_bytes()
        truncated.write_bytes(data[: len(data) // 2])
        with pytest.raises(BuildEnvError, match="could not install build dependency"):
            env._install_wheels([truncated], self._scheme(tmp_path))

    def test_malformed_record_raises_build_env_error(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)
        broken = tmp_path / "good-1.0-py3-none-any.whl"
        files = {
            "good/__init__.py": b"",
            "good-1.0.dist-info/METADATA": (
                b"Metadata-Version: 2.1\nName: good\nVersion: 1.0\n"
            ),
            "good-1.0.dist-info/WHEEL": (
                b"Wheel-Version: 1.0\nGenerator: nab-test\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n"
            ),
        }
        record = "".join(
            f"{p},{_wheel_record_hash(d)},{len(d)}\n" for p, d in files.items()
        )
        record += "brokenrow,onlytwocols\n"
        record += "good-1.0.dist-info/RECORD,,\n"
        files["good-1.0.dist-info/RECORD"] = record.encode()
        with zipfile.ZipFile(broken, "w") as zf:
            for member, data in files.items():
                zf.writestr(member, data)
        with pytest.raises(BuildEnvError, match="could not install build dependency"):
            env._install_wheels([broken], self._scheme(tmp_path))

    def test_run_build_backend_wraps_corrupt_build_dep(
        self,
        tmp_path: Path,
        config: ResolveInputs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A corrupt build-dep wheel makes ``run_build_backend`` raise
        ``BuildBackendError`` (its documented contract) rather than a raw
        ``zipfile.BadZipFile``.
        """
        from nab_project._build import env as env_mod

        class _Builder:
            def __init__(self, **_kw: object) -> None:
                pass

            def create(self, path: Path) -> None:
                path.mkdir(parents=True, exist_ok=True)
                (path / "bin").mkdir(exist_ok=True)
                (path / "bin" / "python").touch()

        import venv as venv_mod

        monkeypatch.setattr(venv_mod, "EnvBuilder", _Builder)
        monkeypatch.setattr(
            env_mod,
            "_venv_scheme_paths",
            lambda _python: {
                "purelib": str(tmp_path / "site"),
                "headers": str(tmp_path / "include"),
            },
        )
        corrupt = tmp_path / "setuptools-70.0.0-py3-none-any.whl"
        corrupt.write_bytes(b"not a wheel")
        monkeypatch.setattr(
            NabBuildEnv, "_resolve_and_download", lambda self, _wd: [corrupt]
        )
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.build_meta"\n'
            "[project]\n"
            'name = "foo"\n'
            'version = "1.0"\n'
            'dynamic = ["dependencies"]\n',
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="build env setup"):
            run_build_backend(tmp_path, config=config)


class TestBuildEnvHeaderScheme:
    """A build requirement whose wheel ships a ``.data/headers/`` payload.

    greenlet, a build requirement of gevent, ships ``greenlet.h`` that way.
    """

    def _py_version(self) -> str:
        return f"{sys.version_info.major}.{sys.version_info.minor}"

    def _patch_probe(self, monkeypatch: pytest.MonkeyPatch, venv_path: Path) -> None:
        """Answer the scheme probe with what a venv interpreter prints."""
        py_version = self._py_version()
        site = venv_path / "lib" / f"python{py_version}" / "site-packages"
        payload = json.dumps(
            {
                "paths": {
                    "purelib": str(site),
                    "platlib": str(site),
                    "scripts": str(venv_path / "bin"),
                    "data": str(venv_path),
                    "include": f"/usr/include/python{py_version}",
                },
                "prefix": str(venv_path),
                "py_version": py_version,
            }
        )

        def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

        monkeypatch.setattr(subprocess, "run", _run)

    def test_scheme_paths_cover_every_installer_scheme(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venv_path = tmp_path / "venv"
        self._patch_probe(monkeypatch, venv_path)

        paths = _venv_scheme_paths(venv_path / "bin" / "python")

        assert set(SCHEME_NAMES) <= set(paths)
        expected = venv_path / "include" / "site" / f"python{self._py_version()}"
        assert paths["headers"] == str(expected)

    def test_header_payload_lands_under_the_dist_include_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = NabBuildEnv(requires=[], config=ResolveInputs())
        venv_path = tmp_path / "venv"
        venv_path.mkdir()
        (tmp_path / "wheels").mkdir()
        env._venv_path = venv_path  # type: ignore[attr-defined]
        env._python_executable = venv_path / "bin" / "python"  # type: ignore[attr-defined]

        wheel = tmp_path / "greenlet-3.2.4-py3-none-any.whl"
        _make_installable_wheel(
            wheel,
            "greenlet",
            "3.2.4",
            data_files={"headers/greenlet.h": b"#define GREENLET_H\n"},
        )

        monkeypatch.setattr(env, "_resolve_and_download", lambda *_a, **_k: [wheel])
        self._patch_probe(monkeypatch, venv_path)

        env.install(["greenlet==3.2.4"])

        py_version = self._py_version()
        header = (
            venv_path
            / "include"
            / "site"
            / f"python{py_version}"
            / "greenlet"
            / "greenlet.h"
        )
        assert header.read_bytes() == b"#define GREENLET_H\n"
        site = venv_path / "lib" / f"python{py_version}" / "site-packages"
        assert (site / "greenlet" / "__init__.py").is_file()


class TestBuildEnvFollowUpInstall:
    """A follow-up ``install`` leaves the venv holding what its resolve pinned.

    The second resolve covers the whole build env, so it can pin a
    different version of a package the first phase installed, or stop
    needing it at all.
    """

    def _env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[NabBuildEnv, dict[str, str], Path]:
        from nab_project._build import env as env_mod

        env = NabBuildEnv(requires=["probefoo"], config=ResolveInputs())
        venv_path = tmp_path / "venv"
        venv_path.mkdir()
        (tmp_path / "wheels").mkdir()
        env._venv_path = venv_path  # type: ignore[attr-defined]
        env._python_executable = venv_path / "bin" / "python"  # type: ignore[attr-defined]
        site = venv_path / "site-packages"
        scheme = {
            "purelib": str(site),
            "platlib": str(site),
            "scripts": str(venv_path / "bin"),
            "data": str(venv_path),
            "headers": str(venv_path / "include"),
        }
        monkeypatch.setattr(env_mod, "_venv_scheme_paths", lambda _python: scheme)
        return env, scheme, site

    def test_lower_pin_replaces_the_installed_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env, scheme, site = self._env(tmp_path, monkeypatch)

        first = tmp_path / "probefoo-2.0-py3-none-any.whl"
        _make_installable_wheel(
            first, "probefoo", "2.0", package_files={"only_in_v2.py": b"VALUE = 2\n"}
        )
        env._install_wheels([first], scheme)
        assert (site / "probefoo" / "only_in_v2.py").is_file()

        second = tmp_path / "probefoo-1.0-py3-none-any.whl"
        _make_installable_wheel(second, "probefoo", "1.0")
        monkeypatch.setattr(env, "_resolve_and_download", lambda *_a, **_k: [second])

        env.install(["probefoo<2"])

        assert sorted(p.name for p in site.iterdir()) == [
            "probefoo",
            "probefoo-1.0.dist-info",
        ]
        assert sorted(p.name for p in (site / "probefoo").iterdir()) == ["__init__.py"]

    def test_package_dropped_by_the_new_resolve_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env, scheme, site = self._env(tmp_path, monkeypatch)

        kept = tmp_path / "probefoo-1.0-py3-none-any.whl"
        _make_installable_wheel(kept, "probefoo", "1.0")
        dropped = tmp_path / "probebar-1.0-py3-none-any.whl"
        _make_installable_wheel(dropped, "probebar", "1.0")
        env._install_wheels([kept, dropped], scheme)
        assert (site / "probebar" / "__init__.py").is_file()

        monkeypatch.setattr(env, "_resolve_and_download", lambda *_a, **_k: [kept])

        env.install(["probefoo<2"])

        assert sorted(p.name for p in site.iterdir()) == [
            "probefoo",
            "probefoo-1.0.dist-info",
        ]

    def test_dropped_package_does_not_take_a_shared_data_file_with_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two distributions can ship the same path within a scheme."""
        env, scheme, _site = self._env(tmp_path, monkeypatch)
        shared = "data/share/probe.txt"

        dropped = tmp_path / "probeold-1.0-py3-none-any.whl"
        _make_installable_wheel(
            dropped, "probeold", "1.0", data_files={shared: b"old\n"}
        )
        env._install_wheels([dropped], scheme)

        added = tmp_path / "probenew-1.0-py3-none-any.whl"
        _make_installable_wheel(added, "probenew", "1.0", data_files={shared: b"new\n"})
        monkeypatch.setattr(env, "_resolve_and_download", lambda *_a, **_k: [added])

        env.install(["probenew"])

        assert (Path(scheme["data"]) / "share" / "probe.txt").read_bytes() == b"new\n"

    def test_bytecode_of_a_dropped_package_goes_with_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leftover ``__pycache__`` would keep the package importable."""
        env, scheme, site = self._env(tmp_path, monkeypatch)

        kept = tmp_path / "probefoo-1.0-py3-none-any.whl"
        _make_installable_wheel(kept, "probefoo", "1.0")
        dropped = tmp_path / "probebar-1.0-py3-none-any.whl"
        _make_installable_wheel(dropped, "probebar", "1.0")
        env._install_wheels([kept, dropped], scheme)

        compiled = Path(cache_from_source(str(site / "probebar" / "__init__.py")))
        compiled.parent.mkdir()
        compiled.write_bytes(b"")

        monkeypatch.setattr(env, "_resolve_and_download", lambda *_a, **_k: [kept])

        env.install(["probefoo<2"])

        assert not (site / "probebar").exists()

    def test_bytecode_of_a_module_named_like_a_pattern_goes_with_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Glob syntax in a wheel member name does not strand its bytecode."""
        env, scheme, site = self._env(tmp_path, monkeypatch)

        kept = tmp_path / "probefoo-1.0-py3-none-any.whl"
        _make_installable_wheel(kept, "probefoo", "1.0")
        dropped = tmp_path / "probebar-1.0-py3-none-any.whl"
        _make_installable_wheel(
            dropped, "probebar", "1.0", package_files={"mod[1].py": b""}
        )
        env._install_wheels([kept, dropped], scheme)

        compiled = Path(cache_from_source(str(site / "probebar" / "mod[1].py")))
        compiled.parent.mkdir()
        compiled.write_bytes(b"")

        monkeypatch.setattr(env, "_resolve_and_download", lambda *_a, **_k: [kept])

        env.install(["probefoo<2"])

        assert not (site / "probebar").exists()


# Windows rejects ``*`` and ``?`` in a filename, leaving the bracket stem.
_GLOB_PATTERN_STEMS = ["mod[1]"]
if sys.platform != "win32":
    _GLOB_PATTERN_STEMS += ["mod*", "mod?", "mod**"]


class TestRemoveFilesNameEscaping:
    """An installed file's name is matched literally, not as glob syntax."""

    @pytest.mark.parametrize("stem", _GLOB_PATTERN_STEMS)
    def test_a_module_named_like_a_pattern_takes_only_its_own_bytecode(
        self, tmp_path: Path, stem: str
    ) -> None:
        """``mod1`` is the neighbour an unescaped ``mod[1]`` would match."""
        package = tmp_path / "pkg"
        package.mkdir()
        (package / f"{stem}.py").write_bytes(b"")
        (package / "mod1.py").write_bytes(b"")

        removed = Path(cache_from_source(str(package / f"{stem}.py")))
        kept = Path(cache_from_source(str(package / "mod1.py")))
        removed.parent.mkdir()
        removed.write_bytes(b"")
        kept.write_bytes(b"")

        _remove_files([(tmp_path, Path(f"pkg/{stem}.py"))])

        assert not removed.exists()
        assert kept.is_file()


class TestVenvSchemeProbeErrors:
    """A failed interpreter scheme probe is wrapped as BuildEnvError."""

    def test_probe_called_process_error_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero probe exit is wrapped as BuildEnvError."""

        def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "run", _run)
        with pytest.raises(BuildEnvError, match="interpreter probe"):
            _venv_scheme_paths(Path("/nonexistent/venv/bin/python"))

    def test_probe_non_json_output_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probe stdout that is not JSON is wrapped as BuildEnvError."""

        def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="not json\n", stderr="")

        monkeypatch.setattr(subprocess, "run", _run)
        with pytest.raises(BuildEnvError, match="interpreter probe"):
            _venv_scheme_paths(Path("/nonexistent/venv/bin/python"))


class TestVenvSchemeProbeIsolation:
    """A module in the working directory must not answer the scheme probe."""

    def test_sysconfig_module_in_the_working_directory_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runs a real interpreter, so ``-I`` is what the assertion pins."""
        (tmp_path / "sysconfig.py").write_text(
            "def get_paths():\n    return {'purelib': '/elsewhere/site-packages'}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        paths = _venv_scheme_paths(Path(sys.executable))

        assert paths["purelib"] == sysconfig.get_paths()["purelib"]


class TestRunBuildBackendVenvRefused:
    """A ValueError from venv creation surfaces as BuildBackendError."""

    def test_valueerror_surfaces_as_build_backend_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapped ValueError reaches the caller with its venv path."""
        monkeypatch.setattr("venv.EnvBuilder", _PathsepRefusingEnvBuilder)

        source = tmp_path / "src"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "dummyreq.backend"\n',
            encoding="utf-8",
        )

        with pytest.raises(
            BuildBackendError,
            match=r"build env setup for 'dummyreq\.backend' failed: could not"
            r" create build venv at .*: Refusing to create a venv",
        ):
            run_build_backend(source, config=ResolveInputs())


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only branch in _venv_python"
)
class TestVenvPythonWindows:  # pragma: no cover
    """The Windows branch of :func:`_venv_python` returns
    ``Scripts\\python.exe``; non-Windows runners take the other branch
    which is already covered.
    """

    def test_windows_layout(self, tmp_path: Path) -> None:
        from nab_project._build.env import _venv_python

        result = _venv_python(tmp_path)
        assert result.name == "python.exe"
        assert result.parent.name == "Scripts"


class TestEndToEndAirflow:
    """Integration: drive the runner against airflow's real task-sdk."""

    @pytest.mark.network
    def test_airflow_task_sdk_dynamic_version(self) -> None:
        """End-to-end: a real PyPI roundtrip resolves hatchling and its
        deps, builds a venv, calls hatchling's metadata hook on
        airflow's task-sdk, and returns ``Version("1.3.0")`` back to
        the caller.

        Skipped by default; run with ``pytest -m network``.  Requires
        the airflow checkout under
        ``/home/damian/opensource/remote/projects/resolver/airflow``.
        """
        task_sdk = Path(
            "/home/damian/opensource/remote/projects/resolver/airflow/task-sdk"
        )
        if not (task_sdk / "pyproject.toml").is_file():
            pytest.skip(f"airflow checkout not present at {task_sdk}")

        metadata = run_build_backend(
            task_sdk,
            config=ResolveInputs(),
        )
        assert metadata.name == "apache-airflow-task-sdk"
        assert str(metadata.version) == "1.3.0"


# Silence pyflakes when sys/Path aren't used directly above.
_ = sys
_ = Path
