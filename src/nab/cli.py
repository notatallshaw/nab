"""Entry point for the nab command.

Holds the tyro :class:`SubcommandApp` registration plus the helpers the
command modules share: config loading and cache-directory defaults,
plus the HTTP transport selection and resolver-error-to-exit-code
translation that only :mod:`nab._lock` and :mod:`nab._download` use.

The subcommands live in :mod:`nab._lock`, :mod:`nab._download`,
:mod:`nab._config_cmd`, and :mod:`nab._cache_cmd`; this module imports
them so their ``@app.command`` decorators run before :func:`main` calls
``app.cli()``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn

import tomli
import tyro
from tyro.extras import SubcommandApp

from nab._version import __version__
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_provider.errors import (
    IndexAccessError,
    MetadataHashMismatchError,
    SdistHashMismatchError,
    WheelHashMismatchError,
)
from nab_provider.provider import (
    InvalidUploadTimeError,
    MetadataError,
    MissingExtraError,
    SiblingMetadataDivergenceError,
    SourceNameMismatchError,
    UnsupportedVcsError,
)
from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    InvalidProjectTableError,
)
from nab_provider.target import NonIntervalMarkerError, UnevaluableMarkerError
from nab_python.config import (
    ConfigError,
    NabProjectConfig,
    ResolveMode,
    read_pyproject_config,
    with_python_override,
)
from nab_python.config_sources import (
    OPTIONS,
    EffectiveValue,
    RejectedLayer,
    Scope,
    SourceConfigError,
    SourceKind,
    SourceRoots,
    build_cli_layer,
    build_cli_overrides,
    discover_layers,
    inspector_anchor,
    project_cli_override_notice,
    project_cli_override_records,
    read_env_layer,
    resolve_config,
)
from nab_python.lockfile import MissingHashError, MissingSdistError
from nab_python.paths import PathState, path_state
from nab_python.resolve import resolve_for_targets
from nab_python.workspace import WorkspaceDiscoveryError
from nab_resolver.errors import ResolutionError

from .output import (
    OUTPUT_ENV_VARS,
    OutputOptionError,
    Printer,
    ProgressReporter,
    install_log_handler,
    parse_output_options,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_index.transport import AsyncHttpTransport
    from nab_provider.provider import ResolutionStrategy
    from nab_python.resolve import ResolveResult

__all__ = [
    "main",
    "printer",
]


# A pyproject.toml positional that may also be omitted to default to ./pyproject.toml.
PathArg = Annotated[Path, tyro.conf.Positional]

# Lowercase Literal types so --http-backend and --format render lowercase
# choices in --help rather than the uppercase enum names.
HttpBackend = Literal["urllib3", "httpx"]
LockFormat = Literal["pylock", "requirements", "requirements-without-hashes"]
ResolutionFlag = Literal["highest", "lowest", "lowest-direct"]
ModeFlag = Literal["specific", "universal"]
DistPolicyFlag = Literal[
    "wheel-only", "prefer-wheel", "wheel-or-sdist", "sdist-only", "sdist-install"
]
BuildPolicyFlag = Literal["never", "build-local", "build-remote"]
DecisionOrderFlag = Literal["arrival", "stable"]

# --offline is layered (an nab.toml or NAB_OFFLINE may set it), so it stays
# tri-state: an explicit value overrides the lower layers while an absent flag
# defers to them.  tyro renders that as a value-taking choice; main() also
# accepts the bare --offline / --no-offline forms (_normalize_layered_bool_flags).
OfflineFlag = Annotated[
    bool | None,
    tyro.conf.arg(
        metavar="{True,False}",
        help="never hit the network; bare --offline / --no-offline also work",
    ),
]

_DEFAULT_OUTPUT: dict[str, str] = {
    "pylock": "pylock.toml",
    "requirements": "requirements.txt",
    "requirements-without-hashes": "requirements.txt",
}

TUPLE_TEMPLATE_VARS = ("{python_version}", "{platform_id}", "{selection}")

# Conventional KeyboardInterrupt exit code: 128 + SIGINT(2).
_SIGINT_EXIT_CODE = 130

app = SubcommandApp()

_printer: Printer | None = None


def printer() -> Printer:
    """Return the run's :class:`~nab.output.Printer`.

    :func:`main` installs the printer resolved from the global output flags.
    A subcommand called directly (bypassing ``main``, as many tests do) gets a
    fresh default printer that reads the current process streams.
    """
    return _printer if _printer is not None else Printer()


def _make_transport(backend: HttpBackend) -> AsyncHttpTransport:
    # httpx is an optional extra; import lazily so a urllib3-only
    # install doesn't need it.
    if backend == "httpx":
        try:
            from nab_index.httpx_async_transport import (  # noqa: PLC0415
                HttpxAsyncTransport,
            )
        except ImportError:
            printer().error("httpx is not installed; run `pip install nab[httpx]`")
            sys.exit(1)

        # httpx raises ImportError from its client constructor when h2 is missing.
        try:
            return HttpxAsyncTransport()
        except ImportError:
            printer().error(
                "httpx is installed without HTTP/2 support; "
                "run `pip install nab[httpx]`"
            )
            sys.exit(1)

    return Urllib3AsyncTransport()


def _default_cache_dir() -> Path:
    """Return the default per-user cache root.

    Mirrors ``platformdirs.user_cache_path("nab")`` without the
    dependency: ``$XDG_CACHE_HOME/nab`` or ``~/.cache/nab``.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "nab"
    return Path.home() / ".cache" / "nab"


