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

from . import env
from .config.hooks import SourceConfigError, inspector_anchor
from .config.inspect import (
    project_cli_override_notice,
    project_cli_override_records,
)
from .config.layers import (
    build_cli_layer,
    config_search_roots,
    discover_layers,
    read_env_layer,
    resolve_config,
)
from .config.registry import (
    OPTIONS,
    EffectiveValue,
    RejectedLayer,
    Scope,
    SourceKind,
    build_cli_overrides,
)
from .output import OUTPUT_ENV_VARS, printer

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_provider.provider import ResolutionStrategy


def require_pyproject_file(path: Path) -> None:
    """Exit 1 if ``path`` is not a readable pyproject file.

    Shared by every command that takes a project path, so the rejection
    wording lives in one place.  A ``--path`` that is missing, a
    directory, or not a regular file is a hard error, not a
    silently-skipped source.  A path whose stat fails passes: the config
    read reports it, naming the errno.
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
    """Return the default per-user cache root.

    Mirrors ``platformdirs.user_cache_path("nab")`` without the
    dependency: ``$XDG_CACHE_HOME/nab`` or ``~/.cache/nab``.
    """
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
    """Resolve the full layered config for the pyproject at ``path``.

    Discovers the TOML layers over :func:`config_search_roots`, reads the
    ``NAB_*`` layer, builds the CLI layer from the keys ``cli_overrides``
    names, and merges the four through the registry.

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
        env_layer = read_env_layer(
            env.current(), reserved_env=OUTPUT_ENV_VARS, rejections=sink
        )
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
    # The (flag, rendered value) pairs for any --project-* override set on
    # the CLI, recorded into the lockfile provenance so the lock is auditable.
    cli_project_overrides: tuple[tuple[str, str], ...]


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
) -> dict[str, object]:
    """Build the registry-keyed CLI override dict from the named flags.

    The one place the ``cli_param`` -> value mapping is written: the run
    subcommands and ``nab config`` route their flag values through here so
    the literal lives once.  ``build_cli_overrides`` then keeps only the
    keys the user actually set.  USER options and the ``--project-*``
    overrides for the scalar and array PROJECT options pass through; the
    structured PROJECT tables stay file-only.
    """
    return build_cli_overrides(
        {
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
        }
    )


def project_config_overrides(
    cli_overrides: Mapping[str, object],
) -> dict[str, object]:
    """Return the PROJECT-scope CLI overrides that belong in the config.

    ``resolution`` is excluded: it keeps its own ``resolution_strategy``
    path into the resolver, so it must not also enter the merged config.
    USER options are excluded too (they configure the run, not the project).
    The rest are the ``--project-*`` overrides the resolve folds in through
    :func:`config.read_pyproject_config`.
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
    merged config.  A repeatable flag is emitted once per element.
    """
    arguments: list[str] = []
    for spec in OPTIONS:
        flag = spec.cli_flag
        if spec.scope is not Scope.PROJECT or flag is None:
            continue
        value = cli_overrides.get(spec.name)
        if value is None:
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
    """Fold the ladder's run knobs, exiting on a category error it holds.

    The single ``SourceConfigError`` -> ``error: config error: ...`` ->
    ``exit(1)`` mapping shared by ``nab lock`` and ``nab download`` lives
    here.  On success it also emits the reproducibility notice when a
    PROJECT option was set on the CLI, so a result-shaping override is
    never silent.  ``produces_lock`` picks the wording: ``nab lock`` warns
    about the lock it produces while ``nab download`` (which writes no
    lock) warns only that the resolved set reflects the override.
    """
    if isinstance(ladder, SourceConfigError):
        _fail_config(ladder)
    settings = _layered_run_settings(ladder)
    notice = project_cli_override_notice(ladder, produces_lock=produces_lock)
    if notice is not None:
        sys.stderr.write(notice)
    return settings


def _fail_config(exc: SourceConfigError) -> NoReturn:
    """Map a layered config error to the shared ``error: config error:`` exit."""
    printer().error(f"config error: {exc}")
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
    """Exit 1 when a ``--project-*`` override has a bad value, naming the flag.

    Without this the value is validated by the ``[tool.nab]`` parse in
    :func:`nab._resolve._load_config`, whose errors read ``in [tool.nab]:``
    and point at a table the project may not have.
    """
    for spec in OPTIONS:
        if spec.name not in project_overrides:
            continue
        try:
            build_cli_layer({spec.name: project_overrides[spec.name]})
        except SourceConfigError as exc:
            printer().error(f"{spec.cli_flag}: {exc}")
            sys.exit(1)
