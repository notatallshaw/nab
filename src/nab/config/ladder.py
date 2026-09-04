"""The layered options, the sources that bind them, and how they print.

:data:`OPTIONS` is the keyed half of :data:`nab.optiontable.ALL`: the rows a
configuration source may set, in the order ``nab config list`` prints them.
``tasks/gen_cli.py`` writes them as literals into :mod:`nab.config.registry`,
which is where this module reads them.  An option's key, scope, hooks, rung 0,
``NAB_*`` name and CLI flag are all declared in the table, so every row here
carries a key and ``name`` is that key.

A run reads seven sources at six ranks, low precedence to high: the built-in
defaults, a system ``nab.toml``, a user ``nab.toml``, then ``pyproject.toml``'s
``[tool.nab]`` and a project-dir ``nab.toml`` sharing one rank, then ``NAB_*``
and finally the CLI.  Each is read into a :class:`Layer` of typed values, and
:func:`resolve_config` gives every row, bar a table key the CLI sub-flags
fold over the table the files declare, the whole value the highest source that
bound it supplied.  The category gate runs while a source is read: a
project-scope option in a user ``nab.toml``, or a user-scope option in
``[tool.nab]``, is a :class:`~nab.config.values.SourceConfigError` rather than
a value.  ``nab config --include-rejected`` collects those refusals instead of
raising, which is what the ``rejections`` parameter threaded through the
readers is for.

The renderers close the loop.  ``list`` prints every row with the value that
won and where it came from, ``get`` prints one row's value alone, and
``explain`` prints one row's help, the page documenting it and its whole
stack, with the winner marked.  All three read what :func:`resolve_config`
returns, so what is printed is what the run would use, and a rejected source
reaches them only as a :class:`RejectedLayer`.
"""

from __future__ import annotations

import enum
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli

from nab_project import toml_io
from nab_project.paths import PathState, path_state, realpath
from nab_project.value import ValueType

from .. import env
from ..optiondefs import Opt, Scope
from .hooks import declaring_dir, matrix_table
from .registry import OPTIONS
from .subflags import (
    BY_PARENT,
    CliTable,
    build_cli_tables,
    cli_table_label,
    fold_cli_table,
    render_cli_table,
)
from .values import CliTableError, SourceConfigError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "BY_KEY",
    "OPTIONS",
    "PRECEDENCE",
    "EffectiveValue",
    "Layer",
    "Opt",
    "Origin",
    "RejectedLayer",
    "Scope",
    "SourceKind",
    "SourceRoots",
    "build_cli_layer",
    "build_cli_overrides",
    "config_search_roots",
    "discover_layers",
    "docs_path",
    "docs_url",
    "orphan_rejections",
    "project_cli_override_notice",
    "project_cli_override_records",
    "project_key_path",
    "pyproject_registry_keys",
    "read_env_layer",
    "reject_user_keys_in_pyproject",
    "render_explain",
    "render_get",
    "render_list",
    "resolve_config",
    "scope_label",
]


class SourceKind(enum.Enum):
    """One discoverable configuration source, low precedence to high.

    The two project-level TOML sources (``PYPROJECT`` and
    ``PROJECT_TOML``) share a precedence rank; the rest are totally
    ordered by :data:`PRECEDENCE`.
    """

    DEFAULT = "default"
    SYSTEM_TOML = "system"
    USER_TOML = "user"
    PYPROJECT = "pyproject"
    PROJECT_TOML = "project"
    ENV = "env"
    CLI = "cli"


# Precedence rank, low -> high.  PYPROJECT and PROJECT_TOML share rank 3:
# they are the same (project) precedence level, and on a tie PROJECT_TOML
# (the project-dir nab.toml) sorts last (wins).
PRECEDENCE: dict[SourceKind, int] = {
    SourceKind.DEFAULT: 0,
    SourceKind.SYSTEM_TOML: 1,
    SourceKind.USER_TOML: 2,
    SourceKind.PYPROJECT: 3,
    SourceKind.PROJECT_TOML: 3,
    SourceKind.ENV: 4,
    SourceKind.CLI: 5,
}

BY_KEY: dict[str, Opt] = {row.name: row for row in OPTIONS}


def project_key_path(key: str, kind: SourceKind) -> str:
    """Return the table path a project file of ``kind`` gives ``key``.

    A project-dir nab.toml takes nab's keys at the top level; a
    pyproject.toml takes them under ``[tool.nab]``.
    """
    return key if kind is SourceKind.PROJECT_TOML else f"tool.nab.{key}"


def pyproject_registry_keys() -> frozenset[str]:
    """Registry keys a pyproject ``[tool.nab]`` table may legitimately carry.

    Only PROJECT-scope registry options are allowed in pyproject; the
    single-environment parser (:mod:`nab.config.model`) folds this set
    into its own known-keys so a registry key is not double-reported as
    an unknown ``[tool.nab]`` key.
    """
    return frozenset(
        spec.name for spec in OPTIONS if spec.allowed_in_toml(SourceKind.PYPROJECT)
    )


