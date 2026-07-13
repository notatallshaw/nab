"""``nab lock`` subcommand and its lockfile-emission helpers.

Wires :func:`resolve_pyproject` / :func:`resolve_universal_pyproject`
to the writers in :mod:`nab_python.lockfile`, plus the universal-mode
per-tuple emission shapes (single-tuple file, templated per-tuple
files, multi-block stdout).

External callers (the resolver entry points, the lockfile writers,
the merge helper) are accessed through :mod:`nab.cli` so the test
suite's ``patch("nab.cli.resolve_pyproject")`` style of monkey
patches keeps working after the per-command split.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import TYPE_CHECKING, Annotated

import tomli
import tomli_w
import tyro

from nab._version import __version__
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import (
    NabProjectConfig,
    ResolveMode,
)
from nab_python.lockfile import (
    ArchivePin,
    IndexPin,
    LockInput,
    Provenance,
    is_valid_pylock_path,
    package_metadata_override_records,
    read_lockfile_anchor,
    read_lockfile_packages,
)
from nab_python.requirements_file import (
    read_pyproject_groups,
    read_pyproject_optional_dependencies,
)

from . import cli as _cli
from .cli import (
    BuildPolicyFlag,
    DistPolicyFlag,
    HttpBackend,
    LockFormat,
    ModeFlag,
    OfflineFlag,
    PathArg,
    ResolutionFlag,
    app,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from nab_index.transport import AsyncHttpTransport
    from nab_python._vendor.packaging.version import Version
    from nab_python.provider import ResolutionStrategy
    from nab_python.resolve import ResolutionResult
    from nab_python.universal.resolve import TupleResult, UniversalResult


def _emit_or_exit(emit: Callable[[], None]) -> None:
    """Run an emit step, mapping an unwritable ``--output`` to a clean exit."""
    try:
        emit()
    except OSError as e:
        sys.stderr.write(f"Error: cannot write output: {e}\n")
        sys.exit(1)


@app.command
def lock(  # noqa: PLR0913 - tyro maps each kwarg to a CLI flag so a config object would hide the user-facing surface
    path: PathArg = Path("pyproject.toml"),
    *,
    output: Path | None = None,
    format: LockFormat = "pylock",  # noqa: A002 - shadows builtin by convention
    http_backend: HttpBackend | None = None,
    cache_dir: Path | None = None,
    cache: bool = True,
    offline: OfflineFlag = None,
    groups: tuple[str, ...] = (),
    all_groups: bool = False,
    extras: tuple[str, ...] = (),
    all_extras: bool = False,
    workspace_discovery: bool = True,
    no_emit_workspace: bool = False,
    project_resolution: ResolutionFlag | None = None,
    project_mode: ModeFlag | None = None,
    project_requires_python: str | None = None,
    project_uploaded_prior_to: str | None = None,
    project_dist_policy: DistPolicyFlag | None = None,
    project_build_policy: BuildPolicyFlag | None = None,
    project_constraint: Annotated[tuple[str, ...], tyro.conf.UseAppendAction] = (),
    project_default_group: Annotated[tuple[str, ...], tyro.conf.UseAppendAction] = (),
    upgrade: bool = False,
    locked: bool = False,
) -> None:
    """Resolve dependencies and emit a lockfile or pin list.

    Formats: ``pylock`` (PEP 751), ``requirements`` (pip-style with
    ``--hash`` lines), ``requirements-without-hashes`` (plain
    ``name==version``).  ``--output`` defaults to ``pylock.toml`` or
    ``requirements.txt``; ``--output -`` writes to stdout.

    ``--groups`` / ``--all-groups`` select PEP 735 dependency groups;
    ``--extras`` / ``--all-extras`` select entries from
    ``[project.optional-dependencies]``.  Selected names are folded into
    the resolve and recorded in the lockfile.

    Universal mode (``[tool.nab].mode = "universal"``) supports all
    three formats.  For requirements formats, an ``--output`` template
    containing ``{python_version}`` or ``{platform_id}`` writes one
    file per matrix tuple; a plain path is rejected when multiple
    tuples would collide.

    ``--no-emit-workspace`` drops workspace member pins from the
    emitted lockfile; the resolver still uses them locally.  Pair
    with ``pip install --no-deps -e <member>`` when consuming the
    lockfile via pip's PEP 751 install or ``--require-hashes``: both
    refuse directory entries because they cannot be hashed.

    ``--project-resolution`` overrides ``[tool.nab].resolution`` for this
    run (a PROJECT-scope override, so it is layered through the config
    sources; see Configuration).  ``--http-backend`` is a USER option, so
    it too is read from the config sources (``NAB_HTTP_BACKEND`` or an
    ``nab.toml``) when the flag is not passed.  ``--upgrade`` re-anchors the
    ``P<n>D`` cutoff to ``datetime.now(UTC)`` instead of reusing the
    timestamp recorded in any existing lockfile.

    ``--locked`` re-resolves and verifies the committed pylock is already
    up to date, writing nothing and exiting non-zero if it would change.
    It is for pylock output to a file, single-environment mode only.
    """
    _validate_pylock_output_name(output=output, format=format)
    if locked and (format != "pylock" or _cli.is_stdout(output)):
        sys.stderr.write(
            "Error: --locked is only supported for pylock output to a file.\n"
        )
        sys.exit(1)
    overrides = _cli._cli_overrides(  # noqa: SLF001
        cli_resolution=project_resolution,
        cli_offline=offline,
        cli_cache_dir=cache_dir,
        cli_http_backend=http_backend,
        cli_mode=project_mode,
        cli_requires_python=project_requires_python,
        cli_uploaded_prior_to=project_uploaded_prior_to,
        cli_dist_policy=project_dist_policy,
        cli_build_policy=project_build_policy,
        cli_constraint=project_constraint,
        cli_default_group=project_default_group,
    )
    project_overrides = _cli.project_config_overrides(overrides)
    anchor = _determine_lock_anchor(
        path,
        output=output,
        format=format,
        upgrade=upgrade,
        cli_overrides=project_overrides,
    )
    config = _cli._load_config(  # noqa: SLF001
        path,
        discover_workspace=workspace_discovery,
        anchor=anchor,
        cli_overrides=project_overrides,
    )
    if locked and config.mode is ResolveMode.UNIVERSAL:
        sys.stderr.write("Error: --locked is not supported in universal mode.\n")
        sys.exit(1)
    settings = _cli._layered_run_settings_or_exit(path, overrides)  # noqa: SLF001
    effective_cache_dir = _cli._resolve_effective_cache_dir(  # noqa: SLF001
        settings.cache_dir, cache=cache
    )
    provenance = _build_provenance(
        path,
        config=config,
        anchor=anchor,
        cli_project_overrides=settings.cli_project_overrides,
    )
    selected_groups = resolve_group_selection(
        path, groups=groups, all_groups=all_groups
    )
    selected_extras = resolve_extra_selection(
        path, extras=extras, all_extras=all_extras
    )

    workspace_to_drop = (
        config.workspace_member_names if no_emit_workspace else frozenset()
    )

    transport = _cli._make_transport(settings.http_backend)  # noqa: SLF001
    if config.mode is ResolveMode.UNIVERSAL:
        _emit_or_exit(
            lambda: _emit_universal(
                path,
                config=config,
                cache_dir=effective_cache_dir,
                transport=transport,
                offline=settings.offline,
                output=output,
                format=format,
                provenance=provenance,
                groups=selected_groups,
                extras=selected_extras,
                resolution_strategy=settings.resolution,
                workspace_to_drop=workspace_to_drop,
            )
        )
        return

    result = _cli._resolve_specific(  # noqa: SLF001
        path,
        config=config,
        cache_dir=effective_cache_dir,
        offline=settings.offline,
        transport=transport,
        failure_prefix="Cannot lock",
        groups=selected_groups,
        extras=selected_extras,
        resolution_strategy=settings.resolution,
    )
    if locked:
        _check_locked(result, output=output, workspace_to_drop=workspace_to_drop)
        return
    _emit_or_exit(
        lambda: _emit_specific(
            result,
            format=format,
            output=output,
            provenance=provenance,
            workspace_to_drop=workspace_to_drop,
        )
    )


def _drop_workspace_pins(
    lock_input: LockInput, workspace_to_drop: frozenset[str]
) -> LockInput:
    """Return a copy of ``lock_input`` with workspace pins removed.

    ``workspace_to_drop`` holds canonical workspace member names; pin
    keys are already canonical.  An empty set returns ``lock_input``
    unchanged.  ``pins`` and ``per_tuple_pins`` are filtered, and the
    forward dependency graph is filtered too so no edge points at a
    dropped member with no ``[[packages]]`` entry.
    """
    if not workspace_to_drop:
        return lock_input

    def keep(name: str) -> bool:
        return canonicalize_name(name) not in workspace_to_drop

    filtered_pins = {name: pin for name, pin in lock_input.pins.items() if keep(name)}
    filtered_per_tuple = {
        label: {name: pin for name, pin in tuple_pins.items() if keep(name)}
        for label, tuple_pins in lock_input.per_tuple_pins.items()
    }
    filtered_dependencies = {
        name: kept
        for name, deps in lock_input.dependencies.items()
        if keep(name) and (kept := tuple(dep for dep in deps if keep(dep)))
    }
    return replace(
        lock_input,
        pins=filtered_pins,
        per_tuple_pins=filtered_per_tuple,
        dependencies=filtered_dependencies,
    )


def _emit_specific(
    result: ResolutionResult,
    *,
    format: str,  # noqa: A002 - shadows builtin by convention
    output: Path | None,
    provenance: Provenance | None = None,
    workspace_to_drop: frozenset[str] = frozenset(),
) -> None:
    """Write a single-environment resolution in the requested format."""
    lock_input = _drop_workspace_pins(result.lock_input, workspace_to_drop)
    if provenance is not None:
        lock_input.provenance = provenance

    try:
        _write_specific(result, lock_input, format=format, output=output)
    except _cli.MissingHashError as e:
        sys.stderr.write(f"Cannot lock: {e}\n")
        sys.exit(1)


def _packages_only(text: str) -> str:
    """Re-render lock TOML without the volatile ``[tool.nab]`` block.

    Drops the provenance block (its command line and timestamp change every
    run) so two locks compare equal whenever their packages, environments,
    and metadata match.
    """
    data = tomli.loads(text)
    data.pop("tool", None)
    return tomli_w.dumps(data)


def _check_locked(
    result: ResolutionResult,
    *,
    output: Path | None,
    workspace_to_drop: frozenset[str],
) -> None:
    """Verify the committed pylock matches a fresh resolve, writing nothing.

    The resolve has already run; this renders the lock it would produce and
    compares it to the committed file with the provenance block dropped from
    both, so only a real change to the locked packages fails.  The committed
    lock is never read back into the resolve.
    """
    lock_input = _drop_workspace_pins(result.lock_input, workspace_to_drop)
    target = output if output is not None else Path(_cli._DEFAULT_OUTPUT["pylock"])  # noqa: SLF001
    if not target.exists():
        sys.stderr.write(
            f"Error: --locked: no lockfile at {target} to check;"
            " run `nab lock` first.\n"
        )
        sys.exit(1)
    try:
        new_text = _cli.render_lock(lock_input, lock_dir=target.parent)
    except _cli.MissingHashError as e:
        sys.stderr.write(f"Cannot lock: {e}\n")
        sys.exit(1)
    try:
        committed = _packages_only(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        sys.stderr.write(
            f"Error: --locked: lockfile {target} is not valid TOML: {e};"
            " re-run `nab lock` to regenerate it.\n"
        )
        sys.exit(1)
    if _packages_only(new_text) == committed:
        sys.stderr.write(f"Lockfile {target} is up to date.\n")
        return
    sys.stderr.write(
        f"Error: --locked: lockfile {target} is out of date;"
        " re-run `nab lock` to update it.\n"
    )
    sys.exit(1)


def _write_specific(
    result: ResolutionResult,
    lock_input: LockInput,
    *,
    format: str,  # noqa: A002 - shadows builtin by convention
    output: Path | None,
) -> None:
    """Render ``lock_input`` in ``format``."""
    if _cli.is_stdout(output):
        if format == "pylock":
            sys.stdout.write(_cli.write_lock(lock_input))
        elif format == "requirements":
            sys.stdout.write(_cli.write_requirements_with_hashes(lock_input))
        else:
            sys.stdout.write(_cli.write_requirements_without_hashes(lock_input))
        return

    target = output if output is not None else Path(_cli._DEFAULT_OUTPUT[format])  # noqa: SLF001
    # Report on the emitted set, not result.pins, so --no-emit-workspace
    # filtering shows up in the diff and the count.
    emitted_pins = {name: result.pins[name] for name in lock_input.pins}
    if format == "pylock":
        # Read the prior pins before write_lock overwrites the file.
        prior = read_lockfile_packages(target)
        _cli.write_lock(lock_input, output_path=target)
        # Index and archive pins record a version; local and VCS pins emit
        # version=None, so read_lockfile_packages never returns them.
        # Diff against the same set or they read as added every relock.
        versioned = {
            name: version
            for name, version in emitted_pins.items()
            if isinstance(lock_input.pins[name], (IndexPin, ArchivePin))
        }
        diff = _diff_summary(prior, versioned)
    elif format == "requirements":
        _cli.write_requirements_with_hashes(lock_input, output_path=target)
        diff = ""
    else:
        _cli.write_requirements_without_hashes(lock_input, output_path=target)
        diff = ""
    sys.stderr.write(f"Wrote {target} ({len(emitted_pins)} packages{diff})\n")


def _diff_summary(
    prior: Mapping[str, Version] | None, current: Mapping[str, Version]
) -> str:
    """Return a ``: A added, B upgraded, ...`` suffix for a re-lock.

    ``prior`` is the previous pylock's pins or ``None`` (first lock or
    an unparseable prior file); both fall back to an empty suffix.  An
    unchanged pin set also yields an empty suffix.
    """
    if prior is None:
        return ""
    added = sum(name not in prior for name in current)
    removed = sum(name not in current for name in prior)
    upgraded = downgraded = 0
    for name, version in current.items():
        old = prior.get(name)
        if old is None or old == version:
            continue
        if version > old:
            upgraded += 1
        else:
            downgraded += 1
    parts = [
        f"{count} {label}"
        for count, label in (
            (added, "added"),
            (upgraded, "upgraded"),
            (downgraded, "downgraded"),
            (removed, "removed"),
        )
        if count
    ]
    return f": {', '.join(parts)}" if parts else ""


def _emit_universal(  # noqa: PLR0913 - one wrapper per resolve_universal_pyproject kwarg
    path: Path,
    *,
    config: NabProjectConfig,
    cache_dir: Path | None,
    transport: AsyncHttpTransport,
    offline: bool,
    output: Path | None,
    format: str,  # noqa: A002 - shadows builtin by convention
    provenance: Provenance | None = None,
    groups: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
    resolution_strategy: ResolutionStrategy | None = None,
    workspace_to_drop: frozenset[str] = frozenset(),
) -> None:
    """Run the universal resolver and emit the requested artefact."""
    sys.stderr.write(
        "warning: mode = 'universal' is experimental; output format may"
        " change without notice\n"
    )

    result = _cli._resolve_universal(  # noqa: SLF001
        path,
        config=config,
        cache_dir=cache_dir,
        offline=offline,
        transport=transport,
        groups=groups,
        extras=extras,
        resolution_strategy=resolution_strategy,
    )

    if format == "pylock":
        _emit_universal_pylock(
            result,
            config=config,
            output=output,
            provenance=provenance,
            groups=groups,
            extras=extras,
            workspace_to_drop=workspace_to_drop,
        )
    else:
        _emit_universal_requirements(
            result,
            output=output,
            with_hashes=format == "requirements",
            workspace_to_drop=workspace_to_drop,
        )


def _emit_universal_pylock(
    result: UniversalResult,
    *,
    config: NabProjectConfig | None = None,
    output: Path | None,
    provenance: Provenance | None = None,
    groups: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
    workspace_to_drop: frozenset[str] = frozenset(),
) -> None:
    """Merge per-tuple LockInputs into one pylock and write/print it.

    ``default_groups`` is taken from ``[tool.nab].default-groups``, not
    from the CLI ``--groups`` selection: PEP 751 ``default-groups``
    means the groups a default install applies, which is project
    policy rather than this run's request.
    """
    default_groups = config.default_groups if config is not None else ()
    conflicts = config.conflicts if config is not None else ()
    lock_input = _cli.merge_universal_lock_inputs(
        result,
        extras=extras,
        dependency_groups=groups,
        default_groups=default_groups,
        conflicts=conflicts,
    )
    lock_input = _drop_workspace_pins(lock_input, workspace_to_drop)
    if provenance is not None:
        lock_input.provenance = provenance

    target: Path | None
    if _cli.is_stdout(output):
        target = None
    else:
        target = output if output is not None else Path(_cli._DEFAULT_OUTPUT["pylock"])  # noqa: SLF001

    try:
        # Pass the target so wheel/sdist/directory paths are written
        # relative to the lockfile's own directory, not the cwd.
        text = _cli.write_lock(lock_input, output_path=target)
    except _cli.MissingHashError as e:
        sys.stderr.write(f"Cannot lock: {e}\n")
        sys.exit(1)
    except (_cli.DisjointnessError, _cli.DivergentBaseDependencyError) as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    if target is None:
        sys.stdout.write(text)
        return
    sys.stderr.write(f"Wrote {target} ({len(result.tuple_results)} tuples)\n")


def _check_output_template(output: Path, template: str) -> None:
    """Reject an --output template that ``str.format`` cannot render.

    Only bare ``{python_version}`` and ``{platform_id}`` fields are
    accepted. An unbalanced brace, an unknown field name, or a field
    carrying a format spec (``{platform_id:d}``) or conversion
    (``{python_version!r}``) is rejected here.
    """
    allowed_vars = _cli.TUPLE_TEMPLATE_VARS
    allowed = {
        name
        for v in allowed_vars
        for _, name, _, _ in Formatter().parse(v)
        if name is not None
    }
    try:
        fields = [
            (name, spec, conversion)
            for _, name, spec, conversion in Formatter().parse(template)
            if name is not None
        ]
    except ValueError as e:
        sys.stderr.write(f"Error: --output {output} is not a valid template: {e}\n")
        sys.exit(1)
    supported = " and ".join(allowed_vars)
    unknown = sorted(f"{{{name}}}" for name, _, _ in fields if name not in allowed)
    if unknown:
        sys.stderr.write(
            f"Error: --output {output} has unknown template placeholder(s)"
            f" {', '.join(unknown)}; only {supported} are supported.\n"
        )
        sys.exit(1)
    decorated = sorted(
        {f"{{{name}}}" for name, spec, conversion in fields if spec or conversion}
    )
    if decorated:
        sys.stderr.write(
            f"Error: --output {output} is not a valid template:"
            f" {', '.join(decorated)} may not carry a format spec or conversion;"
            f" only bare {supported} are supported.\n"
        )
        sys.exit(1)


def _emit_universal_requirements(
    result: UniversalResult,
    *,
    output: Path | None,
    with_hashes: bool,
    workspace_to_drop: frozenset[str] = frozenset(),
) -> None:
    """Emit one requirements file per matrix tuple.

    Three output shapes:

    * ``output`` is ``None`` or ``-``: write one stdout dump with
      ``# label`` blocks separating each tuple's pins.  Inspection /
      piping shape; pip cannot install a multi-block file directly.
    * ``output`` contains ``{python_version}`` or ``{platform_id}``:
      write one file per successful tuple, substituting the tuple's
      values into the template.  This is the constraints-per-
      Python-version shape (e.g. ``constraints-{python_version}.txt``).
    * ``output`` is a plain path AND the matrix has exactly one tuple:
      write that tuple's pins to ``output`` directly.

    A plain path with multiple tuples errors clearly: there is no
    one-file shape that pip can install from across all tuples.
    """
    if output is None or _cli.is_stdout(output):
        _emit_universal_requirements_stdout(
            result, with_hashes=with_hashes, workspace_to_drop=workspace_to_drop
        )
        return

    template = str(output)
    successful = [tr for tr in result.tuple_results if tr.success]
    if not any(var in template for var in _cli.TUPLE_TEMPLATE_VARS):
        if len(successful) > 1:
            sys.stderr.write(
                "Error: universal mode produced multiple tuples but"
                f" --output {output} has no template variable to"
                " disambiguate.  Use {python_version} and/or"
                " {platform_id} in the path, e.g.:\n"
                "  --output 'constraints-{python_version}.txt'\n"
            )
            sys.exit(1)
        # Single successful tuple: write directly to the fixed path.
        _write_one_tuple_requirements(
            successful[0],
            output,
            with_hashes=with_hashes,
            workspace_to_drop=workspace_to_drop,
        )
        return

    _check_output_template(output, template)
    substituted_paths: dict[str, str] = {}
    for tr in successful:
        substituted = template.format(
            python_version=tr.tuple_.python_version,
            platform_id=tr.tuple_.platform_id,
        )
        if substituted in substituted_paths:
            sys.stderr.write(
                "Error: tuples"
                f" {substituted_paths[substituted]!r} and {tr.tuple_.label!r}"
                f" both map to {substituted!r}; --output {output} is missing"
                " a template variable to disambiguate.  Use both"
                " {python_version} and {platform_id} in the path.\n"
            )
            sys.exit(1)
        substituted_paths[substituted] = tr.tuple_.label

    for tr in successful:
        substituted = template.format(
            python_version=tr.tuple_.python_version,
            platform_id=tr.tuple_.platform_id,
        )
        _write_one_tuple_requirements(
            tr,
            Path(substituted),
            with_hashes=with_hashes,
            workspace_to_drop=workspace_to_drop,
        )


def _emit_universal_requirements_stdout(
    result: UniversalResult,
    *,
    with_hashes: bool,
    workspace_to_drop: frozenset[str] = frozenset(),
) -> None:
    """Stdout shape: per-tuple ``# label`` blocks merged into one stream."""
    lock_input = _drop_workspace_pins(
        _cli.merge_universal_lock_inputs(result), workspace_to_drop
    )
    try:
        if with_hashes:
            text = _cli.write_requirements_with_hashes(lock_input)
        else:
            text = _cli.write_requirements_without_hashes(lock_input)
    except _cli.MissingHashError as e:
        sys.stderr.write(f"Cannot lock: {e}\n")
        sys.exit(1)
    sys.stdout.write(text)


