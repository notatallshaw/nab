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
from typing import TYPE_CHECKING, TypeVar

import tomli_w
from installer import install as installer_install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile

from nab_index.client import extract_sdist_archive
from nab_index.urllib3_async_transport import Urllib3AsyncTransport

from .._vcs_admission import UnsupportedVcsError
from .._vendor.packaging.utils import canonicalize_name
from ..config import NabProjectConfig
from ..download import DownloadError, download_lock
from ..lockfile import IndexPin
from ..provider import BuildPolicy, DistPolicy, MissingExtraError
from ..requirements_file import InvalidProjectRequirementError
from .errors import BuildBackendError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from installer.records import RecordEntry
    from installer.utils import Scheme
    from typing_extensions import Self

    from nab_index.transport import AsyncHttpTransport

    from ..config import IndexOverride, PackageOverride
    from ..lockfile import LockInput, PinShape, SdistArtifact, TargetLock
    from ..tags import TagSet

    _OverrideT = TypeVar("_OverrideT", PackageOverride, IndexOverride)

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


BuildChain = tuple[str, ...]
"""The builds already in progress beneath the first one, outermost first.

Each entry is a ``chain_label`` for a build requirement nab is building so
that some other build can run.  The chain is empty for the build a resolve
asks for directly, and gains an entry every time a build requirement has to
be built to populate an env.  Its length is how deep the recursion has gone,
and a repeated entry is a cycle.
"""


def chain_label(name: str, version: str) -> str:
    """Return the chain entry for one build, canonical so two spellings match."""
    return f"{canonicalize_name(name)} {version}"


