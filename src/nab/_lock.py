"""``nab lock`` subcommand and its lockfile-emission helpers.

Wires :func:`resolve_for_targets` to the writers in
:mod:`nab_python.lockfile`, plus the per-target emission shapes a matrix
needs (a templated file per tuple, multi-block stdout).

External callers (the resolver entry point, the lockfile writers) are
accessed through :mod:`nab.cli` so the test suite's
``patch("nab.cli.resolve_for_targets")`` style of monkey patches keeps
working after the per-command split.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import TYPE_CHECKING, Annotated, NamedTuple, NoReturn

import tomli
import tomli_w
import tyro

from nab._version import __version__
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    NabProjectConfig,
    ResolveMode,
)
from nab_python.lockfile import (
    ArchivePin,
    IndexPin,
    LockInput,
    Provenance,
    TargetLock,
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
from .output import ProgressReporter

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_python.target import ResolveTarget


def _emit_or_exit(emit: Callable[[], None]) -> None:
    """Run an emit step, mapping an unwritable ``--output`` to a clean exit."""
    try:
        emit()
    except OSError as e:
        _cli.printer().error(f"cannot write output: {e}")
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
    python: str | None = None,
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
    containing ``{python_version}``, ``{platform_id}`` or
    ``{selection}`` (the conflict fork a tuple belongs to) writes one
    file per matrix tuple; a plain path is rejected when multiple
    tuples would collide.

    ``--no-emit-workspace`` drops workspace member pins from the
    emitted lockfile; the resolver still uses them locally.  Pair
    with ``pip install --no-deps -e <member>`` when consuming the
    lockfile via pip's PEP 751 install or ``--require-hashes``: both
    refuse directory entries because they cannot be hashed.

    ``--python X.Y`` resolves for that Python on this machine instead of
    the running interpreter, like pip's ``--python-version``.  It is
    rejected in universal mode, where the matrix declares the Python axis.

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
        _cli.printer().error("--locked is only supported for pylock output to a file.")
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
        _cli.printer().error("--locked is not supported in universal mode.")
        sys.exit(1)
    _cli._reject_python_override_in_universal(config, python)  # noqa: SLF001
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

    if config.mode is ResolveMode.UNIVERSAL:
        _cli.printer().warning(
            "the multi-target ('universal') lockfile format is"
            " experimental and may change without notice"
        )

    transport = _cli._make_transport(settings.http_backend)  # noqa: SLF001
    result = _cli._resolve(  # noqa: SLF001
        path,
        config=config,
        cache_dir=effective_cache_dir,
        offline=settings.offline,
        transport=transport,
        failure_prefix="cannot lock",
        python=python,
        groups=selected_groups,
        extras=selected_extras,
        resolution_strategy=settings.resolution,
        progress=ProgressReporter(_cli.printer()),
    )

    lock_input = _drop_workspace_pins(
        _cli.build_lock_input(
            result,
            config=config,
            extras=selected_extras,
            dependency_groups=selected_groups,
        ),
        workspace_to_drop,
    )
    lock_input.provenance = provenance

    if locked:
        _check_locked(lock_input, output=output)
        return
    _emit_or_exit(lambda: _emit(lock_input, format=format, output=output))


def _drop_workspace_pins(
    lock_input: LockInput, workspace_to_drop: frozenset[str]
) -> LockInput:
    """Return a copy of ``lock_input`` with workspace pins removed.

    ``workspace_to_drop`` holds canonical workspace member names; pin
    keys are already canonical.  An empty set returns ``lock_input``
    unchanged.  Each target's pins are filtered, and its forward
    dependency graph and membership gates with them, so no edge or gate
    names a dropped member with no ``[[packages]]`` entry.
    """
    if not workspace_to_drop:
        return lock_input

    def keep(name: str) -> bool:
        return canonicalize_name(name) not in workspace_to_drop

    targets = {
        label: TargetLock(
            target=lock.target,
            pins={name: pin for name, pin in lock.pins.items() if keep(name)},
            dependencies={
                name: kept
                for name, deps in lock.dependencies.items()
                if keep(name) and (kept := tuple(dep for dep in deps if keep(dep)))
            },
            package_gates={
                name: gate for name, gate in lock.package_gates.items() if keep(name)
            },
        )
        for label, lock in lock_input.targets.items()
    }
    return replace(lock_input, targets=targets)


def _emit(
    lock_input: LockInput,
    *,
    format: str,  # noqa: A002 - shadows builtin by convention
    output: Path | None,
) -> None:
    """Write the resolved lock in the requested format."""
    if format == "pylock":
        _emit_pylock(lock_input, output=output)
    else:
        _emit_requirements(lock_input, format=format, output=output)


def _packages_only(text: str) -> str:
    """Re-render lock TOML without the volatile ``[tool.nab]`` block.

    Drops the provenance block (its command line and timestamp change every
    run) so two locks compare equal whenever their packages, environments,
    and metadata match.
    """
    data = tomli.loads(text)
    data.pop("tool", None)
    return tomli_w.dumps(data)


def _check_locked(lock_input: LockInput, *, output: Path | None) -> None:
    """Verify the committed pylock matches a fresh resolve, writing nothing.

    The resolve has already run; this renders the lock it would produce and
    compares it to the committed file with the provenance block dropped from
    both, so only a real change to the locked packages fails.  The committed
    lock is never read back into the resolve.
    """
    target = output if output is not None else Path(_cli._DEFAULT_OUTPUT["pylock"])  # noqa: SLF001
    if not target.exists():
        _cli.printer().error(
            f"--locked: no lockfile at {target} to check; run `nab lock` first."
        )
        sys.exit(1)
    try:
        new_text = _cli.render_lock(lock_input, lock_dir=target.parent)
    except _cli.MissingHashError as e:
        _cli.printer().error(f"cannot lock: {e}")
        sys.exit(1)
    try:
        committed = _packages_only(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        _cli.printer().error(
            f"--locked: lockfile {target} is not valid TOML: {e};"
            " re-run `nab lock` to regenerate it."
        )
        sys.exit(1)
    if _packages_only(new_text) == committed:
        _cli.printer().done(f"Lockfile {target} is up to date.")
        return
    _cli.printer().error(
        f"--locked: lockfile {target} is out of date; re-run `nab lock` to update it."
    )
    sys.exit(1)


def _emit_pylock(lock_input: LockInput, *, output: Path | None) -> None:
    """Write the PEP 751 lock to a file, or print it."""
    if _cli.is_stdout(output):
        sys.stdout.write(_render_lock_or_exit(lock_input, target=None))
        return

    target = output if output is not None else Path(_cli._DEFAULT_OUTPUT["pylock"])  # noqa: SLF001
    # Read the prior pins before the write overwrites the file.
    prior = read_lockfile_packages(target)
    # Pass the target so wheel/sdist/directory paths are written relative
    # to the lockfile's own directory, not the cwd.
    _render_lock_or_exit(lock_input, target=target)
    _cli.printer().done(f"Wrote {target} ({_lock_summary(lock_input, prior)})")


def _render_lock_or_exit(lock_input: LockInput, *, target: Path | None) -> str:
    """Render the lock, mapping every emit-time refusal to a clean exit."""
    try:
        return _cli.write_lock(lock_input, output_path=target)
    except _cli.MissingHashError as e:
        _cli.printer().error(f"cannot lock: {e}")
        sys.exit(1)
    except (_cli.DisjointnessError, _cli.DivergentBaseDependencyError) as e:
        _cli.printer().error(str(e))
        sys.exit(1)


def _lock_summary(lock_input: LockInput, prior: Mapping[str, Version] | None) -> str:
    """Summarise what was written: a package diff, or the tuple count.

    A matrix pins a package once per tuple, and two tuples may disagree,
    so there is no one version to diff against the prior lock; it reports
    the tuples it covered instead.
    """
    if len(lock_input.targets) > 1:
        return f"{len(lock_input.targets)} tuples"

    pins = {
        name: pin
        for lock in lock_input.targets.values()
        for name, pin in lock.pins.items()
    }

    # Index and archive pins record a version; local and VCS pins emit
    # version=None, so read_lockfile_packages never returns them.
    # Diff against the same set or they read as added every relock.
    versioned = {
        name: Version(pin.version)
        for name, pin in pins.items()
        if isinstance(pin, (IndexPin, ArchivePin))
    }
    return f"{len(pins)} packages{_diff_summary(prior, versioned)}"


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


def _template_values(target: ResolveTarget) -> dict[str, str]:
    """Return the value each ``--output`` template variable takes on ``target``.

    ``selection`` is empty on an unforked target, so a template naming it
    on a resolve with no conflict fork renders the empty string.
    """
    return {
        "python_version": target.python_version,
        "platform_id": target.platform_id,
        "selection": target.selection_slug,
    }


def _varying_vars(targets: Sequence[ResolveTarget]) -> list[str]:
    """Return the template variables whose value differs across ``targets``.

    These are the variables an ``--output`` template has to name to give
    each tuple its own file.  The list can be empty: two tuples can
    differ in a tag knob no variable names (a musl and a glibc target
    share one ``platform_id``), and then no template separates them.
    """
    values = [_template_values(target) for target in targets]
    first = values[0]
    return [
        name for name in first if any(other[name] != first[name] for other in values)
    ]


def _and_list(names: Sequence[str]) -> str:
    """Render ``names`` as ``a``, ``a and b``, or ``a, b and c``."""
    *rest, last = names
    return f"{', '.join(rest)} and {last}" if rest else last


def _check_output_template(output: Path, template: str) -> None:
    """Reject an --output template that ``str.format`` cannot render.

    Only the bare :data:`~nab.cli.TUPLE_TEMPLATE_VARS` fields are
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
        _cli.printer().error(f"--output {output} is not a valid template: {e}")
        sys.exit(1)
    supported = _and_list(allowed_vars)
    unknown = sorted(f"{{{name}}}" for name, _, _ in fields if name not in allowed)
    if unknown:
        _cli.printer().error(
            f"--output {output} has unknown template placeholder(s)"
            f" {', '.join(unknown)}; only {supported} are supported."
        )
        sys.exit(1)
    decorated = sorted(
        {f"{{{name}}}" for name, spec, conversion in fields if spec or conversion}
    )
    if decorated:
        _cli.printer().error(
            f"--output {output} is not a valid template:"
            f" {', '.join(decorated)} may not carry a format spec or conversion;"
            f" only bare {supported} are supported."
        )
        sys.exit(1)