def _write_one_tuple_requirements(
    tr: TupleResult,
    output: Path,
    *,
    with_hashes: bool,
    workspace_to_drop: frozenset[str] = frozenset(),
) -> None:
    """Write a single TupleResult's pins to ``output``."""
    if tr.lock_input is None:  # pragma: no cover - successful tuples carry one
        return

    lock_input = _drop_workspace_pins(tr.lock_input, workspace_to_drop)
    try:
        if with_hashes:
            text = _cli.write_requirements_with_hashes(lock_input)
        else:
            text = _cli.write_requirements_without_hashes(lock_input)
    except _cli.MissingHashError as e:
        sys.stderr.write(f"Cannot lock: {e}\n")
        sys.exit(1)

    output.write_text(text, encoding="utf-8")
    sys.stderr.write(
        f"Wrote {output} ({len(lock_input.pins)} packages, tuple {tr.tuple_.label})\n"
    )


def resolve_group_selection(
    path: Path,
    *,
    groups: tuple[str, ...],
    all_groups: bool,
) -> tuple[str, ...]:
    """Return the canonical, deduplicated group selection for this run.

    ``groups`` is the user-supplied list (already split by tyro on
    commas).  ``all_groups`` overrides it: when set, every group
    defined in the project's ``[dependency-groups]`` table is
    selected.  An ``--all-groups`` paired with a non-empty
    ``--groups`` list raises a clean error rather than silently
    preferring one over the other.
    """
    if all_groups and groups:
        sys.stderr.write("Error: --all-groups and --groups are mutually exclusive\n")
        sys.exit(1)
    if not (all_groups or groups):
        return ()

    try:
        defined = read_pyproject_groups(path)
    except OSError:
        reason = "is a directory" if path.is_dir() else "not found"
        sys.stderr.write(f"Error: {path} {reason}\n")
        sys.exit(1)
    except TypeError as e:
        sys.stderr.write(f"Error in {path}: {e}\n")
        sys.exit(1)
    return tuple(defined.keys()) if all_groups else tuple(dict.fromkeys(groups))


