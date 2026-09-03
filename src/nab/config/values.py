"""Parse the values ``[tool.nab]`` keys carry.

A parser takes the raw TOML, env or CLI value and the ``where`` its source
names it by, so one wording serves every rung of the ladder: a bad
``max-concurrency`` is refused in the same words whether ``nab.toml``,
``NAB_MAX_CONCURRENCY`` or ``--max-concurrency`` set it, each naming itself.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar, cast
from urllib.parse import urlsplit

from nab_index.file_urls import is_file_url
from nab_project.conflicts import (
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
)
from nab_project.paths import is_usable_path_name, resolve_path
from nab_project.value import ValueType
from nab_project.workspace import WorkspaceConfig
from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from nab_provider._vendor.packaging.utils import InvalidName, canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.archive import ArchiveRequest, ArchiveRequestError
from nab_provider.errors import ConfigError
from nab_provider.iso8601 import parse_iso_datetime
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import (
    ArchiveSource,
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    ResolveMode,
    VcsSource,
)
from nab_provider.records import IndexConfig
from nab_provider.serialization import SimpleSerialization
from nab_provider.subdir import subdirectory_escapes
from nab_provider.tags import (
    DEFAULT_LIBC,
    LIBC_MAJOR,
    Libc,
    PlatformSpec,
    platform_kind,
)
from nab_provider.target import PLATFORM_MARKERS, Matrix
from nab_provider.vcs_admission import VcsConfig, VcsPolicy, known_vcs_schemes

if TYPE_CHECKING:
    import enum
    from collections.abc import Iterator, Mapping, Sequence

    from nab_provider._vendor.packaging.requirements import Requirement


__all__ = [
    "DURATION_PATTERN",
    "ENVIRONMENT_KEYS",
    "IMPLEMENTATIONS",
    "PYTHON_ORDERS",
    "CliTableError",
    "MatrixConfig",
    "SourceConfigError",
    "check_package_override_overlap",
    "environment_platform_spec",
    "matrix_from_config",
    "parse_archive_sources",
    "parse_base_group",
    "parse_build_group",
    "parse_build_policy",
    "parse_conflicts",
    "parse_constraints",
    "parse_decision_order",
    "parse_default_groups",
    "parse_dist_policy",
    "parse_enum",
    "parse_environment",
    "parse_index_overrides",
    "parse_indexes",
    "parse_local_sources",
    "parse_marker_environment",
    "parse_matrix",
    "parse_mode",
    "parse_package_rules",
    "parse_packages_sugar",
    "parse_requires_python",
    "parse_resolution",
    "parse_string_list",
    "parse_uploaded_prior_to",
    "parse_vcs",
    "parse_vcs_sources",
    "parse_workspace",
    "validate_environment_values",
]


class SourceConfigError(ConfigError):
    """A configuration source set a value nab refused.

    Raised for a value's own grammar by the parsers here, and by the ladder
    for the category gate over them (a project-scope option in a user
    ``nab.toml``, a user-scope option in ``pyproject.toml`` ``[tool.nab]``).
    A subclass of :class:`ConfigError`, so a caller catching the broad config
    error catches these too, while ``except SourceConfigError`` still narrows
    to what a source declared.
    """


class CliTableError(SourceConfigError):
    """A ``--project-<table>-<key>`` line nab refused.

    It came from the command line, not a file, so it prints without the
    ``config error:`` prefix.  A refusal from the merged-table parse keeps
    that hook's wording and names the key.
    """


DURATION_PATTERN = re.compile(r"^P(\d+)D$")

# PEP 508 environment-marker variables; reject a misspelled
# [tool.nab.marker-environment] key (e.g. ``python-version``).
_PEP508_MARKER_VARIABLES = frozenset(
    {
        "os_name",
        "sys_platform",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_version",
        "python_full_version",
        "implementation_name",
        "implementation_version",
    },
)

# Marker variables whose values must parse as PEP 440 versions.
_VERSION_MARKER_VARIABLES = frozenset({"python_version", "python_full_version"})


class MatrixConfig(ValueType):
    """User-declared matrix axes for universal resolution."""

    __slots__ = __match_args__ = (
        "python",
        "platforms",
        "python_order",
        "python_patches",
        "implementations",
    )

    python: str
    platforms: tuple[PlatformSpec, ...]
    python_order: str
    python_patches: Mapping[str, str] | None
    implementations: tuple[str, ...]

    def __init__(
        self,
        python: str,
        platforms: tuple[PlatformSpec, ...],
        python_order: str = "asc",
        python_patches: Mapping[str, str] | None = None,
        implementations: tuple[str, ...] = ("cpython",),
    ) -> None:
        """Record the axes ``[tool.nab.matrix]`` declared."""
        self.python = python
        self.platforms = platforms
        self.python_order = python_order
        self.python_patches = python_patches
        self.implementations = implementations


def matrix_from_config(matrix: MatrixConfig) -> Matrix:
    """Build the expandable :class:`Matrix` from its parsed config table."""
    return Matrix(
        python=matrix.python,
        platforms=matrix.platforms,
        python_order=matrix.python_order,
        python_patches=(
            dict(matrix.python_patches) if matrix.python_patches is not None else None
        ),
        implementations=matrix.implementations,
    )


def parse_string_list(value: object, where: str) -> tuple[str, ...]:
    """Read ``value`` as an array of strings, naming the index of a non-string."""
    if not isinstance(value, list):
        msg = f"{where} must be a list of strings, got {type(value).__name__}"
        raise SourceConfigError(msg)
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            msg = f"{where}[{i}] must be a string, got {type(item).__name__}"
            raise SourceConfigError(msg)
        out.append(item)
    return tuple(out)


def _require_constraint(key: str, item: str) -> None:
    """Validate one ``constraints`` entry's shape.

    A constraint is a name with an optional specifier and marker; it bounds
    versions but never pulls a package in, so extras and direct-reference
    URLs are rejected here rather than only at resolve.
    """
    try:
        req = parse_requirement(item)
        # a specifier defers parsing its versions; to_range() forces it
        req.specifier.to_range()
    except ValueError as exc:
        msg = f"{key} is not a valid requirement: {exc}"
        raise SourceConfigError(msg) from exc

    if req.extras:
        msg = f"{key} cannot have extras: {item}"
        raise SourceConfigError(msg)

    if req.url is not None:
        msg = f"{key} cannot be a direct reference (URL): {item}"
        raise SourceConfigError(msg)


def parse_constraints(value: object, where: str) -> tuple[str, ...]:
    """Read ``constraints`` as PEP 508 strings, rejecting a direct reference."""
    items = parse_string_list(value, where)
    for i, item in enumerate(items):
        _require_constraint(f"{where}[{i}]", item)
    return items


def parse_default_groups(value: object, where: str) -> tuple[str, ...]:
    """Read ``default-groups`` as the group names a run selects by default."""
    return parse_string_list(value, where)


def _reject_duplicates(where: str, items: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            msg = f"{where} has duplicate entry: {item!r}"
            raise SourceConfigError(msg)
        seen.add(item)


def _parse_string_value(where: str, value: object) -> str:
    if not isinstance(value, str):
        msg = f"{where} must be a string, got {type(value).__name__}"
        raise SourceConfigError(msg)
    return value


def parse_base_group(value: object, where: str) -> str | None:
    """Parse ``[tool.nab].base-group`` as a PEP 735 group name.

    Names the group a lock gives the project's own dependencies, so an
    installer can ask for one group without them.  Unset leaves them
    unconditional.
    """
    raw = _parse_string_value(where, value)
    try:
        canonical = canonicalize_name(raw, validate=True)
    except InvalidName as e:
        msg = f"{where} {raw!r} is not a valid group name: {e}"
        raise SourceConfigError(msg) from e
    return str(canonical)


def parse_build_group(value: object, where: str) -> str | None:
    """Parse ``[tool.nab].build-group`` as a PEP 735 group name.

    Names the group a lock gives ``[build-system].requires``, so one lock
    can describe the environment the project is built in as well as the
    one it runs in.  Unset, a lock says nothing about how it is built.
    """
    raw = _parse_string_value(where, value)
    try:
        canonical = canonicalize_name(raw, validate=True)
    except InvalidName as e:
        msg = f"{where} {raw!r} is not a valid group name: {e}"
        raise SourceConfigError(msg) from e
    return str(canonical)


def parse_requires_python(value: object, where: str) -> str | None:
    """Parse ``[tool.nab].requires-python`` as a PEP 440 specifier.

    A declaration, not a target: it is recorded as the lock's top-level
    ``requires-python`` and checked against the resolve target, and the
    target itself comes from ``[tool.nab.environment]`` (the host by
    default).  Stored as the raw specifier string so the lockfile writer
    can pass it straight to :class:`SpecifierSet`.  Raises
    :class:`ConfigError` for invalid specifiers and for well-meaning bare
    versions like ``"3.13"``; those are not valid specifiers and must be
    written ``"==3.13"`` or ``">=3.13"``.
    """
    raw = _parse_string_value(where, value)
    try:
        # a specifier defers parsing its versions; to_range() forces it
        SpecifierSet(raw).to_range()
    except ValueError as exc:
        msg = (
            f"{where} must be a PEP 440 specifier, got {raw!r}."
            "  Did you mean ==X.Y or >=X.Y?"
        )
        raise SourceConfigError(msg) from exc
    return raw


def parse_uploaded_prior_to(value: object, where: str, *, anchor: datetime) -> datetime:
    """Parse ``uploaded-prior-to`` (ISO datetime, TOML datetime, or ``P<n>D``).

    Naive datetimes are rejected so lockfiles read identically across
    timezones. ``P<n>D`` (a nab extension) is resolved against
    ``anchor`` so re-locks reproduce the same cutoff.  Callers only reach
    here with a present value (the absent case is handled upstream).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            msg = (
                f"{where} TOML datetime must have an explicit"
                " timezone offset (e.g. ``Z`` or ``+00:00``); got"
                f" {value!r}"
            )
            raise SourceConfigError(msg)
        return value

    if not isinstance(value, str):
        msg = (
            f"{where} must be a TOML offset-date-time, an ISO"
            " 8601 datetime string with timezone, or a 'PnD' duration;"
            f" got {type(value).__name__}"
        )
        raise SourceConfigError(msg)

    duration_match = DURATION_PATTERN.match(value)
    if duration_match is not None:
        # ``int()`` raises ValueError past CPython's int-from-string limit.
        try:
            return anchor - timedelta(days=int(duration_match.group(1)))
        except (OverflowError, ValueError):
            msg = f"{where} duration is too large: {value!r}"
            raise SourceConfigError(msg) from None
    try:
        dt = parse_iso_datetime(value)
    except ValueError as exc:
        msg = (
            f"{where} must be an ISO 8601 datetime with"
            " timezone (e.g. '2026-05-01T00:00:00Z') or a 'PnD'"
            f" duration (e.g. 'P4D'); got {value!r}"
        )
        raise SourceConfigError(msg) from exc
    if dt.tzinfo is None:
        msg = (
            f"{where} ISO datetime must include an explicit"
            " timezone offset (e.g. 'Z' or '+00:00'); got"
            f" {value!r}"
        )
        raise SourceConfigError(msg)
    return dt