def _emit_requirements(
    lock_input: LockInput,
    *,
    format: str,  # noqa: A002 - shadows builtin by convention
    output: Path | None,
) -> None:
    """Emit the pins as requirements, one file per target where needed.

    Four output shapes:

    * ``output`` is ``-``, or is unset for a lock covering more than one
      target: write one stdout dump, the targets separated by ``# label``
      blocks.  That is an inspection / piping shape (pip cannot install a
      multi-block file), and it is why a multi-target lock has no default
      file to fall back to: there is no one file to write.
    * ``output`` is unset and the lock covers one target: write
      ``requirements.txt``.
    * ``output`` names a :data:`~nab.cli.TUPLE_TEMPLATE_VARS` variable:
      write one file per target, substituting the target's values into
      the template.  This is the constraints-per-Python-version shape
      (e.g. ``constraints-{python_version}.txt``), and
      ``{selection}`` names the conflict fork a target belongs to
      (``req-{selection}.txt`` -> ``req-extra-cpu.txt``).
    * ``output`` is a plain path: write the lock's one target there.

    A plain path with several targets errors clearly: there is no
    one-file shape that pip can install from across all of them.

    The templated shape is all-or-nothing: every tuple is rendered and
    staged before any file is moved into place.
    """
    with_hashes = format == "requirements"
    multi_target = len(lock_input.targets) > 1
    if _cli.is_stdout(output) or (output is None and multi_target):
        text, _ = _render_requirements_or_exit(lock_input, with_hashes)
        sys.stdout.write(text)
        return

    target = output if output is not None else Path(_cli._DEFAULT_OUTPUT[format])  # noqa: SLF001
    template = str(target)
    if not any(var in template for var in _cli.TUPLE_TEMPLATE_VARS):
        if multi_target:
            _refuse_untemplated(lock_input, target)
        _, count = _render_requirements_or_exit(
            lock_input, with_hashes, output_path=target
        )
        _cli.printer().done(f"Wrote {target} ({count} packages)")
        return

    _check_output_template(target, template)

    rendered: list[_RenderedFile] = []
    for label, path in _substituted_paths(lock_input, target, template).items():
        text, count = _render_requirements_or_exit(
            _for_target(lock_input, label), with_hashes
        )
        rendered.append(
            _RenderedFile(path=path, label=label, text=text, pin_count=count)
        )

    _write_requirements_files(rendered)


