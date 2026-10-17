"""The settings a nab command reads before it runs.

The project-path guard, the layered config read and the run knobs
folded out of it, the cache-directory default, and the map from a
command's flags to the registry keys they set.

:mod:`nab._resolve` holds the half only ``nab lock`` and ``nab
download`` need, so inspecting the cache or the config loads no
resolver.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import tomli

from nab_markersets.python_versions import validate_python_version
from nab_project import toml_io
from nab_project.paths import PathState, path_state
from nab_provider.policy import ResolveMode

from . import env
from ._cli.definition.model import Kind
from ._cli.parse import option_tokens
from .config.hooks import inspector_anchor
from .config.ladder import (
    OPTIONS,
    ConfigSnapshot,
    EffectiveValue,
    RejectedLayer,
    Scope,
    SourceKind,
    build_cli_layer,
    build_cli_overrides,
    config_search_roots,
    discover_layers,
    project_cli_override_notice,
    project_cli_override_records,
    read_env_layer,
)
from .config.registry import SUB_ROWS
from .config.subflags import CliTable
from .config.values import CliTableError, SourceConfigError
from .output import printer

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_provider.provider import ResolutionStrategy


def require_pyproject_file(path: Path) -> None:
    """Exit 1 for a missing, non-file, or pylock project path.

    Stat and read failures pass through so the config reader reports the
    errno.
    """
    state = path_state(path)

    if state is PathState.DIRECTORY:
        printer().error(f"{path} is a directory")
        sys.exit(1)

    if state is PathState.OTHER:
        printer().error(f"{path} exists but is not a regular file")
        sys.exit(1)

    if state is PathState.ABSENT:
        printer().error(f"{path} not found")
        sys.exit(1)

    if _is_pylock(path):
        printer().error(
            f"{path} is a PEP 751 lockfile, not a pyproject.  nab resolves"
            " from project inputs, so pass the pyproject.toml instead."
        )
        sys.exit(1)


def _default_cache_dir() -> Path:
    """Return ``$XDG_CACHE_HOME/nab`` or ``~/.cache/nab``."""
    base = env.cache_root()
    if base:
        return Path(base) / "nab"
    return Path.home() / ".cache" / "nab"


def _resolve_effective_cache_dir(cache_dir: Path | None, *, cache: bool) -> Path | None:
    if not cache:
        return None
    if cache_dir is not None:
        return cache_dir
    return _default_cache_dir()


def effective_config(
    path: Path,
    *,
    cli_overrides: Mapping[str, object] | None = None,
    collect_rejected: bool = False,
    rejected_out: list[RejectedLayer] | None = None,
    read_pyproject: bool = True,
) -> ConfigSnapshot:
    """Resolve config layers for the project at ``path``.

    The option registry parses and merges the discovered TOML,
    environment, and CLI layers.

    ``collect_rejected`` attaches each key's category-rejections to its
    ``EffectiveValue``.  ``rejected_out`` takes the whole rejection list,
    which is the only way a caller sees an orphan rejection: a key or
    ``NAB_*`` var naming no registry option attaches to none.
    ``read_pyproject=False`` skips the pyproject layer, for a caller
    reading a USER-scope key that pyproject may not set.
    """
    roots = config_search_roots(path)
    rejected: list[RejectedLayer] = []
    sink = rejected if collect_rejected else None
    # Pin one ``now`` for the pass so identical relative ``P<n>D`` override
    # durations across the two project files are not read as conflicting
    # values (the resolve path uses its lockfile anchor instead).
    with inspector_anchor():
        layers = discover_layers(roots, rejections=sink, read_pyproject=read_pyproject)
        env_layer = read_env_layer(rejections=sink)
        if rejected_out is not None:
            rejected_out.extend(rejected)
        return ConfigSnapshot(layers, env_layer, cli_overrides or {}, rejected=rejected)


ConfigLadder = ConfigSnapshot | SourceConfigError
"""One read of the layered config: the effective map, or the error it raised."""


def read_config_ladder(path: Path, cli_overrides: Mapping[str, object]) -> ConfigLadder:
    """Read the layered config once for a run and hold what came back.

    A command builds one of these and threads it, so the environment is
    read, and an unknown ``NAB_*`` var warned about, once per
    invocation.  A category error is held rather than raised because the
    lock anchor tolerates one and config loading reports it before execution.
    """
    try:
        return effective_config(path, cli_overrides=cli_overrides)
    except SourceConfigError as exc:
        return exc


def lock_anchor(ladder: ConfigLadder) -> datetime | None:
    """Return the absolute ``uploaded-prior-to`` cutoff for ``nab lock``.

    An absolute datetime is the lock anchor: it already fixes the resolve
    window, so anchoring there makes ``created-at`` deterministic and two
    locks from identical inputs produce identical bytes.  A relative
    ``P<n>D`` duration anchors to run time, so it is not reproducible and
    returns ``None``; an unset value, and a ladder that failed to
    resolve, return ``None`` too.
    """
    if isinstance(ladder, SourceConfigError):
        return None
    value = ladder["uploaded-prior-to"].value
    return value if isinstance(value, datetime) else None


@dataclass(frozen=True, slots=True)
class RunSettings:
    """The run knobs a subcommand reads from the layered config for one run."""

    resolution: ResolutionStrategy | None
    offline: bool
    cache_dir: Path | None
    http_backend: str
    max_concurrency: int
    # Explicit project settings as rendered flag/value pairs.
    cli_project_overrides: tuple[tuple[str, str], ...]
    sources: bool = True
    workspace_discovery: bool = True


_BOTH_PYTHON_AXES = (
    "--python and --project-environment-python both set the python axis;"
    " pass one of them."
)


def command_overrides(arguments: Mapping[str, object]) -> dict[str, object]:
    """Collect declared CLI bindings and resolve the Python-version shorthand."""
    values = {
        row.cli_param: row.default
        for row in (*OPTIONS, *SUB_ROWS)
        if row.cli_param is not None
    }
    values.update(arguments)
    python = arguments.get("python")
    flags: dict[str, str] = {}
    if python is not None:
        assert isinstance(python, str)
        if values["project_environment_python"] is not None:
            _fail_cli(SourceConfigError(_BOTH_PYTHON_AXES))
        _check_python_flag(python)
        values["project_environment_python"] = python
        flags["project_environment_python"] = "--python"
    try:
        return build_cli_overrides(values, flags)
    except SourceConfigError as exc:
        _fail_cli(exc)


def _check_python_flag(python: str) -> None:
    """Read ``--python`` as a version here, so a bad value names the flag."""
    try:
        validate_python_version(python)
    except ValueError as exc:
        msg = f"--python must be a version like '3.12' or '3.12.4', got {python!r}"
        msg += f": {exc}"
        _fail_cli(SourceConfigError(msg))


def _reject_python_flag_in_universal(ladder: ConfigLadder, python: str | None) -> None:
    """Exit 1 when ``--python`` is written for a resolve a matrix targets.

    Read off the ladder rather than the parsed config: the flag folds into
    the environment table, and a matrix beside an environment is refused
    as the config is assembled, before a config-shaped check could run.
    """
    if python is None or isinstance(ladder, SourceConfigError):
        return
    if ladder["mode"].value is ResolveMode.UNIVERSAL:
        printer().error(
            "--python is not supported in universal mode;"
            " [tool.nab.matrix].python declares the Python axis."
        )
        sys.exit(1)


def project_override_arguments(cli_overrides: Mapping[str, object]) -> list[str]:
    """Return CLI tokens reproducing the project settings selected for this run."""
    arguments: list[str] = []
    for spec in OPTIONS:
        if spec.scope is not Scope.PROJECT:
            continue
        value = cli_overrides.get(spec.name)
        if isinstance(value, CliTable):
            for key in value.keys:
                arguments += option_tokens(key.flag, key.tokens)
            continue
        flag = spec.cli_flag
        if flag is None or value is None:
            continue
        if spec.kind is Kind.TRI:
            arguments.append(flag if value else "--no-" + flag[2:])
            continue
        items = value if isinstance(value, tuple) else (value,)
        for item in items:
            arguments += option_tokens(flag, (str(item),))
    return arguments


def _layered_run_settings(effective: Mapping[str, EffectiveValue]) -> RunSettings:
    """Fold the effective registry values into a subcommand's run knobs.

    ``resolution`` stays ``None`` (config wins downstream) when no source
    above the default set it, preserving the contract that the resolver
    falls back to ``config.resolution``.
    """
    res_ev = effective["resolution"]
    resolution = res_ev.value if res_ev.origin.kind is not SourceKind.DEFAULT else None
    return RunSettings(
        resolution=resolution,
        offline=effective["offline"].value,
        cache_dir=effective["cache-dir"].value,
        http_backend=effective["http-backend"].value,
        max_concurrency=effective["max-concurrency"].value,
        cli_project_overrides=project_cli_override_records(effective),
        sources=effective["sources"].value,
        workspace_discovery=effective["workspace-discovery"].value,
    )


def run_settings(
    ladder: Mapping[str, EffectiveValue], *, produces_lock: bool = True
) -> RunSettings:
    """Fold the validated run settings and report project overrides.

    Emit a normal-level notice for CLI PROJECT overrides.
    ``produces_lock`` selects its wording.
    """
    settings = _layered_run_settings(ladder)
    notice = project_cli_override_notice(ladder, produces_lock=produces_lock)
    if notice is not None:
        printer().stderr_line(notice)
    return settings


def _fail_config(exc: SourceConfigError) -> NoReturn:
    """Map a layered config error to the shared ``error: config error:`` exit.

    A ``CliTableError`` came from the command line, not a file, so it is
    printed as it stands.
    """
    if isinstance(exc, CliTableError):
        _fail_cli(exc)
    printer().error(f"config error: {exc}")
    sys.exit(1)


def _fail_cli(exc: SourceConfigError) -> NoReturn:
    """Exit 1 on a command-line value, with no ``config error:`` prefix."""
    printer().error(str(exc))
    sys.exit(1)


def _is_pylock(path: Path) -> bool:
    """Whether ``path`` holds a PEP 751 lock rather than a pyproject.

    ``lock-version`` is the one required key PEP 751 gives a lock and a
    pyproject never carries.  An unreadable or malformed file is left for
    the pyproject parser to report.
    """
    try:
        data = toml_io.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError):
        return False
    return "lock-version" in data and "project" not in data


def validate_project_cli_bindings(overrides: Mapping[str, object]) -> None:
    """Validate project flags before reading configuration sources."""
    for spec in OPTIONS:
        if spec.scope is not Scope.PROJECT or spec.name not in overrides:
            continue
        try:
            build_cli_layer({spec.name: overrides[spec.name]})
        except SourceConfigError as exc:
            _fail_cli(exc)
