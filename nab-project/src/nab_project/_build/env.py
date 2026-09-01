"""Isolated build env that uses nab itself plus the PyPA ``installer``.

``NabBuildEnv`` implements ``build.env.IsolatedEnv`` so it slots into
``build.ProjectBuilder.from_isolated_env``.  The pieces:

* ``venv.EnvBuilder`` (stdlib) creates an empty interpreter at a temp
  path, ``with_pip=False``; nab does not need pip in there.
* nab's own resolver picks versions for ``[build-system].requires``
  using the same indexes / ``uploaded-prior-to`` window as the outer
  resolve.
* ``download_lock`` from :mod:`nab_project.download` fetches the
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

import glob
import json
import logging
import os
import subprocess
import sys
import tempfile
import venv
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import tomli_w
from installer import install as installer_install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from installer.utils import get_launcher_kind

from nab_index.client import extract_sdist_archive
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_provider._vendor.packaging.requirements import InvalidRequirement
from nab_provider._vendor.packaging.utils import (
    canonicalize_name,
    parse_wheel_filename,
)
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import MissingExtraError
from nab_provider.marker_holds import (
    IntractableMarkerError,
    UnevaluableMarkerError,
    dependency_marker_holds,
)
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import BuildPolicy, DistPolicy
from nab_provider.requirements_file import InvalidProjectRequirementError
from nab_provider.target import ResolveTarget, host_environment
from nab_provider.vcs_admission import UnsupportedVcsError

from ..download import DownloadError, download_lock
from ..inputs import ResolveInputs
from ..lockfile import IndexPin, strip_userinfo
from .errors import BuildBackendError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from installer.records import RecordEntry
    from installer.scripts import LauncherKind
    from installer.utils import Scheme
    from typing_extensions import Self

    from nab_index.transport import AsyncHttpTransport
    from nab_provider.overrides import IndexOverride, PackageOverride
    from nab_provider.tags import TagSet

    from ..lockfile import LockInput, PinShape, SdistArtifact, TargetLock

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


def _detect_launcher_kind() -> LauncherKind:
    """Name the script-launcher stub this interpreter needs.

    ``get_launcher_kind`` reads the architecture out of ``sys.version``, which
    carries none on a Windows build MSVC did not compile.  It answers
    ``win-ia32`` whenever it cannot tell, so fall back to the word size there.
    """
    kind = get_launcher_kind()
    if kind == "win-ia32" and sys.maxsize > 2**32:
        return "win-amd64"
    return kind


_LAUNCHER_KIND = _detect_launcher_kind()

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

    Covers the temp tree the env lives in, venv creation, the
    interpreter scheme probe, and the inner resolve, download, and
    install of ``[build-system].requires``.
    """


@contextmanager
def _as_build_env_error(action: str) -> Iterator[None]:
    """Re-raise an ``OSError`` from the block as :class:`BuildEnvError`.

    ``action`` opens the message, so it names the entry point rather
    than the individual write that failed.
    """
    try:
        yield
    except OSError as exc:
        msg = f"{action}: {exc}"
        raise BuildEnvError(msg) from exc


def _build_tempdir(prefix: str) -> tempfile.TemporaryDirectory[str]:
    """Return a temp directory for the build, or raise :class:`BuildEnvError`."""
    try:
        return tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    except OSError as exc:
        msg = f"could not create a temporary build directory: {exc}"
        raise BuildEnvError(msg) from exc


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

    name: str
    version: str
    sdist: SdistArtifact

    @property
    def label(self) -> str:
        """The chain entry naming this build."""
        return chain_label(self.name, self.version)