def _resolve_effective_cache_dir(cache_dir: Path | None, *, cache: bool) -> Path | None:
    if not cache:
        return None
    if cache_dir is not None:
        return cache_dir
    return _default_cache_dir()


def _config_search_roots(pyproject: Path) -> SourceRoots:
    """Locate the system/user/project config roots for ``pyproject``.

    Uses the same XDG roots as cache-dir: the user ``nab.toml`` lives at
    ``$XDG_CONFIG_HOME/nab/nab.toml`` or ``~/.config/nab/nab.toml``; the
    system file at ``/etc/nab/nab.toml``.  Tests inject roots by
    monkeypatching this function, so the real ``~/.config`` is never
    touched in the suite.  Discovery is project-dir only.  There is no
    walk-up.

    ``pyproject`` is the project file the user pointed at, threaded
    through so the registry's pyproject layer reads that exact file even
    when its name is not ``pyproject.toml``; the project-dir ``nab.toml``
    is looked up beside it.  The pyproject root keeps the file's own
    directory (resolved) rather than resolving the file itself, so a
    relative ``local-sources`` path resolves against the symlink's
    directory, matching the resolve path; ``open`` still follows the
    symlink to read it.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    user_dir = Path(base) if base else Path.home() / ".config"
    project_dir = pyproject.parent.resolve()
    return SourceRoots(
        system_toml=Path("/etc/nab/nab.toml"),
        user_toml=user_dir / "nab" / "nab.toml",
        project_dir=project_dir,
        pyproject=project_dir / pyproject.name,
    )


def effective_config(
    path: Path,
    *,
    cli_overrides: Mapping[str, object] | None = None,
    collect_rejected: bool = False,
    rejected_out: list[RejectedLayer] | None = None,
    read_pyproject: bool = True,
) -> dict[str, EffectiveValue]:
    """Resolve the full layered config for the pyproject at ``path``.

    Discovers the system/user/project TOML layers (roots from
    :func:`_config_search_roots`), reads the ``NAB_*`` env layer, builds
    the CLI layer from ``cli_overrides`` (only keys the user set), and
    merges them through the registry.  Returns the effective map; when
    ``collect_rejected`` is set the category-rejections are attached per
    key (``EffectiveValue.rejected``) for ``explain --include-rejected``.
    ``rejected_out``, when supplied, is filled with the full rejection
    list so the caller can also surface the orphan rejections (an unknown
    key or ``NAB_*`` var that names no registry option, and so attaches to
    no key) that ``nab config list`` reports.  ``read_pyproject=False``
    skips the pyproject layer, for a caller reading a USER-scope key that
    pyproject may not set.
    """
    roots = _config_search_roots(path)
    rejected: list[RejectedLayer] = []
    sink = rejected if collect_rejected else None
    # Pin one ``now`` for the pass so identical relative ``P<n>D`` override
    # durations across the two project files are not read as conflicting
    # values (the resolve path uses its lockfile anchor instead).
    with inspector_anchor():
        layers = discover_layers(roots, rejections=sink, read_pyproject=read_pyproject)
        env_layer = read_env_layer(
            os.environ, reserved_env=OUTPUT_ENV_VARS, rejections=sink
        )
        cli_layer = build_cli_layer(cli_overrides or {})
        if rejected_out is not None:
            rejected_out.extend(rejected)
        return resolve_config(layers, env_layer, cli_layer, rejected=rejected)


def lock_anchor(
    path: Path, cli_overrides: Mapping[str, object] | None = None
) -> datetime | None:
    """Return the absolute ``uploaded-prior-to`` cutoff for ``nab lock``.

    Sources the cutoff through the same registry ladder the resolve uses
    (so a value set in the project-dir ``nab.toml`` is honoured exactly
    like one in pyproject ``[tool.nab]``).  ``cli_overrides`` lets a
    ``--project-uploaded-prior-to`` flag set the cutoff too, so the lock
    anchor matches the resolve window the override produces.  An absolute
    datetime is the lock anchor: the resolve window is already fixed, so
    anchoring there makes ``created-at`` deterministic and two locks from
    identical inputs produce identical bytes.  A relative ``P<n>D``
    duration anchors to run time, so it is not reproducible and returns
    ``None``; an unset value returns ``None`` too.  Config errors are
    swallowed here (the full resolve parse reports them) so this
    best-effort read never crashes.
    """
    try:
        effective = effective_config(path, cli_overrides=cli_overrides)
    except SourceConfigError:
        return None
    value = effective["uploaded-prior-to"].value
    return value if isinstance(value, datetime) else None


@dataclass(frozen=True, slots=True)
class RunSettings:
    """The run knobs a subcommand reads from the layered config for one run."""

    resolution: ResolutionStrategy | None
    offline: bool
    cache_dir: Path | None
    http_backend: HttpBackend
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
        spec.key
        for spec in OPTIONS
        if spec.scope is Scope.PROJECT and spec.key != "resolution"
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
        value = cli_overrides.get(spec.key)
        if value is None:
            continue
        items = value if isinstance(value, tuple) else (value,)
        for item in items:
            arguments += [flag, str(item)]
    return arguments


def _layered_run_settings(
    path: Path, cli_overrides: Mapping[str, object]
) -> tuple[RunSettings, Mapping[str, EffectiveValue]]:
    """Fold the layered registry values into a subcommand's run knobs.

    Returns the :class:`RunSettings` reflecting the full ladder, plus the
    effective map so the caller can emit the reproducibility notice for a
    CLI PROJECT override.  ``resolution`` stays ``None`` (config wins
    downstream) when no source above the default set it, preserving the
    contract that the resolver falls back to ``config.resolution``.
    """
    effective = effective_config(path, cli_overrides=cli_overrides)
    res_ev = effective["resolution"]
    resolution = res_ev.value if res_ev.origin.kind is not SourceKind.DEFAULT else None
    settings = RunSettings(
        resolution=resolution,
        offline=effective["offline"].value,
        cache_dir=effective["cache-dir"].value,
        http_backend=effective["http-backend"].value,
        max_concurrency=effective["max-concurrency"].value,
        cli_project_overrides=project_cli_override_records(effective),
    )
    return settings, effective


def _layered_run_settings_or_exit(
    path: Path,
    cli_overrides: Mapping[str, object],
    *,
    produces_lock: bool = True,
) -> RunSettings:
    """Fold the layered run settings, exiting on a category error.

    Wraps :func:`_layered_run_settings` with the single
    ``SourceConfigError`` -> ``error: config error: ...`` -> ``exit(1)`` mapping
    shared by ``nab lock`` and ``nab download``.  On success it also emits
    the reproducibility notice when a PROJECT option was set on the CLI,
    so a result-shaping override is never silent.  ``produces_lock`` picks
    the wording: ``nab lock`` warns about the lock it produces while ``nab
    download`` (which writes no lock) warns only that the resolved set
    reflects the override.
    """
    try:
        settings, effective = _layered_run_settings(path, cli_overrides)
    except SourceConfigError as exc:
        _fail_config(exc)
    notice = project_cli_override_notice(effective, produces_lock=produces_lock)
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
        data = tomli.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError):
        return False
    return "lock-version" in data and "project" not in data


def require_pyproject_file(path: Path) -> None:
    """Exit 1 if ``path`` is not a readable pyproject file.

    Shared by every command that takes a project path, so the
    not-found/directory wording lives in one place.  A missing or
    directory ``--path`` is a hard error, not a silently-skipped source.
    A path whose stat fails passes: the config read reports it, naming
    the errno.
    """
    state = path_state(path)

    if state is PathState.DIRECTORY:
        printer().error(f"{path} is a directory")
        sys.exit(1)

    if not state.should_read:
        printer().error(f"{path} not found")
        sys.exit(1)

    if _is_pylock(path):
        printer().error(
            f"{path} is a PEP 751 lockfile, not a pyproject.  nab resolves"
            " from project inputs, so pass the pyproject.toml instead."
        )
        sys.exit(1)


def _project_cli_overrides_or_exit(project_overrides: Mapping[str, object]) -> None:
    """Exit 1 when a ``--project-*`` override has a bad value, naming the flag.

    These overrides otherwise reach validation only through the
    ``[tool.nab]`` parse in :func:`_load_config`, which stamps every error
    ``in [tool.nab]:`` and points at a table the project may not have.
    Parsing them here surfaces the error against the flag instead.
    """
    for spec in OPTIONS:
        if spec.key not in project_overrides:
            continue
        try:
            build_cli_layer({spec.key: project_overrides[spec.key]})
        except SourceConfigError as exc:
            printer().error(f"{spec.cli_flag}: {exc}")
            sys.exit(1)


def _load_config(
    path: Path,
    *,
    discover_workspace: bool = True,
    anchor: datetime | None = None,
    cli_overrides: Mapping[str, object] | None = None,
) -> NabProjectConfig:
    require_pyproject_file(path)

    try:
        return read_pyproject_config(
            path,
            discover_workspace=discover_workspace,
            anchor=anchor,
            cli_overrides=cli_overrides,
        )
    except ConfigError as exc:
        printer().error(f"in [tool.nab]: {exc}")
        sys.exit(1)
    except WorkspaceDiscoveryError as exc:
        printer().error(f"workspace discovery error: {exc}")
        sys.exit(1)


def is_stdout(output: Path | None) -> bool:
    return output is not None and str(output) == "-"


def _reject_python_override_in_universal(
    config: NabProjectConfig, python: str | None
) -> None:
    """Exit 1 when ``--python`` is passed to a matrix-declaring project.

    The matrix declares the python axis itself, so a single Python for the
    run has nowhere to land; silently ignoring the flag would lock a set
    the user did not ask for.
    """
    if python is not None and config.mode is ResolveMode.UNIVERSAL:
        printer().error(
            "--python is not supported in universal mode;"
            " [tool.nab.matrix].python declares the Python axis."
        )
        sys.exit(1)


def _python_override_or_exit(
    config: NabProjectConfig, python: str | None
) -> NabProjectConfig:
    """Retarget ``config`` onto the ``--python`` value, exiting 1 on a bad one.

    Applied here rather than forwarded to the resolve, so the error reads
    as a flag error, not a ``[tool.nab]`` one.
    """
    try:
        return with_python_override(config, python)
    except ConfigError as e:
        printer().error(str(e))
        sys.exit(1)


def _resolve(  # noqa: PLR0913, PLR0912, C901 - one wrapper per resolve_for_targets kwarg / exit-mapped error
    path: Path,
    *,
    config: NabProjectConfig,
    cache_dir: Path | None,
    offline: bool,
    transport: AsyncHttpTransport,
    failure_prefix: str,
    python: str | None = None,
    groups: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
    build_requirements: bool = False,
    resolution_strategy: ResolutionStrategy | None = None,
    progress: ProgressReporter | None = None,
) -> ResolveResult:
    """Run the resolver and translate every failure to an exit.

    A returned result is always fully successful: a target that did not
    resolve is reported (one line for a single environment, a per-tuple
    block for a matrix) and the process exits 1.

    ``progress`` renders the live resolve line while ``resolve_for_targets``
    runs; it is cleared before any summary or error is written, so the two
    never collide.
    """
    config = _python_override_or_exit(config, python)
    try:
        try:
            result = resolve_for_targets(
                path,
                transport,
                config=config,
                cache_dir=cache_dir,
                offline=offline,
                groups=groups,
                extras=extras,
                build_requirements=build_requirements,
                resolution_strategy=resolution_strategy,
                progress=progress,
            )
        finally:
            if progress is not None:
                progress.clear()
    except ResolutionError as e:
        printer().error(f"resolution failed: {e}")
        sys.exit(1)
    except (
        UnsupportedVcsError,
        MissingExtraError,
        SiblingMetadataDivergenceError,
        SourceNameMismatchError,
        NonIntervalMarkerError,
        UnevaluableMarkerError,
    ) as e:
        printer().error(f"{failure_prefix}: {e}")
        sys.exit(1)
    except InvalidUploadTimeError as e:
        printer().error(str(e))
        sys.exit(1)
    except KeyError:
        printer().error(f"{path} has no [project].dependencies")
        sys.exit(1)
    except InvalidProjectTableError as e:
        printer().error(f"in {path}: {e}")
        sys.exit(1)
    except InvalidProjectRequirementError as e:
        printer().error(str(e))
        sys.exit(1)
    except LookupError as e:
        printer().error(str(e))
        sys.exit(1)
    except (
        MissingHashError,
        MissingSdistError,
        MetadataError,
        MetadataHashMismatchError,
        SdistHashMismatchError,
        WheelHashMismatchError,
    ) as e:
        printer().error(f"{failure_prefix}: {e}")
        sys.exit(1)
    except NotImplementedError as e:
        printer().error(f"{failure_prefix}: {e}")
        sys.exit(1)
    except ConfigError as e:
        printer().error(f"in [tool.nab]: {e}")
        sys.exit(1)
    except IndexAccessError as e:
        printer().error(f"{failure_prefix}: {e}")
        sys.exit(1)

    if not result.success:
        _report_failures(result)
        sys.exit(1)
    return result


def _report_failures(result: ResolveResult) -> None:
    """Report the targets that did not resolve.

    A resolve with one target has one error, and it is the run's error.
    A resolve with several (a matrix's tuples, or a conflict fork's
    members, which fork in specific mode too) has one error per target,
    and the pins that did resolve are as informative as the failures, so
    each target gets a labelled block.  The check keys on the target
    count, matching the lock-emission paths.  Both go to stderr: the
    report is a diagnostic, not the requested lock, so stdout stays clean
    for a caller that piped it.
    """
    if len(result.target_results) <= 1:
        first = next(tr.error for tr in result.every_result if tr.error is not None)
        printer().error(f"resolution failed: {first}")
        return

    blocks: list[str] = []
    for tr in result.target_results:
        label = tr.target.label
        if not tr.success:
            blocks.append(f"# {label}: FAILED")
            blocks.extend(_error_lines(tr.error))
            continue
        blocks.append(f"# {label}")
        blocks.extend(f"{name}=={tr.pins[name]}" for name in sorted(tr.pins))

    # Surface base-pass failures so a successful tuple set does not
    # mask a missing base attribution.
    for br in result.base_results:
        if br.success:
            continue
        blocks.append(f"# base/{br.target.label}: FAILED")
        blocks.extend(_error_lines(br.error))

    sys.stderr.write("\n".join(blocks) + "\n")


def _error_lines(error: ResolutionError | None) -> list[str]:
    """Render a failed target's error as commented block lines."""
    text = f"{type(error).__name__}: {error}" if error is not None else ""
    return [f"#   {line}" for line in text.splitlines()]