_DIST_POLICY_TABLE_KEYS = frozenset({"policy", "trust-unverified-deps"})


def parse_dist_policy(value: object, where: str) -> tuple[DistPolicy, bool]:
    """Parse ``dist-policy``: an enum string or a policy table.

    The table form ``{ policy = "...", trust-unverified-deps = bool }``
    folds the sdist-trust flag into the dist body.  Returns
    ``(policy, trust_unverified)``.
    """
    if not isinstance(value, dict):
        return (
            parse_enum(value, where, DistPolicy, DistPolicy.WHEEL_OR_SDIST),
            False,
        )
    unknown = sorted(set(value) - _DIST_POLICY_TABLE_KEYS)
    if unknown:
        msg = (
            f"{where} table has unknown key(s) {unknown!r};"
            f" expected {sorted(_DIST_POLICY_TABLE_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    if "policy" not in value:
        msg = f"{where} table must set 'policy'"
        raise SourceConfigError(msg)
    policy = parse_enum(
        value["policy"], f"{where}.policy", DistPolicy, DistPolicy.WHEEL_OR_SDIST
    )
    trust = _parse_bool(
        f"{where}.trust-unverified-deps",
        value.get("trust-unverified-deps"),
        default=False,
    )
    return (policy, trust)


def _parse_bool(key: str, value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean, got {type(value).__name__}"
        raise SourceConfigError(msg)
    return value


# The bound is a string so ``enum`` can stay a typing-only import.
_EnumT = TypeVar("_EnumT", bound="enum.Enum")


def parse_enum(
    value: object,
    where: str,
    enum_cls: type[_EnumT],
    default: _EnumT,
) -> _EnumT:
    """Read ``value`` as one of ``enum_cls``'s values, or ``default`` when unset."""
    if value is None:
        return default
    if not isinstance(value, str):
        msg = f"{where} must be a string, got {type(value).__name__}"
        raise SourceConfigError(msg)

    try:
        # Spelled through ``__call__`` because zuban reads ``enum_cls(value)``
        # as the functional ``Enum("Name", ...)`` API and asks for a literal.
        return enum_cls.__call__(value)
    except ValueError as exc:
        valid = sorted(m.value for m in enum_cls)
        msg = f"{where} must be one of {valid!r}, got {value!r}"
        raise SourceConfigError(msg) from exc


def parse_bool(value: object, where: str) -> bool:
    """Parse a TOML/env boolean.

    Accepts a real bool (TOML) or one of ``1/0/true/false`` (env,
    case-insensitive).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true"}:
            return True
        if lowered in {"0", "false"}:
            return False
        msg = f"{where} must be one of 1/0/true/false, got {value!r}"
        raise SourceConfigError(msg)
    msg = f"{where} must be a boolean, got {type(value).__name__}"
    raise SourceConfigError(msg)


def parse_path(value: object, where: str) -> Path:
    """Return the ``cache-dir`` row's value as a path."""
    # A CLI layer already supplies a Path; TOML/env supply a string.
    if isinstance(value, Path):
        return value
    if not isinstance(value, str):
        msg = f"{where} must be a string path, got {type(value).__name__}"
        raise SourceConfigError(msg)
    if not value.strip():
        # An empty NAB_CACHE_DIR would resolve Path("") to the cwd; reject
        # it instead.
        msg = f"{where} must be a non-empty path"
        raise SourceConfigError(msg)
    if not is_usable_path_name(value):
        msg = f"{where} {value!r} is not a usable filesystem path"
        raise SourceConfigError(msg)
    return Path(value)


HTTP_BACKENDS = ("urllib3", "httpx")


def parse_http_backend(value: object, where: str) -> str:
    """Return the ``http-backend`` row's backend name."""
    if not isinstance(value, str):
        msg = f"{where} must be a string, got {type(value).__name__}"
        raise SourceConfigError(msg)
    lowered = value.strip()
    if lowered not in HTTP_BACKENDS:
        msg = f"{where} must be one of {list(HTTP_BACKENDS)!r}, got {value!r}"
        raise SourceConfigError(msg)
    return lowered


def parse_max_concurrency(value: object, where: str) -> int:
    """Return the ``max-concurrency`` row's positive count."""
    # TOML supplies an int; env/CLI supply a string.  bool is an int
    # subclass, so reject it explicitly rather than read True as 1.
    if isinstance(value, bool):
        msg = f"{where} must be an integer, got {type(value).__name__}"
        raise SourceConfigError(msg)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value)
        except ValueError as exc:
            msg = f"{where} must be an integer, got {value!r}"
            raise SourceConfigError(msg) from exc
    else:
        msg = f"{where} must be an integer, got {type(value).__name__}"
        raise SourceConfigError(msg)
    if result < 1:
        msg = f"{where} must be at least 1, got {result}"
        raise SourceConfigError(msg)
    return result


def parse_build_requires_depth(value: object, where: str) -> int:
    """Read ``build-requires-depth``: how deep a build environment may nest."""
    # bool is an int subclass, so reject it rather than read True as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = (
            f"{where} must be a non-negative integer number of nested build"
            f" environments, got {value!r}"
        )
        raise SourceConfigError(msg)
    return value


def parse_mode(value: object, where: str) -> ResolveMode:
    """Read ``mode``: resolve for one environment, or across a matrix."""
    return parse_enum(value, where, ResolveMode, ResolveMode.SPECIFIC)


def parse_resolution(value: object, where: str) -> ResolutionStrategy:
    """Read ``resolution``: which end of a range a candidate is taken from."""
    return parse_enum(value, where, ResolutionStrategy, ResolutionStrategy.HIGHEST)


def parse_build_policy(value: object, where: str) -> BuildPolicy:
    """Read ``build-policy``: when nab may build an sdist.

    The plain last-wins value only.  The host-build gate that forces never
    for a declared target runs over the merged config, not here.
    """
    return parse_enum(value, where, BuildPolicy, BuildPolicy.BUILD_LOCAL)


