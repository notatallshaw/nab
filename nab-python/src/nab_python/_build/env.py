"""Isolated build env that uses nab itself plus the PyPA ``installer``.

``NabBuildEnv`` implements ``build.env.IsolatedEnv`` so it slots into
``build.ProjectBuilder.from_isolated_env``.  The pieces:

* ``venv.EnvBuilder`` (stdlib) creates an empty interpreter at a temp
  path, ``with_pip=False``; nab does not need pip in there.
* nab's own resolver picks versions for ``[build-system].requires``
  using the same indexes / ``uploaded-prior-to`` window as the outer
  resolve.
* ``download_lock`` from :mod:`nab_python.download` fetches the
  resolved wheels into a temp directory.
* :func:`installer.install` writes each wheel into the venv via
  ``installer.SchemeDictionaryDestination``, configured from the
  venv's own scheme paths.

The env is a context manager.  Entering it builds the venv and
installs the requirements; exiting removes the temp tree.  The
``python_executable`` and ``make_extra_environ`` properties come
from ``build.env.IsolatedEnv``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import TYPE_CHECKING

from installer import install as installer_install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from nab_index.urllib3_async_transport import Urllib3AsyncTransport

from .._vcs_admission import UnsupportedVcsError
from ..config import NabProjectConfig
from ..download import download_lock
from ..requirements_file import InvalidProjectRequirementError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing_extensions import Self

    from installer.records import RecordEntry
    from installer.utils import Scheme
    from nab_index.transport import AsyncHttpTransport

__all__ = [
    "NabBuildEnv",
]


class _FastSchemeDictionaryDestination(SchemeDictionaryDestination):
    """``SchemeDictionaryDestination`` that skips bytecode-compile work.

    The stock ``_compile_bytecode`` always resolves the target file's
    real path before checking ``bytecode_optimization_levels``; with
    an empty levels tuple, the loop body never runs but the resolve
    still hits the filesystem twice per record.  Skipping the early
    work when the levels tuple is empty is safe because the body
    would have been a no-op.
    """

    def _compile_bytecode(self, scheme: Scheme, record: RecordEntry) -> None:
        if not self.bytecode_optimization_levels:
            return
        super()._compile_bytecode(scheme, record)


logger = logging.getLogger(__name__)


_LAUNCHER_KIND = (
    "win-amd64"
    if sys.platform == "win32" and sys.maxsize > 2**32
    else "win-ia32"
    if sys.platform == "win32"
    else "posix"
)

_SCHEME_PROBE = (
    "import json, sys, sysconfig;"
    "print(json.dumps({"
    "'paths': sysconfig.get_paths(),"
    "'prefix': sys.prefix,"
    "'py_version': '%d.%d' % sys.version_info[:2],"
    "}))"
)


class BuildEnvError(Exception):
    """The build env could not be set up (resolve, download, or install)."""


class NabBuildEnv:
    """An isolated PEP 518 build environment driven by nab.

    Implements ``build.env.IsolatedEnv`` so it can be passed to
    ``build.ProjectBuilder.from_isolated_env``. The runtime cost
    is one venv creation, one inner resolve over
    ``[build-system].requires``, one wheel download per dep, and
    one ``installer.install`` per wheel.

    ``requires`` is the PEP 508 string list from
    ``[build-system].requires``. ``config`` carries the indexes,
    ``uploaded-prior-to`` window and other nab inputs from the outer
    resolve; it is pruned (no local sources, no workspace, no
    marker overlay) before the inner resolve so the build env is
    computed against PyPI alone.

    The venv is created from the host interpreter and the PEP 517
    hooks run in it, so the build requirements resolve for the host
    and not for any ``--python`` retarget: a wheel for another
    Python's ABI would not import, and a build requirement the host
    needs would be dropped by its marker.

    Construction is cheap; the work happens in ``__enter__``.
    """

    def __init__(
        self,
        requires: list[str],
        *,
        config: NabProjectConfig,
        transport_factory: Callable[[], AsyncHttpTransport] = Urllib3AsyncTransport,
    ) -> None:
        """Capture inputs; the venv and inner resolve happen in __enter__."""
        self._requires = list(requires)
        self._config = config
        self._transport_factory = transport_factory

        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._venv_path: Path | None = None
        self._python_executable: Path | None = None
        self._scripts_dir: Path | None = None

    def __enter__(self) -> Self:
        """Build the venv, run the inner resolve, install build requirements."""
        self._tmpdir = tempfile.TemporaryDirectory(prefix="nab-build-env-")
        try:
            self._provision(Path(self._tmpdir.name))
        except BaseException:
            self._tmpdir.cleanup()
            self._tmpdir = None
            raise
        return self

    def _provision(self, root: Path) -> None:
        """Lay out the venv, install build requirements, populate paths."""
        self._venv_path = root / "venv"
        wheel_dir = root / "wheels"
        wheel_dir.mkdir()

        logger.debug("creating build venv at %s", self._venv_path)
        builder = venv.EnvBuilder(
            with_pip=False, symlinks=_supports_symlinks(), clear=False
        )
        builder.create(self._venv_path)

        self._python_executable = _venv_python(self._venv_path)
        self._scripts_dir = self._python_executable.parent

        scheme_paths = _venv_scheme_paths(self._python_executable)

        if not self._requires:
            return

        self._install_wheels(self._resolve_and_download(wheel_dir), scheme_paths)

    def _install_wheels(
        self, wheel_paths: list[Path], scheme_paths: dict[str, str]
    ) -> None:
        """Write each wheel into the venv with ``installer``."""
        for wheel_path in wheel_paths:
            logger.debug("installing %s", wheel_path.name)
            with WheelFile.open(wheel_path) as source:
                destination = _FastSchemeDictionaryDestination(
                    scheme_dict=_dist_scheme_paths(scheme_paths, source.distribution),
                    interpreter=str(self._python_executable),
                    script_kind=_LAUNCHER_KIND,
                    bytecode_optimization_levels=(),
                    overwrite_existing=True,
                )
                installer_install(
                    source=source,
                    destination=destination,
                    additional_metadata={"INSTALLER": b"nab\n"},
                )

    def __exit__(self, *args: object) -> None:
        """Remove the temp tree that holds the venv and downloaded wheels."""
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    @property
    def python_executable(self) -> str:
        """Path to the venv's interpreter (str, per ``IsolatedEnv``)."""
        if self._python_executable is None:
            msg = "NabBuildEnv used outside its context-manager scope"
            raise BuildEnvError(msg)
        return str(self._python_executable)

    def make_extra_environ(self) -> Mapping[str, str]:
        """PATH / PYTHONPATH overrides for the build subprocess.

        Prepends the venv's scripts dir so build backends find their
        installed entry points; clears ``PYTHONPATH`` so the host's
        ``sys.path`` does not leak in (matches the convention used by
        ``build.env.DefaultIsolatedEnv``).
        """
        if self._scripts_dir is None:
            msg = "NabBuildEnv used outside its context-manager scope"
            raise BuildEnvError(msg)
        host_path = os.environ.get("PATH")
        new_path = (
            f"{self._scripts_dir}{os.pathsep}{host_path}"
            if host_path
            else str(self._scripts_dir)
        )
        return {"PATH": new_path, "PYTHONPATH": ""}

    def install(self, requirements: list[str]) -> None:
        """Install additional requirements into the live env.

        Used for ``get_requires_for_build_wheel`` follow-up requests
        (the backend asks for additional deps after the env is
        already up).  Same code path as the constructor's install,
        targeting the same venv.
        """
        if self._venv_path is None or self._python_executable is None:
            msg = "NabBuildEnv used outside its context-manager scope"
            raise BuildEnvError(msg)
        if not requirements:
            return
        wheel_dir = self._venv_path.parent / "wheels"
        # Append a fresh subdir so a re-install does not re-download
        # the same wheel into the same path under a different version.
        sub = wheel_dir / f"_extra_{len(list(wheel_dir.iterdir()))}"
        sub.mkdir(parents=True, exist_ok=True)
        wheel_paths = self._resolve_and_download(sub, extra=requirements)
        self._install_wheels(wheel_paths, _venv_scheme_paths(self._python_executable))

    def _resolve_and_download(
        self,
        wheel_dir: Path,
        *,
        extra: list[str] | None = None,
    ) -> list[Path]:
        """Resolve ``requires`` (+ ``extra``) and write wheels under ``wheel_dir``.

        The inner resolve runs against a synthetic pyproject so it
        can reuse :func:`nab_python.resolve.resolve_for_targets` and
        :func:`nab_python.download.download_lock` end-to-end.  No
        local sources / workspace / marker overlay; build deps
        come from the configured indexes only.
        """
        # Late import: avoids a cycle through ``resolve.py`` which
        # itself imports ``pypi.py`` which imports ``build_backend``
        # which imports this module.
        from ..resolve import build_lock_input, resolve_for_targets

        requires = list(self._requires)
        if extra:
            requires.extend(extra)

        synthetic_dir = wheel_dir.parent / "_inner_project"
        synthetic_dir.mkdir(parents=True, exist_ok=True)
        synthetic = synthetic_dir / "pyproject.toml"
        synthetic.write_text(_render_synthetic_pyproject(requires), encoding="utf-8")

        inner_config = NabProjectConfig(
            indexes=self._config.indexes,
            package_overrides=self._config.package_overrides,
            index_overrides=self._config.index_overrides,
            uploaded_prior_to=self._config.uploaded_prior_to,
        )
        # download_lock closes its transport, and ``install`` may call
        # this again for ``get_requires_for_build_wheel`` follow-ups;
        # build a fresh transport each time.
        transport = self._transport_factory()
        try:
            result = resolve_for_targets(
                synthetic,
                transport,
                config=inner_config,
            )
            # The build env resolves for the host alone, so its one
            # target's failure is the whole resolve's.
            result.raise_for_failure()
        except (
            UnsupportedVcsError,
            NotImplementedError,
            InvalidProjectRequirementError,
        ) as exc:
            # A build requirement nab cannot resolve: a direct-URL/VCS pin, or
            # a string that is not valid PEP 508. Wrap it so the outer resolve
            # skips this sdist rather than aborting on the raw error.
            msg = f"build env resolve failed: {exc}"
            raise BuildEnvError(msg) from exc

        lock_input = build_lock_input(result, config=inner_config)

        # Reject sdist-only pins early: build deps that ship only an
        # sdist trigger a recursive backend invocation that this
        # builder does not handle.  Most build tools (hatchling,
        # setuptools, flit, pdm-backend) publish wheels.
        from ..lockfile import IndexPin

        pins = {
            name: pin
            for lock in lock_input.targets.values()
            for name, pin in lock.pins.items()
        }
        sdist_only: list[str] = []
        for canonical, pin in pins.items():
            if isinstance(pin, IndexPin) and not pin.wheels:
                sdist_only.append(f"{canonical}=={pin.version}")
        if sdist_only:
            msg = (
                "build env requires sdist-only packages which nab cannot"
                " install without recursing through the build path: "
                + ", ".join(sdist_only)
            )
            raise BuildEnvError(msg)

        download_result = download_lock(lock_input, transport, wheel_dir)
        # Both wheels and sdists are downloaded; only wheels feed
        # ``installer.install``.  The sdists are inert clutter under
        # the temp dir, cleaned up with the env.
        all_paths = list(download_result.written) + list(download_result.skipped)
        return [p for p in all_paths if p.suffix == ".whl"]


