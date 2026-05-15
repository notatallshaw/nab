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
import tempfile
from email import message_from_string
from pathlib import Path
from typing import TYPE_CHECKING

import build
import pyproject_hooks
import tomli

from .._vendor.packaging.requirements import Requirement
from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name
from .._vendor.packaging.version import InvalidVersion, Version
from ..metadata import WheelMetadata
from .env import NabBuildEnv
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
    python_version: str | None = None,
) -> WheelMetadata:
    """Extract wheel metadata for ``source_dir`` via the build backend.

    Returns a :class:`~nab_python.metadata.WheelMetadata` parsed from
    the ``METADATA`` file the backend produces.  Raises
    :class:`BuildBackendError` on any failure: backend import
    error, hook crash, malformed METADATA, or sdist-only build deps.

    The build runs in an isolated venv driven by
    :class:`NabBuildEnv`; nothing in the user's main environment is
    perturbed.  The build env owns its own HTTP transport (see
    :class:`NabBuildEnv` for why) so callers do not pass one in.
    """
    pyproject = source_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomli.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomli.TOMLDecodeError) as exc:
            msg = f"could not read pyproject.toml at {source_dir}: {exc}"
            raise BuildBackendError(msg) from exc
    elif (source_dir / "setup.py").is_file():
        # PEP 517 fallback for legacy setup.py projects: treat the
        # missing ``[build-system]`` as the documented default
        # (setuptools.build_meta:__legacy__).
        data = {}
    else:
        msg = f"no pyproject.toml or setup.py at {source_dir}"
        raise BuildBackendError(msg)

    backend, requires, _backend_path = _read_build_system(data)

    skip_prepare = _should_skip_prepare(backend, data)

    try:
        with NabBuildEnv(
            requires=list(requires),
            config=config,
            python_version=python_version,
        ) as env:
            project = build.ProjectBuilder.from_isolated_env(
                env,
                source_dir=str(source_dir),
                runner=pyproject_hooks.quiet_subprocess_runner,
            )

            extra = list(project.get_requires_for_build("wheel"))
            if extra:
                logger.debug("build backend asked for extras: %s", extra)
                env.install(extra)

            with tempfile.TemporaryDirectory(prefix="nab-build-meta-") as out_str:
                output_dir = Path(out_str)
                if skip_prepare:
                    metadata_dir = _build_wheel_and_extract(project, output_dir)
                else:
                    metadata_dir = Path(project.metadata_path(output_dir))
                return _parse_metadata(metadata_dir / "METADATA")
    except build.BuildBackendException as exc:
        msg = f"build backend {backend!r} failed: {exc}"
        raise BuildBackendError(msg) from exc


def _read_build_system(
    data: dict,
) -> tuple[str, tuple[str, ...], tuple[str, ...] | None]:
    """Return ``(backend, requires, backend_path)`` per PEP 517 / 518."""
    table = data.get("build-system")
    if not isinstance(table, dict):
        return _DEFAULT_BACKEND, _DEFAULT_REQUIRES, None
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


def _build_wheel_and_extract(
    project: build.ProjectBuilder, output_directory: Path
) -> Path:
    """Build a wheel and extract its dist-info directory.

    The built wheel ends up in ``output_directory``; this helper
    pulls out its ``*.dist-info/`` and returns the path so the
    caller can read ``METADATA``.  Mirrors what
    :meth:`build.ProjectBuilder.metadata_path` does internally
    when the prepare hook is missing; we just call it
    unconditionally for the hatchling+dynamic-deps case.
    """
    wheel = project.build("wheel", str(output_directory))
    wheel_path = Path(wheel)
    # zipfile is only needed for the build path; deferred to keep
    # import-time work minimal.
    import zipfile

    with zipfile.ZipFile(wheel_path) as zf:
        dist_info_members = [
            n
            for n in zf.namelist()
            if "/" in n and n.split("/")[0].endswith(".dist-info")
        ]
        if not dist_info_members:
            msg = f"built wheel {wheel_path.name} has no .dist-info directory"
            raise BuildBackendError(msg)
        distinfo_dir = dist_info_members[0].split("/")[0]
        zf.extractall(
            output_directory,
            (
                m
                for m in zf.namelist()
                if m == distinfo_dir or m.startswith(distinfo_dir + "/")
            ),
        )
    return output_directory / distinfo_dir


def _parse_metadata(metadata_path: Path) -> WheelMetadata:
    """Parse a ``METADATA`` file into :class:`WheelMetadata`."""
    if not metadata_path.is_file():
        msg = f"backend produced no METADATA file at {metadata_path}"
        raise BuildBackendError(msg)
    text = metadata_path.read_text(encoding="utf-8")
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
    except InvalidVersion as exc:
        msg = f"backend METADATA has invalid Version {version_raw!r}: {exc}"
        raise BuildBackendError(msg) from exc

    requires_python_raw = msg_obj.get("Requires-Python")
    requires_python = SpecifierSet(requires_python_raw) if requires_python_raw else None

    requires_dist: list[Requirement] = []
    for raw in msg_obj.get_all("Requires-Dist") or ():
        try:
            requires_dist.append(Requirement(raw))
        except (ValueError, TypeError):  # noqa: PERF203
            logger.warning("skipping unparseable Requires-Dist: %s", raw)

    provides_extra: list[str] = sorted(
        {
            canonicalize_name(extra)
            for extra in msg_obj.get_all("Provides-Extra") or ()
            if extra
        }
    )

    return WheelMetadata(
        name=canonicalize_name(name_raw),
        version=version,
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
    )
