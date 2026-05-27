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
from typing import TYPE_CHECKING

from nab._version import __version__
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import (
    NabProjectConfig,
    ResolveMode,
    read_pyproject_lock_anchor,
)
from nab_python.lockfile import (
    LockInput,
    Provenance,
    is_valid_pylock_path,
    read_lockfile_anchor,
    read_lockfile_packages,
)
from nab_python.provider import ResolutionStrategy
from nab_python.requirements_file import (
    read_pyproject_groups,
    read_pyproject_optional_dependencies,
)

from . import cli as _cli
from .cli import (
    HttpBackend,
    LockFormat,
    PathArg,
    ResolutionFlag,
    app,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_index.transport import AsyncHttpTransport
    from nab_python._vendor.packaging.version import Version
    from nab_python.resolve import ResolutionResult
    from nab_python.universal.resolve import TupleResult, UniversalResult


@app.command
def lock(  # noqa: PLR0913 - tyro maps each kwarg to a CLI flag so a config object would hide the user-facing surface
    path: PathArg = Path("pyproject.toml"),
    *,
    output: Path | None = None,
    format: LockFormat = "pylock",  # noqa: A002 - shadows builtin by convention
    http_backend: HttpBackend = "urllib3",
    cache_dir: Path | None = None,
    cache: bool = True,
    offline: bool = False,
    groups: tuple[str, ...] = (),
    all_groups: bool = False,
    extras: tuple[str, ...] = (),
    all_extras: bool = False,
    workspace_discovery: bool = True,
    no_emit_workspace: bool = False,
    resolution: ResolutionFlag | None = None,
    upgrade: bool = False,
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

    ``--resolution`` overrides ``[tool.nab].resolution`` for this run.
    ``--upgrade`` re-anchors the ``P<n>D`` cutoff to ``datetime.now(UTC)``
    instead of reusing the timestamp recorded in any existing lockfile.
    """
    _validate_pylock_output_name(output=output, format=format)
    anchor = _determine_lock_anchor(path, output=output, format=format, upgrade=upgrade)
    config = _cli._load_config(  # noqa: SLF001
        path, discover_workspace=workspace_discovery, anchor=anchor
    )
    effective_cache_dir = _cli._resolve_effective_cache_dir(  # noqa: SLF001
        cache_dir, cache=cache
    )
    provenance = _build_provenance(path, config=config, anchor=anchor)
    selected_groups = _resolve_group_selection(
        path, groups=groups, all_groups=all_groups
    )
    selected_extras = _resolve_extra_selection(
        path, extras=extras, all_extras=all_extras
    )
    strategy_override = (
        ResolutionStrategy(resolution) if resolution is not None else None
    )

    workspace_to_drop = (
        config.workspace_member_names if no_emit_workspace else frozenset()
    )

    transport = _cli._make_transport(http_backend)  # noqa: SLF001
    if config.mode is ResolveMode.UNIVERSAL:
        _emit_universal(
            path,
            config=config,
            cache_dir=effective_cache_dir,
            transport=transport,
            offline=offline,
            output=output,
            format=format,
            provenance=provenance,
            groups=selected_groups,
            extras=selected_extras,
            resolution_strategy=strategy_override,
            workspace_to_drop=workspace_to_drop,
        )
        return

    result = _cli._resolve_specific(  # noqa: SLF001
        path,
        config=config,
        cache_dir=effective_cache_dir,
        offline=offline,
        transport=transport,
        failure_prefix="Cannot lock",
        groups=selected_groups,
        extras=selected_extras,
        resolution_strategy=strategy_override,
    )
    _emit_specific(
        result,
        format=format,
        output=output,
        provenance=provenance,
        workspace_to_drop=workspace_to_drop,
    )


def _drop_workspace_pins(
    lock_input: LockInput, workspace_to_drop: frozenset[str]
) -> LockInput:
    """Return a copy of ``lock_input`` with workspace pins removed.

    ``workspace_to_drop`` holds canonical workspace member names; pin
    keys are already canonical.  An empty set returns ``lock_input``
    unchanged.  Both ``pins`` and ``per_tuple_pins`` are filtered.
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
    return replace(
        lock_input,
        pins=filtered_pins,
        per_tuple_pins=filtered_per_tuple,
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
        diff = _diff_summary(prior, emitted_pins)
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
    lock_input = _cli.merge_universal_lock_inputs(
        result,
        extras=extras,
        dependency_groups=groups,
        default_groups=default_groups,
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

    if target is None:
        sys.stdout.write(text)
        return
    sys.stderr.write(f"Wrote {target} ({len(result.tuple_results)} tuples)\n")


def _check_output_template(output: Path, template: str) -> None:
    """Reject unknown placeholders in --output before str.format is called."""
    allowed_vars = _cli.TUPLE_TEMPLATE_VARS
    allowed = {
        name
        for v in allowed_vars
        for _, name, _, _ in Formatter().parse(v)
        if name is not None
    }
    try:
        fields = {
            name for _, name, _, _ in Formatter().parse(template) if name is not None
        }
    except ValueError as e:
        sys.stderr.write(f"Error: --output {output} is not a valid template: {e}\n")
        sys.exit(1)
    unknown = sorted(f"{{{name}}}" for name in fields if name not in allowed)
    if unknown:
        supported = " and ".join(allowed_vars)
        sys.stderr.write(
            f"Error: --output {output} has unknown template placeholder(s)"
            f" {', '.join(unknown)}; only {supported} are supported.\n"
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


def _resolve_group_selection(
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
    if not all_groups:
        return tuple(dict.fromkeys(groups))

    try:
        defined = read_pyproject_groups(path)
    except FileNotFoundError:
        sys.stderr.write(f"Error: {path} not found\n")
        sys.exit(1)
    return tuple(defined.keys())


def _resolve_extra_selection(
    path: Path,
    *,
    extras: tuple[str, ...],
    all_extras: bool,
) -> tuple[str, ...]:
    """Return the canonical, deduplicated extras selection for this run."""
    if all_extras and extras:
        sys.stderr.write("Error: --all-extras and --extras are mutually exclusive\n")
        sys.exit(1)
    if not all_extras:
        return tuple(dict.fromkeys(extras))

    try:
        defined = read_pyproject_optional_dependencies(path)
    except FileNotFoundError:
        sys.stderr.write(f"Error: {path} not found\n")
        sys.exit(1)
    return tuple(defined.keys())


def _build_provenance(
    path: Path, *, config: NabProjectConfig, anchor: datetime
) -> Provenance:
    """Capture the inputs that produced this run for the lockfile.

    The block lands under ``[tool.nab]`` and is informational only.

    ``anchor`` is the timestamp used as ``now`` when resolving relative
    ``P<n>D`` durations on this run.  Recording it as ``created-at``
    lets the next ``nab lock`` reuse the same anchor and reproduce the
    same cutoff.
    """
    python_specifier: str | None
    platforms: tuple[str, ...]
    if config.mode is ResolveMode.UNIVERSAL and config.matrix is not None:
        python_specifier = config.matrix.python
        platforms = tuple(config.matrix.platforms)
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
    )


def _determine_lock_anchor(
    path: Path,
    *,
    output: Path | None,
    format: str,  # noqa: A002 - shadows builtin by convention
    upgrade: bool,
) -> datetime:
    """Pick the ``P<n>D`` anchor for ``nab lock``.

    When the project pins an absolute ``uploaded-prior-to``, that timestamp
    is the anchor: the resolve window is already fixed, so anchoring there
    makes ``created-at`` deterministic and two locks from identical inputs
    produce identical bytes. ``--upgrade`` still re-floats to now.

    Otherwise returns ``datetime.now(UTC)`` (a fresh anchor) when:

    - ``--upgrade`` is set: the user is opting into a calendar refresh.
    - Output is stdout (``-``): there is no file to read back later, so
      no point preserving an anchor that nothing will reuse.
    - Format is not ``pylock``: requirements files do not carry the
      ``[tool.nab]`` block we read the anchor from.
    - The expected pylock does not exist or has no recorded anchor:
      first lock or a damaged file.

    Otherwise returns the ``[tool.nab].created-at`` from the existing
    pylock, so a re-lock against the same project produces the same
    cutoff for ``P<n>D`` durations.
    """
    fresh = datetime.now(timezone.utc)
    if upgrade:
        return fresh
    absolute = read_pyproject_lock_anchor(path)
    if absolute is not None:
        return absolute
    if _cli.is_stdout(output):
        return fresh
    if format != "pylock":
        return fresh
    target = output if output is not None else Path(_cli._DEFAULT_OUTPUT[format])  # noqa: SLF001
    prior = read_lockfile_anchor(target)
    return prior if prior is not None else fresh


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