class NabBuildEnv:
    """An isolated PEP 518 build environment driven by nab.

    Implements ``build.env.IsolatedEnv`` so it can be passed to
    ``build.ProjectBuilder.from_isolated_env``. The runtime cost
    is one venv creation, one inner resolve over
    ``[build-system].requires``, one wheel download per dep, and
    one ``installer.install`` per wheel.

    ``requires`` is the PEP 508 string list from
    ``[build-system].requires``.  ``config`` carries the outer resolve's
    settings, pruned of declared sources, constraints and group selection
    so the build env resolves against the configured indexes alone.

    ``offline`` refuses to populate the env when a build requirement
    would have to come off the network.  A ``requires`` that is empty,
    or whose entries are all excluded by their markers on the host,
    needs nothing fetched and is still served.

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
        config: ResolveInputs,
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
        self._tmpdir = _build_tempdir("nab-build-env-")
        try:
            with _as_build_env_error("could not populate the build env"):
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

        # From 3.11, venv refuses an env path containing os.pathsep with a ValueError.
        try:
            wheel_dir.mkdir()
            builder.create(self._venv_path)
        except (OSError, ValueError) as exc:
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
        """Clean up the temp tree that holds the venv and downloaded wheels."""
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

        with _as_build_env_error("could not install extra build requirements"):
            wheel_dir = self._venv_path.parent / "wheels"
            # Append a fresh subdir so a re-install does not re-download
            # the same wheel into the same path under a different version.
            sub = wheel_dir / f"_extra_{len(list(wheel_dir.iterdir()))}"
            sub.mkdir(parents=True, exist_ok=True)

            wheel_paths = self._resolve_and_download(sub, extra=requirements)
            scheme_paths = _venv_scheme_paths(self._python_executable)
            self._install_wheels(wheel_paths, scheme_paths)

    def _resolve_and_download(
        self,
        wheel_dir: Path,
        *,
        extra: list[str] | None = None,
    ) -> list[Path]:
        """Resolve ``requires`` (+ ``extra``) and write wheels under ``wheel_dir``.

        The inner resolve runs against a synthetic pyproject so it
        can reuse :func:`nab_project.resolve.resolve_for_targets` and
        :func:`nab_project.download.download_lock` end-to-end.  No
        local sources and no marker overlay; build deps come from the
        configured indexes only.

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

        Offline it returns no wheels rather than refusing when the
        host's markers exclude every entry.
        """
        # Late import, breaking the cycle ``resolve`` -> ``fetch`` ->
        # ``_sources`` -> ``build_backend`` -> ``_build.runner`` -> this
        # module: building an sdist resolves that sdist's build requirements.
        from ..resolve import build_lock_input, resolve_for_targets

        requires = list(self._requires)
        if extra:
            requires.extend(extra)

        if self._offline:
            needed = _applicable_requirements(requires)
            if not needed:
                return []

            joined = ", ".join(needed)
            msg = f"build requirements unavailable in offline mode: {joined}"
            raise BuildEnvError(msg)

        synthetic_dir = wheel_dir.parent / "_inner_project"
        synthetic_dir.mkdir(parents=True, exist_ok=True)
        synthetic = synthetic_dir / "pyproject.toml"
        synthetic.write_text(_render_synthetic_pyproject(requires), encoding="utf-8")

        inner_inputs = _inner_resolve_inputs(self._config)

        # download_lock closes its transport, and ``install`` may call
        # this again for ``get_requires_for_build_wheel`` follow-ups;
        # build a fresh transport each time.
        transport = self._transport_factory()
        try:
            result = resolve_for_targets(
                synthetic,
                transport,
                targets=(ResolveTarget.for_host(),),
                inputs=inner_inputs,
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
            build_lock_input(result, inputs=inner_inputs)
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
            return pin.replace(wheels=(preferred,), sdist=None)

        label = chain_label(pin.name, pin.version)
        chain = self._chain

        if pin.sdist is None:
            reason = _no_wheel_clause(self._config, pin, label)
            msg = f"{reason}, and no sdist to build{_chain_suffix(chain)}"
            raise BuildEnvError(msg)

        # A cycle before a depth: raising the depth to walk into a loop is
        # the one piece of advice that cannot help.
        if label in chain:
            msg = f"cyclic build requirement: {' -> '.join([*chain, label])}"
            raise BuildEnvError(msg)

        if self._build_budget <= 0:
            reason = _no_wheel_clause(self._config, pin, label)
            msg = (
                f"{reason}, so satisfying it means building it;"
                " [tool.nab].build-requires-depth is"
                f" {self._config.build_requires_depth}{_chain_suffix(chain)}"
            )
            raise BuildEnvError(msg)

        to_build.append(
            _PendingBuild(name=pin.name, version=pin.version, sdist=pin.sdist)
        )
        return pin.replace(wheels=())

    def _build_requirement(self, pending: _PendingBuild, wheel_dir: Path) -> Path:
        """Build the downloaded sdist of ``pending`` and return the wheel's path.

        The wheel lands beside the downloaded artifacts, which live as
        long as the env does.  A wheel naming another release is
        refused.
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

        with _build_tempdir("nab-build-req-") as td:
            try:
                source_dir = extract_sdist_archive(data, Path(td))
            except ValueError as exc:
                msg = f"build requirement {label} could not be extracted: {exc}"
                raise BuildEnvError(msg) from exc
            try:
                built = build_wheel_for_install(
                    source_dir,
                    output_dir=wheel_dir,
                    config=self._config,
                    offline=self._offline,
                    chain=(*self._chain, label),
                )
            except BuildBackendError as exc:
                msg = f"build requirement {label} could not be built: {exc}"
                raise BuildEnvError(msg) from exc

            _check_built_identity(pending, built)
            return built


