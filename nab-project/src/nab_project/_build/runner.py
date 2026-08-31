"""Orchestrate ``[build-system]`` setup and metadata extraction.

The entry point is :func:`run_build_backend`.  Given a source
directory with a ``pyproject.toml`` whose ``project.dynamic``
contains fields nab cannot read statically, run_build_backend:

1. Reads ``[build-system].requires`` and ``[build-system].build-backend``
   (defaulting to ``setuptools.build_meta:__legacy__`` per PEP 517).
2. Opens a :class:`~nab_project._build.env.NabBuildEnv` populated with
   those requirements.
3. Hands the env to ``build.ProjectBuilder.from_isolated_env`` and
   asks it for the wheel metadata via ``prepare()``, falling back to
   a full ``build_wheel`` and reading the wheel's own dist-info when
   the backend lacks ``prepare_metadata_for_build_wheel``.
4. Parses the resulting ``METADATA`` file into
   :class:`~nab_provider.metadata.WheelMetadata`.

The hatchling-with-dynamic-deps quirk uv documents (the prepare
hook can return data that does not match the eventual wheel) is
covered by skipping the prepare step for that combination; see
:func:`_should_skip_prepare`.
"""

from __future__ import annotations

import logging
import lzma
import os
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from email import message_from_string
from pathlib import Path
from typing import TYPE_CHECKING, Any

import build
import pyproject_hooks
import tomli

from nab_index.local_index import wheel_metadata_member
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import UnsupportedWheelError
from nab_provider.metadata import WheelMetadata, validate_specifier_versions
from nab_provider.pep508 import parse_requirement
from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    require_string_list,
)
from nab_resolver.errors import ResolutionError

from .. import toml_io
from ..paths import PathState, path_state
from .env import BuildChain, BuildEnvError, NabBuildEnv
from .errors import BuildBackendError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..inputs import ResolveInputs

__all__ = [
    "BuildBackendError",
    "build_wheel_for_install",
    "run_build_backend",
]

logger = logging.getLogger(__name__)


_DEFAULT_BACKEND = "setuptools.build_meta:__legacy__"
_DEFAULT_REQUIRES = ("setuptools >= 40.8.0",)


def run_build_backend(
    source_dir: Path,
    *,
    config: ResolveInputs,
    offline: bool = False,
    chain: BuildChain = (),
) -> WheelMetadata:
    """Extract wheel metadata for ``source_dir`` via the build backend.

    Returns a :class:`~nab_provider.metadata.WheelMetadata` parsed from
    the ``METADATA`` file the backend produces.  Raises
    :class:`BuildBackendError` on any failure: backend import
    error, a rejected ``backend-path``, hook crash, METADATA that
    is unreadable or malformed, an unreadable built wheel, scratch
    space that cannot be created, a build requirement that cannot be
    installed or built, or build requirements ``offline`` bars from
    being fetched.

    The build runs in an isolated venv driven by
    :class:`NabBuildEnv`; nothing in the user's main environment is
    perturbed.  The build env owns its own HTTP transport (see
    :class:`NabBuildEnv` for why) so callers do not pass one in.

    ``chain`` names the builds this one is nested inside; see
    :data:`~nab_project._build.env.BuildChain`.
    """
    data = _read_pyproject(source_dir)

    with (
        _prepared_project(
            source_dir, data, config=config, offline=offline, chain=chain
        ) as (project, backend),
        _metadata_output_dir(backend) as out_str,
    ):
        metadata_dir = _extract_metadata_dir(
            project,
            Path(out_str),
            backend=backend,
            skip_prepare=_should_skip_prepare(backend, data),
        )
        return _parse_metadata(metadata_dir / "METADATA")


def build_wheel_for_install(
    source_dir: Path,
    *,
    output_dir: Path,
    config: ResolveInputs,
    offline: bool = False,
    chain: BuildChain = (),
) -> Path:
    """Build ``source_dir`` into a wheel under ``output_dir`` and return its path.

    The metadata path only ever needs a backend's answer, so it can
    stop at ``prepare_metadata_for_build_wheel``.  This one is for a
    build requirement that has to be installed, which takes a real
    wheel and therefore the full ``build_wheel`` hook.

    Raises :class:`BuildBackendError` on any failure, like
    :func:`run_build_backend`.
    """
    data = _read_pyproject(source_dir)

    with _prepared_project(
        source_dir, data, config=config, offline=offline, chain=chain
    ) as (project, backend):
        try:
            return Path(project.build("wheel", str(output_dir)))
        # PEP 517's path-returning hooks must return a basename string, so a
        # non-string reaches os.path.join as-is.
        except TypeError as exc:
            msg = (
                f"build backend {backend!r} returned a non-string path"
                f" from build_wheel: {exc}"
            )
            raise BuildBackendError(msg) from exc


