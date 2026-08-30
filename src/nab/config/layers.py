"""Find the sources that configure a run, gate them, and merge them.

A run reads six rungs, low precedence to high: the built-in defaults, a system
``nab.toml``, a user ``nab.toml``, ``pyproject.toml``'s ``[tool.nab]``, a
project-dir ``nab.toml``, ``NAB_*`` and finally the CLI.  Each is read into a
:class:`~nab.config.registry.Layer` of typed values, and
:func:`resolve_config` gives every registry row the whole value the highest
source that bound it supplied.

The category gate runs while a source is read: a project-scope option in a
user ``nab.toml``, or a user-scope option in ``[tool.nab]``, is a
:class:`~nab.config.hooks.SourceConfigError` rather than a value.  ``nab
config --include-rejected`` collects those refusals instead of raising, which
is what the ``rejections`` parameter threaded through this module is for.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli

from nab_project import toml_io
from nab_project.paths import PathState, path_state, realpath

from .. import env
from .hooks import SourceConfigError, declaring_dir
from .registry import (
    BY_KEY,
    OPTIONS,
    PRECEDENCE,
    EffectiveValue,
    Layer,
    Opt,
    Origin,
    RejectedLayer,
    Scope,
    SourceKind,
    SourceRoots,
    scope_label,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "build_cli_layer",
    "config_search_roots",
    "discover_layers",
    "read_env_layer",
    "reject_user_keys_in_pyproject",
    "resolve_config",
]

_logger = logging.getLogger(__name__)


def tool_nab_section(data: dict[str, Any]) -> Any:
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

    The message carries no ``[tool.nab]:`` prefix: the only caller is the
    pyproject parser, whose ``error: in [tool.nab]:`` wrapper already
    supplies it, so prefixing here would double it.
    """
    for key in raw:
        spec = BY_KEY.get(key)
        if spec is None:
            continue
        if not spec.allowed_in_toml(SourceKind.PYPROJECT):
            reason = _gate_reason(spec, SourceKind.PYPROJECT)
            msg = f"{key}: {reason}"
            raise SourceConfigError(msg)


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
    # Carry the declaring file's directory structurally so a relative
    # local-source path resolves against it.
    with declaring_dir(path.parent):
        for key, value in raw.items():
            spec = BY_KEY.get(key)
            if spec is None:
                # An unknown key (a typo) crashes naming the file rather than
                # being dropped, the same way an unknown NAB_* var does.  On
                # the resolve path config.read_pyproject_config rejects an
                # unknown pyproject [tool.nab] key before this loader runs;
                # the inspector reaches here, so it reports the typo too
                # instead of silently ignoring it.
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
                msg = f"{where}: {reason}"
                raise SourceConfigError(msg)
            values[key] = spec.parse(value, where)
    return Layer(origin, values)


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
    environ: Mapping[str, str],
    *,
    reserved_env: Iterable[str] = (),
    rejections: list[RejectedLayer] | None = None,
) -> Layer:
    """Read ``NAB_*`` for every registry row that declares an env var.

    PROJECT options never declare an env var, so the env layer carries
    USER options only.  A ``NAB_<KEY>`` naming a PROJECT option (e.g.
    ``NAB_RESOLUTION``) draws a warning from :func:`_warn_renamed_env`;
    any other unknown ``NAB_*`` name (e.g. a typo) one from
    :func:`_warn_unknown_env`.  Neither is applied, and neither is fatal.
    ``reserved_env`` names the ``NAB_*`` vars other layers own (nab's
    output layer consumes ``NAB_VERBOSITY`` and ``NAB_NO_PROGRESS``), so
    the guard skips them silently.  When ``rejections`` is supplied
    (``nab config --include-rejected``) those env casualties are recorded
    there instead of warned, mirroring the TOML loader.
    """
    _warn_renamed_env(environ, rejections=rejections)
    _warn_unknown_env(environ, reserved_env=reserved_env, rejections=rejections)
    values: dict[str, Any] = {}
    for spec in OPTIONS:
        if spec.env_var is None:
            continue
        if spec.env_var not in environ:
            continue
        where = f"env:{spec.env_var}"
        values[spec.name] = spec.parse(environ[spec.env_var], where)
    return Layer(Origin(SourceKind.ENV, "env"), values)


def _warn_unknown_env(
    environ: Mapping[str, str],
    *,
    reserved_env: Iterable[str] = (),
    rejections: list[RejectedLayer] | None = None,
) -> None:
    """Warn about any ``NAB_*`` var that is neither a known nor renamed name.

    The env half of the category gate: a typo'd or made-up ``NAB_<KEY>``
    (e.g. ``NAB_OFLINE``) is ignored with a warning naming the variable,
    never applied and never fatal.  Renamed PROJECT names are left for
    :func:`_warn_renamed_env`, which gives the more specific
    not-env-settable message.  ``reserved_env`` names ``NAB_*`` vars owned
    by other layers (the output layer's ``NAB_VERBOSITY`` and
    ``NAB_NO_PROGRESS``); those are skipped silently.  When ``rejections``
    is supplied the var is recorded there instead of warned.
    """
    known = {spec.env_var for spec in OPTIONS if spec.env_var is not None}
    renamed = set(_renamed_env_names())
    reserved = set(reserved_env)
    for name in environ:
        if (
            not name.startswith("NAB_")
            or name in known
            or name in renamed
            or name in reserved
        ):
            continue
        valid = sorted(known)
        msg = (
            f"{name} is not a recognized nab setting and was ignored; the"
            f" known NAB_* variables are {valid!r}."
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
    replaces the one below rather than adding to it.
    """
    all_layers = [*layers, env_layer, cli_layer]
    out: dict[str, EffectiveValue] = {}
    for spec in OPTIONS:
        stack = _stack_for(spec, all_layers)
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
        )
    return out


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
    # Sort by precedence; break the pyproject/project-dir rank-3 tie so
    # the project-dir nab.toml (False < True) sorts last and wins.
    found.sort(
        key=lambda item: (
            PRECEDENCE[item[0].kind],
            item[0].kind is SourceKind.PROJECT_TOML,
        )
    )
    _check_project_file_conflict(spec, found)
    return found


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
    """
    parsed: dict[str, Any] = {}
    for key, value in values.items():
        spec = BY_KEY[key]
        # A repeatable flag arrives as a tuple; the parse hooks expect a
        # TOML list, so normalise it here.
        raw = list(value) if isinstance(value, tuple) else value
        parsed[key] = spec.parse(raw, f"cli:{spec.cli_flag}")
    return Layer(Origin(SourceKind.CLI, "cli"), parsed)
