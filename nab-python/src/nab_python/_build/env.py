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
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from installer import install as installer_install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from nab_index.urllib3_async_transport import Urllib3AsyncTransport

from .._vcs_admission import UnsupportedVcsError
from ..config import NabProjectConfig
from ..download import DownloadError, download_lock
from ..lockfile import IndexPin
from ..requirements_file import InvalidProjectRequirementError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from typing_extensions import Self

    from installer.records import RecordEntry
    from installer.utils import Scheme
    from nab_index.transport import AsyncHttpTransport

    from ..lockfile import LockInput, PinShape
    from ..tags import TagSet

__all__ = [
    "NabBuildEnv",
]


@dataclass
class _FastSchemeDictionaryDestination(SchemeDictionaryDestination):
    """``SchemeDictionaryDestination`` that skips bytecode-compile work.

    The stock ``_compile_bytecode`` always resolves the target file's
    real path before checking ``bytecode_optimization_levels``; with
    an empty levels tuple, the loop body never runs but the resolve
    still hits the filesystem twice per record.  Skipping the early
    work when the levels tuple is empty is safe because the body
    would have been a no-op.

    ``written`` records ``(scheme root, path within it)`` for every
    file the install writes, so a later install can remove them.
    """

    written: list[tuple[Path, Path]] = field(
        default_factory=list, init=False, repr=False
    )

    def _compile_bytecode(self, scheme: Scheme, record: RecordEntry) -> None:
        if not self.bytecode_optimization_levels:
            return
        super()._compile_bytecode(scheme, record)

    def finalize_installation(
        self,
        scheme: Scheme,
        record_file_path: str,
        records: Iterable[tuple[Scheme, RecordEntry]],
    ) -> None:
        # ``records`` is consumed once, so materialize it before passing it on.
        entries = list(records)
        self.written = [
            (Path(self.scheme_dict[entry_scheme]), Path(entry.path))
            for entry_scheme, entry in entries
        ]

        super().finalize_installation(scheme, record_file_path, entries)


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
    """The build env could not be set up.

    Covers venv creation, the interpreter scheme probe, and the inner
    resolve, download, and install of ``[build-system].requires``.
    """


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

    ``offline`` refuses to populate the env at all, since every build
    requirement would have to come off the network.  An empty
    ``requires`` needs nothing fetched and is still served.

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
        offline: bool = False,
        transport_factory: Callable[[], AsyncHttpTransport] = Urllib3AsyncTransport,
    ) -> None:
        """Capture inputs; the venv and inner resolve happen in __enter__."""
        self._requires = list(requires)
        self._config = config
        self._offline = offline
        self._transport_factory = transport_factory

        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._venv_path: Path | None = None
        self._python_executable: Path | None = None
        self._scripts_dir: Path | None = None
        self._installed_files: list[tuple[Path, Path]] = []

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

        logger.debug("creating build venv at %s", self._venv_path)
        builder = venv.EnvBuilder(
            with_pip=False, symlinks=_supports_symlinks(), clear=False
        )
        try:
            wheel_dir.mkdir()
            builder.create(self._venv_path)
        except OSError as exc:
            msg = f"could not create build venv at {self._venv_path}: {exc}"
            raise BuildEnvError(msg) from exc

        self._python_executable = _venv_python(self._venv_path)
        self._scripts_dir = self._python_executable.parent

        scheme_paths = _venv_scheme_paths(self._python_executable)

        if not self._requires:
            return

        self._install_wheels(self._resolve_and_download(wheel_dir), scheme_paths)

    def _install_wheels(
        self, wheel_paths: list[Path], scheme_paths: dict[str, str]
    ) -> None:
        """Remove what this env installed before, then install ``wheel_paths``.

        Each resolve produces a whole environment, so the previous
        install comes out rather than being written over: two
        ``.dist-info`` directories for one distribution leave the
        version ``importlib.metadata`` reports up to listing order.
        The removals all run before the first write, since two
        distributions can ship the same path within a scheme.

        A downloaded build dependency can be corrupt or malformed, so
        opening or installing it surfaces as :class:`BuildEnvError`
        rather than a raw zip or installer error.
        """
        _remove_files(self._installed_files)
        self._installed_files = []

        for wheel_path in wheel_paths:
            logger.debug("installing %s", wheel_path.name)
            try:
                with WheelFile.open(wheel_path) as source:
                    destination = _FastSchemeDictionaryDestination(
                        scheme_dict=_dist_scheme_paths(
                            scheme_paths, source.distribution
                        ),
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
                    self._installed_files.extend(destination.written)
            except Exception as exc:
                msg = f"could not install build dependency {wheel_path.name}: {exc}"
                raise BuildEnvError(msg) from exc

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
        already up).  The inner resolve runs over ``requires`` and
        ``requirements`` together, so its result is the whole build
        env rather than an addition to it: it can pin a different
        version of something already installed, or drop it.
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

        if self._offline:
            joined = ", ".join(requires)
            msg = f"build requirements unavailable in offline mode: {joined}"
            raise BuildEnvError(msg)

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

        lock_input = _one_wheel_per_pin(build_lock_input(result, config=inner_config))

        # Reject sdist-only pins early: build deps that ship only an
        # sdist trigger a recursive backend invocation that this
        # builder does not handle.  Most build tools (hatchling,
        # setuptools, flit, pdm-backend) publish wheels.
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

        try:
            download_result = download_lock(lock_input, transport, wheel_dir)
        except DownloadError as exc:
            # A build dependency failed its HTTP fetch or hash check. Wrap it so
            # the outer resolve skips this sdist rather than aborting on the raw
            # download error.
            msg = f"build env download failed: {exc}"
            raise BuildEnvError(msg) from exc

        # Both wheels and sdists are downloaded; only wheels feed
        # ``installer.install``.  The sdists are inert clutter under
        # the temp dir, cleaned up with the env.
        all_paths = list(download_result.written) + list(download_result.skipped)
        return [p for p in all_paths if p.suffix == ".whl"]