class Origin(ValueType):
    """Where a value came from: a source kind plus a display label."""

    __slots__ = __match_args__ = ("kind", "label")

    kind: SourceKind
    label: str

    def __init__(self, kind: SourceKind, label: str) -> None:
        """Record the source a value came from."""
        self.kind = kind
        self.label = label

    @property
    def scope(self) -> str:
        """The provenance scope name shown by ``nab config``.

        Mirrors the source kind's value for every kind except
        ``PYPROJECT``, which reports "project" (it sits at the project
        precedence level alongside the project-dir nab.toml).
        """
        return scope_label(self.kind)

    def outranks(self, other: Origin) -> bool:
        """Whether this origin sits at a strictly higher precedence level.

        A tie is not an outranking: ``PYPROJECT`` and ``PROJECT_TOML``
        share a rank, so neither overrides the other here.
        """
        return PRECEDENCE[self.kind] > PRECEDENCE[other.kind]


def scope_label(kind: SourceKind) -> str:
    """Return the scope name ``nab config`` reports for a source kind.

    Distinct from Scope (PROJECT/USER, the gate axis): provenance reports
    the source, so a project nab.toml reports "project", env reports
    "env", etc.  Every kind reports its own value except PYPROJECT, which
    shares the project precedence level and so reports "project".
    """
    return "project" if kind is SourceKind.PYPROJECT else kind.value


# A source that declared no table a sub-flag spells.
_NO_RAW: Mapping[str, Any] = {}


class Layer(ValueType):
    """A set of (key -> value) bindings discovered from one source.

    ``raw`` carries, for a key that sub-rows spell, the table as the
    source wrote it: unparsed for a file source, and the
    :class:`~nab.config.subflags.CliTable` for the CLI layer.  The fold
    lays one over the other and parses the merged table.
    """

    __slots__ = __match_args__ = ("origin", "values", "raw")

    origin: Origin
    values: Mapping[str, Any]
    raw: Mapping[str, Any]

    def __init__(
        self,
        origin: Origin,
        values: Mapping[str, Any],
        raw: Mapping[str, Any] = _NO_RAW,
    ) -> None:
        """Record the bindings ``origin`` supplied."""
        self.origin = origin
        self.values = values
        self.raw = raw


class RejectedLayer(ValueType):
    """A source refused by the registry: a key outside its scope, or unknown.

    Captured (not raised) by :func:`discover_layers` for the TOML sources and
    :func:`read_env_layer` for the ``NAB_*`` ones, only when the caller asks
    to collect rejections for ``nab config --include-rejected``.  The normal
    load path raises :class:`SourceConfigError` for TOML and warns for env.
    """

    __slots__ = __match_args__ = ("origin", "key", "reason")

    origin: Origin
    key: str
    reason: str

    def __init__(self, origin: Origin, key: str, reason: str) -> None:
        """Record why ``origin``'s ``key`` was refused."""
        self.origin = origin
        self.key = key
        self.reason = reason


class EffectiveValue(ValueType):
    """One option's winning value plus its full shadowed stack."""

    __slots__ = __match_args__ = (
        "spec",
        "value",
        "origin",
        "stack",
        "rejected",
        "cli_table",
    )

    spec: Opt
    value: Any
    origin: Origin
    # Every binding for this key in precedence order (low -> high),
    # the last of which is the winner.
    stack: tuple[tuple[Origin, Any], ...]
    rejected: tuple[RejectedLayer, ...]
    # The keys the command line set, on a table key it folded into.
    cli_table: CliTable | None

    def __init__(
        self,
        spec: Opt,
        value: Any,
        origin: Origin,
        stack: tuple[tuple[Origin, Any], ...],
        rejected: tuple[RejectedLayer, ...] = (),
        cli_table: CliTable | None = None,
    ) -> None:
        """Record the value ``origin`` bound for ``spec``."""
        self.spec = spec
        self.value = value
        self.origin = origin
        self.stack = stack
        self.rejected = rejected
        self.cli_table = cli_table


class SourceRoots(ValueType):
    """Injectable search roots so config discovery is hermetic in tests.

    ``system_toml`` and ``user_toml`` point at the system/user
    ``nab.toml`` files.  ``project_dir`` is the directory holding the
    pyproject; the project ``nab.toml`` is looked up beside it.
    ``pyproject`` names the pyproject file itself when the user pointed at
    a non-default name; left ``None`` it defaults to
    ``project_dir / "pyproject.toml"`` so the registry reads the same file
    the rest of nab does.  Any field may be ``None`` to skip that source.
    There is no walk-up: the project source is the directory of the
    pyproject only.
    """

    __slots__ = __match_args__ = (
        "system_toml",
        "user_toml",
        "project_dir",
        "pyproject",
    )

    system_toml: Path | None
    user_toml: Path | None
    project_dir: Path | None
    pyproject: Path | None

    def __init__(
        self,
        system_toml: Path | None = None,
        user_toml: Path | None = None,
        project_dir: Path | None = None,
        pyproject: Path | None = None,
    ) -> None:
        """Record the roots config discovery may read."""
        self.system_toml = system_toml
        self.user_toml = user_toml
        self.project_dir = project_dir
        self.pyproject = pyproject