def _check_built_identity(pending: _PendingBuild, wheel: Path) -> None:
    """Raise unless ``wheel``'s filename names the release ``pending`` pinned.

    A backend picks the name and version it emits, and the env's other
    wheels were resolved for the release the pin names.
    """
    try:
        name, version, _build, _tags = parse_wheel_filename(wheel.name)
    except ValueError as exc:
        msg = (
            f"build requirement {pending.label} produced {wheel.name!r},"
            f" which is not a wheel filename: {exc}"
        )
        raise BuildEnvError(msg) from exc

    if name != canonicalize_name(pending.name) or version != Version(pending.version):
        msg = (
            f"build requirement {pending.label} built a wheel whose filename"
            f" names {name}=={version}, not the release the resolve pinned"
        )
        raise BuildEnvError(msg)


def _chain_suffix(chain: BuildChain) -> str:
    """Render ``chain`` for an error message, or nothing when it is empty."""
    return f" (chain: {' -> '.join(chain)})" if chain else ""


def _wheel_barring_dist_policy(
    inputs: ResolveInputs, pin: IndexPin
) -> DistPolicy | None:
    """Return the ``dist-policy`` in force for ``pin`` when it bars wheels.

    ``None`` when neither a per-package nor a per-index override sets
    ``sdist-only`` or ``sdist-install``: those two are the whole of what
    can bar a wheel here, because the build env's own resolve runs at
    ``wheel-or-sdist``.  Whether the index published a wheel to bar is
    not known here, so a returned policy says the env would have refused
    one, not that one existed.

    A per-package and a per-index override that both set the field are a
    conflict the resolve has already raised, so the per-package one is
    read first.
    """
    canonical = canonicalize_name(pin.name)
    version = Version(pin.version)

    policy = next(
        (
            package.dist_policy
            for package in inputs.package_overrides
            if package.dist_policy is not None
            and package.name == canonical
            and version in package.version_range
        ),
        None,
    )

    if policy is None:
        policy = _serving_index_dist_policy(inputs, pin.index)

    if policy is DistPolicy.SDIST_ONLY or policy is DistPolicy.SDIST_INSTALL:
        return policy
    return None


