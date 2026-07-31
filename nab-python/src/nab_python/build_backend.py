"""Metadata extraction for source trees.

Hand a source directory to :func:`extract_metadata` and get back a
:class:`WheelMetadata`. The static pyproject.toml reader runs first;
dynamic ``project.dependencies`` fall through to a PEP 517 backend
invocation inside :class:`~nab_python._build.env.NabBuildEnv`. The
dynamic path needs a :class:`NabProjectConfig`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from ._build.errors import (
    BuildBackendError as BuildBackendError,  # noqa: PLC0414  (public re-export)
)
from ._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from ._vendor.packaging.utils import canonicalize_name
from ._vendor.packaging.version import InvalidVersion, Version
from .metadata import WheelMetadata, load_static_project
from .requirements_file import (
    InvalidProjectRequirementError,
    _parse_project_requirement,
    _require_string_list,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ._vendor.packaging.requirements import Requirement
    from .config import NabProjectConfig

__all__ = [
    "BuildBackendError",
    "extract_metadata",
    "extract_static_metadata",
]


@lru_cache(maxsize=4096)
def extract_static_metadata(source_dir: Path) -> WheelMetadata | None:
    """Build :class:`WheelMetadata` from a directory's static pyproject.toml.

    Returns ``None`` when ``[project]`` is missing, malformed, or
    ``project.dynamic`` includes ``dependencies``,
    ``optional-dependencies``, ``version``, or ``requires-python`` (the
    field cannot be trusted as static).
    Returns a :class:`WheelMetadata` shape when the static fields are
    authoritative, populating ``name``, ``version``,
    ``requires_python``, ``requires_dist``, and ``provides_extra``.

    Raises :class:`InvalidProjectRequirementError` when a present,
    non-dynamic field is corrupt: a structurally wrong ``dependencies`` /
    ``optional-dependencies`` (not an array of strings / not a table), a
    ``version`` string that is not valid :pep:`440`, or a
    ``requires-python`` that is not a string or not a valid specifier.  A
    corrupt static value is not something the build backend can compute,
    so it raises rather than deferring to a build of the same broken file.

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
    except UnicodeDecodeError:
        # TOML is UTF-8, so a file that will not decode has no static metadata.
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
    except InvalidVersion as exc:
        msg = f"invalid [project].version {version_raw!r}: {exc}"
        raise InvalidProjectRequirementError(msg) from exc

    requires_python = _static_requires_python(project.get("requires-python"))

    return WheelMetadata(
        name=canonicalize_name(name),
        version=version,
        requires_python=requires_python,
        requires_dist=_collect_requires_dist(project),
        provides_extra=sorted(_collect_provides_extra(project)),
    )


def _static_requires_python(raw: object) -> SpecifierSet | None:
    """Parse a static ``requires-python``, raising when it is corrupt.

    Absent is a valid "no constraint". A non-string or an unparseable
    specifier is corrupt: it raises rather than silently dropping the
    Python bound, which would admit candidates the bound excludes.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"[project].requires-python must be a string, got {type(raw).__name__}"
        raise InvalidProjectRequirementError(msg)
    try:
        return SpecifierSet(raw)
    except InvalidSpecifier as exc:
        msg = f"invalid [project].requires-python {raw!r}: {exc}"
        raise InvalidProjectRequirementError(msg) from exc


def _collect_requires_dist(project: dict) -> list[Requirement]:
    """Concatenate PEP 631 ``dependencies`` + ``optional-dependencies``.

    Optional-dependencies entries get an ``; extra == "name"`` marker
    appended (combined with any existing marker via ``and``).

    A structurally wrong value (``dependencies`` that is not an array of
    strings, ``optional-dependencies`` that is not a table, or a per-extra
    value that is not an array of strings) raises
    :class:`InvalidProjectRequirementError`.  A well-typed entry that is
    not valid PEP 508 is dropped with a warning.
    """
    out: list[Requirement] = []
    _extend_with_dep_strings(
        out,
        project.get("dependencies", []),
        source="[project].dependencies",
    )
    for raw_extra, deps in sorted(_require_optional_dependencies(project).items()):
        extra = canonicalize_name(str(raw_extra))
        _extend_with_dep_strings(
            out,
            deps,
            source=f"[project].optional-dependencies extra {raw_extra!r}",
            extra=extra,
        )
    return out


def _extend_with_dep_strings(
    out: list[Requirement],
    raw: object,
    *,
    source: str,
    extra: str | None = None,
) -> None:
    out.extend(
        _parse_project_requirement(dep, source, extra=extra)
        for dep in _require_string_list(raw, source)
    )


def _require_optional_dependencies(project: dict) -> dict:
    """Return ``[project.optional-dependencies]`` as a table, or raise."""
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        msg = "[project].optional-dependencies must be a table"
        raise InvalidProjectRequirementError(msg)
    return optional


def _collect_provides_extra(project: dict) -> set[str]:
    return {
        canonicalize_name(str(extra))
        for extra in _require_optional_dependencies(project)
    }


def extract_metadata(
    source_dir: Path,
    *,
    config: NabProjectConfig | None = None,
    offline: bool = False,
) -> WheelMetadata:
    """Extract metadata for a source directory.

    Tries the static path first (:func:`extract_static_metadata`).
    When that returns ``None``, the dynamic-build path is taken:
    the project's PEP 517 backend is invoked in an isolated venv
    via :func:`nab_python._build.runner.run_build_backend`.  That
    needs a :class:`NabProjectConfig`; callers that cannot provide
    one get a :class:`BuildBackendError` instead.  ``offline`` bars
    that path from fetching the backend's build requirements.

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

    return run_build_backend(source_dir, config=config, offline=offline)