def _refuse_untemplated(lock_input: LockInput, output: Path) -> NoReturn:
    """Exit 1: several tuples, and one plain ``--output`` path for them all.

    Names the variables that actually vary across the tuples, which for a
    resolve forked by ``[tool.nab].conflicts`` is ``{selection}`` alone:
    the fork is a dimension of its own, and the tuples it produces can
    share every other axis.
    """
    targets = [lock.target for lock in lock_input.targets.values()]
    count = len(targets)
    varying = _varying_vars(targets)
    if not varying:
        _cli.printer().error(
            f"the resolve produced {count} tuples and no --output template"
            f" variable tells them apart, so {output} cannot hold them."
            "  Emit pylock output instead."
        )
        sys.exit(1)
    placeholders = _and_list([f"{{{name}}}" for name in varying])
    example = "constraints" + "".join(f"-{{{name}}}" for name in varying) + ".txt"
    _cli.printer().error(
        f"the resolve produced {count} tuples but --output {output} has no"
        f" template variable to disambiguate.  Use {placeholders} in the path,"
        f" e.g.:\n  --output '{example}'"
    )
    sys.exit(1)


def _refuse_collision(
    first: TargetLock, second: TargetLock, *, output: Path, template: str, path: str
) -> NoReturn:
    """Exit 1: two tuples render one path under this template."""
    missing = [
        name
        for name in _varying_vars([first.target, second.target])
        if f"{{{name}}}" not in template
    ]
    head = (
        f"tuples {first.target.label!r} and {second.target.label!r}"
        f" both map to {path!r};"
    )
    if not missing:
        _cli.printer().error(
            f"{head} no --output template variable tells them apart."
            "  Emit pylock output instead."
        )
        sys.exit(1)
    placeholders = _and_list([f"{{{name}}}" for name in missing])
    _cli.printer().error(
        f"{head} --output {output} is missing a template variable to"
        f" disambiguate.  Add {placeholders} to the path."
    )
    sys.exit(1)


