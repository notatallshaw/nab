"""The resolve step ``nab lock`` and ``nab download`` share.

Group and extra selection, the transport a run fetches over, the
project config load, and the resolve itself with the ladder that maps
its failures to exit codes.

Only those two command modules import this, which is what keeps the
resolver and the lockfile readers off ``nab cache`` and ``nab config``.
"""

from __future__ import annotations

import gc
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

import tomli

from nab_project.lockfile import MissingHashError, MissingSdistError
from nab_project.pyproject_files import (
    read_pyproject_groups,
    read_pyproject_optional_dependencies,
)
from nab_project.resolve import resolve_for_targets
from nab_project.workspace import WorkspaceDiscoveryError
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
from nab_provider.target import (
    IntractableMarkerError,
    NonIntervalMarkerError,
    UnevaluableMarkerError,
)
from nab_resolver.errors import ResolutionError

from ._run import require_pyproject_file
from .config.model import (
    ConfigError,
    NabProjectConfig,
    ResolveMode,
    plan_targets,
    read_pyproject_config,
    with_python_override,
)
from .output import Verbosity, printer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from datetime import datetime
    from pathlib import Path

    from nab_index.transport import AsyncHttpTransport, HttpResponse
    from nab_project.resolve import ResolveResult
    from nab_provider.provider import ResolutionStrategy

    from .output import ProgressReporter


def _read_selection_table_or_exit(
    path: Path,
    reader: Callable[[Path], Mapping[str, object]],
) -> Mapping[str, object]:
    """Read the table a selection flag expands over, exiting 1 on a bad file.

    ``nab download`` selects groups and extras before it loads the config, so
    this read can be the first to touch the pyproject and runs the path guards
    itself rather than relying on the config load having run.
    """
    require_pyproject_file(path)

    try:
        return reader(path)
    except OSError as e:
        printer().error(f"cannot read {path}: {e}")
        sys.exit(1)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        printer().error(f"{path} is not valid TOML: {e}")
        sys.exit(1)
    except TypeError as e:
        printer().error(f"in {path}: {e}")
        sys.exit(1)


def resolve_group_selection(
    path: Path,
    *,
    groups: tuple[str, ...],
    all_groups: bool,
) -> tuple[str, ...]:
    """Return the canonical, deduplicated group selection for this run.

    ``groups`` is the list the line gave.  ``all_groups`` overrides it:
    when set, every group defined in the project's ``[dependency-groups]``
    table is selected.  An ``--all-groups`` paired with a non-empty
    ``--groups`` list raises a clean error rather than silently preferring
    one over the other.
    """
    if all_groups and groups:
        printer().error("--all-groups and --groups are mutually exclusive")
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
        printer().error("--all-extras and --extras are mutually exclusive")
        sys.exit(1)
    if not (all_extras or extras):
        return ()

    defined = _read_selection_table_or_exit(path, read_pyproject_optional_dependencies)
    return tuple(defined.keys()) if all_extras else tuple(dict.fromkeys(extras))


def _make_urllib3_transport() -> AsyncHttpTransport:
    """Return a urllib3 transport, importing urllib3 and truststore to build it."""
    from nab_index.urllib3_async_transport import (  # noqa: PLC0415
        Urllib3AsyncTransport,
    )

    return Urllib3AsyncTransport()


def _make_transport(backend: str) -> AsyncHttpTransport:
    """Return the transport for ``backend``.

    Importing either transport module loads its HTTP library and truststore,
    so both imports stay local: the CLI itself needs neither, and httpx is an
    optional extra a urllib3-only install will not have.
    """
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

    return _make_urllib3_transport()


class _DeferredUrllib3Transport:
    """Transport that builds a urllib3 transport on the first request.

    Building one imports urllib3 and truststore, which a resolve answered
    entirely from the cache never needs.
    """

    def __init__(self) -> None:
        self._transport: AsyncHttpTransport | None = None

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        if self._transport is None:
            self._transport = _make_urllib3_transport()
        return await self._transport.get(url, headers=headers)

    async def aclose(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()


def _make_resolve_transport(backend: str, *, offline: bool) -> AsyncHttpTransport:
    """Return the transport for a resolve on ``backend``.

    An offline resolve is served from the cache and asks for no URL, so the
    urllib3 transport is built only if something does. httpx is built up front
    either way: a missing httpx exits the CLI, which has to happen on the main
    thread before the resolve starts.
    """
    if offline and backend == "urllib3":
        return _DeferredUrllib3Transport()
    return _make_transport(backend)


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
        printer().error(str(exc))
        sys.exit(1)
    except WorkspaceDiscoveryError as exc:
        printer().error(f"workspace discovery error: {exc}")
        sys.exit(1)


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


@contextmanager
def _collector_paused() -> Iterator[None]:
    """Disable the cyclic collector for the duration of the resolve.

    Exit enables the collector and unfreezes rather than restoring what it
    found, so every caller of :func:`_resolve` is left in that state and not
    the one it came in with.

    Everything the resolve allocated is in generation 0 by then, so the
    collections that follow the enable walk the whole resolve graph. Freezing
    empties every generation into the permanent one and unfreezing returns the
    permanent generation to generation 2, which takes the graph out of
    generation 0 and leaves those collections less to walk. PyPy has no
    ``gc.freeze``.
    """
    gc.disable()
    try:
        yield
    finally:
        if hasattr(gc, "freeze"):
            gc.freeze()
            gc.unfreeze()
        gc.enable()


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
            targets = plan_targets(config)
            with _collector_paused():
                result = resolve_for_targets(
                    path,
                    transport,
                    targets=targets,
                    inputs=config.resolve_inputs(),
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
        printer().error(f"resolution failed: {_error_text(e)}")
        sys.exit(1)
    except (
        UnsupportedVcsError,
        MissingExtraError,
        SiblingMetadataDivergenceError,
        SourceNameMismatchError,
        NonIntervalMarkerError,
        UnevaluableMarkerError,
        IntractableMarkerError,
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
        printer().error(str(e))
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

    A resolve with one target has one error, and it is the run's error.  A
    resolve with several (a matrix's tuples, or a conflict fork's members,
    which fork in specific mode too) has one error per target, and the pins
    that did resolve are as informative as the failures, so each target gets
    a labelled block under the same ``error: resolution failed:`` line.
    """
    if len(result.target_results) <= 1:
        first = next(tr.error for tr in result.every_result if tr.error is not None)
        printer().error(f"resolution failed: {_error_text(first)}")
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

    printer().error("resolution failed:\n" + "\n".join(blocks))


def _error_lines(error: ResolutionError | None) -> list[str]:
    """Render a failed target's error as commented block lines."""
    text = f"{type(error).__name__}: {_error_text(error)}" if error is not None else ""
    return [f"#   {line}" for line in text.splitlines()]


def _error_text(error: ResolutionError) -> str:
    """Return the error at the depth the run's verbosity asks for.

    A resolution failure carries two renderings of its ``Diagnostics:``
    section: one line per package by default, and each package's clauses
    and ``note:`` at ``-v``.  An error nothing augmented carries only the
    one.
    """
    if printer().verbosity >= Verbosity.VERBOSE and error.verbose_message is not None:
        return error.verbose_message
    return str(error)