def resolve_extra_selection(
    path: Path,
    *,
    extras: tuple[str, ...],
    all_extras: bool,
) -> tuple[str, ...]:
    """Return the canonical, deduplicated extras selection for this run."""
    if all_extras and extras:
        sys.stderr.write("Error: --all-extras and --extras are mutually exclusive\n")
        sys.exit(1)
    if not (all_extras or extras):
        return ()

    try:
        defined = read_pyproject_optional_dependencies(path)
    except OSError:
        reason = "is a directory" if path.is_dir() else "not found"
        sys.stderr.write(f"Error: {path} {reason}\n")
        sys.exit(1)
    except TypeError as e:
        sys.stderr.write(f"Error in {path}: {e}\n")
        sys.exit(1)
    return tuple(defined.keys()) if all_extras else tuple(dict.fromkeys(extras))


def _build_provenance(
    path: Path,
    *,
    config: NabProjectConfig,
    anchor: datetime,
    cli_project_overrides: tuple[tuple[str, str], ...] = (),
) -> Provenance:
    """Capture the inputs that produced this run for the lockfile.

    The block lands under ``[tool.nab]`` and is informational only.

    ``anchor`` is the timestamp used as ``now`` when resolving relative
    ``P<n>D`` durations on this run.  Recording it as ``created-at``
    lets the next ``nab lock`` reuse the same anchor and reproduce the
    same cutoff.  ``cli_project_overrides`` records the ``--project-*``
    overrides passed on this run so the lock is auditable.
    """
    python_specifier: str | None
    platforms: tuple[str, ...]
    if config.mode is ResolveMode.UNIVERSAL and config.matrix is not None:
        python_specifier = config.matrix.python
        platforms = tuple(p.label for p in config.matrix.platforms)
    else:
        python_specifier = config.requires_python
        platforms = ()

    return Provenance(
        nab_version=__version__,
        created_at=anchor,
        command_line=tuple(sys.argv),
        input_path=str(path),
        mode=config.mode.value,
        python_specifier=python_specifier,
        platforms=platforms,
        cli_project_overrides=cli_project_overrides,
        package_metadata_overrides=package_metadata_override_records(
            config.package_overrides
        ),
    )