def build_cli_overrides(
    locals_by_param: Mapping[str, Any], flags: Mapping[str, str]
) -> dict[str, Any]:
    """Map ``{cli_param: value}`` to a registry-keyed override dict.

    Iterates :data:`OPTIONS`, reads each row's ``cli_param`` out of
    ``locals_by_param``, and keeps only the keys the user actually set.
    An unset scalar flag is ``None`` and an unset repeatable flag is an
    empty tuple (the append-action default); both are omitted so they do not
    shadow the file ladder.  A file-only row (``cli_param`` is ``None``)
    has no CLI flag; a table key is spelled by its sub-rows instead, and
    :func:`nab.config.subflags.build_cli_tables` collects the keys the
    line set for it.  ``flags`` names the flag a parameter was spelled by
    when that is not the row's own flag, and is empty for a line with no
    such parameter.  Both the run subcommands and ``nab config`` build
    their override dict through this single helper, keyed off the registry
    rather than a per-option if-chain.
    """
    out: dict[str, Any] = {}
    for spec in OPTIONS:
        if spec.cli_param is None:
            continue
        value = locals_by_param[spec.cli_param]
        if value is None or (isinstance(value, tuple) and not value):
            continue
        out[spec.name] = value
    out.update(build_cli_tables(locals_by_param, flags))
    return out


_logger = logging.getLogger(__name__)


def tool_nab_section(data: Mapping[str, Any]) -> Any:
    """Return the raw ``[tool.nab]`` value from parsed TOML ``data``.

    Returns ``{}`` when ``[tool]`` is absent or is not a table, so callers can
    chain ``.get`` safely.  The value may itself be a non-table when
    ``[tool.nab]`` is malformed.
    """
    tool = data.get("tool", {})
    return tool.get("nab", {}) if isinstance(tool, dict) else {}


def reject_user_keys_in_pyproject(raw: Mapping[str, Any]) -> None:
    """Raise the category error for any USER registry key in ``[tool.nab]``.

    The parser fold: a USER-scope option (e.g. ``offline``,
    ``cache-dir``) set in pyproject ``[tool.nab]`` must surface the
    registry category error (``pyproject [tool.nab]`` is project-scope
    only) rather than the generic unknown-key error the pyproject
    parser would otherwise raise.  PROJECT keys and keys the registry
    does not own are left for the pyproject parser to handle.

    Each message quotes the key and names the table it refuses, so the
    caller adds no prefix of its own.
    """
    for key in raw:
        spec = BY_KEY.get(key)
        if spec is None:
            continue
        if not spec.allowed_in_toml(SourceKind.PYPROJECT):
            raise SourceConfigError(_gate_reason(spec, SourceKind.PYPROJECT))


def _load_toml_layer(
    path: Path,
    kind: SourceKind,
    *,
    rejections: list[RejectedLayer] | None = None,
) -> Layer:
    """Read one TOML source into a :class:`Layer`, gating by category.

    ``kind`` selects how the file is read: a ``PYPROJECT`` source reads
    ``[tool.nab]``; the standalone ``nab.toml`` sources read top-level
    keys.  Every registry key is parsed by its row.  A key that names an
    option not allowed in ``kind`` (the category gate) raises
    :class:`SourceConfigError`, unless ``rejections`` is supplied, in which
    case it is appended there and skipped (for ``--include-rejected``).
    """
    raw = _read_raw_table(path, kind)
    origin = Origin(kind, str(path))
    values: dict[str, Any] = {}
    raw_tables: dict[str, Any] = {}
    # Carry the declaring file's directory and matrix header structurally: a
    # relative local-source path resolves against the directory, and the
    # patch-table refusal names the header this file writes.
    with declaring_dir(path.parent), matrix_table(project_key_path("matrix", kind)):
        for key, value in raw.items():
            spec = BY_KEY.get(key)
            if spec is None:
                # An unknown key (a typo) crashes naming the file rather than
                # being dropped, the same way an unknown NAB_* var does.  On
                # the resolve path read_pyproject_config rejects an unknown
                # pyproject [tool.nab] key before this loader runs; the
                # inspector reaches here, so it reports the typo too instead
                # of silently ignoring it.
                valid = sorted(BY_KEY)
                msg = (
                    f"{path}: {key!r} is not a valid nab setting; the known"
                    f" keys are {valid!r}."
                )
                if rejections is not None:
                    rejections.append(RejectedLayer(origin, key, msg))
                    continue
                raise SourceConfigError(msg)
            where = f"{path}: {key}"
            if not spec.allowed_in_toml(kind):
                reason = _gate_reason(spec, kind)
                if rejections is not None:
                    rejections.append(RejectedLayer(origin, key, reason))
                    continue
                msg = f"{path}: {reason}"
                raise SourceConfigError(msg)
            values[key] = spec.parse(value, where)
            if key in BY_PARENT:
                raw_tables[key] = value
    return Layer(origin, values, raw_tables)