def _venv_python(venv_path: Path) -> Path:
    r"""Return the venv interpreter path (``Scripts\python.exe`` on Windows)."""
    if sys.platform == "win32":
        return venv_path / "Scripts" / "python.exe"  # pragma: no cover
    return venv_path / "bin" / "python"  # pragma: no cover


def _venv_scheme_paths(python_executable: Path) -> dict[str, str]:
    """Ask the venv's interpreter for the scheme paths ``installer`` writes to.

    Subprocessing the venv guarantees the returned paths reflect the
    venv's layout (``site-packages`` under the venv root, scripts in
    its ``bin``/``Scripts`` dir, etc.) regardless of how nab itself
    was installed.  One subprocess per env construction; negligible.

    ``sysconfig`` has no ``headers`` scheme, and its ``include`` names
    the base interpreter rather than the venv, so the header root comes
    from the venv's own prefix instead.
    """
    result = subprocess.run(  # noqa: S603 - controlled command, no shell
        [str(python_executable), "-c", _SCHEME_PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    probe = json.loads(result.stdout)

    paths: dict[str, str] = dict(probe["paths"])
    paths["headers"] = str(
        Path(probe["prefix"], "include", "site", f"python{probe['py_version']}")
    )
    return paths


def _dist_scheme_paths(
    scheme_paths: dict[str, str], distribution: str
) -> dict[str, str]:
    """Scheme paths for one wheel; its headers go in a directory of its own."""
    paths = dict(scheme_paths)
    paths["headers"] = str(Path(paths["headers"], distribution))
    return paths


def _supports_symlinks() -> bool:
    """``venv.EnvBuilder(symlinks=...)`` heuristic; avoids Windows traps."""
    return sys.platform != "win32"


def _render_synthetic_pyproject(requires: list[str]) -> str:
    """Render a tiny pyproject.toml whose deps are ``requires``.

    Used as input to the inner resolve; the on-disk file is
    discarded with the rest of the temp tree.  The dummy name and
    version make the inner resolver happy without depending on the
    outer project's name/version.
    """
    deps_block = ",\n    ".join(_toml_str(s) for s in requires)
    if deps_block:
        deps_block = f"\n    {deps_block},\n"
    return (
        "[project]\n"
        'name = "_nab_build_env"\n'
        'version = "0.0.0"\n'
        f"dependencies = [{deps_block}]\n"
    )


def _toml_str(value: str) -> str:
    """Escape a string as a single-line TOML basic-string literal.

    Backslash, double-quote, and the control characters TOML forbids bare in a
    basic string (below U+0020, plus U+007F) are escaped, so a requirement
    carrying a newline stays valid TOML instead of splitting across a line.
    """
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch < "\x20" or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
