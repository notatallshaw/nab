"""Tests for the dynamic-metadata path through ``nab_python._build``.

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
import struct
import subprocess
import sys
import tarfile
import zipfile
from importlib.util import cache_from_source
from pathlib import Path
from unittest.mock import MagicMock, patch

import build
import pyproject_hooks
import pytest
from installer.utils import SCHEME_NAMES, Scheme

from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import IndexConfig
from nab_python._build import runner as runner_mod
from nab_python._build.env import (
    BuildEnvError,
    NabBuildEnv,
    _FastSchemeDictionaryDestination,
    _picked_wheel_pin,
    _venv_scheme_paths,
)
from nab_python._build.runner import BuildBackendError, run_build_backend
from nab_python._provider.metadata_resolver import pick_dist
from nab_python._vendor.packaging.version import Version
from nab_python.config import NabProjectConfig
from nab_python.download import DownloadError, DownloadResult, iter_artifacts
from nab_python.lockfile import (
    IndexPin,
    LockInput,
    SdistArtifact,
    TargetLock,
    WheelArtifact,
)
from nab_python.resolve import ResolveResult, TargetResult
from nab_python.tags import PlatformSpec, TagSet
from nab_python.target import ResolveTarget
from nab_resolver.resolver import ResolutionError

# A minimal, in-tree PEP 517 backend.  Implements
# ``prepare_metadata_for_build_wheel`` only, enough to exercise
# the happy path through ``BuildBackendHookCaller`` ->
# ``build.ProjectBuilder.from_isolated_env`` -> our ``NabBuildEnv``.
_FAKE_BACKEND_SRC = '''\
"""Minimal PEP 517 backend used by nab_python._build tests."""
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
    distinfo = f"{name}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\\n"
        f"Name: {name}\\n"
        f"Version: {version}\\n"
        "Requires-Python: >=3.10\\n"
        "Requires-Dist: click>=8\\n"
    )
    wheel_name = f"{name}-{version}-py3-none-any.whl"
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


def _make_local_index(root: Path, name: str, version: str) -> None:
    """Create a PEP 503 ``file://`` index serving ``name`` as a wheel + sdist."""
    pkg_dir = root / name
    pkg_dir.mkdir(parents=True)
    wheel = pkg_dir / f"{name}-{version}-py3-none-any.whl"
    sdist = pkg_dir / f"{name}-{version}.tar.gz"
    _make_installable_wheel(wheel, name, version)
    _make_pep643_sdist(sdist, name, version)

    def _digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    links = "".join(
        f'<a href="{p.name}#sha256={_digest(p)}">{p.name}</a>\n' for p in (wheel, sdist)
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


@pytest.fixture
def config() -> NabProjectConfig:
    """A minimal :class:`NabProjectConfig` for the tests."""
    return NabProjectConfig()


class TestRunBuildBackend:
    def test_prepare_metadata_happy_path(
        self, tmp_path: Path, config: NabProjectConfig
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
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        with pytest.raises(BuildBackendError, match="no pyproject.toml or setup.py"):
            run_build_backend(tmp_path, config=config)

    def test_legacy_setup_py_uses_default_backend(
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        """A source tree with only ``setup.py`` invokes the PEP 517 fallback.

        The legacy ``setuptools.build_meta:__legacy__`` backend handles
        projects that pre-date ``pyproject.toml``.  We exercise the
        branch by stubbing :class:`NabBuildEnv` and ``ProjectBuilder``
        so the test stays offline and fast.
        """
        from nab_python.metadata import WheelMetadata

        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")

        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)
        project = MagicMock()
        project.get_requires_for_build.return_value = []
        project.metadata_path.side_effect = lambda out: out

        metadata_text = "Metadata-Version: 2.1\nName: legacy-pkg\nVersion: 0.1\n"

        def fake_metadata_path(out_dir: str) -> str:
            target = Path(out_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / "METADATA").write_text(metadata_text, encoding="utf-8")
            return str(target)

        project.metadata_path.side_effect = fake_metadata_path

        with (
            patch("nab_python._build.runner.NabBuildEnv", return_value=env),
            patch(
                "nab_python._build.runner.build.ProjectBuilder.from_isolated_env",
                return_value=project,
            ),
        ):
            metadata = run_build_backend(tmp_path, config=config)

        assert isinstance(metadata, WheelMetadata)
        assert metadata.name == "legacy-pkg"
        assert str(metadata.version) == "0.1"

    def test_malformed_pyproject(
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "not = valid = toml = [", encoding="utf-8"
        )
        with pytest.raises(BuildBackendError, match="could not read"):
            run_build_backend(tmp_path, config=config)

    def test_non_utf8_pyproject(self, tmp_path: Path, config: NabProjectConfig) -> None:
        (tmp_path / "pyproject.toml").write_bytes(
            b"[build-system]\nrequires = []\n# \xe9\n"
        )
        with pytest.raises(BuildBackendError, match="could not read"):
            run_build_backend(tmp_path, config=config)

    def test_backend_metadata_missing_version_raises(
        self, tmp_path: Path, config: NabProjectConfig
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
        config: NabProjectConfig,
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
        from nab_python._build import runner as runner_mod

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
        self, tmp_path: Path, config: NabProjectConfig
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
            patch("nab_python._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="build env setup"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_build_env_resolution_error_wrapped(
        self, tmp_path: Path, config: NabProjectConfig
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
            patch("nab_python._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="build env setup"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_build_system_table_rejected_wrapped(
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        """build.BuildSystemTableValidationError from an invalid build-system table is wrapped as BuildBackendError."""
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)
        with (
            patch("nab_python._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="setuptools.build_meta"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_build_system_not_a_table(
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        """A scalar build-system key fails the build."""
        (tmp_path / "pyproject.toml").write_text(
            'build-system = "hatchling.build"\n', encoding="utf-8"
        )
        with pytest.raises(BuildBackendError, match="must be a table"):
            run_build_backend(tmp_path, config=config)

    def test_venv_creation_oserror_wrapped(
        self,
        tmp_path: Path,
        config: NabProjectConfig,
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

    def test_non_string_build_requirement_wrapped(
        self,
        tmp_path: Path,
        config: NabProjectConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-string build requirement is wrapped as BuildBackendError."""
        from nab_python._build import env as env_mod

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
                "nab_python._build.runner.build.ProjectBuilder.from_isolated_env",
                return_value=project,
            ),
            pytest.raises(BuildBackendError, match="non-string"),
        ):
            run_build_backend(tmp_path, config=config)


class TestShouldSkipPrepare:
    """Unit tests for the hatchling+dynamic-deps detection predicate."""

    def test_non_hatchling_backend_does_not_skip(self) -> None:
        from nab_python._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["dependencies"]}}
        assert _should_skip_prepare("setuptools.build_meta", data) is False
        assert _should_skip_prepare("flit_core.buildapi", data) is False

    def test_hatchling_without_dynamic_deps_does_not_skip(self) -> None:
        from nab_python._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["version"]}}
        assert _should_skip_prepare("hatchling.build", data) is False

    def test_hatchling_with_dynamic_deps_skips(self) -> None:
        from nab_python._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["version", "dependencies"]}}
        assert _should_skip_prepare("hatchling.build", data) is True

    def test_hatchling_with_dynamic_optional_dependencies_skips(self) -> None:
        from nab_python._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": ["optional-dependencies"]}}
        assert _should_skip_prepare("hatchling.build", data) is True

    def test_no_project_table_does_not_skip(self) -> None:
        from nab_python._build.runner import _should_skip_prepare

        assert _should_skip_prepare("hatchling.build", {}) is False

    def test_dynamic_not_a_list_does_not_skip(self) -> None:
        """``dynamic`` value of the wrong type is treated as no dynamic
        fields; the prepare hook is allowed to run."""
        from nab_python._build.runner import _should_skip_prepare

        data = {"project": {"dynamic": "wrong-shape"}}
        assert _should_skip_prepare("hatchling.build", data) is False


class TestReadBuildSystem:
    """Defaults for missing or wrongly-typed ``[build-system]`` fields."""

    def test_no_build_system_table_returns_defaults(self) -> None:
        from nab_python._build.runner import (
            _DEFAULT_BACKEND,
            _DEFAULT_REQUIRES,
            _read_build_system,
        )

        assert _read_build_system({}) == (_DEFAULT_BACKEND, _DEFAULT_REQUIRES, None)

    @pytest.mark.parametrize("value", ["hatchling.build", ["setuptools>=61"], 1])
    def test_build_system_not_a_table_raises(self, value: object) -> None:
        """PEP 518 defines build-system as a table."""
        from nab_python._build.runner import _read_build_system

        with pytest.raises(BuildBackendError, match="must be a table"):
            _read_build_system({"build-system": value})

    def test_build_backend_wrong_type_uses_default(self) -> None:
        from nab_python._build.runner import _DEFAULT_BACKEND, _read_build_system

        backend, _, _ = _read_build_system(
            {"build-system": {"build-backend": 1234, "requires": []}}
        )
        assert backend == _DEFAULT_BACKEND

    def test_requires_wrong_type_uses_default(self) -> None:
        from nab_python._build.runner import _DEFAULT_REQUIRES, _read_build_system

        _, requires, _ = _read_build_system(
            {"build-system": {"build-backend": "x", "requires": "not-a-list"}}
        )
        assert requires == _DEFAULT_REQUIRES

    def test_backend_path_returns_tuple_when_strings(self) -> None:
        from nab_python._build.runner import _read_build_system

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

    def test_backend_path_wrong_type_returns_none(self) -> None:
        from nab_python._build.runner import _read_build_system

        _, _, backend_path = _read_build_system(
            {
                "build-system": {
                    "build-backend": "x",
                    "requires": [],
                    "backend-path": "src",
                }
            }
        )
        assert backend_path is None


class TestParseMetadata:
    """Direct unit tests for :func:`_parse_metadata` cover error paths
    that are awkward to reach through ``run_build_backend`` end-to-end.
    """

    def test_missing_metadata_file_raises(self, tmp_path: Path) -> None:
        from nab_python._build.runner import _parse_metadata

        with pytest.raises(BuildBackendError, match="no METADATA file"):
            _parse_metadata(tmp_path / "DOES-NOT-EXIST")

    def test_non_utf8_metadata_raises_build_backend_error(self, tmp_path: Path) -> None:
        from nab_python._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_bytes(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nAuthor: café\n".encode(
                "latin-1"
            )
        )
        with pytest.raises(BuildBackendError, match="not valid UTF-8"):
            _parse_metadata(path)

    def test_invalid_version_raises(self, tmp_path: Path) -> None:
        from nab_python._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: not-a-version\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Version"):
            _parse_metadata(path)

    def test_invalid_requires_python_raises(self, tmp_path: Path) -> None:
        from nab_python._build.runner import _parse_metadata

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
        from nab_python._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            "Requires-Dist: bad junk @@@\n"
            "Requires-Dist: click>=8\n",
            encoding="utf-8",
        )
        with pytest.raises(BuildBackendError, match="invalid Requires-Dist"):
            _parse_metadata(path)

    def test_provides_extra_whitespace_stripped(self, tmp_path: Path) -> None:
        """Surrounding whitespace on a Provides-Extra value is insignificant
        per RFC 822; canonicalize_name does not strip it, so strip first."""
        from nab_python._build.runner import _parse_metadata

        path = tmp_path / "METADATA"
        path.write_text(
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            "Provides-Extra: security \nProvides-Extra: docs\t\n",
            encoding="utf-8",
        )
        meta = _parse_metadata(path)
        assert meta.provides_extra == ["docs", "security"]


class TestBuildWheelExtraction:
    """``_build_wheel_and_extract`` raises when the built wheel has
    no .dist-info directory.  The happy path is exercised end-to-end
    through the hatchling-quirk-skip test in ``TestRunBuildBackend``.
    """

    def test_wheel_without_dist_info_raises(self, tmp_path: Path) -> None:
        import zipfile

        from nab_python._build.runner import _build_wheel_and_extract

        wheel_path = tmp_path / "fake-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr("loose-file.txt", "hi")

        class _Builder:
            def build(self, _kind: str, _outdir: str) -> str:
                return str(wheel_path)

        with pytest.raises(BuildBackendError, match="no .dist-info"):
            _build_wheel_and_extract(_Builder(), tmp_path)  # type: ignore[arg-type]


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
    sits outside the hook-error wrapper on both the ``build.metadata_path``
    fallback and the runner's own skip path.
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
        ``wheel_name``. ``prepare`` returns None so ``metadata_path`` runs
        ``build``'s real build_wheel fallback, whose read-back hits the real
        ``parse_wheel_filename`` and ``zipfile.ZipFile``.
        """
        project = MagicMock()
        project.get_requires_for_build.return_value = []
        project.prepare.return_value = None

        def fake_build(_dist: str, outdir: str, *_a: object, **_k: object) -> str:
            path = Path(outdir) / wheel_name
            path.write_bytes(data)
            return str(path)

        project.build.side_effect = fake_build
        project.metadata_path.side_effect = lambda outdir: (
            build.ProjectBuilder.metadata_path(project, outdir)
        )
        return project

    def _run(
        self, tmp_path: Path, config: NabProjectConfig, project: MagicMock
    ) -> None:
        with (
            patch(
                "nab_python._build.runner.NabBuildEnv",
                return_value=self._mock_env(),
            ),
            patch(
                "nab_python._build.runner.build.ProjectBuilder.from_isolated_env",
                return_value=project,
            ),
            pytest.raises(BuildBackendError, match="unreadable wheel"),
        ):
            run_build_backend(tmp_path, config=config)

    def test_default_path_corrupt_wheel_wrapped(
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        """The backend has no prepare hook, so ``build.metadata_path`` builds a
        wheel and reads it with ``zipfile.ZipFile`` after the hook wrapper; a
        corrupt wheel raises ``BadZipFile`` there, which the runner normalizes.
        """
        self._pyproject(tmp_path)
        self._run(
            tmp_path, config, self._corrupt_building_project("foo-1.0-py3-none-any.whl")
        )

    def test_invalid_wheel_name_wrapped(
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        """``build.metadata_path`` raises a bare ``ValueError('Invalid wheel')``
        when the built wheel's name does not parse; the runner normalizes it.
        """
        self._pyproject(tmp_path)
        self._run(tmp_path, config, self._corrupt_building_project("garbage.whl"))

    def test_skip_prepare_corrupt_wheel_wrapped(
        self,
        tmp_path: Path,
        config: NabProjectConfig,
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

    @pytest.mark.parametrize("skip_prepare", [False, True])
    @pytest.mark.parametrize(
        "kind", ["corrupt-deflate", "corrupt-lzma", "unsupported-method"]
    )
    def test_unreadable_dist_info_member_wrapped(
        self,
        tmp_path: Path,
        config: NabProjectConfig,
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
        sends ``build.ProjectBuilder.metadata_path`` down its ``build_wheel``
        fallback.
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

    def _run(self, tmp_path: Path, config: NabProjectConfig) -> None:
        env = MagicMock()
        env.__enter__ = MagicMock(return_value=env)
        env.__exit__ = MagicMock(return_value=None)
        with (
            patch("nab_python._build.runner.NabBuildEnv", return_value=env),
            pytest.raises(BuildBackendError, match="non-string path"),
        ):
            run_build_backend(tmp_path, config=config)

    @pytest.mark.parametrize("value", [None, 1, ["foo-1.0.dist-info"]])
    def test_prepare_hook_non_string_wrapped(
        self,
        tmp_path: Path,
        config: NabProjectConfig,
        monkeypatch: pytest.MonkeyPatch,
        value: object,
    ) -> None:
        self._pyproject(tmp_path)
        self._stub_hooks(monkeypatch, prepare=value)
        self._run(tmp_path, config)

    def test_build_wheel_fallback_non_string_wrapped(
        self,
        tmp_path: Path,
        config: NabProjectConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no prepare hook, ``build.metadata_path`` falls back to
        ``build_wheel``, whose return hits the same join.
        """
        self._pyproject(tmp_path)
        self._stub_hooks(monkeypatch, build_wheel=1)
        self._run(tmp_path, config)

    def test_skip_prepare_non_string_wrapped(
        self,
        tmp_path: Path,
        config: NabProjectConfig,
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
        config = NabProjectConfig(indexes=(IndexConfig("local", index_dir.as_uri()),))

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

        monkeypatch.setattr("nab_python._build.env.download_lock", fake_download_lock)
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
        config: NabProjectConfig,
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


class TestNabBuildEnvOutsideContext:
    """Accessors and ``install`` raise when used outside ``with`` scope."""

    def _env(self) -> NabBuildEnv:
        return NabBuildEnv(requires=[], config=NabProjectConfig())

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
    """The ``install`` shortcut: empty list, the inner re-resolve path,
    and the sdist-only rejection.
    """

    def test_install_empty_returns_immediately(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty requirements list is a no-op (does not even touch
        the venv state).  We assert by checking the resolve helper is
        not called.
        """
        env = NabBuildEnv(requires=[], config=NabProjectConfig())
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

        env = NabBuildEnv(requires=[], config=NabProjectConfig())
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
            "nab_python._build.env._venv_scheme_paths",
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
            "nab_python._build.env.installer_install",
            lambda **kwargs: installer_calls.append(kwargs),
        )
        env.install(["pip"])
        assert len(installer_calls) == 1


class TestResolveAndDownload:
    """``_resolve_and_download`` rejects sdist-only pins so the build
    env never ends up with a dep that needs another build to install.
    """

    def test_sdist_only_pin_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nab_python._vendor.packaging.version import Version
        from nab_python.lockfile import IndexPin, TargetLock
        from nab_python.resolve import ResolveResult, TargetResult
        from nab_python.target import ResolveTarget

        env = NabBuildEnv(requires=["foo"], config=NabProjectConfig())
        env._tmpdir = MagicMock()  # type: ignore[attr-defined]
        env._venv_path = tmp_path / "venv"  # type: ignore[attr-defined]
        env._python_executable = tmp_path / "venv" / "bin" / "python"  # type: ignore[attr-defined]
        sdist_pin = IndexPin(
            name="foo",
            version="1.0",
            index="pypi",
            wheels=(),
            sdist=None,
        )
        target = ResolveTarget.for_host()
        fake_result = ResolveResult(
            targets=(target,),
            target_results=[
                TargetResult(
                    target=target,
                    success=True,
                    pins={"foo": Version("1.0")},
                    lock=TargetLock(target=target, pins={"foo": sdist_pin}),
                )
            ],
        )
        with patch("nab_python.resolve.resolve_for_targets", return_value=fake_result):
            wheel_dir = tmp_path / "wheels"
            wheel_dir.mkdir()
            with pytest.raises(BuildEnvError, match="sdist-only"):
                env._resolve_and_download(wheel_dir)

    def test_inner_resolve_runs_for_the_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inner resolve gets no Python override.

        The venv is built from the host interpreter, so a target
        Python would pick wheels for another ABI and evaluate the
        build requirements' markers against the wrong interpreter.
        """
        from nab_python.lockfile import TargetLock
        from nab_python.resolve import ResolveResult, TargetResult
        from nab_python.target import ResolveTarget

        env = NabBuildEnv(requires=["foo"], config=NabProjectConfig())
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

        monkeypatch.setattr("nab_python.resolve.resolve_for_targets", fake_resolve)
        monkeypatch.setattr(
            "nab_python._build.env.download_lock",
            lambda *_a, **_k: MagicMock(written=[], skipped=[]),
        )
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()

        assert env._resolve_and_download(wheel_dir) == []
        assert "python_version" not in captured

    def test_url_build_requirement_wrapped(self, tmp_path: Path) -> None:
        """A direct-URL build requirement the inner resolve refuses is
        wrapped as BuildEnvError, so the outer resolve skips the
        unbuildable sdist instead of aborting on the raw
        UnsupportedVcsError.
        """
        env = NabBuildEnv(
            requires=["plugin @ https://example.com/plugin-1.0.tar.gz"],
            config=NabProjectConfig(),
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
            config=NabProjectConfig(),
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
        from nab_python.lockfile import TargetLock
        from nab_python.resolve import ResolveResult, TargetResult
        from nab_python.target import ResolveTarget

        env = NabBuildEnv(requires=["foo"], config=NabProjectConfig())
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
            "nab_python.resolve.resolve_for_targets", lambda *_a, **_k: fake_result
        )

        def _boom(*_a: object, **_k: object) -> DownloadResult:
            msg = "foo==1.0: failed to fetch foo-1.0-py3-none-any.whl: GET x 404"
            raise DownloadError(msg)

        monkeypatch.setattr("nab_python._build.env.download_lock", _boom)
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        with pytest.raises(BuildEnvError, match="build env download"):
            env._resolve_and_download(wheel_dir)


def _no_network(*_a: object, **_k: object) -> object:
    """Stand in for anything an offline build env must not call."""
    raise AssertionError("offline must not reach the network")


class TestBuildEnvOffline:
    """``offline`` bars the build env from fetching its requirements."""

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nab_python.resolve.resolve_for_targets", _no_network)
        monkeypatch.setattr("nab_python._build.env.download_lock", _no_network)

    def test_refuses_before_the_inner_resolve(self, tmp_path: Path) -> None:
        env = NabBuildEnv(
            requires=["foo"],
            config=NabProjectConfig(),
            offline=True,
            transport_factory=_no_network,  # type: ignore[arg-type]
        )
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
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
            run_build_backend(source, config=NabProjectConfig(), offline=True)

    def test_backend_with_no_build_requirements_still_builds(
        self, tmp_path: Path, config: NabProjectConfig
    ) -> None:
        """Nothing to fetch, so the offline run is served."""
        source = _write_fake_backend_project(tmp_path)
        metadata = run_build_backend(source, config=config, offline=True)
        assert metadata.name == "fake-pkg"


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
            "nab_python.resolve.resolve_for_targets", lambda *_a, **_k: fake_result
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

        monkeypatch.setattr("nab_python._build.env.download_lock", _fake_download)

        env = NabBuildEnv(requires=["demo"], config=NabProjectConfig())
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

    def test_sdist_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Narrowing the wheels leaves the sdist, so the pin does not
        then read as sdist-only.
        """
        requested, wheels = self._run(
            tmp_path,
            monkeypatch,
            [self.MANYLINUX2014, self.MANYLINUX1],
            with_sdist=True,
        )
        assert requested == [self.SDIST, self.MANYLINUX2014]
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
        """
        with pytest.raises(
            BuildEnvError, match="no wheel of demo==1.0 matches the build host's tags"
        ):
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
        config = NabProjectConfig(indexes=(IndexConfig("local", index_dir.as_uri()),))

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

        pin = _picked_wheel_pin(self._pin([self.TIE_PLAIN, self.TIE_SIDECAR]), tags)
        assert isinstance(pin, IndexPin)
        assert [w.filename for w in pin.wheels] == [self.TIE_PLAIN]

    def test_metadata_may_come_from_a_wheel_the_host_cannot_install(self) -> None:
        """Two off-target wheels, since ``pick_dist`` falls back only
        when the tags rank nothing at all.
        """
        tags = self._tags()
        listed = [
            self._listed(self.WINDOWS, has_metadata=True),
            self._listed(self.MACOS, has_metadata=False),
        ]
        picked = pick_dist(listed, tags)
        assert picked.filename == self.WINDOWS
        assert not tags.accepts(picked.filename)

        with pytest.raises(BuildEnvError, match="matches the build host's tags"):
            _picked_wheel_pin(self._pin([self.WINDOWS, self.MACOS]), tags)

    def test_metadata_may_come_from_an_sdist(self) -> None:
        """A version publishing no wheel is read from its sdist.

        Two sdists, because ``pick_dist`` hands back a lone dist
        without ranking anything.  The install side has no such
        fallback: the pin comes back untouched, and the sdist-only
        check rejects it.
        """
        tags = self._tags()
        listed = [self._listed_sdist(self.SDIST), self._listed_sdist(self.SDIST_ZIP)]
        assert pick_dist(listed, tags) is listed[0]

        pin = IndexPin(
            name="demo",
            version="1.0",
            index="https://pypi.org/simple/",
            sdist=SdistArtifact(
                filename=self.SDIST,
                url=f"https://pypi.example/{self.SDIST}",
                hashes=(("sha256", "1" * 64),),
            ),
        )
        assert _picked_wheel_pin(pin, tags) is pin


class TestNabBuildEnvLifecycle:
    """Edge cases of the context-manager lifecycle that fall outside
    the happy-path runner tests.
    """

    def test_exit_without_enter_is_a_noop(self) -> None:
        """``__exit__`` on a never-entered env is a quiet no-op so
        helpers that call ``with`` against an env construction failure
        do not double-fault.
        """
        env = NabBuildEnv(requires=[], config=NabProjectConfig())
        env.__exit__(None, None, None)
        assert env._tmpdir is None  # type: ignore[attr-defined]

    def test_render_synthetic_pyproject_empty_requires(self) -> None:
        """The synthetic-pyproject helper renders an empty
        ``dependencies = []`` block when called with no requires.
        """
        from nab_python._build.env import _render_synthetic_pyproject

        text = _render_synthetic_pyproject([])
        assert "dependencies = []" in text

    def test_render_synthetic_pyproject_escapes_control_chars(self) -> None:
        """Control characters, quotes, and backslashes in a requirement
        round-trip through the synthetic pyproject as valid TOML.
        """
        import tomli

        from nab_python._build.env import _render_synthetic_pyproject

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

        from nab_python._build import env as env_mod

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
        with NabBuildEnv(requires=["pip"], config=NabProjectConfig()):
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
        from nab_python._build import env as env_mod

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
        env = NabBuildEnv(requires=["pip"], config=NabProjectConfig())
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
        env = NabBuildEnv(requires=[], config=NabProjectConfig())
        with pytest.raises(BuildEnvError, match="build venv"):
            env.__enter__()
        assert env._tmpdir is None  # type: ignore[attr-defined]


class TestInstallWheelsCorruptArtifact:
    """A corrupt or malformed build-dependency wheel surfaces as
    ``BuildEnvError`` rather than a raw zip or installer error.

    A downloaded wheel has no hash to reject bad bytes, so corruption is
    only found when ``installer`` opens it.
    """

    def _env(self, tmp_path: Path) -> NabBuildEnv:
        env = NabBuildEnv(requires=[], config=NabProjectConfig())
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
        config: NabProjectConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A corrupt build-dep wheel makes ``run_build_backend`` raise
        ``BuildBackendError`` (its documented contract) rather than a raw
        ``zipfile.BadZipFile``.
        """
        from nab_python._build import env as env_mod

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
        env = NabBuildEnv(requires=[], config=NabProjectConfig())
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
        from nab_python._build import env as env_mod

        env = NabBuildEnv(requires=["probefoo"], config=NabProjectConfig())
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


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only branch in _venv_python"
)
class TestVenvPythonWindows:  # pragma: no cover
    """The Windows branch of :func:`_venv_python` returns
    ``Scripts\\python.exe``; non-Windows runners take the other branch
    which is already covered.
    """

    def test_windows_layout(self, tmp_path: Path) -> None:
        from nab_python._build.env import _venv_python

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
            config=NabProjectConfig(),
        )
        assert metadata.name == "apache-airflow-task-sdk"
        assert str(metadata.version) == "1.3.0"


# Silence pyflakes when sys/Path aren't used directly above.
_ = sys
_ = Path
