"""Declare project setting defaults and parsers independently of CLI options."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from nab_provider.policy import (
    ArchiveSource,
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    LocalSource,
    MetadataPolicy,
    ResolutionStrategy,
    ResolveMode,
    VcsSource,
)
from nab_provider.records import IndexConfig
from nab_provider.vcs_admission import VcsConfig

from . import _config_hooks as config_hooks
from . import config_values
from .config_values import MatrixConfig
from .conflicts import ConflictSet
from .inputs import ResolveInputs
from .workspace import WorkspaceConfig

C = TypeVar("C")
_EMPTY_MAPPING: Mapping[str, Any] = MappingProxyType({})
_DEFAULT_INPUTS = ResolveInputs()


class Setting(Generic[C]):
    """A project's configuration key, parsed value and default."""

    __slots__ = ("_parse", "default", "name")

    def __init__(
        self, name: str, *, default: C, parse: Callable[[Any, str], C]
    ) -> None:
        """Record a project key without command-line or display metadata."""
        self.name = name
        self.default = default
        self._parse = parse

    def parse(
        self,
        value: object,
        where: str,
        *,
        context: ProjectValueContext | None = None,
    ) -> C:
        """Interpret a project value with its declaring location and cutoff context."""
        if context is None:
            context = ProjectValueContext(Path.cwd())
        with (
            config_hooks.resolve_anchor(context.anchor),
            config_hooks.inspector_anchor(context.comparison_anchor),
            config_hooks.declaring_dir(context.directory),
            config_hooks.key_prefix(context.prefix),
        ):
            return self._parse(value, where)


class ProjectValueContext:
    """File-format context for interpreting one project setting."""

    __slots__ = ("anchor", "comparison_anchor", "directory", "prefix")

    def __init__(
        self,
        directory: Path,
        *,
        prefix: str = "tool.nab.",
        anchor: datetime | None = None,
        comparison_anchor: datetime | None = None,
    ) -> None:
        """Keep source-relative paths and time references explicit at the boundary."""
        self.directory = directory
        self.prefix = prefix
        self.anchor = anchor
        self.comparison_anchor = comparison_anchor