def _gate_reason(spec: Opt, kind: SourceKind) -> str:
    if kind is SourceKind.PYPROJECT:
        where = "pyproject [tool.nab] (project-scope only)"
    else:
        where = f"a {scope_label(kind)} nab.toml"
    return (
        f"{spec.name!r} is a {spec.scope_name}-scope option and cannot be set"
        f" in {where}"
    )


def _read_raw_table(path: Path, kind: SourceKind) -> Mapping[str, Any]:
    # TOML is UTF-8, so a file that will not decode is invalid TOML.
    try:
        with path.open("rb") as f:
            data = toml_io.load(f)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise SourceConfigError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise SourceConfigError(msg) from exc
    if kind is SourceKind.PYPROJECT:
        section = tool_nab_section(data)
        # A non-table [tool.nab] is malformed, not an empty config.
        if not isinstance(section, dict):
            msg = f"{path}: [tool.nab] must be a table, got {type(section).__name__}"
            raise SourceConfigError(msg)
        return section
    return data


def read_env_layer(
    environ: Mapping[str, str] | None = None,
    *,
    rejections: list[RejectedLayer] | None = None,
) -> Layer:
    """Read ``NAB_*`` for every registry row that declares an env var.

    ``environ`` defaults to the process environment through
    :func:`nab.env.current`.  PROJECT options never declare an env var, so
    the env layer carries USER options only.  A ``NAB_<KEY>`` naming a
    PROJECT option (e.g. ``NAB_RESOLUTION``) draws a warning from
    :func:`_warn_renamed_env`; any other unknown ``NAB_*`` name (e.g. a
    typo) one from :func:`_warn_unknown_env`.  Neither is applied, and
    neither is fatal.  The two names the output layer owns
    (:data:`nab.env.OUTPUT_OWNED`) are skipped silently and bind nothing.
    When ``rejections`` is supplied (``nab config --include-rejected``)
    those env casualties are recorded there instead of warned, mirroring
    the TOML loader.
    """
    environ = env.current(environ)
    _warn_renamed_env(environ, rejections=rejections)
    _warn_unknown_env(environ, rejections=rejections)
    values: dict[str, Any] = {}
    for spec in OPTIONS:
        if spec.env_var is None:
            continue
        if spec.env_var not in environ:
            continue
        values[spec.name] = spec.parse(environ[spec.env_var], spec.env_var)
    return Layer(Origin(SourceKind.ENV, "env"), values)


def _warn_unknown_env(
    environ: Mapping[str, str],
    *,
    rejections: list[RejectedLayer] | None = None,
) -> None:
    """Warn about any ``NAB_*`` var that is neither a known nor renamed name.

    The env half of the category gate: a typo'd or made-up ``NAB_<KEY>``
    (e.g. ``NAB_OFLINE``) is ignored with a warning naming the variable,
    never applied and never fatal.  Renamed PROJECT names are left for
    :func:`_warn_renamed_env`, which gives the more specific
    not-env-settable message.  The names the output layer owns
    (:data:`nab.env.OUTPUT_OWNED`) set no registry row, so they are skipped
    here, but the message still offers them: a user who mistyped
    ``NAB_VERBOSITY`` is reaching for a variable nab honours.  When
    ``rejections`` is supplied the var is recorded there instead of warned.
    """
    known = {spec.env_var for spec in OPTIONS if spec.env_var is not None}
    renamed = set(_renamed_env_names())
    honoured = ", ".join(sorted(known | env.OUTPUT_OWNED))
    for name in environ:
        if (
            not name.startswith("NAB_")
            or name in known
            or name in renamed
            or name in env.OUTPUT_OWNED
        ):
            continue
        msg = (
            f"{name} is not a recognized nab setting and was ignored; the"
            f" known NAB_* variables are {honoured}."
        )
        if rejections is not None:
            rejections.append(RejectedLayer(Origin(SourceKind.ENV, name), name, msg))
            continue
        _logger.warning("%s", msg)


def _renamed_env_names() -> dict[str, Opt]:
    """Map every PROJECT row's would-be ``NAB_<KEY>`` name to its row."""
    names: dict[str, Opt] = {}
    for spec in OPTIONS:
        if spec.scope is Scope.PROJECT:
            env_name = "NAB_" + spec.name.upper().replace("-", "_")
            names[env_name] = spec
    return names


