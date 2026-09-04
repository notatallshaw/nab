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

from nab_project import toml_io
from nab_project.paths import PathState, path_state
from nab_provider._vendor.packaging.version import Version
from nab_provider.policy import ResolveMode

from . import env
from .config.hooks import inspector_anchor
from .config.ladder import (
    OPTIONS,
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
    resolve_config,
)
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
) -> dict[str, EffectiveValue]:
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
        cli_layer = build_cli_layer(cli_overrides or {})
        if rejected_out is not None:
            rejected_out.extend(rejected)
        return resolve_config(layers, env_layer, cli_layer, rejected=rejected)


ConfigLadder = dict[str, EffectiveValue] | SourceConfigError
"""One read of the layered config: the effective map, or the error it raised."""


def read_config_ladder(path: Path, cli_overrides: Mapping[str, object]) -> ConfigLadder:
    """Read the layered config once for a run and hold what came back.

    A command builds one of these and threads it, so the environment is
    read, and an unknown ``NAB_*`` var warned about, once per
    invocation.  A category error is held rather than raised because the
    lock anchor tolerates one and the run-settings fold exits on it.
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
    # Explicit --project-* overrides as rendered flag/value pairs.
    cli_project_overrides: tuple[tuple[str, str], ...]


_BOTH_PYTHON_AXES = (
    "--python and --project-environment-python both set the python axis;"
    " pass one of them."
)


def _cli_overrides(  # noqa: PLR0913 - one keyword per CLI flag it maps to a registry key
    *,
    cli_resolution: str | None,
    cli_offline: bool | None,
    cli_cache_dir: Path | None,
    cli_http_backend: str | None = None,
    cli_max_concurrency: int | None = None,
    cli_mode: str | None = None,
    cli_requires_python: str | None = None,
    cli_uploaded_prior_to: str | None = None,
    cli_dist_policy: str | None = None,
    cli_build_policy: str | None = None,
    cli_build_requires_depth: int | None = None,
    cli_decision_order: str | None = None,
    cli_constraint: tuple[str, ...] = (),
    cli_default_group: tuple[str, ...] = (),
    cli_base_group: str | None = None,
    cli_build_group: str | None = None,
    cli_matrix_python: str | None = None,
    cli_matrix_platforms: tuple[str, ...] = (),
    cli_matrix_implementations: tuple[str, ...] = (),
    cli_matrix_python_order: str | None = None,
    cli_matrix_python_patches: tuple[str, ...] = (),
    cli_python: str | None = None,
    cli_environment_python: str | None = None,
    cli_environment_platform: tuple[str, ...] = (),
    cli_environment_implementation: str | None = None,
) -> dict[str, object]:
    """Build the registry-keyed CLI override dict from the named flags.

    The one place the ``cli_param`` -> value mapping is written: the run
    subcommands and ``nab config`` route their flag values through here so
    the literal lives once.  ``build_cli_overrides`` then keeps only the
    keys the user actually set.  USER options and the ``--project-*``
    overrides for the scalar and array PROJECT options pass through; a
    structured table has no flag of its own, so the ``--project-matrix-*``
    and ``--project-environment-*`` flags spell one key each of ``matrix``
    and ``environment``.  ``--python`` is the short form of
    ``--project-environment-python``, recorded under its own name; writing
    both is refused.  A refusal assembling a table exits 1 rather than
    raising.
    """
    values: dict[str, object] = {
        "project_resolution": cli_resolution,
        "offline": cli_offline,
        "cache_dir": cli_cache_dir,
        "http_backend": cli_http_backend,
        "max_concurrency": cli_max_concurrency,
        "project_mode": cli_mode,
        "project_requires_python": cli_requires_python,
        "project_uploaded_prior_to": cli_uploaded_prior_to,
        "project_dist_policy": cli_dist_policy,
        "project_build_policy": cli_build_policy,
        "project_build_requires_depth": cli_build_requires_depth,
        "project_decision_order": cli_decision_order,
        "project_constraint": cli_constraint,
        "project_default_group": cli_default_group,
        "project_base_group": cli_base_group,
        "project_build_group": cli_build_group,
        "project_matrix_python": cli_matrix_python,
        "project_matrix_platforms": cli_matrix_platforms,
        "project_matrix_implementations": cli_matrix_implementations,
        "project_matrix_python_order": cli_matrix_python_order,
        "project_matrix_python_patches": cli_matrix_python_patches,
        "project_environment_python": cli_environment_python,
        "project_environment_platform": cli_environment_platform,
        "project_environment_implementation": cli_environment_implementation,
    }

    flags: dict[str, str] = {}
    if cli_python is not None:
        if cli_environment_python is not None:
            _fail_cli(SourceConfigError(_BOTH_PYTHON_AXES))
        _check_python_flag(cli_python)
        values["project_environment_python"] = cli_python
        flags["project_environment_python"] = "--python"

    try:
        return build_cli_overrides(values, flags)
    except SourceConfigError as exc:
        _fail_cli(exc)


def _check_python_flag(python: str) -> None:
    """Read ``--python`` as a version here, so a bad value names the flag."""
    try:
        Version(python)
    except ValueError:
        msg = f"--python must be a version like '3.12' or '3.12.4', got {python!r}"
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


def project_config_overrides(
    cli_overrides: Mapping[str, object],
) -> dict[str, object]:
    """Return the PROJECT-scope CLI overrides that belong in the config.

    ``resolution`` is excluded: it keeps its own ``resolution_strategy``
    path into the resolver, so it must not also enter the merged config.
    USER options are excluded too (they configure the run, not the project).
    The rest are the ``--project-*`` overrides the resolve folds in through
    :func:`read_pyproject_config`.
    """
    project_keys = {
        spec.name
        for spec in OPTIONS
        if spec.scope is Scope.PROJECT and spec.name != "resolution"
    }
    return {key: value for key, value in cli_overrides.items() if key in project_keys}


def project_override_arguments(cli_overrides: Mapping[str, object]) -> list[str]:
    """Return the CLI tokens that re-apply this run's ``--project-*`` overrides.

    Takes the raw map :func:`_cli_overrides` built, not the config subset, so
    ``resolution`` is carried too: it shapes the resolve without entering the
    merged config.  A repeatable flag is emitted once per element, and a
    table key is re-spelled flag by flag with the tokens the user typed.
    """
    arguments: list[str] = []
    for spec in OPTIONS:
        if spec.scope is not Scope.PROJECT:
            continue
        value = cli_overrides.get(spec.name)
        if isinstance(value, CliTable):
            for key in value.keys:
                arguments += [key.flag, *key.tokens]
            continue
        flag = spec.cli_flag
        if flag is None or value is None:
            continue
        items = value if isinstance(value, tuple) else (value,)
        for item in items:
            arguments += [flag, str(item)]
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
    )


def _layered_run_settings_or_exit(
    ladder: ConfigLadder, *, produces_lock: bool = True
) -> RunSettings:
    """Fold the run settings or exit on a config error.

    Emit a normal-level notice for CLI PROJECT overrides.
    ``produces_lock`` selects its wording.
    """
    if isinstance(ladder, SourceConfigError):
        _fail_config(ladder)
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


def _project_cli_overrides_or_exit(project_overrides: Mapping[str, object]) -> None:
    """Exit 1 when a ``--project-*`` override has a bad value.

    The refusal names the flag, since that is the ``where`` the CLI layer
    parses under.  Without this the value is validated by the ``[tool.nab]``
    parse instead, which points at a table the project may not have.
    """
    for spec in OPTIONS:
        if spec.name not in project_overrides:
            continue
        try:
            build_cli_layer({spec.name: project_overrides[spec.name]})
        except SourceConfigError as exc:
            _fail_cli(exc)