def parse_decision_order(value: object, where: str) -> DecisionOrder:
    """Read ``decision-order``: the order the resolver decides packages in."""
    return parse_enum(value, where, DecisionOrder, DecisionOrder.ARRIVAL)


def parse_marker_environment(value: object, where: str) -> dict[str, str]:
    """Read ``marker-environment``, a table of PEP 508 marker variables to strings."""
    if not isinstance(value, dict):
        msg = f"{where} must be a table of string -> string, got {type(value).__name__}"
        raise SourceConfigError(msg)
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            msg = f"{where} entries must be string -> string, got {k!r}: {v!r}"
            raise SourceConfigError(msg)
        if k not in _PEP508_MARKER_VARIABLES:
            valid = sorted(_PEP508_MARKER_VARIABLES)
            msg = (
                f"{where} has unknown variable {k!r}; expected a PEP 508"
                f" marker variable, one of {valid!r}"
            )
            raise SourceConfigError(msg)
        if k in _VERSION_MARKER_VARIABLES:
            try:
                Version(v)
            except ValueError as exc:
                msg = f"{where}.{k} must be a PEP 440 version, got {v!r}"
                raise SourceConfigError(msg) from exc
        out[k] = v
    return out


ENVIRONMENT_KEYS = frozenset({"python", "platform", "implementation"})


def parse_environment(value: object, where: str) -> dict[str, Any]:
    """Parse ``[tool.nab.environment]``: the one environment to resolve for.

    Kept as the raw table so the registry merges it sub-key by sub-key
    across the config sources rather than as a whole.  ``platform`` takes
    the two shapes a ``matrix.platforms`` entry takes, a bare id or a table
    of the wheel-tag knobs, so a dict value passes through here.
    """
    if not isinstance(value, dict):
        msg = f"{where} must be a table, got {type(value).__name__}"
        raise SourceConfigError(msg)
    unknown = sorted(set(value) - ENVIRONMENT_KEYS)
    if unknown:
        msg = (
            f"{where} has unknown keys: {unknown!r};"
            f" expected {sorted(ENVIRONMENT_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    out: dict[str, Any] = {
        key: item if key == "platform" else _parse_string_value(f"{where}.{key}", item)
        for key, item in value.items()
    }
    validate_environment_values(out)
    return out


def environment_platform_spec(value: object) -> PlatformSpec:
    """Build the :class:`PlatformSpec` ``[tool.nab.environment].platform`` names.

    The same two shapes ``matrix.platforms`` entries take, parsed by the same
    code: a bare id at the platform's default tag knobs, or a table declaring
    them.
    """
    where = "environment.platform"
    if isinstance(value, str):
        return _platform_spec(where, platform_id=value)
    if isinstance(value, dict):
        return _parse_platform_table(where, cast("dict[str, Any]", value))
    msg = f"{where} must be a platform id or a table, got {type(value).__name__}"
    raise SourceConfigError(msg)


def validate_environment_values(environment: Mapping[str, Any]) -> None:
    """Validate the value of every environment axis the table names.

    Shared by the ``[tool.nab.environment]`` parse and the
    ``[tool.nab.marker-environment]`` translation, so both reject the
    same bad values with one message.
    """
    python = environment.get("python")
    if python is not None:
        try:
            Version(python)
        except ValueError as exc:
            msg = (
                "environment.python must be a version like '3.12' or"
                f" '3.12.4', got {python!r}"
            )
            raise SourceConfigError(msg) from exc
    platform = environment.get("platform")
    if platform is not None:
        platform_id = environment_platform_spec(platform).platform_id
        if platform_id not in PLATFORM_MARKERS:
            valid = sorted(PLATFORM_MARKERS)
            msg = (
                f"unknown environment.platform {platform_id!r};"
                f" expected one of {valid!r}"
            )
            raise SourceConfigError(msg)
    implementation = environment.get("implementation")
    if implementation is not None and implementation not in IMPLEMENTATIONS:
        valid = list(IMPLEMENTATIONS)
        msg = (
            f"unknown environment.implementation {implementation!r};"
            f" expected one of {valid!r}"
        )
        raise SourceConfigError(msg)


_NAME_URL_KEYS = frozenset({"name", "url"})


class _NameUrlTable(NamedTuple):
    """One checked entry of a ``name``/``url`` array of tables.

    ``table`` is the raw entry, for a caller that reads further keys.  The
    ordinal is ``position`` because ``index`` would shadow ``tuple.index``.
    """

    position: int
    name: str
    url: str
    table: dict[str, Any]


def _iter_name_url_tables(
    where: str, value: object, *, keys: frozenset[str] = _NAME_URL_KEYS
) -> Iterator[_NameUrlTable]:
    """Yield each entry of a ``name``/``url`` array of tables, rejecting bad ones.

    Lazy, so a caller's own check on an entry runs before the next is checked.
    """
    if not isinstance(value, list):
        msg = f"{where} must be an array of tables, got {type(value).__name__}"
        raise SourceConfigError(msg)

    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"{where}[{i}] must be a table, got {type(entry).__name__}"
            raise SourceConfigError(msg)

        unknown = sorted(set(entry) - keys)
        if unknown:
            msg = (
                f"{where}[{i}] has unknown keys: {unknown!r}; expected {sorted(keys)!r}"
            )
            raise SourceConfigError(msg)

        try:
            name = entry["name"]
            url = entry["url"]
        except KeyError as missing:
            msg = f"{where}[{i}] missing required key {missing!s}"
            raise SourceConfigError(msg) from None

        if not isinstance(name, str) or not isinstance(url, str):
            msg = f"{where}[{i}] name and url must be strings"
            raise SourceConfigError(msg)

        yield _NameUrlTable(position=i, name=name, url=url, table=entry)


_INDEX_KEYS = frozenset({"name", "url", "serialization"})


def parse_indexes(value: object, where: str) -> tuple[IndexConfig, ...]:
    """Read one file's ``indexes`` array of tables, names checked for uniqueness."""
    out: list[IndexConfig] = []
    for entry in _iter_name_url_tables(where, value, keys=_INDEX_KEYS):
        if "serialization" in entry.table and is_file_url(entry.url):
            msg = (
                f"{where}[{entry.position}].serialization is not settable on a"
                " file:// index: a local index is read from disk with no"
                " Accept negotiation, so the pin would do nothing."
                f"  Drop it from index {entry.name!r}."
            )
            raise SourceConfigError(msg)

        serialization = parse_enum(
            entry.table.get("serialization"),
            f"{where}[{entry.position}].serialization",
            SimpleSerialization,
            SimpleSerialization.NEGOTIATE,
        )
        out.append(
            IndexConfig(name=entry.name, url=entry.url, serialization=serialization)
        )

    if not out:
        msg = f"{where} must contain at least one entry when present"
        raise SourceConfigError(msg)

    _check_index_name_uniqueness(out)
    return tuple(out)


def _check_index_name_uniqueness(indexes: Sequence[IndexConfig]) -> None:
    """Reject two indexes declared with the same name."""
    seen: set[str] = set()
    for index in indexes:
        if index.name in seen:
            msg = f"duplicate index name: {index.name!r}"
            raise SourceConfigError(msg)
        seen.add(index.name)


_PACKAGE_OVERRIDE_BODY_KEYS = frozenset(
    {
        "dist-policy",
        "build-policy",
        "uploaded-prior-to",
        "index",
        "strict",
        "dependencies",
        "requires-python",
        "provides-extra",
    }
)
# A [[tool.nab.package-rules]] entry carries a ``match`` selector plus body keys.
_PACKAGE_RULE_KEYS = frozenset({"match"}) | _PACKAGE_OVERRIDE_BODY_KEYS
_INDEX_OVERRIDE_KEYS = frozenset(
    {"dist-policy", "build-policy", "uploaded-prior-to", "assume-fresh-seconds"}
)
# Override-body keys not supported yet; rejected so nothing inert ships.
# ``metadata`` is the nested-table form the flat body keys replace.
_OVERRIDE_DEFERRED_KEYS = frozenset(
    {"resolution", "prereleases", "source", "vcs", "metadata", "marker"}
)
# The policy fields a per-package override may carry per field name, mapping
# each to the offending-entry attribute used by the parse-time overlap check
# below.  uploaded-prior-to is one field set by either a cutoff datetime or
# the ``false`` disable form, so both forms share one row (see _override_sets).
_PACKAGE_POLICY_FIELDS = (
    ("dist-policy", "dist_policy"),
    ("dist-policy.trust-unverified-deps", "dist_trust_unverified_deps"),
    ("build-policy", "build_policy"),
    ("uploaded-prior-to", "uploaded_prior_to"),
    ("index", "index"),
    ("dependencies", "dependencies"),
    ("requires-python", "requires_python"),
    ("provides-extra", "provides_extra"),
)


