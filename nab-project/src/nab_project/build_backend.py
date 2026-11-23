"""Metadata extraction for source trees.

Hand a source directory to :func:`extract_metadata` and get back a
:class:`WheelMetadata`. The static pyproject.toml reader runs first;
a directory :func:`extract_static_metadata` returns ``None`` for falls
through to a PEP 517 backend invocation inside
:class:`~nab_project._build.env.NabBuildEnv`. The dynamic path needs the
settings a resolve runs under.
"""

from __future__ import annotations

__lazy_modules__ = (
    "nab_project",
    "nab_project._build.errors",
    "nab_project.paths",
    "packaging.utils",
    "packaging.version",
    "nab_provider.metadata",
    "nab_provider.project_requirements",
    "nab_resolver.diagnostics",
    "tomli",
)

from typing import TYPE_CHECKING

import tomli
from packaging.utils import canonicalize_name
from packaging.version import Version

from nab_provider.metadata import (
    WheelMetadata,
    static_project_from_table,
)
from nab_provider.project_requirements import (
    InvalidProjectRequirementError,
    normalize_project_extra,
    parse_project_requirement,
    parse_project_requires_python,
    parse_requirements,
    require_string_list,
)
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion

from . import toml_io
from ._build.errors import (
    BuildBackendError,
)
from .paths import PathState, is_absent_error, path_state

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from packaging.requirements import Requirement

    from nab_index.transport import AsyncHttpTransport

    from .inputs import ResolveInputs

__all__ = [
    "BuildBackendError",
    "extract_metadata",
    "extract_static_metadata",
    "project_metadata",
]


def extract_static_metadata(source_dir: Path) -> WheelMetadata | None:
    """Return authoritative static metadata from ``pyproject.toml`` when possible.

    Missing or non-static project metadata returns ``None``. An unreadable or
    invalid TOML file raises :class:`BuildBackendError`.

    Invalid static dependency fields, versions, and Python constraints raise
    :class:`InvalidProjectRequirementError`. ``provides_extra`` contains the
    normalized keys declared by ``project.optional-dependencies``.

    Reads the current file on each call; a source directory is mutable.
    """
    pyproject = source_dir / "pyproject.toml"
    state = path_state(pyproject)
    if state is PathState.ABSENT:
        return None

    if not state.should_read:
        msg = f"{pyproject} exists but is not a regular file"
        raise BuildBackendError(msg)

    try:
        data = toml_io.loads(pyproject.read_text(encoding="utf-8"))
    except OSError as exc:
        if is_absent_error(exc):
            # The presence check is racy: the file may vanish before the read.
            return None
        msg = f"could not read pyproject.toml at {source_dir}: {exc}"
        raise BuildBackendError(msg) from exc
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"could not read pyproject.toml at {source_dir}: {exc}"
        raise BuildBackendError(msg) from exc

    project = static_project_from_table(data)
    if project is None:
        return None
    return project_metadata(project)


def project_metadata(project: Mapping[str, object]) -> WheelMetadata | None:
    """Convert a static ``[project]`` table to :class:`WheelMetadata`."""
    name = project.get("name")
    version_raw = project.get("version")
    if not isinstance(name, str) or not isinstance(version_raw, str):
        return None

    # A dynamic field is computed by the build backend; a static value
    # alongside it is a stale placeholder, not authoritative.
    dynamic = project.get("dynamic")
    if isinstance(dynamic, list) and (
        "version" in dynamic or "requires-python" in dynamic
    ):
        return None

    # Past the dynamic guard, version is authoritative, so a corrupt value
    # raises instead of returning None.
    try:
        version = Version(version_raw)
    except ValueError as exc:
        # InvalidVersion, or int() refusing a digit run past CPython's limit.
        msg = f"invalid [project].version {version_raw!r}: {exc}"
        raise InvalidProjectRequirementError(msg) from exc

    requires_python = parse_project_requires_python(project.get("requires-python"))
    requires_dist, provides_extra = _project_dependencies(project)

    return WheelMetadata(
        name=canonicalize_name(name),
        version=version,
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
    )


def _project_dependencies(
    project: Mapping[str, object],
) -> tuple[list[Requirement], list[str]]:
    """Read the requirements and declared extras from a static project table."""
    source = "[project].dependencies"
    requires_dist = parse_requirements(
        require_string_list(project.get("dependencies", []), source), source
    )
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        msg = "[project].optional-dependencies must be a table"
        raise InvalidProjectRequirementError(msg)

    extras = set()
    for raw_extra, deps in sorted(optional.items()):
        extra = normalize_project_extra(raw_extra)
        extras.add(extra)
        source = f"[project].optional-dependencies extra {raw_extra!r}"
        requires_dist.extend(
            parse_project_requirement(dep, source, extra=extra)
            for dep in require_string_list(deps, source)
        )
    return requires_dist, sorted(extras)


def extract_metadata(
    source_dir: Path,
    *,
    config: ResolveInputs | None = None,
    offline: bool = False,
    transport_factory: Callable[[], AsyncHttpTransport] | None = None,
) -> WheelMetadata:
    """Extract metadata for a source directory.

    Tries the static path first (:func:`extract_static_metadata`).
    When that returns ``None``, the dynamic-build path is taken:
    the project's PEP 517 backend is invoked in an isolated venv
    via :func:`nab_project._build.runner.run_build_backend`.  That
    needs a :class:`~nab_project.inputs.ResolveInputs`; callers that
    cannot provide one get a :class:`BuildBackendError` instead.
    ``offline`` bars that path from fetching the backend's build
    requirements.

    The build env owns its own HTTP transport (see
    :class:`~nab_project._build.env.NabBuildEnv` for why); callers
    may supply ``transport_factory`` to create its clients.
    """
    static = extract_static_metadata(source_dir)
    if static is not None:
        return static
    if config is None:
        report = DiagnosticMessage(
            f"Reading package metadata from {source_dir} requires a build backend.",
            suggestions=(
                Suggestion(
                    "Pass the project's build settings (ResolveInputs) "
                    "as the config argument to extract_metadata()."
                ),
            ),
        )
        raise BuildBackendError(report)
    # Late import: ``_build.runner`` pulls in ``build`` and friends,
    # which we should not pay for in static-only callers.
    from ._build.runner import run_build_backend  # noqa: PLC0415

    return run_build_backend(
        source_dir, config=config, offline=offline, transport_factory=transport_factory
    )