def _metadata_output_dir(backend: str) -> tempfile.TemporaryDirectory[str]:
    """Return the temp directory ``backend`` writes its metadata into."""
    try:
        return tempfile.TemporaryDirectory(
            prefix="nab-build-meta-", ignore_cleanup_errors=True
        )
    except OSError as exc:
        msg = (
            "could not create a temporary metadata directory for build"
            f" backend {backend!r}: {exc}"
        )
        raise BuildBackendError(msg) from exc


@contextmanager
def _prepared_project(
    source_dir: Path,
    data: dict,
    *,
    config: ResolveInputs,
    offline: bool,
    chain: BuildChain,
) -> Iterator[tuple[build.ProjectBuilder, str]]:
    """Yield a builder for ``source_dir`` in an env holding its build requirements.

    Yields the backend name beside the builder because every failure
    message names it.  Failures from setting the env up and from the
    caller's own block both come out as :class:`BuildBackendError`,
    which is the contract both entry points advertise.
    """
    backend, requires, backend_path = _read_build_system(data)
    _validate_backend_path(source_dir, backend_path)

    try:
        with NabBuildEnv(
            requires=list(requires), config=config, offline=offline, chain=chain
        ) as env:
            project = build.ProjectBuilder.from_isolated_env(
                env,
                source_dir=str(source_dir),
                runner=pyproject_hooks.quiet_subprocess_runner,
            )
            _install_extra_requires(project, env, backend=backend)
            yield project, backend
    except (
        build.BuildException,
        build.BuildBackendException,
        build.FailedProcessError,
    ) as exc:
        msg = f"build backend {backend!r} failed: {exc}"
        raise BuildBackendError(msg) from exc
    except (BuildEnvError, ResolutionError) as exc:
        msg = f"build env setup for {backend!r} failed: {exc}"
        raise BuildBackendError(msg) from exc


def _install_extra_requires(
    project: build.ProjectBuilder, env: NabBuildEnv, *, backend: str
) -> None:
    """Install whatever ``get_requires_for_build_wheel`` asks for on top."""
    # build returns the hook's result as a set, so sort for a stable
    # order. key=str keeps a non-string item from raising here.
    extra = sorted(project.get_requires_for_build("wheel"), key=str)
    if not extra:
        return

    _validate_extra_requires(extra, backend=backend)

    logger.debug("build backend asked for extras: %s", extra)
    env.install(extra)


def _validate_extra_requires(extra: list[Any], *, backend: str) -> None:
    """Reject a hook result the build env cannot install.

    PEP 517 requires strings, and the build env writes each one into the
    UTF-8 pyproject its inner resolve reads.
    """
    non_str = [item for item in extra if not isinstance(item, str)]
    if non_str:
        msg = (
            f"build backend {backend!r} returned a non-string build"
            f" requirement from get_requires_for_build_wheel: {non_str!r}"
        )
        raise BuildBackendError(msg)

    for item in extra:
        try:
            item.encode("utf-8")
        except UnicodeEncodeError as exc:
            msg = (
                f"build backend {backend!r} returned a build requirement from"
                f" get_requires_for_build_wheel that cannot be encoded as"
                f" UTF-8: {item!r}"
            )
            raise BuildBackendError(msg) from exc


def _read_pyproject(source_dir: Path) -> dict:
    """Return the parsed ``pyproject.toml``, or ``{}`` for a legacy setup.py tree."""
    pyproject = source_dir / "pyproject.toml"
    state = path_state(pyproject)

    if state.should_read:
        try:
            return toml_io.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
            msg = f"could not read pyproject.toml at {source_dir}: {exc}"
            raise BuildBackendError(msg) from exc
    if state is not PathState.ABSENT:
        # The file is there, so the tree is not the legacy setup.py case.
        msg = f"{pyproject} exists but is not a regular file"
        raise BuildBackendError(msg)
    if path_state(source_dir / "setup.py").should_read:
        # PEP 517 fallback for legacy setup.py projects: treat the
        # missing ``[build-system]`` as the documented default
        # (setuptools.build_meta:__legacy__).
        return {}

    msg = f"no pyproject.toml or setup.py at {source_dir}"
    raise BuildBackendError(msg)