# Layered boolean flags (currently just --offline) are tri-state, which tyro
# renders as a value-taking --flag {True,False} rather than a --flag / --no-flag
# pair.  main() rewrites the bare forms into that value form before tyro parses.
_LAYERED_BOOL_FLAGS = frozenset({"offline"})

# The tokens that count as a value already spelled out after the flag.
_BOOL_FLAG_VALUES = frozenset({"True", "False", "None"})


def _normalize_layered_bool_flags(argv: list[str]) -> list[str]:
    """Rewrite bare ``--offline`` / ``--no-offline`` into tyro's value form.

    A layered boolean then reads like ``--cache`` / ``--no-cache`` at the
    CLI: ``--offline`` becomes ``--offline True`` and ``--no-offline`` becomes
    ``--offline False``.  An absent flag is left alone and still defers to the
    config layers, and an explicit ``--offline True`` / ``--offline False`` is
    passed through unchanged.
    """
    normalized: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]

        # --offline [value]: keep an explicit True/False/None, else it is bare.
        if token.startswith("--") and token[2:] in _LAYERED_BOOL_FLAGS:
            following = argv[i + 1] if i + 1 < len(argv) else None
            if following is not None and following in _BOOL_FLAG_VALUES:
                normalized += [token, following]
                i += 2
            else:
                normalized += [token, "True"]
                i += 1

        # --no-offline is shorthand for --offline False.
        elif token.startswith("--no-") and token[5:] in _LAYERED_BOOL_FLAGS:
            normalized += [f"--{token[5:]}", "False"]
            i += 1

        # Any other token (subcommand, path, unrelated flag) passes through.
        else:
            normalized.append(token)
            i += 1

    return normalized