def _substituted_paths(
    lock_input: LockInput, output: Path, template: str
) -> dict[str, Path]:
    """Render the per-target output paths, refusing a template that collides."""
    by_path: dict[str, TargetLock] = {}
    paths: dict[str, Path] = {}
    for label, lock in lock_input.targets.items():
        substituted = template.format(**_template_values(lock.target))
        if substituted in by_path:
            _refuse_collision(
                by_path[substituted],
                lock,
                output=output,
                template=template,
                path=substituted,
            )
        by_path[substituted] = lock
        paths[label] = Path(substituted)
    return paths


def _for_target(lock_input: LockInput, label: str) -> LockInput:
    """Narrow ``lock_input`` to the one target ``label`` names."""
    return replace(lock_input, targets={label: lock_input.targets[label]})


class _RenderedFile(NamedTuple):
    """One tuple's requirements text and the file it belongs in."""

    path: Path
    label: str
    text: str
    pin_count: int


def _write_requirements_files(rendered: list[_RenderedFile]) -> None:
    """Write every rendered tuple to its path, or none of them.

    Each text is staged beside its destination and moved into place only
    once every stage is on disk, so an unwritable path fails before any
    file has been replaced.
    """
    staged: list[tuple[Path, _RenderedFile]] = []
    try:
        for item in rendered:
            tmp = _stage_path(item.path)
            staged.append((tmp, item))
            tmp.write_text(item.text, encoding="utf-8")
            tmp.chmod(_destination_mode(item.path))
    except OSError:
        for tmp, _ in staged:
            _discard(tmp)
        raise

    for tmp, item in staged:
        tmp.replace(item.path)
        _cli.printer().done(
            f"Wrote {item.path} ({item.pin_count} packages, tuple {item.label})"
        )