def _read_build_system(
    data: dict,
) -> tuple[str, tuple[str, ...], tuple[str, ...] | None]:
    """Return ``(backend, requires, backend_path)`` per PEP 517 / 518.

    A missing ``[build-system]`` takes the PEP 517 defaults; a
    ``build-system`` that is not a table is malformed, not absent.
    Inside the table a default stands in only for a key the file omits,
    never for one it declares with the wrong shape.
    """
    if "build-system" not in data:
        return _DEFAULT_BACKEND, _DEFAULT_REQUIRES, None

    table = data["build-system"]
    if not isinstance(table, dict):
        msg = (
            "[build-system] in pyproject.toml must be a table,"
            f" not {type(table).__name__}"
        )
        raise BuildBackendError(msg)

    if "requires" not in table:
        msg = (
            "[build-system].requires is required by PEP 518 and"
            " pyproject.toml does not declare it"
        )
        raise BuildBackendError(msg)

    requires = _read_string_array(table["requires"], "requires")

    backend = table.get("build-backend", _DEFAULT_BACKEND)
    if not isinstance(backend, str):
        msg = (
            "[build-system].build-backend in pyproject.toml must be a string,"
            f" not {type(backend).__name__}"
        )
        raise BuildBackendError(msg)

    backend_path = (
        _read_string_array(table["backend-path"], "backend-path")
        if "backend-path" in table
        else None
    )

    return backend, requires, backend_path


def _read_string_array(value: object, key: str) -> tuple[str, ...]:
    """Return ``[build-system].<key>``, or raise if it is not an array of strings."""
    try:
        return tuple(require_string_list(value, f"[build-system].{key}"))
    except InvalidProjectRequirementError as exc:
        raise BuildBackendError(str(exc)) from exc


def _validate_backend_path(
    source_dir: Path, backend_path: tuple[str, ...] | None
) -> None:
    """Reject a ``backend-path`` a PEP 517 frontend would refuse.

    pyproject_hooks raises a bare ``ValueError`` for an absolute entry
    or one that leaves the source tree.
    """
    root = os.path.abspath(source_dir)
    norm_root = os.path.normcase(root)

    for entry in backend_path or ():
        if os.path.isabs(entry):
            msg = (
                f"backend-path entry {entry!r} in pyproject.toml must be"
                " relative to the project root"
            )
            raise BuildBackendError(msg)

        # pyproject_hooks compares the two paths as strings, so match that
        # rather than a stricter containment check.
        resolved = os.path.normcase(os.path.normpath(os.path.join(root, entry)))
        if not resolved.startswith(norm_root):
            msg = (
                f"backend-path entry {entry!r} in pyproject.toml is outside"
                " the source tree"
            )
            raise BuildBackendError(msg)


def _should_skip_prepare(backend: str, data: dict) -> bool:
    """Skip ``prepare_metadata_for_build_wheel`` when it would lie.

    uv documents one specific quirk: hatchling's prepare-metadata
    hook can return a metadata that does not match the eventual
    ``build_wheel`` output when ``project.dynamic`` includes
    ``dependencies`` (or ``optional-dependencies``).  Mirror that.
    """
    if not backend.startswith("hatchling."):
        return False
    project = data.get("project")
    if not isinstance(project, dict):
        return False
    dynamic = project.get("dynamic")
    if not isinstance(dynamic, list):
        return False
    dyn = {d for d in dynamic if isinstance(d, str)}
    return bool(dyn & {"dependencies", "optional-dependencies"})