# Side-effect imports: each module's @app.command decorators register the
# subcommand.  Placed at the bottom so the helpers above bind before the
# command modules import them back.
from . import _cache_cmd as _cache_module  # noqa: E402, F401 - side-effect
from . import _config_cmd as _config_module  # noqa: E402, F401 - side-effect
from . import _download as _download_module  # noqa: E402, F401 - side-effect
from . import _lock as _lock_module  # noqa: E402, F401 - side-effect


def main() -> None:
    """Entry point for the nab command."""
    global _printer  # noqa: PLW0603 - the run's printer is a module singleton main() sets
    # Tyro's SubcommandApp does not surface global flags, so ``--version`` and
    # the output flags (-v/-q, --color, --no-progress) are parsed before
    # ``app.cli()`` sees the sub-command.
    argv = sys.argv[1:]
    if argv and argv[0] in {"--version", "-V"}:
        sys.stdout.write(f"nab {__version__}\n")
        return

    try:
        options, rest = parse_output_options(argv, os.environ)
    except OutputOptionError as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)

    _printer = Printer(
        verbosity=options.verbosity, color=options.color, progress=options.progress
    )
    install_log_handler(_printer)

    try:
        app.cli(prog="nab", args=_normalize_layered_bool_flags(rest))
    except KeyboardInterrupt:
        _printer.error("interrupted")
        sys.exit(_SIGINT_EXIT_CODE)