def _reused_lock_anchor(
    path: Path,
    *,
    output: Path | None,
    format: str,  # noqa: A002 - shadows builtin by convention
    cli_overrides: Mapping[str, object] | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return ``(absolute_cutoff, prior_lock_anchor)`` for the re-lock anchor.

    An absolute ``uploaded-prior-to`` fixes the resolve window, so it is the
    anchor regardless of ``--upgrade``, and two locks from identical inputs
    produce identical bytes.  ``cli_overrides`` carries a
    ``--project-uploaded-prior-to`` override so the anchor it produces matches
    the resolve window.  When there is no absolute cutoff, a ``pylock`` written
    to a file reuses the ``created-at`` from the existing lock so re-locks
    reproduce the same cutoff; that recorded timestamp is the only anchor
    ``--upgrade`` actually drops.  Stdout, the requirements formats, and a
    first lock have nothing to reuse.
    """
    absolute = _cli.lock_anchor(path, cli_overrides)
    if absolute is not None:
        return absolute, None
    if _cli.is_stdout(output) or format != "pylock":
        return None, None
    target = output if output is not None else Path(_cli._DEFAULT_OUTPUT[format])  # noqa: SLF001
    return None, read_lockfile_anchor(target)


def _determine_lock_anchor(
    path: Path,
    *,
    output: Path | None,
    format: str,  # noqa: A002 - shadows builtin by convention
    upgrade: bool,
    cli_overrides: Mapping[str, object] | None = None,
) -> datetime:
    """Pick the ``P<n>D`` anchor for ``nab lock``.

    Without ``--upgrade`` the anchor is the cutoff a re-lock reuses: an
    absolute ``uploaded-prior-to`` cutoff, or the ``created-at`` from an
    existing pylock, falling back to ``datetime.now(UTC)``.  ``cli_overrides``
    is forwarded so a ``--project-uploaded-prior-to`` flag pins the anchor.
    ``--upgrade`` re-anchors to now.  It changes the resolve only when it drops
    a reused lockfile ``created-at`` (an absolute cutoff still governs the
    resolve either way), so the notice fires only in that case.
    """
    absolute, prior = _reused_lock_anchor(
        path, output=output, format=format, cli_overrides=cli_overrides
    )
    if upgrade:
        fresh = datetime.now(timezone.utc)
        if prior is not None:
            sys.stderr.write(
                "notice: --upgrade re-anchored the resolve window to"
                f" {fresh.isoformat()}, dropping the cutoff {prior.isoformat()}"
                " recorded in the existing lockfile.\n"
            )
        return fresh
    reused = absolute if absolute is not None else prior
    return reused if reused is not None else datetime.now(timezone.utc)


def _validate_pylock_output_name(
    *,
    output: Path | None,
    format: str,  # noqa: A002 - shadows builtin by convention
) -> None:
    """Reject a ``--output`` name that PEP 751 would not recognise.

    A pylock-format lockfile must be ``pylock.toml`` or match
    ``pylock.<name>.toml`` (dot separators).  stdout (``-``) and the
    requirements formats are exempt; exits 1 on a bad name with a
    suggested correction.
    """
    if format != "pylock" or output is None or _cli.is_stdout(output):
        return
    if is_valid_pylock_path(output):
        return
    dotted = output.with_name(output.name.replace("-", "."))
    suggestion = dotted.name if is_valid_pylock_path(dotted) else "pylock.toml"
    sys.stderr.write(
        f"Error: output file name {output.name!r} must match 'pylock.toml'"
        " or 'pylock.<name>.toml' per PEP 751 (note the dot separator,"
        f" not a hyphen).  Try {suggestion!r}.\n"
    )
    sys.exit(1)
