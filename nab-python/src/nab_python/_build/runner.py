"""Orchestrate ``[build-system]`` setup and metadata extraction.

The entry point is :func:`run_build_backend`.  Given a source
directory with a ``pyproject.toml`` whose ``project.dynamic``
contains fields nab cannot read statically, run_build_backend:

1. Reads ``[build-system].requires`` and ``[build-system].build-backend``
   (defaulting to ``setuptools.build_meta:__legacy__`` per PEP 517).
2. Opens a :class:`~nab_python._build.env.NabBuildEnv` populated with
   those requirements.
3. Hands the env to ``build.ProjectBuilder.from_isolated_env`` and
   asks it for the wheel metadata via ``metadata_path()``, which
   tries ``prepare_metadata_for_build_wheel`` and falls back to a
   full ``build_wheel`` when the backend lacks that hook.
4. Parses the resulting ``METADATA`` file into
   :class:`~nab_python.metadata.WheelMetadata`.

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
from email import message_from_string
from pathlib import Path
from typing import TYPE_CHECKING

import build
import pyproject_hooks
import tomli

from nab_index.local_index import UnsupportedWheelError, wheel_metadata_member
from nab_resolver.resolver import ResolutionError

from .._vendor.packaging.requirements import Requirement
from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
from .._vendor.packaging.version import Version
from ..metadata import WheelMetadata, validate_specifier_versions
from ..paths import PathState, path_state
from .env import BuildEnvError, NabBuildEnv
from .errors import BuildBackendError

if TYPE_CHECKING:
    from ..config import NabProjectConfig

__all__ = [
    "BuildBackendError",
    "run_build_backend",
]

logger = logging.getLogger(__name__)


_DEFAULT_BACKEND = "setuptools.build_meta:__legacy__"
_DEFAULT_REQUIRES = ("setuptools >= 40.8.0",)


def run_build_backend(
    source_dir: Path,
    *,
    config: NabProjectConfig,
    offline: bool = False,
) -> WheelMetadata:
    """Extract wheel metadata for ``source_dir`` via the build backend.

    Returns a :class:`~nab_python.metadata.WheelMetadata` parsed from
    the ``METADATA`` file the backend produces.  Raises
    :class:`BuildBackendError` on any failure: backend import
    error, a rejected ``backend-path``, hook crash, malformed
    METADATA, an unreadable built wheel, sdist-only build deps, or
    build requirements ``offline`` bars from being fetched.

    The build runs in an isolated venv driven by
    :class:`NabBuildEnv`; nothing in the user's main environment is
    perturbed.  The build env owns its own HTTP transport (see
    :class:`NabBuildEnv` for why) so callers do not pass one in.
    """
    pyproject = source_dir / "pyproject.toml"
    state = path_state(pyproject)

    if state.should_read:
        try:
            data = tomli.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
            msg = f"could not read pyproject.toml at {source_dir}: {exc}"
            raise BuildBackendError(msg) from exc
    elif state is not PathState.ABSENT:
        # The file is there, so the tree is not the legacy setup.py case.
        msg = f"{pyproject} exists but is not a regular file"
        raise BuildBackendError(msg)
    elif path_state(source_dir / "setup.py").should_read:
        # PEP 517 fallback for legacy setup.py projects: treat the
        # missing ``[build-system]`` as the documented default
        # (setuptools.build_meta:__legacy__).
        data = {}
    else:
        msg = f"no pyproject.toml or setup.py at {source_dir}"
        raise BuildBackendError(msg)

    backend, requires, backend_path = _read_build_system(data)
    _validate_backend_path(source_dir, backend_path)

    skip_prepare = _should_skip_prepare(backend, data)

    try:
        with NabBuildEnv(
            requires=list(requires), config=config, offline=offline
        ) as env:
            project = build.ProjectBuilder.from_isolated_env(
                env,
                source_dir=str(source_dir),
                runner=pyproject_hooks.quiet_subprocess_runner,
            )

            # build returns the hook's result as a set, so sort for a stable
            # order. key=str keeps a non-string item from raising here.
            extra = sorted(project.get_requires_for_build("wheel"), key=str)
            if extra:
                # PEP 517 requires the hook to return a list of strings.
                non_str = [item for item in extra if not isinstance(item, str)]
                if non_str:
                    msg = (
                        f"build backend {backend!r} returned a non-string build"
                        f" requirement from get_requires_for_build_wheel: {non_str!r}"
                    )
                    raise BuildBackendError(msg)

                logger.debug("build backend asked for extras: %s", extra)
                env.install(extra)

            with tempfile.TemporaryDirectory(prefix="nab-build-meta-") as out_str:
                metadata_dir = _extract_metadata_dir(
                    project,
                    Path(out_str),
                    backend=backend,
                    skip_prepare=skip_prepare,
                )
                return _parse_metadata(metadata_dir / "METADATA")
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


def _read_build_system(
    data: dict,
) -> tuple[str, tuple[str, ...], tuple[str, ...] | None]:
    """Return ``(backend, requires, backend_path)`` per PEP 517 / 518.

    A missing ``[build-system]`` takes the PEP 517 defaults; a
    ``build-system`` that is not a table is malformed, not absent.
    """
    if "build-system" not in data:
        return _DEFAULT_BACKEND, _DEFAULT_REQUIRES, None

    table = data["build-system"]
    if not isinstance(table, dict):
        msg = (
            "build-system in pyproject.toml must be a table,"
            f" not {type(table).__name__}"
        )
        raise BuildBackendError(msg)

    backend = table.get("build-backend")
    if not isinstance(backend, str):
        backend = _DEFAULT_BACKEND
    raw_requires = table.get("requires")
    requires: tuple[str, ...]
    if isinstance(raw_requires, list) and all(isinstance(r, str) for r in raw_requires):
        requires = tuple(raw_requires)
    else:
        requires = _DEFAULT_REQUIRES
    raw_path = table.get("backend-path")
    backend_path: tuple[str, ...] | None = None
    if isinstance(raw_path, list) and all(isinstance(p, str) for p in raw_path):
        backend_path = tuple(raw_path)
    return backend, requires, backend_path


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

    ``build`` lets two faults through raw, outside its own hook-error
    wrapper: reading back a wheel it just built, and joining a
    path-returning hook's result onto ``output_dir``.
    """
    try:
        if skip_prepare:
            return _build_wheel_and_extract(project, output_dir)
        return Path(project.metadata_path(output_dir))
    # A wheel whose name will not parse raises a bare ValueError on either
    # branch; a member zipfile cannot decompress surfaces as zlib.error,
    # lzma.LZMAError, or NotImplementedError (a RuntimeError subclass).
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
    if not metadata_path.is_file():
        msg = f"backend produced no METADATA file at {metadata_path}"
        raise BuildBackendError(msg)
    try:
        text = metadata_path.read_text(encoding="utf-8")
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
            req = Requirement(raw)
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