def _one_wheel_per_pin(lock_input: LockInput) -> LockInput:
    """Narrow every index pin to the one wheel its target installs.

    A lock records every wheel of the pinned version the target
    accepts, so a pin can carry several: the tiered manylinux
    aliases, or a ``py3-none-any`` beside a platform wheel.  The
    inner resolve plans the host alone, so the target's tags are
    those of the interpreter the backend runs under.

    Narrowing is for the download and the install; a written lock
    keeps every wheel, so one lock stays portable across targets.
    """
    targets = {
        label: replace(
            lock,
            pins={
                name: _picked_wheel_pin(pin, lock.target.tags)
                for name, pin in lock.pins.items()
            },
        )
        for label, lock in lock_input.targets.items()
    }

    return replace(lock_input, targets=targets)


def _picked_wheel_pin(pin: PinShape, tags: TagSet) -> PinShape:
    """Return ``pin`` narrowed to the one wheel ``tags`` prefers.

    Deliberately not the provider's ``pick_dist``.  That one answers
    whose METADATA the pin has to satisfy, so between wheels the tags
    rank equally it takes the one carrying a :pep:`658` sidecar, and
    it may answer with an sdist or with a wheel this host cannot
    install.  An install has none of those outs: it must be a wheel,
    it must be the wheel :pep:`425` ranks highest, and nothing
    compatible is an error.
    """
    if not isinstance(pin, IndexPin) or not pin.wheels:
        return pin

    preferred = tags.pick(pin.wheels)
    if preferred is None:
        msg = f"no wheel of {pin.name}=={pin.version} matches the build host's tags"
        raise BuildEnvError(msg)

    return replace(pin, wheels=(preferred,))


def _remove_files(entries: list[tuple[Path, Path]]) -> None:
    """Delete installed files and their bytecode, and directories left empty.

    ``entries`` are ``(scheme root, path within it)`` pairs.  The
    scheme roots stay, as does any directory still holding something
    the install did not write.

    Cached bytecode goes with its source file: the backend has already
    run in this venv, so a removed package has usually been imported,
    and the ``__pycache__`` that import wrote would keep its directory
    importable as a namespace package.
    """
    directories: set[Path] = set()
    roots: set[Path] = set()

    for root, relative in entries:
        target = root / relative
        target.unlink(missing_ok=True)

        if target.suffix == ".py":
            cache = target.parent / "__pycache__"
            for compiled in cache.glob(f"{target.stem}.*.pyc"):
                compiled.unlink()
            directories.add(cache)

        roots.add(root)
        directories.update(root / parent for parent in relative.parents)

    # Deepest first, so a parent is tried after the children that emptied it.
    for directory in sorted(
        directories - roots, key=lambda path: len(path.parts), reverse=True
    ):
        with suppress(OSError):
            directory.rmdir()


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
    try:
        result = subprocess.run(  # noqa: S603 - controlled command, no shell
            [str(python_executable), "-c", _SCHEME_PROBE],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        msg = f"build venv interpreter probe failed: {exc}"
        raise BuildEnvError(msg) from exc

    try:
        probe = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        msg = f"build venv interpreter probe returned non-JSON output: {exc}"
        raise BuildEnvError(msg) from exc

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