def parse_packages_sugar(
    value: object,
    where: str,
    *,
    anchor: datetime,
) -> list[PackageOverride]:
    """Parse ``[tool.nab.packages.<name>]`` into per-package overrides.

    Each key is a PEP 508 requirement (a bare name, or a name plus a
    version specifier in a quoted key such as ``"numpy <= 1.21"``) and the
    sub-table is the override body.  The key is the whole selector, so the
    sugar form carries no inner selector key.
    """
    if isinstance(value, list):
        msg = (
            f"{where} is the name-keyed table form ([tool.nab.packages.<name>]);"
            " for one body across several requirements use"
            " [[tool.nab.package-rules]] with match = [...]"
        )
        raise SourceConfigError(msg)
    if not isinstance(value, dict):
        msg = (
            f"{where} must be a table keyed by package name, got {type(value).__name__}"
        )
        raise SourceConfigError(msg)
    out: list[PackageOverride] = []
    for key, body in value.items():
        entry = f"{where}.{key!r}"
        requirement = _requirement_from_selector(key, entry)
        if not isinstance(body, dict):
            msg = f"{entry} must be a table, got {type(body).__name__}"
            raise SourceConfigError(msg)
        _reject_deferred(body, entry)
        unknown = sorted(set(body) - _PACKAGE_OVERRIDE_BODY_KEYS)
        if unknown:
            msg = (
                f"{entry}: unknown override key(s) {unknown!r}; expected body"
                f" keys {sorted(_PACKAGE_OVERRIDE_BODY_KEYS)!r}"
            )
            raise SourceConfigError(msg)
        out.extend(
            _build_package_overrides(
                (requirement,),
                body,
                entry,
                anchor=anchor,
                name_keyed=True,
            )
        )
    return out


def parse_package_rules(
    value: object,
    where: str,
    *,
    anchor: datetime,
) -> list[PackageOverride]:
    """Parse ``[[tool.nab.package-rules]]`` into per-package overrides.

    Each entry's ``match`` selector lists PEP 508 requirements (name plus
    an optional version specifier); the body applies to every one, so a
    single rule can cover many packages (e.g. routing a namespace to one
    index).
    """
    if not isinstance(value, list):
        msg = (
            f"{where} must be an array of tables ([[tool.nab.package-rules]]);"
            " for per-package policy keyed by name use"
            f" [tool.nab.packages.<name>].  Got {type(value).__name__}"
        )
        raise SourceConfigError(msg)
    out: list[PackageOverride] = []
    for i, entry in enumerate(value):
        out.extend(_parse_package_rule_entry(entry, where, i, anchor=anchor))
    return out