def _warn_renamed_env(
    environ: Mapping[str, str],
    *,
    rejections: list[RejectedLayer] | None = None,
) -> None:
    """Warn about a ``NAB_<KEY>`` naming a PROJECT (non-env-settable) option.

    The variable is ignored, never applied and never fatal.  When
    ``rejections`` is supplied the var is recorded there under the spec's
    key (so ``explain <key> --include-rejected`` lists it) instead of
    warned.
    """
    for env_name, spec in _renamed_env_names().items():
        if env_name in environ:
            # A file-only row (cli_flag is None) carries no per-run flag,
            # so the message names only the two project files for it.
            override = (
                ""
                if spec.cli_flag is None
                else f", or override per-run with {spec.cli_flag}"
            )
            msg = (
                f"{env_name} was ignored: {spec.name!r} is a"
                f" {spec.scope_name}-scope option and is not env-settable."
                f"  Set it in pyproject [tool.nab].{spec.name} or a project-dir"
                f" nab.toml{override}."
            )
            if rejections is not None:
                rejections.append(
                    RejectedLayer(Origin(SourceKind.ENV, env_name), spec.name, msg)
                )
                continue
            _logger.warning("%s", msg)


def config_search_roots(pyproject: Path) -> SourceRoots:
    """Locate the system/user/project config roots for ``pyproject``.

    The same XDG roots as cache-dir: the user ``nab.toml`` at
    ``$XDG_CONFIG_HOME/nab/nab.toml`` or ``~/.config/nab/nab.toml``, the
    system one at ``/etc/nab/nab.toml``.  Discovery is project-dir only,
    with no walk-up, and the suite injects roots by monkeypatching this
    function so the real ``~/.config`` is never read.

    ``pyproject`` is the file the user pointed at, so the pyproject layer
    reads that exact file even when its name is not ``pyproject.toml``, and
    the project-dir ``nab.toml`` is looked up beside it.  The root keeps
    that file's resolved directory rather than the resolved file, so a
    relative ``local-sources`` path resolves against the symlink's
    directory the way the resolve does.
    """
    base = env.config_root()
    user_dir = Path(base) if base else Path.home() / ".config"
    project_dir = realpath(pyproject.parent)
    return SourceRoots(
        system_toml=Path("/etc/nab/nab.toml"),
        user_toml=user_dir / "nab" / "nab.toml",
        project_dir=project_dir,
        pyproject=project_dir / pyproject.name,
    )


def discover_layers(
    roots: SourceRoots,
    *,
    rejections: list[RejectedLayer] | None = None,
    read_pyproject: bool = True,
) -> list[Layer]:
    """Read every present TOML source into ordered layers (low -> high).

    Reads system, user, pyproject, and project-dir ``nab.toml`` in
    precedence order, skipping any root that is ``None`` or whose file is
    absent.  Hermetic: the caller supplies the roots, so nothing touches
    the real ``~/.config``.  There is no walk-up: the project source is
    the pyproject directory only.

    ``read_pyproject=False`` drops the pyproject layer while keeping the
    project-dir ``nab.toml``, for a caller reading a USER-scope option
    the category gate bars pyproject from setting.
    """
    if roots.project_dir is None or not read_pyproject:
        pyproject_path = None
    elif roots.pyproject is not None:
        pyproject_path = roots.pyproject
    else:
        pyproject_path = roots.project_dir / "pyproject.toml"
    layers: list[Layer] = []
    plan: list[tuple[Path | None, SourceKind]] = [
        (roots.system_toml, SourceKind.SYSTEM_TOML),
        (roots.user_toml, SourceKind.USER_TOML),
        (pyproject_path, SourceKind.PYPROJECT),
        (
            None if roots.project_dir is None else roots.project_dir / "nab.toml",
            SourceKind.PROJECT_TOML,
        ),
    ]
    for path, kind in plan:
        if path is None:
            continue
        state = path_state(path)
        if state is PathState.ABSENT:
            continue
        if not state.should_read:
            # A path that exists but is not a regular file (e.g. an
            # accidental `mkdir nab.toml`) would be silently ignored by an
            # is_file() filter, so crash naming it rather than dropping the
            # config source.  A failed stat goes to the read instead, which
            # names the errno.
            msg = f"{path} exists but is not a regular file"
            raise SourceConfigError(msg)
        layers.append(_load_toml_layer(path, kind, rejections=rejections))
    return layers