def _serving_index_dist_policy(
    inputs: ResolveInputs, pin_index: str
) -> DistPolicy | None:
    """Return the ``dist-policy`` the index that served ``pin_index`` sets.

    An override is keyed by the configured index name, and a pin records
    its index URL with credentials stripped, so the match runs over
    stripped URLs.  Two indexes differing only in credentials are
    indistinguishable to a pin, so neither is read.
    """
    serving = [
        index for index in inputs.indexes if strip_userinfo(index.url) == pin_index
    ]
    if len(serving) != 1:
        return None

    override = inputs.index_overrides.get(serving[0].name)
    return override.dist_policy if override is not None else None


def _no_wheel_clause(inputs: ResolveInputs, pin: IndexPin, label: str) -> str:
    """Return the opening of a refusal: why the env has no wheel of ``label``."""
    barred_by = _wheel_barring_dist_policy(inputs, pin)
    if barred_by is None:
        return f"{label} publishes no wheel this build host can install"
    return (
        f"{label} has dist-policy '{barred_by.value}',"
        " which admits no wheel into the build env"
    )


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


def _inner_resolve_inputs(inputs: ResolveInputs) -> ResolveInputs:
    """Return the settings the build-requires resolve runs under.

    Only the fields named here cross into it: build deps come from the
    configured indexes alone, so the outer run's own requirements and
    sources stay out.  The cutoff and the decision order cross because
    this resolve picks the backend that writes the metadata the outer
    resolve reads, and a lock reproduces only if this search does too.
    """
    return ResolveInputs(
        indexes=inputs.indexes,
        package_overrides=tuple(
            _without_build_permission(override) for override in inputs.package_overrides
        ),
        index_overrides={
            name: _without_build_permission(override)
            for name, override in inputs.index_overrides.items()
        },
        uploaded_prior_to=inputs.uploaded_prior_to,
        decision_order=inputs.decision_order,
        dist_policy=DistPolicy.WHEEL_OR_SDIST,
        build_policy=BuildPolicy.NEVER,
    )


def _remove_files(entries: list[tuple[Path, Path]]) -> None:
    """Delete installed files and their bytecode, and directories left empty.

    ``entries`` are ``(scheme root, path within it)`` pairs.  The
    scheme roots stay, as does any directory still holding something
    the install did not write.

    Cached bytecode goes with its source file: the backend has already
    run in this venv, so a removed package has usually been imported,
    and the ``__pycache__`` that import wrote would keep its directory
    importable as a namespace package.

    A wheel member's name can hold glob syntax, so the stem is escaped
    before the cache is searched for it.
    """
    directories: set[Path] = set()
    roots: set[Path] = set()

    for root, relative in entries:
        target = root / relative
        target.unlink(missing_ok=True)

        if target.suffix == ".py":
            cache = target.parent / "__pycache__"
            for compiled in cache.glob(f"{glob.escape(target.stem)}.*.pyc"):
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

    ``-I`` keeps the current directory off the probe's ``sys.path``, so a
    ``json.py`` or ``sysconfig.py`` sitting there cannot answer it.
    """
    try:
        result = subprocess.run(  # noqa: S603 - controlled command, no shell
            [str(python_executable), "-I", "-c", _SCHEME_PROBE],
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


def _applicable_requirements(requires: list[str]) -> list[str]:
    """Return the entries of ``requires`` the host has to install.

    The venv is created from the host interpreter, so a requirement
    whose PEP 508 marker excludes the host is never installed.
    """
    environment = host_environment()
    return [
        requirement
        for requirement in requires
        if _requirement_applies(requirement, environment)
    ]


def _requirement_applies(requirement: str, environment: Mapping[str, str]) -> bool:
    """Whether ``requirement`` applies under the ``environment`` marker values.

    An unparseable string and a marker nothing decides both count as
    applying, since neither shows the host to be excluded.
    """
    try:
        parsed = parse_requirement(requirement)
    except InvalidRequirement:
        return True

    if parsed.marker is None:
        return True

    try:
        return dependency_marker_holds(parsed.marker, environment)
    except (UnevaluableMarkerError, IntractableMarkerError):
        return True


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