def _extract_metadata_dir(
    project: build.ProjectBuilder,
    output_dir: Path,
    *,
    backend: str,
    skip_prepare: bool,
) -> Path:
    """Return the dist-info directory the backend produced.

    ``prepare_metadata_for_build_wheel`` is optional; ``prepare``
    returns ``None`` when the backend has no such hook, which takes
    the same route as ``skip_prepare``.

    Two faults arrive raw rather than as a ``BuildBackendException``:
    reading back the built wheel, and ``build`` joining a
    path-returning hook's result onto ``output_dir``.
    """
    try:
        if not skip_prepare:
            prepared = project.prepare("wheel", output_dir)
            if prepared is not None:
                return Path(prepared)

        return _build_wheel_and_extract(project, output_dir)
    # A wheel whose name will not parse raises a bare ValueError; a member
    # zipfile cannot decompress surfaces as zlib.error, lzma.LZMAError, or
    # NotImplementedError (a RuntimeError subclass).
    except (
        zipfile.BadZipFile,
        ValueError,
        OSError,
        zlib.error,
        lzma.LZMAError,
        RuntimeError,
    ) as exc:
        msg = f"build backend {backend!r} produced an unreadable wheel: {exc}"
        raise BuildBackendError(msg) from exc
    # PEP 517's path-returning hooks must return a basename string, so a
    # non-string reaches os.path.join as-is.
    except TypeError as exc:
        msg = (
            f"build backend {backend!r} returned a non-string path"
            f" from a build hook: {exc}"
        )
        raise BuildBackendError(msg) from exc


def _build_wheel_and_extract(
    project: build.ProjectBuilder, output_directory: Path
) -> Path:
    """Build a wheel and extract its own dist-info directory.

    The built wheel ends up in ``output_directory``; this helper pulls
    out the ``*.dist-info/`` for the distribution the wheel's filename
    names and returns the path so the caller can read ``METADATA``.
    Selection goes through
    :func:`nab_index.local_index.wheel_metadata_member` so a built wheel
    and an indexed one agree on what a wheel's own metadata is.
    """
    wheel = project.build("wheel", str(output_directory))
    wheel_path = Path(wheel)
    expected = parse_wheel_filename(wheel_path.name)[0]

    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()

        try:
            member = wheel_metadata_member(names, expected)
        except UnsupportedWheelError as exc:
            msg = f"built wheel {wheel_path.name} is unusable: {exc}"
            raise BuildBackendError(msg) from exc
        if member is None:
            msg = f"built wheel {wheel_path.name} has no .dist-info/METADATA"
            raise BuildBackendError(msg)

        distinfo_dir = member.partition("/")[0]
        zf.extractall(
            output_directory,
            (m for m in names if m.startswith(distinfo_dir + "/")),
        )

    return output_directory / distinfo_dir


def _parse_metadata(metadata_path: Path) -> WheelMetadata:
    """Parse a ``METADATA`` file into :class:`WheelMetadata`."""
    if not path_state(metadata_path).should_read:
        msg = f"backend produced no METADATA file at {metadata_path}"
        raise BuildBackendError(msg)

    try:
        text = metadata_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"backend METADATA at {metadata_path} could not be read: {exc}"
        raise BuildBackendError(msg) from exc
    except UnicodeDecodeError as exc:
        msg = f"backend METADATA at {metadata_path} is not valid UTF-8: {exc}"
        raise BuildBackendError(msg) from exc
    msg_obj = message_from_string(text)

    name_raw = msg_obj.get("Name")
    version_raw = msg_obj.get("Version")
    if not name_raw or not version_raw:
        msg = (
            f"backend METADATA at {metadata_path} is missing Name or Version"
            f" (Name={name_raw!r}, Version={version_raw!r})"
        )
        raise BuildBackendError(msg)
    try:
        version = Version(version_raw)
    except ValueError as exc:
        # InvalidVersion, or int() refusing a digit run past CPython's limit.
        msg = f"backend METADATA has invalid Version {version_raw!r}: {exc}"
        raise BuildBackendError(msg) from exc

    requires_python_raw = msg_obj.get("Requires-Python")
    requires_python = None
    if requires_python_raw:
        try:
            requires_python = SpecifierSet(requires_python_raw)
            validate_specifier_versions(requires_python)
        except ValueError as exc:
            msg = (
                f"backend METADATA has invalid Requires-Python "
                f"{requires_python_raw!r}: {exc}"
            )
            raise BuildBackendError(msg) from exc

    requires_dist: list[Requirement] = []
    for raw in msg_obj.get_all("Requires-Dist") or ():
        try:
            req = parse_requirement(raw)
            validate_specifier_versions(req.specifier)
        except ValueError as exc:
            msg = f"backend METADATA has an invalid Requires-Dist: {exc}"
            raise BuildBackendError(msg) from exc
        requires_dist.append(req)

    provides_extra: list[str] = sorted(
        {
            canonicalize_name(stripped)
            for extra in msg_obj.get_all("Provides-Extra") or ()
            if (stripped := extra.strip())
        }
    )

    return WheelMetadata(
        name=canonicalize_name(name_raw),
        version=version,
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
    )