def resolve_config(
    layers: Sequence[Layer],
    env_layer: Layer,
    cli_layer: Layer,
    *,
    rejected: Sequence[RejectedLayer] = (),
) -> dict[str, EffectiveValue]:
    """Merge all layers into one effective value per registry option.

    ``layers`` are the discovered TOML layers (any order; ranked by
    :data:`PRECEDENCE`).  ``env_layer`` and ``cli_layer`` are the env
    and CLI bindings.  Returns a ``key -> EffectiveValue`` map covering
    every registry row, each carrying its winner ``(scope, origin)`` and
    the full shadowed stack.  ``rejected`` (category-gate casualties) is
    attached per key for ``explain --include-rejected``.

    Whatever the row's type, the highest source that binds the key
    supplies the whole value: a list or table from a higher source
    replaces the one below rather than adding to it.  The command line is
    the one exception, and only on a key its sub-flags spell: those keys
    are laid over the table the files declare and the merged table goes
    to the key's parse hook.
    """
    all_layers = [*layers, env_layer, cli_layer]
    out: dict[str, EffectiveValue] = {}
    for spec in OPTIONS:
        stack = _stack_for(spec, all_layers)
        cli_table = cli_layer.raw.get(spec.name)
        if cli_table is not None:
            stack = [*stack, _folded(spec, cli_table, all_layers)]
        rejected_for_key = tuple(r for r in rejected if r.key == spec.name)
        if stack:
            origin, value = stack[-1]
        else:
            origin, value = Origin(SourceKind.DEFAULT, "builtin-default"), spec.rdefault
        out[spec.name] = EffectiveValue(
            spec=spec,
            value=value,
            origin=origin,
            stack=tuple(stack) if stack else ((origin, value),),
            rejected=rejected_for_key,
            cli_table=cli_table,
        )
    return out


def _folded(
    spec: Opt, table: CliTable, all_layers: Sequence[Layer]
) -> tuple[Origin, Any]:
    """Parse the table the command line wrote over the one the files declare.

    The parse hook sees the merged table, so a knob the file declared and a
    platform the flag named are checked against each other.  The file table
    is parsed alone as well, when its layer is read, so a cross-key rule the
    flag would have satisfied can still refuse there.
    """
    merged = fold_cli_table(spec, table, _file_raw(spec, all_layers))
    try:
        value = spec.parse(merged, cli_table_label(spec))
    except SourceConfigError as exc:
        raise CliTableError(str(exc)) from exc
    return Origin(SourceKind.CLI, "cli"), value


def _file_raw(spec: Opt, all_layers: Iterable[Layer]) -> Any | None:
    """Return the unparsed table the files declared, or ``None`` for none.

    Only the two project files can declare a table a sub-flag spells, and
    ``_stack_for`` has already refused them declaring it differently, so the
    fold reads the same table whichever of them ranks highest.
    """
    found = [
        (layer.origin, layer.raw[spec.name])
        for layer in all_layers
        if spec.name in layer.raw and layer.origin.kind is not SourceKind.CLI
    ]
    found.sort(key=_rank)
    return found[-1][1] if found else None


def _stack_for(spec: Opt, all_layers: Iterable[Layer]) -> list[tuple[Origin, Any]]:
    """Bindings for one option across layers, sorted low -> high.

    Sorted by source precedence; the pyproject/project-dir tie is broken
    so the project-dir nab.toml sorts last (wins).  Co-presence in both
    project files with conflicting values is a hard error; identical
    values pass.
    """
    found = [
        (layer.origin, layer.values[spec.name])
        for layer in all_layers
        if spec.name in layer.values
    ]
    found.sort(key=_rank)
    _check_project_file_conflict(spec, found)
    return found


def _rank(binding: tuple[Origin, Any]) -> tuple[int, bool]:
    """Sort key for one binding: source precedence, then the rank-3 tie.

    The pyproject/project-dir tie is broken so the project-dir nab.toml
    (``False < True``) sorts last and wins.
    """
    origin, _value = binding
    return (PRECEDENCE[origin.kind], origin.kind is SourceKind.PROJECT_TOML)


def _check_project_file_conflict(
    spec: Opt, found: Sequence[tuple[Origin, Any]]
) -> None:
    """Reject one key set differently in pyproject and the project nab.toml.

    Co-presence is allowed; setting the same key to different values
    across the two same-precedence project files is a hard
    :class:`SourceConfigError`, not a silent last-wins.  Identical values
    are fine.

    Every row is compared as one whole value, lists and tables included,
    so two project files declaring different constraints or different
    sub-keys of one table conflict rather than combining.
    """
    by_kind = {origin.kind: value for origin, value in found}
    if SourceKind.PYPROJECT not in by_kind or SourceKind.PROJECT_TOML not in by_kind:
        return
    pyproject_value = by_kind[SourceKind.PYPROJECT]
    project_value = by_kind[SourceKind.PROJECT_TOML]
    if pyproject_value == project_value:
        return
    msg = (
        f"config {spec.name!r} is set to conflicting values in pyproject"
        f" [tool.nab] ({spec.render(pyproject_value)!r}) and project-dir"
        f" nab.toml ({spec.render(project_value)!r}).  Both files sit at the"
        " same precedence level; set the key in only one, or set them to the"
        " same value."
    )
    raise SourceConfigError(msg)