def _parse_package_rule_entry(
    entry: object,
    label: str,
    index: int,
    *,
    anchor: datetime,
) -> list[PackageOverride]:
    where = f"{label}[{index}]"
    if not isinstance(entry, dict):
        msg = f"{where} must be a table, got {type(entry).__name__}"
        raise SourceConfigError(msg)
    _reject_deferred(entry, where)
    unknown = sorted(set(entry) - _PACKAGE_RULE_KEYS)
    if unknown:
        msg = (
            f"{where}: unknown override key(s) {unknown!r}; expected 'match'"
            f" and body keys {sorted(_PACKAGE_OVERRIDE_BODY_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    requirements = _parse_match(entry.get("match"), where)
    if not requirements:
        msg = (
            f"{where} must carry a 'match' selector listing at least one"
            " PEP 508 requirement"
        )
        raise SourceConfigError(msg)
    body = {key: val for key, val in entry.items() if key != "match"}
    return _build_package_overrides(requirements, body, where, anchor=anchor)


def _build_package_overrides(
    requirements: tuple[Requirement, ...],
    body: dict[str, Any],
    where: str,
    *,
    anchor: datetime,
    name_keyed: bool = False,
) -> list[PackageOverride]:
    """Turn a validated selector and body into one override per requirement."""
    dist_policy, dist_trust = _parse_override_dist(body.get("dist-policy"), where)
    build_policy = (
        parse_enum(
            body["build-policy"],
            f"{where}.build-policy",
            BuildPolicy,
            BuildPolicy.NEVER,
        )
        if "build-policy" in body
        else None
    )
    uploaded_prior_to, uploaded_disabled = _parse_override_uploaded_prior_to(
        body.get("uploaded-prior-to"),
        where,
        anchor=anchor,
        present="uploaded-prior-to" in body,
    )
    route = _parse_override_index(body, where)
    dependencies = _parse_override_dependencies(body.get("dependencies"), where)
    requires_python = _parse_override_requires_python(
        body.get("requires-python"), where
    )
    provides_extra = _parse_override_provides_extra(body.get("provides-extra"), where)
    has_body = (
        dist_policy is not None
        or dist_trust is not None
        or build_policy is not None
        or uploaded_prior_to is not None
        or uploaded_disabled
        or route is not None
        or dependencies is not None
        or requires_python is not None
        or provides_extra is not None
    )
    if not has_body:
        msg = (
            f"{where} sets no policy; an entry must set at least one of"
            f" {sorted(_PACKAGE_OVERRIDE_BODY_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    if route is not None:
        for requirement in requirements:
            if str(requirement.specifier):
                msg = (
                    f"{where}.index routing requires bare-name requirements"
                    " (no version specifier); routing decides where to fetch a"
                    " listing before any version is known, but"
                    f" {str(requirement)!r} is version-scoped"
                )
                raise SourceConfigError(msg)

    return [
        PackageOverride(
            requirement=requirement,
            name=canonicalize_name(requirement.name),
            version_range=requirement.specifier.to_range(),
            dist_policy=dist_policy,
            dist_trust_unverified_deps=dist_trust,
            build_policy=build_policy,
            uploaded_prior_to=uploaded_prior_to,
            uploaded_prior_to_disabled=uploaded_disabled,
            index=route,
            dependencies=dependencies,
            requires_python=requires_python,
            provides_extra=provides_extra,
            name_keyed=name_keyed,
            source_label=where,
        )
        for requirement in requirements
    ]


def _requirement_from_selector(raw: str, where: str) -> Requirement:
    """Parse one selector into a name-plus-optional-specifier requirement.

    Extras, markers, and URLs are rejected: a selector carries only a
    package name and an optional version specifier.
    """
    try:
        requirement = parse_requirement(raw)
        # a specifier defers parsing its versions; to_range() forces it
        requirement.specifier.to_range()
    except ValueError as exc:
        msg = f"{where} entry {raw!r} is not a valid PEP 508 requirement"
        raise SourceConfigError(msg) from exc
    if requirement.extras or requirement.marker is not None or requirement.url:
        msg = (
            f"{where} entry {raw!r} may carry only a name and an optional"
            " version specifier; extras, markers, and URLs are not supported"
            " on the override surface"
        )
        raise SourceConfigError(msg)
    return requirement


def check_package_override_overlap(
    overrides: tuple[PackageOverride, ...],
) -> None:
    """Reject two per-package entries setting one field for overlapping ranges.

    For each (canonical name, policy field) the entries that set the field
    must have pairwise-disjoint version ranges.  Two ranges overlap when
    ``not (range_a & range_b).is_empty``.  A bare-name requirement is the
    full range, so it overlaps every range for that package; in
    particular two routing entries for one package always conflict.
    """
    for _field, attr in _PACKAGE_POLICY_FIELDS:
        by_name: defaultdict[str, list[PackageOverride]] = defaultdict(list)
        for entry in overrides:
            if _override_sets(entry, attr):
                by_name[entry.name].append(entry)
        for name, entries in by_name.items():
            for left, right in itertools.combinations(entries, 2):
                if not (left.version_range & right.version_range).is_empty:
                    msg = (
                        f"two per-package overrides for {name!r} both set"
                        f" {_field!r} for overlapping versions:"
                        f" {str(left.requirement)!r} and"
                        f" {str(right.requirement)!r}.  Per-package overrides for"
                        " one field must cover disjoint version ranges."
                    )
                    raise SourceConfigError(msg)


def _override_sets(override: PackageOverride, attr: str) -> bool:
    """Whether ``override`` carries the policy field tracked by ``attr``.

    uploaded-prior-to counts as set by either a cutoff datetime or the
    ``false`` disable form, so a cutoff entry and a disable entry for one
    package with overlapping ranges still conflict.
    """
    if attr == "uploaded_prior_to":
        return override.uploaded_prior_to is not None or (
            override.uploaded_prior_to_disabled
        )
    return getattr(override, attr) is not None


def _reject_deferred(
    entry: dict[str, Any], where: str, *, flat_metadata_advice: bool = True
) -> None:
    """Reject override-body keys that are not supported.

    ``flat_metadata_advice`` gates the package-surface hint to set metadata
    via the flat body keys; the index surface passes ``False`` since those
    keys are rejected there too.
    """
    deferred = sorted(set(entry) & _OVERRIDE_DEFERRED_KEYS)
    if deferred:
        msg = f"{where}: key(s) {deferred!r} are not supported"
        if flat_metadata_advice and "metadata" in deferred:
            msg += (
                "; set metadata as the flat body keys 'dependencies',"
                " 'requires-python', and 'provides-extra' instead"
            )
        raise SourceConfigError(msg)


def parse_index_overrides(
    value: object,
    where: str,
    *,
    anchor: datetime,
) -> dict[str, IndexOverride]:
    """Parse ``[tool.nab.index.<name>]`` into a name-keyed policy map.

    Each key must name a declared ``[[tool.nab.indexes]]`` entry, checked
    post-merge on the resolve path because this parser does not see the
    declared set.  The body sets policy fields only (no routing, no version
    scope); the override applies to every package served from that index.
    """
    if not isinstance(value, dict):
        msg = f"{where} must be a table keyed by index name, got {type(value).__name__}"
        raise SourceConfigError(msg)
    return {
        name: _parse_index_override_body(body, f"{where}.{name}", anchor=anchor)
        for name, body in value.items()
    }


def _parse_index_override_body(
    body: object, where: str, *, anchor: datetime
) -> IndexOverride:
    if not isinstance(body, dict):
        msg = f"{where} must be a table, got {type(body).__name__}"
        raise SourceConfigError(msg)
    _reject_deferred(body, where, flat_metadata_advice=False)
    unknown = sorted(set(body) - _INDEX_OVERRIDE_KEYS)
    if unknown:
        msg = (
            f"{where}: unknown override key(s) {unknown!r}; expected body keys"
            f" {sorted(_INDEX_OVERRIDE_KEYS)!r} (per-index overrides carry no"
            " routing and no version scope)"
        )
        raise SourceConfigError(msg)
    dist_policy, dist_trust = _parse_override_dist(body.get("dist-policy"), where)
    build_policy = (
        parse_enum(
            body["build-policy"],
            f"{where}.build-policy",
            BuildPolicy,
            BuildPolicy.NEVER,
        )
        if "build-policy" in body
        else None
    )
    uploaded_prior_to, uploaded_disabled = _parse_override_uploaded_prior_to(
        body.get("uploaded-prior-to"),
        where,
        anchor=anchor,
        present="uploaded-prior-to" in body,
    )
    assume_fresh_seconds = _parse_index_assume_fresh(
        body.get("assume-fresh-seconds"), where
    )
    has_body = (
        dist_policy is not None
        or dist_trust is not None
        or build_policy is not None
        or uploaded_prior_to is not None
        or uploaded_disabled
        or assume_fresh_seconds is not None
    )
    if not has_body:
        msg = (
            f"{where} sets no policy; an entry must set at least one of"
            f" {sorted(_INDEX_OVERRIDE_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    return IndexOverride(
        dist_policy=dist_policy,
        dist_trust_unverified_deps=dist_trust,
        build_policy=build_policy,
        uploaded_prior_to=uploaded_prior_to,
        uploaded_prior_to_disabled=uploaded_disabled,
        assume_fresh_seconds=assume_fresh_seconds,
    )


def _parse_match(value: object, where: str) -> tuple[Requirement, ...]:
    """Parse a ``match`` selector into PEP 508 requirements.

    Each entry is a requirement of name plus an optional version
    specifier; extras, markers, and URLs are rejected.  A bare name means
    all versions; a version specifier scopes the entry to matching ones.
    """
    if value is None:
        return ()
    names = parse_string_list(value, f"{where}.match")
    return tuple(_requirement_from_selector(raw, f"{where}.match") for raw in names)


def _parse_override_dist(
    value: object, where: str
) -> tuple[DistPolicy | None, bool | None]:
    """Parse the ``dist-policy`` body: an enum string or a policy table.

    The table form ``{ policy = ..., trust-unverified-deps = bool }``
    folds the sdist-trust flag into the dist body.
    """
    if value is None:
        return (None, None)
    if isinstance(value, str):
        return (
            parse_enum(
                value, f"{where}.dist-policy", DistPolicy, DistPolicy.WHEEL_OR_SDIST
            ),
            None,
        )
    if not isinstance(value, dict):
        msg = (
            f"{where}.dist-policy must be a policy string or a table"
            f" {{ policy, trust-unverified-deps }}, got {type(value).__name__}"
        )
        raise SourceConfigError(msg)
    unknown = sorted(set(value) - _DIST_POLICY_TABLE_KEYS)
    if unknown:
        msg = (
            f"{where}.dist-policy has unknown key(s) {unknown!r};"
            f" expected {sorted(_DIST_POLICY_TABLE_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    if "policy" not in value:
        msg = f"{where}.dist-policy table must set 'policy'"
        raise SourceConfigError(msg)
    policy = parse_enum(
        value["policy"],
        f"{where}.dist-policy.policy",
        DistPolicy,
        DistPolicy.WHEEL_OR_SDIST,
    )
    trust = value.get("trust-unverified-deps")
    if trust is not None and not isinstance(trust, bool):
        msg = f"{where}.dist-policy.trust-unverified-deps must be a boolean"
        raise SourceConfigError(msg)
    return (policy, trust)


def _parse_override_uploaded_prior_to(
    value: object, where: str, *, anchor: datetime, present: bool
) -> tuple[datetime | None, bool]:
    """Parse the ``uploaded-prior-to`` body: ``false`` disables, else a cutoff."""
    if not present:
        return (None, False)
    if value is False:
        return (None, True)
    if value is True:
        msg = (
            f"{where}.uploaded-prior-to: ``true`` is not a valid value; use"
            " ``false`` to disable the cutoff or a datetime / 'PnD' duration"
            " to set a window"
        )
        raise SourceConfigError(msg)
    cutoff = parse_uploaded_prior_to(value, f"{where}.uploaded-prior-to", anchor=anchor)
    return (cutoff, False)


def _parse_index_assume_fresh(value: object, where: str) -> int | None:
    """Parse ``assume-fresh-seconds``: a positive integer number of seconds."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = (
            f"{where}.assume-fresh-seconds must be a positive integer number of"
            f" seconds, got {value!r}"
        )
        raise SourceConfigError(msg)
    return value


def _parse_override_requires_python(value: object, where: str) -> str | None:
    """Parse a per-package ``requires-python`` override, naming the entry.

    An absent key (``None``) means no override; a present value delegates
    to :func:`parse_requires_python` for PEP 440 validation, prefixing the
    ``where`` selector on failure so the message names the offending entry.
    """
    if value is None:
        return None

    return parse_requires_python(value, f"{where}.requires-python")


def _parse_override_dependencies(
    value: object, where: str
) -> tuple[Requirement, ...] | None:
    """Parse the ``dependencies`` body: PEP 508 strings that replace deps.

    The list replaces a package's declared runtime dependencies for the
    matched version range.  Each item is a full PEP 508 dependency
    *value*, so extras, markers, and version specifiers are all legal
    (unlike the override *key*, which :func:`_requirement_from_selector`
    restricts to a name plus specifier).  A present-but-empty list is
    stored as ``()`` (replace with zero deps), distinct from the key
    being absent (``None``).
    """
    if value is None:
        return None

    if not isinstance(value, list):
        msg = (
            f"{where}.dependencies must be a list of PEP 508 requirement"
            f" strings, got {type(value).__name__}"
        )
        raise SourceConfigError(msg)

    out: list[Requirement] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            msg = (
                f"{where}.dependencies[{i}] must be a string, got {type(item).__name__}"
            )
            raise SourceConfigError(msg)
        try:
            requirement = parse_requirement(item)
            # a specifier defers parsing its versions; to_range() forces it
            requirement.specifier.to_range()
        except ValueError as exc:
            msg = (
                f"{where}.dependencies[{i}] is not a valid PEP 508"
                f" requirement: {item!r}"
            )
            raise SourceConfigError(msg) from exc
        out.append(requirement)
    return tuple(out)


def _parse_override_provides_extra(value: object, where: str) -> tuple[str, ...] | None:
    """Parse the ``provides-extra`` body: the extras the override declares.

    A TOML array of extra names, each normalised per PEP 685 like a parsed
    ``Provides-Extra``, so an extra compares equal regardless of spelling. A
    present-but-empty list is stored as ``()`` (declares no extras), distinct
    from the key being absent (``None``).
    """
    if value is None:
        return None

    if not isinstance(value, list):
        msg = (
            f"{where}.provides-extra must be a list of extra names, got"
            f" {type(value).__name__}"
        )
        raise SourceConfigError(msg)

    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            msg = (
                f"{where}.provides-extra[{i}] must be a string, got"
                f" {type(item).__name__}"
            )
            raise SourceConfigError(msg)
        out.append(canonicalize_name(item))
    return tuple(out)


def _parse_override_index(entry: dict[str, Any], where: str) -> str | None:
    """Parse the routing ``index`` body and validate its ``strict`` flag.

    The route is always a strict pin to one index, so ``strict`` only
    accepts ``true``.  ``strict = false`` is rejected: fallthrough on a
    miss is not cleanly wireable through the single-index-pin router this
    release ships.

    The route-names-a-declared-index check runs post-merge on the resolve
    path, because this parser does not see the declared set.
    """
    route = entry.get("index")
    if route is not None and not isinstance(route, str):
        msg = f"{where}.index must be a string, got {type(route).__name__}"
        raise SourceConfigError(msg)
    if "strict" not in entry:
        return route
    if route is None:
        msg = f"{where}.strict is only meaningful alongside an 'index' route"
        raise SourceConfigError(msg)
    strict = entry["strict"]
    if not isinstance(strict, bool):
        msg = f"{where}.strict must be a boolean, got {type(strict).__name__}"
        raise SourceConfigError(msg)
    if not strict:
        msg = (
            f"{where}.strict = false (fallthrough routing) is not supported in"
            " this release; the index route is always a strict pin"
        )
        raise SourceConfigError(msg)
    return route


def parse_vcs(value: object, where: str) -> VcsConfig:
    """Read ``[tool.nab.vcs]`` into the policy VCS sources are admitted under."""
    if not isinstance(value, dict):
        msg = f"{where} must be a table, got {type(value).__name__}"
        raise SourceConfigError(msg)
    allowed = sorted({"policy", "allowed-schemes", "allowed-repos", "require-pin"})
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        msg = f"{where} has unknown keys: {unknown!r}; expected {allowed!r}"
        raise SourceConfigError(msg)
    policy = parse_enum(
        value.get("policy"), f"{where}.policy", VcsPolicy, VcsPolicy.BLOCK
    )
    allowed_schemes = parse_string_list(
        value.get("allowed-schemes", []), f"{where}.allowed-schemes"
    )
    unknown_schemes = sorted(set(allowed_schemes) - known_vcs_schemes())
    if unknown_schemes:
        msg = (
            f"{where}.allowed-schemes has unknown entries: {unknown_schemes!r};"
            " nab recognises"
            f" {sorted(known_vcs_schemes())!r}"
        )
        raise SourceConfigError(msg)
    allowed_repos = parse_string_list(
        value.get("allowed-repos", []), f"{where}.allowed-repos"
    )
    for repo in allowed_repos:
        _validate_allowed_repo(repo)
    require_pin_raw = value.get("require-pin", True)
    if not isinstance(require_pin_raw, bool):
        msg = (
            f"{where}.require-pin must be a boolean,"
            f" got {type(require_pin_raw).__name__}"
        )
        raise SourceConfigError(msg)
    return VcsConfig(
        policy=policy,
        allowed_schemes=frozenset(allowed_schemes),
        allowed_repos=tuple(allowed_repos),
        require_pin=require_pin_raw,
    )


def _validate_allowed_repo(repo: str) -> None:
    """Reject an ``allowed-repos`` entry whose authority does not parse.

    :func:`urlsplit` raises ValueError on an authority it cannot parse,
    such as an unterminated IPv6 bracket.
    """
    try:
        urlsplit(repo)
    except ValueError as exc:
        msg = f"vcs.allowed-repos entry {repo!r} does not parse: {exc}"
        raise SourceConfigError(msg) from exc


def _validate_source_name(where: str, index: int, name: str) -> None:
    """Reject a declared source ``name`` that is not a valid package name.

    A source is matched to a requirement by canonical name, so a name
    that is not a package name can never match one.
    """
    try:
        canonicalize_name(name, validate=True)
    except InvalidName as exc:
        msg = f"{where}[{index}] name {name!r} is not a valid package name"
        raise SourceConfigError(msg) from exc


_LOCAL_SOURCE_KEYS = frozenset({"name", "path", "editable", "subdirectory"})


def _parse_local_source(
    entry: object, where: str, i: int, *, pyproject_dir: Path
) -> LocalSource:
    if not isinstance(entry, dict):
        msg = f"{where}[{i}] must be a table, got {type(entry).__name__}"
        raise SourceConfigError(msg)

    unknown = sorted(set(entry) - _LOCAL_SOURCE_KEYS)
    if unknown:
        msg = (
            f"{where}[{i}] has unknown keys: {unknown!r};"
            f" expected {sorted(_LOCAL_SOURCE_KEYS)!r}"
        )
        raise SourceConfigError(msg)

    try:
        name = entry["name"]
        path_value = entry["path"]
    except KeyError as missing:
        msg = f"{where}[{i}] missing required key {missing!s}"
        raise SourceConfigError(msg) from None
    if not isinstance(name, str) or not isinstance(path_value, str):
        msg = f"{where}[{i}] name and path must be strings"
        raise SourceConfigError(msg)

    _validate_source_name(where, i, name)

    editable = entry.get("editable", False)
    if not isinstance(editable, bool):
        msg = f"{where}[{i}] editable must be a boolean"
        raise SourceConfigError(msg)

    subdirectory = entry.get("subdirectory")
    if subdirectory is not None and not isinstance(subdirectory, str):
        msg = f"{where}[{i}] subdirectory must be a string"
        raise SourceConfigError(msg)
    if subdirectory is not None and subdirectory_escapes(subdirectory):
        msg = f"{where}[{i}] subdirectory {subdirectory!r} escapes the source tree"
        raise SourceConfigError(msg)

    resolved = resolve_path(pyproject_dir, path_value)
    if resolved is None:
        msg = f"{where}[{i}] path {path_value!r} is not a usable filesystem path"
        raise SourceConfigError(msg)

    return LocalSource(
        name=name,
        path=str(resolved),
        editable=editable,
        subdirectory=subdirectory,
    )


def parse_local_sources(
    value: object, where: str, *, pyproject_dir: Path
) -> tuple[LocalSource, ...]:
    """Read ``local-sources``, resolving each path against ``pyproject_dir``."""
    if not isinstance(value, list):
        msg = f"{where} must be an array of tables, got {type(value).__name__}"
        raise SourceConfigError(msg)
    return tuple(
        _parse_local_source(entry, where, i, pyproject_dir=pyproject_dir)
        for i, entry in enumerate(value)
    )


def parse_vcs_sources(value: object, where: str) -> tuple[VcsSource, ...]:
    """Read ``vcs-sources``, one name and url per table."""
    out: list[VcsSource] = []
    for entry in _iter_name_url_tables(where, value):
        _validate_source_name(where, entry.position, entry.name)
        out.append(VcsSource(name=entry.name, url=entry.url))
    return tuple(out)


def parse_archive_sources(value: object, where: str) -> tuple[ArchiveSource, ...]:
    """Read ``archive-sources``, one name and url per table, each url validated."""
    out: list[ArchiveSource] = []
    for entry in _iter_name_url_tables(where, value):
        _validate_source_name(where, entry.position, entry.name)
        _validate_archive_url(where, entry.position, entry.url)
        out.append(ArchiveSource(name=entry.name, url=entry.url))
    return tuple(out)


def _validate_archive_url(where: str, index: int, url: str) -> None:
    """Reject an archive URL that is malformed, has no hash, or is not a .tar.gz.

    PEP 751 ``packages.archive.hashes`` is required, so nab requires the
    hash in the URL fragment and verifies the download against it.  Only
    ``.tar.gz`` source archives are supported today; wheels and zips are
    refused loudly rather than mis-handled.  :func:`urlsplit` raises
    ValueError on an authority it cannot parse, such as an unterminated
    IPv6 bracket, so that surfaces as a ConfigError here too.
    """
    try:
        request = ArchiveRequest.parse(url)
    except ArchiveRequestError as exc:
        msg = f"{where}[{index}] url: {exc}"
        raise SourceConfigError(msg) from exc

    if not request.has_usable_hash:
        msg = (
            f"{where}[{index}] url {url!r} has no hash; add a"
            " '#sha256=<hex>' fragment (PEP 751 requires an archive hash)"
        )
        raise SourceConfigError(msg)

    try:
        path = urlsplit(request.url).path
    except ValueError as exc:
        msg = f"{where}[{index}] url {url!r} does not parse: {exc}"
        raise SourceConfigError(msg) from exc

    if not path.endswith(".tar.gz"):
        msg = (
            f"{where}[{index}] url {url!r} is not a .tar.gz archive;"
            " only .tar.gz source archives are supported"
        )
        raise SourceConfigError(msg)


def _parse_python_patches(value: object, where: str) -> dict[str, str] | None:
    if value is None:
        return None
    label = f"{where}.python-patches"
    if not isinstance(value, dict):
        msg = (
            f"{label} must be a table of minor -> full version, got"
            f" {type(value).__name__}"
        )
        raise SourceConfigError(msg)
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            msg = f"{label} entries must be string -> string, got {k!r}: {v!r}"
            raise SourceConfigError(msg)
        try:
            minor = Version(k)
            full = Version(v)
        except ValueError as exc:
            msg = f"{label} expects version strings, got {k!r}: {v!r}"
            raise SourceConfigError(msg) from exc

        if full.release[:2] != minor.release[:2]:
            msg = f"{label} value {v!r} is not a patch release of {k!r}"
            raise SourceConfigError(msg)
        out[k] = v
    return out


def parse_workspace(value: object, where: str) -> WorkspaceConfig | None:
    """Parse the optional ``[tool.nab.workspace]`` table.

    Schema today is a single ``members`` field listing literal paths.
    Globs and member-existence checks happen in
    :func:`nab_project.workspace.workspace_local_sources`; this layer only
    validates the table shape so typos like ``member = ...`` (missing
    the ``s``) fail loud at config-parse time.
    """
    if not isinstance(value, dict):
        msg = f"{where} must be a table, got {type(value).__name__}"
        raise SourceConfigError(msg)
    allowed = {"members"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        msg = f"{where} has unknown keys: {unknown!r}; expected {sorted(allowed)!r}"
        raise SourceConfigError(msg)
    members = parse_string_list(value.get("members", []), f"{where}.members")
    return WorkspaceConfig(members=members)


# A declared conflict set must list at least this many members to mean
# anything.  Distinct from ``MIN_ENGAGED_MEMBERS`` (a runtime engagement
# threshold), which happens to be the same number for unrelated reasons.
_MIN_CONFLICT_MEMBERS = 2
_CONFLICT_POLICY_VALUES = {p.value: p for p in ConflictPolicy}
_CONFLICT_SET_KEYS = frozenset({"members", "policy"})


def parse_conflicts(value: object, where: str) -> tuple[ConflictSet, ...]:
    """Parse the optional ``[tool.nab].conflicts`` array.

    Each item is either a bare array of members (uv-compatible; the
    members are mutually exclusive under the default at-most-one
    policy) or a table ``{ members = [...], policy = "..." }`` whose
    ``policy`` value is ``at-most-one`` / ``exactly-one`` /
    ``at-least-one``.  A member is ``{ extra = "NAME" }`` or
    ``{ group = "NAME" }``.
    """
    if not isinstance(value, list):
        msg = f"{where} must be an array of conflict sets, got {type(value).__name__}"
        raise SourceConfigError(msg)
    sets = tuple(_parse_conflict_set(item, where, i) for i, item in enumerate(value))
    _check_conflict_member_uniqueness(sets)
    return sets


def _check_conflict_member_uniqueness(sets: Sequence[ConflictSet]) -> None:
    """Reject a member declared in more than one conflict set."""
    seen: set[ConflictMember] = set()
    for conflict_set in sets:
        for member in conflict_set.members:
            if member in seen:
                msg = (
                    f"conflicts declares {member} in more than one set;"
                    " a member may belong to at most one conflict set"
                )
                raise SourceConfigError(msg)
            seen.add(member)


def _parse_conflict_set(item: object, label: str, index: int) -> ConflictSet:
    where = f"{label}[{index}]"
    if isinstance(item, list):
        return ConflictSet(
            members=_parse_conflict_members(item, where),
            policy=ConflictPolicy.AT_MOST_ONE,
        )
    if isinstance(item, dict):
        unknown = sorted(set(item) - _CONFLICT_SET_KEYS)
        if unknown:
            valid = sorted(_CONFLICT_POLICY_VALUES)
            msg = (
                f"{where}: unknown conflict-set key(s) {unknown!r}; expected a"
                f" table {{ members = [...], policy = '...' }} with policy one of"
                f" {valid!r}, or a bare array of members"
            )
            raise SourceConfigError(msg)
        if "members" not in item:
            msg = f"{where}: a conflict-set table must set 'members'"
            raise SourceConfigError(msg)
        policy = _parse_conflict_policy(item.get("policy"), where)
        return ConflictSet(
            members=_parse_conflict_members(item["members"], f"{where}.members"),
            policy=policy,
        )
    msg = (
        f"{where} must be an array of members or a conflict-set table, got"
        f" {type(item).__name__}"
    )
    raise SourceConfigError(msg)


def _parse_conflict_policy(value: object, where: str) -> ConflictPolicy:
    """Parse the ``policy`` value of a conflict-set table; default at-most-one."""
    return parse_enum(
        value, f"{where}.policy", ConflictPolicy, ConflictPolicy.AT_MOST_ONE
    )


def _parse_conflict_members(value: object, where: str) -> tuple[ConflictMember, ...]:
    if not isinstance(value, list):
        msg = f"{where} must be an array of members, got {type(value).__name__}"
        raise SourceConfigError(msg)
    members = tuple(
        _parse_conflict_member(item, f"{where}[{i}]") for i, item in enumerate(value)
    )
    if len(members) < _MIN_CONFLICT_MEMBERS:
        msg = (
            f"{where} must list at least {_MIN_CONFLICT_MEMBERS} members to be"
            f" a conflict; got {len(members)}"
        )
        raise SourceConfigError(msg)
    if len(set(members)) != len(members):
        msg = f"{where} lists a member more than once"
        raise SourceConfigError(msg)
    return members


def _parse_conflict_member(item: object, where: str) -> ConflictMember:
    if not isinstance(item, dict):
        msg = (
            f"{where} must be a table {{ extra = ... }} or {{ group = ... }},"
            f" got {type(item).__name__}"
        )
        raise SourceConfigError(msg)
    kinds = {k.value for k in ConflictKind}
    unknown = sorted(set(item) - kinds)
    if unknown:
        msg = f"{where}: unknown member key(s) {unknown!r}; expected {sorted(kinds)!r}"
        raise SourceConfigError(msg)
    present = sorted(set(item) & kinds)
    if len(present) != 1:
        msg = f"{where} must name exactly one of {sorted(kinds)!r}, got {present!r}"
        raise SourceConfigError(msg)
    kind = ConflictKind(present[0])
    name = item[present[0]]
    if not isinstance(name, str) or not name:
        msg = f"{where}.{kind.value} must be a non-empty string, got {name!r}"
        raise SourceConfigError(msg)
    try:
        canonical = canonicalize_name(name, validate=True)
    except InvalidName:
        canonical = canonicalize_name(name)
        msg = (
            f"{where}.{kind.value} is not a valid extra/group name: {name!r}"
            f" (canonicalises to {canonical!r})"
        )
        raise SourceConfigError(msg) from None
    return ConflictMember(kind=kind, name=canonical)


_MINOR_RELEASE_PARTS = 2


def _patches_spelling(where: str) -> str:
    """Spell python-patches the way the source behind ``where`` writes it.

    A CLI label ends in the ``*`` its sub-flags fill, so the key takes the
    star's place and the sentence names a flag the user can type.  Every
    other source is a file, which writes the table.
    """
    if where.endswith("*"):
        return f"{where[:-1]}python-patches"
    return "[tool.nab.matrix.python-patches]"


def _validate_matrix_python(spec: str, where: str) -> None:
    """Reject a python axis finer than major.minor.

    The axis lists language (minor) Python versions; patch pins belong in
    the python-patches key.
    """
    try:
        specifier_set = SpecifierSet(spec)
    except InvalidSpecifier as exc:
        msg = f"{where}.python must be a PEP 440 specifier, got {spec!r}"
        raise SourceConfigError(msg) from exc
    for clause in specifier_set:
        try:
            version = Version(clause.version.removesuffix(".*"))
        except ValueError as exc:
            msg = f"{where}.python clause {clause} is not a valid version"
            raise SourceConfigError(msg) from exc

        # Reject pre/post/dev/local qualifiers and patch-level release tuples.
        finer = (
            version.epoch != 0,
            version.pre is not None,
            version.post is not None,
            version.dev is not None,
            version.local is not None,
        )
        if len(version.release) > _MINOR_RELEASE_PARTS or any(finer):
            msg = (
                f"{where}.python axis is a language (minor) version only; "
                f"{clause} is finer than major.minor. Put patch versions in "
                f"{_patches_spelling(where)}."
            )
            raise SourceConfigError(msg)


_PLATFORM_TABLE_KEYS = frozenset(
    {
        "id",
        "libc",
        "runs-on-libc",
        "runs-on-macos",
        "platform-release",
        "platform-version",
        "free-threaded",
    }
)
# The platform kind that reads each knob key; any other kind rejects it.
_PLATFORM_KNOB_OWNER: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "linux": frozenset({"libc", "runs-on-libc"}),
        "macos": frozenset({"runs-on-macos"}),
    }
)


def _parse_matrix_platforms(value: object, label: str) -> tuple[PlatformSpec, ...]:
    """Parse ``matrix.platforms``: bare ids, tables, or a mix of both.

    A bare id takes the platform's default tag knobs; the table form declares
    them (libc family, the libc and macOS the lock must run on, kernel
    marker values, free-threaded build).  Both become a :class:`PlatformSpec`,
    so everything downstream reads one shape.
    """
    if not isinstance(value, list):
        msg = f"{label}.platforms must be a list, got {type(value).__name__}"
        raise SourceConfigError(msg)
    platforms: list[PlatformSpec] = []
    for i, item in enumerate(value):
        where = f"{label}.platforms[{i}]"
        if isinstance(item, str):
            platforms.append(_platform_spec(where, platform_id=item))
        elif isinstance(item, dict):
            platforms.append(_parse_platform_table(where, item))
        else:
            msg = f"{where} must be a platform id or a table, got {type(item).__name__}"
            raise SourceConfigError(msg)
    return tuple(platforms)


def _platform_spec(where: str, **knobs: Any) -> PlatformSpec:
    """Build a :class:`PlatformSpec`, reporting its knob check as a config error."""
    try:
        return PlatformSpec(**knobs)
    except ValueError as exc:
        msg = f"invalid {where}: {exc}"
        raise SourceConfigError(msg) from exc


def _parse_platform_table(where: str, value: dict[str, Any]) -> PlatformSpec:
    """Parse one ``matrix.platforms`` table entry into a :class:`PlatformSpec`."""
    unknown = sorted(set(value) - _PLATFORM_TABLE_KEYS)
    if unknown:
        msg = (
            f"{where} has unknown keys: {unknown!r};"
            f" expected {sorted(_PLATFORM_TABLE_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    if "id" not in value:
        msg = f"{where} missing required key 'id'"
        raise SourceConfigError(msg)

    platform_id = _parse_string_value(f"{where}.id", value["id"])
    _reject_foreign_knobs(where, value, platform_id)

    return _platform_spec(
        where,
        platform_id=platform_id,
        libc=_parse_libc(f"{where}.libc", value.get("libc")),
        runs_on_libc=_parse_major_minor(
            f"{where}.runs-on-libc", value.get("runs-on-libc")
        ),
        runs_on_macos=_parse_major_minor(
            f"{where}.runs-on-macos", value.get("runs-on-macos")
        ),
        platform_release=_parse_string_value(
            f"{where}.platform-release", value.get("platform-release", "")
        ),
        platform_version=_parse_string_value(
            f"{where}.platform-version", value.get("platform-version", "")
        ),
        free_threaded=_parse_bool(
            f"{where}.free-threaded", value.get("free-threaded"), default=False
        ),
    )


def _reject_foreign_knobs(where: str, value: dict[str, Any], platform_id: str) -> None:
    """Reject a knob key the declared platform's kind cannot read.

    :class:`PlatformSpec` refuses a knob whose *value* moves a platform that
    ignores it, but it cannot see a key written at its own default.  The
    table can, and a key that selects no wheel is a mistake either way.  An
    unknown ``platform_id`` is left to the matrix, which names the whole
    unknown set at once.
    """
    kind = platform_kind(platform_id)
    if kind is None:
        return
    for owner, keys in _PLATFORM_KNOB_OWNER.items():
        if kind == owner:
            continue
        foreign = sorted(keys & set(value))
        if foreign:
            msg = (
                f"{where} declares {foreign!r}, which only a {owner} platform"
                f" reads, but its id is {platform_id!r}"
            )
            raise SourceConfigError(msg)


def _parse_libc(key: str, value: object) -> Libc:
    """Parse a libc family name; an absent key takes the default family."""
    if value is None:
        return DEFAULT_LIBC
    text = _parse_string_value(key, value)
    if text not in LIBC_MAJOR:
        msg = f"{key} must be one of {sorted(LIBC_MAJOR)!r}, got {text!r}"
        raise SourceConfigError(msg)
    return cast("Libc", text)


def _parse_major_minor(key: str, value: object) -> tuple[int, int] | None:
    """Parse a ``major.minor`` string into a pair; ``None`` passes through."""
    if value is None:
        return None
    text = _parse_string_value(key, value)
    try:
        version = Version(text)
    except ValueError as exc:
        msg = f"{key} must be a 'major.minor' version, got {text!r}"
        raise SourceConfigError(msg) from exc
    release = version.release
    two_part = len(release) == _MINOR_RELEASE_PARTS
    # str() renders the normalized version, so an epoch or a pre/post/dev/local
    # qualifier shows up as a mismatch here.
    if not two_part or str(version) != f"{release[0]}.{release[1]}":
        msg = f"{key} must be exactly 'major.minor', got {text!r}"
        raise SourceConfigError(msg)
    return (release[0], release[1])


# The two tokens matrix.python-order takes; ``nab.flagtypes`` spells the
# same pair as a Literal.
PYTHON_ORDERS = ("asc", "desc")

# The interpreter implementations both the matrix and the environment model.
IMPLEMENTATIONS = ("cpython", "pypy")

_MATRIX_KEYS = frozenset(
    {
        "python",
        "platforms",
        "python-order",
        "python-patches",
        "implementations",
    }
)


def parse_matrix(value: object, where: str) -> MatrixConfig:
    """Read ``[tool.nab.matrix]`` into its five axes."""
    if not isinstance(value, dict):
        msg = f"{where} must be a table, got {type(value).__name__}"
        raise SourceConfigError(msg)
    unknown = sorted(set(value) - _MATRIX_KEYS)
    if unknown:
        msg = (
            f"{where} has unknown keys: {unknown!r}; expected {sorted(_MATRIX_KEYS)!r}"
        )
        raise SourceConfigError(msg)
    try:
        python = value["python"]
        platforms_raw = value["platforms"]
    except KeyError as missing:
        msg = f"{where} missing required key {missing!s}"
        raise SourceConfigError(msg) from None
    if not isinstance(python, str):
        msg = f"{where}.python must be a string PEP 440 specifier"
        raise SourceConfigError(msg)
    _validate_matrix_python(python, where)
    platforms = _parse_matrix_platforms(platforms_raw, where)
    if not platforms:
        msg = f"{where}.platforms must list at least one platform id"
        raise SourceConfigError(msg)
    # One target per platform id.  A lockfile entry is selected by a PEP 508
    # marker, which has no libc or free-threading variable, so two targets
    # sharing an id would render the same marker.
    _reject_duplicates(f"{where}.platforms", tuple(p.platform_id for p in platforms))
    python_order = _parse_string_value(
        f"{where}.python-order", value.get("python-order", "asc")
    )
    if python_order not in PYTHON_ORDERS:
        msg = f"{where}.python-order must be 'asc' or 'desc', got {python_order!r}"
        raise SourceConfigError(msg)
    patches = _parse_python_patches(value.get("python-patches"), where)
    implementations = _parse_implementations(value.get("implementations"), where)
    config = MatrixConfig(
        python=python,
        platforms=platforms,
        python_order=python_order,
        python_patches=patches,
        implementations=implementations,
    )
    _validate_matrix_axes(config, where)
    return config


def _validate_matrix_axes(config: MatrixConfig, where: str) -> None:
    """Expand the matrix eagerly to catch bad axes at parse time."""
    matrix = matrix_from_config(config)
    try:
        matrix.expand()
    except ValueError as exc:
        msg = f"{where} is invalid: {exc}"
        raise SourceConfigError(msg) from exc


def _parse_implementations(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ("cpython",)
    label = f"{where}.implementations"
    impls = parse_string_list(value, label)
    if not impls:
        msg = f"{label} must list at least one implementation"
        raise SourceConfigError(msg)
    _reject_duplicates(label, impls)
    unknown = sorted(set(impls) - set(IMPLEMENTATIONS))
    if unknown:
        msg = (
            f"{label} has unknown entries: {unknown!r}; "
            f"expected {list(IMPLEMENTATIONS)!r}"
        )
        raise SourceConfigError(msg)
    return impls
