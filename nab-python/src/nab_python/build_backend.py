"""Metadata extraction for source trees.

Hand a source directory to :func:`extract_metadata` and get back a
:class:`WheelMetadata`. The static pyproject.toml reader runs first;
dynamic ``project.dependencies`` fall through to a PEP 517 backend
invocation inside :class:`~nab_python._build.env.NabBuildEnv`. The
dynamic path needs a :class:`NabProjectConfig`.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from ._build.errors import (
    BuildBackendError as BuildBackendError,  # noqa: PLC0414  (public re-export)
)
from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.specifiers import SpecifierSet
from ._vendor.packaging.utils import canonicalize_name
from ._vendor.packaging.version import InvalidVersion, Version
from .metadata import WheelMetadata, load_static_project

if TYPE_CHECKING:
    from pathlib import Path

    from .config import NabProjectConfig

__all__ = [
    "BuildBackendError",
    "extract_metadata",
    "extract_static_metadata",
]


logger = logging.getLogger(__name__)


@lru_cache(maxsize=4096)
def extract_static_metadata(source_dir: Path) -> WheelMetadata | None:
    """Build :class:`WheelMetadata` from a directory's static pyproject.toml.

    Returns ``None`` when ``[project]`` is missing, malformed, or
    ``project.dynamic`` includes ``dependencies`` /
    ``optional-dependencies`` (the field cannot be trusted as static).
    Returns a :class:`WheelMetadata` shape when the static fields are
    authoritative, populating ``name``, ``version``,
    ``requires_python``, ``requires_dist``, and ``provides_extra``.

    The returned ``provides_extra`` includes both the lower-cased
    keys of ``project.optional-dependencies`` and any extras declared
    via PEP 685 markers in ``requires-dist``.  PEP 685 normalisation
    is applied to all extra names.

    Cached per ``source_dir``: the static fields are deterministic
    derivations of the on-disk ``pyproject.toml``, and a universal
    resolve reads the same workspace member's file once per matrix
    tuple.  Callers must treat the returned :class:`WheelMetadata`
    as read-only.
    """
    pyproject = source_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        # ``is_file`` is racy: the file may vanish or become unreadable
        # between the check and the read.  Treat it the same as missing.
        return None
    project = load_static_project(text)
    if project is None:
        return None
    return _project_to_metadata(project)


def _project_to_metadata(project: dict) -> WheelMetadata | None:
    """Convert a static ``[project]`` table to :class:`WheelMetadata`."""
    name = project.get("name")
    version_raw = project.get("version")
    if not isinstance(name, str) or not isinstance(version_raw, str):
        return None
    try:
        version = Version(version_raw)
    except InvalidVersion:
        return None
    requires_python_raw = project.get("requires-python")
    requires_python = (
        SpecifierSet(requires_python_raw)
        if isinstance(requires_python_raw, str)
        else None
    )
    return WheelMetadata(
        name=canonicalize_name(name),
        version=version,
        requires_python=requires_python,
        requires_dist=_collect_requires_dist(project),
        provides_extra=sorted(_collect_provides_extra(project)),
    )


def _collect_requires_dist(project: dict) -> list[Requirement]:
    """Concatenate PEP 631 ``dependencies`` + ``optional-dependencies``.

    Optional-dependencies entries get an ``; extra == "name"`` marker
    appended (combined with any existing marker via ``and``).
    """
    out: list[Requirement] = []
    _extend_with_dep_strings(out, project.get("dependencies"))
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for raw_extra, deps in sorted(optional.items()):
            extra = canonicalize_name(str(raw_extra))
            _extend_with_dep_strings(out, deps, extra=extra)
    return out


def _extend_with_dep_strings(
    out: list[Requirement],
    raw: object,
    *,
    extra: str | None = None,
) -> None:
    if not isinstance(raw, list):
        return
    # Late import: ``pypi`` imports this module at module load.
    from .provider import _add_extra_marker  # noqa: PLC0415

    for dep in raw:
        if not isinstance(dep, str):
            continue
        text = _add_extra_marker(dep, extra) if extra is not None else dep
        try:
            out.append(Requirement(text))
        except (ValueError, TypeError):
            logger.warning("skipping unparseable requirement: %s", dep)


def _collect_provides_extra(project: dict) -> set[str]:
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        return set()
    return {canonicalize_name(str(extra)) for extra in optional}


def extract_metadata(
    source_dir: Path,
    *,
    config: NabProjectConfig | None = None,
    python_version: str | None = None,
) -> WheelMetadata:
    """Extract metadata for a source directory.

    Tries the static path first (:func:`extract_static_metadata`).
    When that returns ``None``, the dynamic-build path is taken:
    the project's PEP 517 backend is invoked in an isolated venv
    via :func:`nab_python._build.runner.run_build_backend`.  That
    needs a :class:`NabProjectConfig`; callers that cannot provide
    one get a :class:`BuildBackendError` instead.

    The build env owns its own HTTP transport (see
    :class:`~nab_python._build.env.NabBuildEnv` for why); callers
    do not pass one in.
    """
    static = extract_static_metadata(source_dir)
    if static is not None:
        return static
    if config is None:
        msg = (
            "dynamic-metadata path requires a NabProjectConfig;"
            f" the static reader returned None for {source_dir}."
            "  Pass one through ``extract_metadata`` or use a"
            " build-policy that does not enter the dynamic path."
        )
        raise BuildBackendError(msg)
    # Late import: ``_build.runner`` pulls in ``build`` and friends,
    # which we should not pay for in static-only callers.
    from ._build.runner import run_build_backend  # noqa: PLC0415

    return run_build_backend(
        source_dir,
        config=config,
        python_version=python_version,
    )
