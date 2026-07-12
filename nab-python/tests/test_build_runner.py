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
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from installer.utils import SCHEME_NAMES, Scheme

from nab_index.multi_index import IndexConfig
from nab_python._build.env import (
    BuildEnvError,
    NabBuildEnv,
    _FastSchemeDictionaryDestination,
    _venv_scheme_paths,
)
from nab_python._build.runner import BuildBackendError, run_build_backend
from nab_python.config import NabProjectConfig
from nab_python.download import DownloadResult
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
) -> None:
    """Write a minimal but installer-valid pure-Python wheel.

    ``data_files`` are placed under the PEP 427 ``<dist>.data``
    directory, keyed by their path within it (``headers/foo.h``).
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
            lock_input: object, _transport: object, wheel_dir: Path, *_a: object
        ) -> DownloadResult:
            written = []
            for name, pin in lock_input.pins.items():  # type: ignore[attr-defined]
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
        from nab_python.lockfile import IndexPin, LockInput
        from nab_python.resolve import ResolutionResult

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
        fake_result = ResolutionResult(
            pins={"foo": MagicMock()},
            lock_input=LockInput(pins={"foo": sdist_pin}),
        )
        with patch("nab_python.resolve.resolve_pyproject", return_value=fake_result):
            wheel_dir = tmp_path / "wheels"
            wheel_dir.mkdir()
            with pytest.raises(BuildEnvError, match="sdist-only"):
                env._resolve_and_download(wheel_dir)

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