class HostKey:
    """A standalone-file extension whose value is interpreted by the host."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        """Reserve a format name without defining its value or default."""
        self.name = name


OFFLINE = HostKey("offline")
CACHE_DIR = HostKey("cache-dir")
HTTP_BACKEND = HostKey("http-backend")
MAX_CONCURRENCY = HostKey("max-concurrency")
HOST_SETTINGS = (OFFLINE, CACHE_DIR, HTTP_BACKEND, MAX_CONCURRENCY)


RESOLUTION = Setting[ResolutionStrategy](
    "resolution",
    default=_DEFAULT_INPUTS.resolution,
    parse=config_values.parse_resolution,
)
DECISION_ORDER = Setting[DecisionOrder](
    "decision-order",
    default=_DEFAULT_INPUTS.decision_order,
    parse=config_values.parse_decision_order,
)
MODE = Setting[ResolveMode](
    "mode", default=ResolveMode.SPECIFIC, parse=config_values.parse_mode
)
CONSTRAINTS = Setting[tuple[str, ...]](
    "constraints",
    default=_DEFAULT_INPUTS.constraints,
    parse=config_values.parse_constraints,
)
DEFAULT_GROUPS = Setting[tuple[str, ...]](
    "default-groups",
    default=_DEFAULT_INPUTS.default_groups,
    parse=config_values.parse_default_groups,
)
BASE_GROUP = Setting[str | None](
    "base-group",
    default=_DEFAULT_INPUTS.base_group,
    parse=config_values.parse_base_group,
)
BUILD_GROUP = Setting[str | None](
    "build-group",
    default=_DEFAULT_INPUTS.build_group,
    parse=config_values.parse_build_group,
)
REQUIRES_PYTHON = Setting[str | None](
    "requires-python",
    default=_DEFAULT_INPUTS.requires_python,
    parse=config_values.parse_requires_python,
)
UPLOADED_PRIOR_TO = Setting[datetime | str | None](
    "uploaded-prior-to",
    default=_DEFAULT_INPUTS.uploaded_prior_to,
    parse=config_hooks.parse_uploaded_prior_to,
)
DIST_POLICY = Setting[tuple[DistPolicy, bool]](
    "dist-policy",
    default=(_DEFAULT_INPUTS.dist_policy, _DEFAULT_INPUTS.trust_unverified_sdist_deps),
    parse=config_values.parse_dist_policy,
)
METADATA_POLICY = Setting[MetadataPolicy](
    "metadata-policy",
    default=_DEFAULT_INPUTS.metadata_policy,
    parse=config_values.parse_metadata_policy,
)
ASSUME_UNIVERSAL_DEPS = Setting[bool](
    "assume-universal-deps",
    default=_DEFAULT_INPUTS.assume_universal_deps,
    parse=config_values.parse_bool,
)
ALLOW_ZIP_SDISTS = Setting[bool](
    "allow-zip-sdists",
    default=_DEFAULT_INPUTS.allow_zip_sdists,
    parse=config_values.parse_bool,
)
BUILD_POLICY = Setting[BuildPolicy](
    "build-policy",
    default=_DEFAULT_INPUTS.build_policy,
    parse=config_values.parse_build_policy,
)
BUILD_REQUIRES_DEPTH = Setting[int](
    "build-requires-depth",
    default=_DEFAULT_INPUTS.build_requires_depth,
    parse=config_values.parse_build_requires_depth,
)
ENVIRONMENT = Setting[Mapping[str, Any]](
    "environment", default=_EMPTY_MAPPING, parse=config_values.parse_environment
)
MARKER_ENVIRONMENT = Setting[Mapping[str, str]](
    "marker-environment",
    default=_EMPTY_MAPPING,
    parse=config_values.parse_marker_environment,
)
VCS = Setting[VcsConfig](
    "vcs", default=_DEFAULT_INPUTS.vcs, parse=config_values.parse_vcs
)
WORKSPACE = Setting[WorkspaceConfig | None](
    "workspace", default=None, parse=config_values.parse_workspace
)
INDEXES = Setting[tuple[IndexConfig, ...]](
    "indexes",
    default=_DEFAULT_INPUTS.indexes,
    parse=config_hooks.parse_indexes,
)
LOCAL_SOURCES = Setting[tuple[LocalSource, ...]](
    "local-sources",
    default=_DEFAULT_INPUTS.local_sources,
    parse=config_hooks.parse_local_sources,
)
VCS_SOURCES = Setting[tuple[VcsSource, ...]](
    "vcs-sources",
    default=_DEFAULT_INPUTS.vcs_sources,
    parse=config_values.parse_vcs_sources,
)
ARCHIVE_SOURCES = Setting[tuple[ArchiveSource, ...]](
    "archive-sources",
    default=_DEFAULT_INPUTS.archive_sources,
    parse=config_values.parse_archive_sources,
)
PACKAGES = Setting[tuple[Any, ...]](
    "packages",
    default=_DEFAULT_INPUTS.package_overrides,
    parse=config_hooks.parse_packages,
)
PACKAGE_RULES = Setting[tuple[Any, ...]](
    "package-rules", default=(), parse=config_hooks.parse_package_rules
)
INDEX = Setting[Mapping[str, Any]](
    "index",
    default=_DEFAULT_INPUTS.index_overrides,
    parse=config_hooks.parse_index_overrides,
)
CONFLICTS = Setting[tuple[ConflictSet, ...]](
    "conflicts",
    default=_DEFAULT_INPUTS.conflicts,
    parse=config_values.parse_conflicts,
)
MATRIX = Setting[MatrixConfig | None](
    "matrix", default=None, parse=config_hooks.parse_matrix
)

SETTINGS: tuple[Setting[Any], ...] = (
    RESOLUTION,
    DECISION_ORDER,
    MODE,
    CONSTRAINTS,
    DEFAULT_GROUPS,
    BASE_GROUP,
    BUILD_GROUP,
    REQUIRES_PYTHON,
    UPLOADED_PRIOR_TO,
    DIST_POLICY,
    METADATA_POLICY,
    ASSUME_UNIVERSAL_DEPS,
    ALLOW_ZIP_SDISTS,
    BUILD_POLICY,
    BUILD_REQUIRES_DEPTH,
    ENVIRONMENT,
    MARKER_ENVIRONMENT,
    VCS,
    WORKSPACE,
    INDEXES,
    LOCAL_SOURCES,
    VCS_SOURCES,
    ARCHIVE_SOURCES,
    PACKAGES,
    PACKAGE_RULES,
    INDEX,
    CONFLICTS,
    MATRIX,
)


def project_file_conflict(
    bindings: Iterable[tuple[str, object]],
) -> tuple[str, object, object] | None:
    """Return unequal settings from the input and adjacent project file."""
    values = dict(bindings)
    input_kind = "script" if "script" in values else "pyproject"
    if input_kind not in values or "project" not in values:
        return None
    first, second = values[input_kind], values["project"]
    return None if first == second else (input_kind, first, second)