def build_cli_layer(values: Mapping[str, Any]) -> Layer:
    """Build the CLI layer from a ``{key: value}`` map of set overrides.

    ``values`` holds only keys the user actually set on the CLI (an
    unset flag is omitted, so it does not shadow lower layers).  Each
    value is normalised through its registry row so the effective value
    carries the typed form regardless of how the flag was spelled.

    A row with no command line is set from a file alone, so a value for
    one is the caller's mistake and raises ``ValueError`` rather than the
    user-facing ``SourceConfigError``.  A table key's value is a
    :class:`~nab.config.subflags.CliTable`, carried unparsed:
    :func:`resolve_config` parses it once, folded over whatever the
    project files declared.
    """
    parsed: dict[str, Any] = {}
    raw_tables: dict[str, Any] = {}
    for key, value in values.items():
        spec = BY_KEY[key]
        if isinstance(value, CliTable):
            raw_tables[key] = value
            continue
        if not spec.commands:
            msg = f"{key} takes no command line, so no CLI layer can set it"
            raise ValueError(msg)

        # A repeatable flag arrives as a tuple; the parse hooks expect a
        # TOML list, so normalise it here.
        raw_value = list(value) if isinstance(value, tuple) else value
        parsed[key] = spec.parse(raw_value, str(spec.cli_flag))
    return Layer(Origin(SourceKind.CLI, "cli"), parsed, raw_tables)


def _ordered(effective: Mapping[str, EffectiveValue]) -> list[EffectiveValue]:
    return [effective[spec.name] for spec in OPTIONS]


# Column widths for the ``nab config list`` table, shared by the header
# and every row so the two cannot drift.  The trailing ``origin`` column
# is unpadded (last field).
_LIST_KEY_W = 20
_LIST_VALUE_W = 20
_LIST_SCOPE_W = 9
# Status column width for ``nab config explain`` (winner/shadowed/rejected).
_EXPLAIN_STATUS_W = 9

# Where the pages live in the checkout, and where they are published:
# [project.urls].Documentation from pyproject.toml, at the released version
# its bare URL redirects to.
_DOCS_DIR = "docs/"
_DOCS_SITE = "https://nab.readthedocs.io/en/stable/"


def orphan_rejections(
    rejected: Iterable[RejectedLayer],
) -> tuple[RejectedLayer, ...]:
    """Rejections that name no registry option, so attach to no key.

    An unknown standalone ``nab.toml`` key or an unknown ``NAB_*`` var is
    recorded with ``key`` set to the offending name, which matches no
    registry key, so :func:`resolve_config` attaches it to no
    :class:`EffectiveValue` and no ``explain <key>`` reaches it.  These
    orphans are surfaced by :func:`render_list` instead.
    """
    return tuple(rej for rej in rejected if rej.key not in BY_KEY)


def render_list(
    effective: Mapping[str, EffectiveValue],
    *,
    rejected: Iterable[RejectedLayer] = (),
) -> str:
    """Render every effective option: value, scope, origin.

    ``rejected`` (when collecting for ``--include-rejected``) adds a
    trailing section listing every rejected source: a key set outside its
    scope, and an unknown key or ``NAB_*`` var.  ``explain`` reaches the
    former (it attaches to the named option) but not the latter (it names
    no option), so ``list`` is the one place that shows both together.
    """
    header = (
        f"{'key':<{_LIST_KEY_W}} {'value':<{_LIST_VALUE_W}}"
        f" {'scope':<{_LIST_SCOPE_W}} origin"
    )
    lines = [header]
    for ev in _ordered(effective):
        rendered = ev.spec.render(ev.value)
        lines.append(
            f"{ev.spec.name:<{_LIST_KEY_W}} {rendered:<{_LIST_VALUE_W}}"
            f" {ev.origin.scope:<{_LIST_SCOPE_W}} {ev.origin.label}"
        )
    rejected = tuple(rejected)
    if rejected:
        lines.append("")
        lines.append("rejected:")
        lines.extend(
            f"  {rej.origin.scope:<{_LIST_SCOPE_W}} {rej.origin.label}"
            f"  {rej.key}: {rej.reason}"
            for rej in rejected
        )
    return "\n".join(lines) + "\n"


def render_get(effective: Mapping[str, EffectiveValue], key: str) -> str:
    """Render only the effective value of ``key``."""
    ev = _require_key(effective, key)
    return ev.spec.render(ev.value) + "\n"


def render_explain(
    effective: Mapping[str, EffectiveValue],
    key: str,
    *,
    include_rejected: bool = False,
) -> str:
    """Render the row's help, its documentation page and its shadowed stack.

    The header names the key, its scope and its type, and ends with the
    effective value; under it come the row's own help line and the page
    that documents it.  The highest source is the ``winner`` and carries a
    ``>`` gutter; every source it beats is ``shadowed``.  A
    ``--project-<table>-<key>`` flag replaces one key of the table below
    it, so that source is ``merged`` rather than shadowed: it supplied the
    rest of the value.  With ``include_rejected`` the category-rejected
    sources (a source that tried to set ``key`` but was not allowed) are
    listed too, labelled ``rejected``.
    """
    ev = _require_key(effective, key)
    header = f"{key} ({ev.spec.scope_name}, {_type_label(ev.spec)})"
    lines = [
        f"{header} = {ev.spec.render(ev.value)}",
        f"  {ev.spec.help}",
        f"  see {docs_url(ev.spec)}",
    ]
    winner_index = len(ev.stack) - 1
    for i, (origin, value) in enumerate(ev.stack):
        winner = i == winner_index
        gutter = ">" if winner else " "
        status = _explain_status(ev, i, winner_index)
        rendered = (
            render_cli_table(ev.cli_table)
            if winner and ev.cli_table is not None
            else ev.spec.render(value)
        )
        lines.append(
            f"{gutter} {origin.scope:<{_LIST_SCOPE_W}} {rendered:<{_LIST_VALUE_W}}"
            f" {status:<{_EXPLAIN_STATUS_W}} {origin.label}"
        )
    if include_rejected:
        lines.extend(
            f"  {rej.origin.scope:<{_LIST_SCOPE_W}} {'-':<{_LIST_VALUE_W}}"
            f" {'rejected':<{_EXPLAIN_STATUS_W}} {rej.origin.label} ({rej.reason})"
            for rej in ev.rejected
        )
    return "\n".join(lines) + "\n"