@dataclass(frozen=True, slots=True)
class _PendingBuild:
    """A build requirement this env has to build before it can install it."""

    label: str
    sdist: SdistArtifact


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

    ``chain`` is the :data:`BuildChain` of builds this env is already
    nested inside.  It is empty for the build a resolve asked for, and
    every build requirement this env has to build itself passes on a
    longer one, which is what bounds the recursion against
    ``[tool.nab].build-requires-depth`` and detects a cycle.

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
        chain: BuildChain = (),
    ) -> None:
        """Capture inputs; the venv and inner resolve happen in __enter__."""
        self._requires = list(requires)
        self._config = config
        self._offline = offline
        self._transport_factory = transport_factory
        self._chain = tuple(chain)

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

        It admits wheels and sdists, like any resolve, so the version
        it settles on is the one the requirement asked for rather than
        the newest that happens to publish a wheel.  Whether a version
        without an installable wheel can be used is
        :meth:`_plan_install`'s decision, taken once the resolve has
        named it.

        It reads metadata statically: its build policy is ``never`` and
        no inherited override may raise it.  A backend invocation to
        learn a build requirement's dependencies would be a second
        recursion, one the depth budget does not count.
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
            package_overrides=tuple(
                _without_build_permission(override)
                for override in self._config.package_overrides
            ),
            index_overrides={
                name: _without_build_permission(override)
                for name, override in self._config.index_overrides.items()
            },
            uploaded_prior_to=self._config.uploaded_prior_to,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
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
            MissingExtraError,
        ) as exc:
            # A build requirement nab cannot resolve: a direct-URL/VCS pin, an
            # invalid PEP 508 string, or an extra the resolved version does not
            # declare. Wrap it so the outer resolve skips this sdist rather
            # than aborting on the raw error.
            msg = f"build env resolve failed: {exc}"
            raise BuildEnvError(msg) from exc

        lock_input, to_build = self._plan_install(
            build_lock_input(result, config=inner_config)
        )

        try:
            download_result = download_lock(lock_input, transport, wheel_dir)
        except DownloadError as exc:
            # A build dependency failed its HTTP fetch or hash check. Wrap it so
            # the outer resolve skips this sdist rather than aborting on the raw
            # download error.
            msg = f"build env download failed: {exc}"
            raise BuildEnvError(msg) from exc

        all_paths = list(download_result.written) + list(download_result.skipped)
        wheels = [p for p in all_paths if p.suffix == ".whl"]
        wheels.extend(
            self._build_requirement(pending, wheel_dir) for pending in to_build
        )
        return wheels

    @property
    def _build_budget(self) -> int:
        """How many more build envs may be opened beneath this one."""
        return self._config.build_requires_depth - len(self._chain)

    def _plan_install(
        self, lock_input: LockInput
    ) -> tuple[LockInput, list[_PendingBuild]]:
        """Decide how each pin reaches the env, and narrow it to that artifact.

        A pin with a wheel the host accepts is narrowed to that one
        wheel: a lock records every wheel of the pinned version the
        target admits, which can be several (the tiered manylinux
        aliases, or a ``py3-none-any`` beside a platform wheel), and
        the env installs one.  Narrowing is for the download and the
        install; a written lock keeps every wheel, so one lock stays
        portable across targets.

        A pin with no such wheel has to be built, and comes back in the
        second element with its wheels dropped so only its sdist is
        fetched.  Refusing that build is this method's other job: it
        raises rather than leaving a requirement quietly uninstalled,
        because the backend would then fail on an import error that
        names nothing useful.
        """
        planned: dict[str, TargetLock] = {}
        to_build: list[_PendingBuild] = []
        for label, lock in lock_input.targets.items():
            pins: dict[str, PinShape] = {}
            for name, pin in lock.pins.items():
                pins[name] = self._planned_pin(pin, lock.target.tags, to_build)
            planned[label] = replace(lock, pins=pins)

        return replace(lock_input, targets=planned), to_build

    def _planned_pin(
        self, pin: PinShape, tags: TagSet, to_build: list[_PendingBuild]
    ) -> PinShape:
        """Return ``pin`` narrowed to the artifact the env will install.

        Appends to ``to_build`` when the answer is the sdist, so the
        caller knows which pins still owe it a build.

        The wheel is chosen by the target's tags rather than by the
        provider's ``pick_dist``.  That one answers whose METADATA the
        pin has to satisfy, so between wheels the tags rank equally it
        takes the one carrying a :pep:`658` sidecar, and it may answer
        with an sdist or with a wheel this host cannot install.  An
        install has none of those outs: it must be the wheel
        :pep:`425` ranks highest.
        """
        if not isinstance(pin, IndexPin):
            return pin

        preferred = tags.pick(pin.wheels) if pin.wheels else None
        if preferred is not None:
            # The sdist goes with the wheels the narrowing drops: nothing
            # installs it, and a bad one would fail the download and take
            # the build with it.
            return replace(pin, wheels=(preferred,), sdist=None)

        label = chain_label(pin.name, pin.version)
        chain = self._chain
        if pin.sdist is None:
            msg = (
                f"{label} publishes no wheel this build host can install,"
                f" and no sdist to build{_chain_suffix(chain)}"
            )
            raise BuildEnvError(msg)
        # A cycle before a depth: raising the depth to walk into a loop is
        # the one piece of advice that cannot help.
        if label in chain:
            msg = f"cyclic build requirement: {' -> '.join([*chain, label])}"
            raise BuildEnvError(msg)
        if self._build_budget <= 0:
            msg = (
                f"{label} publishes no wheel this build host can install, so"
                " satisfying it means building it; [tool.nab].build-requires-depth"
                f" is {self._config.build_requires_depth}{_chain_suffix(chain)}"
            )
            raise BuildEnvError(msg)

        to_build.append(_PendingBuild(label=label, sdist=pin.sdist))
        return replace(pin, wheels=())

    def _build_requirement(self, pending: _PendingBuild, wheel_dir: Path) -> Path:
        """Build the downloaded sdist of ``pending`` and return the wheel's path.

        The wheel lands beside the downloaded artifacts, which live as
        long as the env does.
        """
        # Late import: ``runner`` imports this module at module load.
        from .runner import build_wheel_for_install

        label = pending.label
        archive = wheel_dir / pending.sdist.filename
        logger.info("building %s to populate a build env", label)

        try:
            data = archive.read_bytes()
        except OSError as exc:
            msg = f"build requirement {label} could not be read at {archive}: {exc}"
            raise BuildEnvError(msg) from exc

        with tempfile.TemporaryDirectory(prefix="nab-build-req-") as td:
            try:
                source_dir = extract_sdist_archive(data, Path(td))
            except ValueError as exc:
                msg = f"build requirement {label} could not be extracted: {exc}"
                raise BuildEnvError(msg) from exc
            try:
                return build_wheel_for_install(
                    source_dir,
                    output_dir=wheel_dir,
                    config=self._config,
                    offline=self._offline,
                    chain=(*self._chain, label),
                )
            except BuildBackendError as exc:
                msg = f"build requirement {label} could not be built: {exc}"
                raise BuildEnvError(msg) from exc


def _chain_suffix(chain: BuildChain) -> str:
    """Render ``chain`` for an error message, or nothing when it is empty."""
    return f" (chain: {' -> '.join(chain)})" if chain else ""


def _without_build_permission(override: _OverrideT) -> _OverrideT:
    """Return ``override`` with any build permission it grants removed.

    An override reaches the build env's own resolve, where the build
    policy is ``never``.  Letting one raise it there would start a
    backend invocation nothing counts against the depth budget, so a
    permission is dropped and only a refusal survives.
    """
    if override.build_policy in (None, BuildPolicy.NEVER):
        return override
    return replace(override, build_policy=None)


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
    return tomli_w.dumps(
        {
            "project": {
                "name": "_nab_build_env",
                "version": "0.0.0",
                "dependencies": requires,
            }
        }
    )