def _stage_path(output: Path) -> Path:
    """Create an empty file beside ``output`` to stage its text in.

    Staging in the destination's directory keeps the later rename on one
    filesystem, so it is atomic.  A directory at ``output`` is refused
    here because the rename would only refuse it once earlier files had
    already been replaced.
    """
    if output.is_dir():
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(output))
    fd, name = tempfile.mkstemp(
        dir=output.parent, prefix=f"{output.name}.", suffix=".tmp"
    )
    os.close(fd)
    return Path(name)


def _destination_mode(output: Path) -> int:
    """Permissions to give a staged file before it replaces ``output``.

    ``mkstemp`` creates at 0600, so a staged file needs the mode the
    destination already has, or the one a fresh write would have given it.
    """
    try:
        return stat.S_IMODE(output.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def _discard(staged: Path) -> None:
    """Remove a staged file, ignoring a failure to remove it."""
    with contextlib.suppress(OSError):
        staged.unlink()


def _render_requirements_or_exit(
    lock_input: LockInput,
    with_hashes: bool,  # noqa: FBT001 - internal, one call shape
    *,
    output_path: Path | None = None,
) -> tuple[str, int]:
    """Render the requirements text and pin count, exiting on a missing hash.

    The emitter does the writing when ``output_path`` is given, so the
    text lands through a temp file and a rename rather than truncating
    the destination first.
    """
    try:
        if with_hashes:
            text = _cli.write_requirements_with_hashes(
                lock_input, output_path=output_path
            )
        else:
            text = _cli.write_requirements_without_hashes(
                lock_input, output_path=output_path
            )
    except _cli.MissingHashError as e:
        _cli.printer().error(f"cannot lock: {e}")
        sys.exit(1)
    return text, sum(len(lock.pins) for lock in lock_input.targets.values())


def _read_selection_table_or_exit(
    path: Path,
    reader: Callable[[Path], Mapping[str, object]],
) -> Mapping[str, object]:
    """Read the table a selection flag expands over, exiting 1 on a bad file.

    ``nab download`` selects groups and extras before it loads the config, so
    this read is the first to touch the pyproject and reports a bad file itself.
    """
    try:
        return reader(path)
    except OSError:
        reason = "is a directory" if path.is_dir() else "not found"
        _cli.printer().error(f"{path} {reason}")
        sys.exit(1)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        _cli.printer().error(f"{path} is not valid TOML: {e}")
        sys.exit(1)
    except TypeError as e:
        _cli.printer().error(f"in {path}: {e}")
        sys.exit(1)


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
        _cli.printer().error("--all-groups and --groups are mutually exclusive")
        sys.exit(1)
    if not (all_groups or groups):
        return ()

    defined = _read_selection_table_or_exit(path, read_pyproject_groups)
    return tuple(defined.keys()) if all_groups else tuple(dict.fromkeys(groups))


def resolve_extra_selection(
    path: Path,
    *,
    extras: tuple[str, ...],
    all_extras: bool,
) -> tuple[str, ...]:
    """Return the canonical, deduplicated extras selection for this run."""
    if all_extras and extras:
        _cli.printer().error("--all-extras and --extras are mutually exclusive")
        sys.exit(1)
    if not (all_extras or extras):
        return ()

    defined = _read_selection_table_or_exit(path, read_pyproject_optional_dependencies)
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
        # argv[0] is a path into the checkout or venv, not the program name.
        command_line=("nab", *sys.argv[1:]),
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
            _cli.printer().note(
                "--upgrade re-anchored the resolve window to"
                f" {fresh.isoformat()}, dropping the cutoff {prior.isoformat()}"
                " recorded in the existing lockfile."
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
    _cli.printer().error(
        f"output file name {output.name!r} must match 'pylock.toml'"
        " or 'pylock.<name>.toml' per PEP 751 (note the dot separator,"
        f" not a hyphen).  Try {suggestion!r}."
    )
    sys.exit(1)