def _explain_status(ev: EffectiveValue, index: int, winner_index: int) -> str:
    """Name one rung's standing: it won, the fold read it, or it lost.

    Only the winner's immediate predecessor can be ``merged``: that is the
    binding the command line's keys were laid over.  A second source at the
    same rank lost the tie, so it contributed nothing.
    """
    if index == winner_index:
        return "winner"
    if ev.cli_table is not None and index == winner_index - 1:
        return "merged"
    return "shadowed"


def docs_path(row: Opt) -> str:
    """Return the page ``row`` names, as a path from the repository root.

    ``tasks/gen_cli.py`` resolves this against the checkout and
    :func:`docs_url` publishes it, so a row naming a page nobody wrote
    fails the generator.
    """
    return f"{_DOCS_DIR}{row.docs}"


def docs_url(row: Opt) -> str:
    """Return the published page ``row`` names, as a URL to open.

    Built from :func:`docs_path`, so the page the generator checks on
    disk is the page a reader is sent to.  The path alone names nothing
    an installed nab has: the sdist ships ``src/nab`` and ``tests``.
    Sphinx builds with ``-b html``, so each page is served under its own
    name.
    """
    page = docs_path(row).removeprefix(_DOCS_DIR).removesuffix(".md")
    return f"{_DOCS_SITE}{page}.html"


def _type_label(row: Opt) -> str:
    """Name ``row``'s type for the explain header, marking a deprecated key."""
    return row.type_label + (" [deprecated]" if row.deprecated else "")


def _require_key(effective: Mapping[str, EffectiveValue], key: str) -> EffectiveValue:
    ev = effective.get(key)
    if ev is None:
        valid = sorted(BY_KEY)
        msg = f"unknown config key {key!r}; known keys are {valid!r}"
        raise SourceConfigError(msg)
    return ev


def project_cli_override_records(
    effective: Mapping[str, EffectiveValue],
) -> tuple[tuple[str, str], ...]:
    """Return the ``(flag, value)`` pairs for PROJECT options set on the CLI.

    A PROJECT option changes the resolved set, so a CLI override means the
    result no longer derives from the committed files alone.  These pairs
    drive both the reproducibility notice and the auditable record written
    into the lockfile provenance.  A table key is recorded flag by flag,
    each with the tokens that flag was given; a row with no flag at all is
    set from a file alone and cannot appear.
    """
    records: list[tuple[str, str]] = []
    for spec in OPTIONS:
        if spec.scope is not Scope.PROJECT:
            continue
        ev = effective[spec.name]
        if ev.cli_table is not None:
            records.extend((key.flag, key.written) for key in ev.cli_table.keys)
            continue
        if ev.origin.kind is not SourceKind.CLI or spec.cli_flag is None:
            continue
        records.append((spec.cli_flag, spec.render(ev.value)))
    return tuple(records)


def project_cli_override_notice(
    effective: Mapping[str, EffectiveValue],
    *,
    produces_lock: bool = True,
) -> str | None:
    """Reproducibility notice for any PROJECT option set on the CLI.

    Returns a notice listing every PROJECT override that came from the CLI
    rung; ``None`` when no PROJECT option was set on the CLI.

    ``produces_lock`` tailors the wording: ``nab lock`` produces a lock, so
    the notice warns the lock will not derive from the committed files; the
    read-only ``nab config`` inspector produces no lock, so it warns only
    that the displayed values reflect a CLI override.
    """
    records = project_cli_override_records(effective)
    if not records:
        return None
    if produces_lock:
        header = (
            "notice: project-scope overrides were applied from the CLI; the lock"
            " they produce does not derive from the committed pyproject/nab.toml"
            " alone:"
        )
    else:
        header = (
            "notice: project-scope overrides were applied from the CLI; the"
            " values below reflect that override, not the committed"
            " pyproject/nab.toml alone:"
        )
    lines = [header]
    lines.extend(f"  {flag} -> {rendered}" for flag, rendered in records)
    return "\n".join(lines) + "\n"
