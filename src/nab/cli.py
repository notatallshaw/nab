"""Entry point for the nab command.

Holds the tyro :class:`SubcommandApp` registration plus the helpers
shared between :mod:`nab._lock` and :mod:`nab._download`: HTTP
transport selection, cache-directory defaults, config loading, and
the resolver-error-to-exit-code translation.

The two subcommands live in :mod:`nab._lock` and :mod:`nab._download`;
this module imports them so their ``@app.command`` decorators run
before :func:`main` calls ``app.cli()``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import tomli
import tyro
from tyro.extras import SubcommandApp

from nab._version import __version__
from nab_index.transport import HttpError
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_python.config import (
    ConfigError,
    NabProjectConfig,
    read_pyproject_config,
)
from nab_python.download import download_lock  # noqa: F401 - re-exported for tests
from nab_python.lockfile import (
    MissingHashError,
    MissingSdistError,
    write_lock,  # noqa: F401 - re-exported for tests
    write_requirements_with_hashes,  # noqa: F401 - re-exported for tests
    write_requirements_without_hashes,  # noqa: F401 - re-exported for tests
)
from nab_python.provider import UnsupportedVcsError
from nab_python.requirements_file import InvalidProjectRequirementError
from nab_python.resolve import (
    resolve_pyproject,
    resolve_universal_pyproject,
)
from nab_python.universal.resolve import (
    merge_universal_lock_inputs,  # noqa: F401 - re-exported for tests
)
from nab_python.workspace import WorkspaceDiscoveryError
from nab_resolver.resolver import ResolutionError

if TYPE_CHECKING:
    from datetime import datetime

    from nab_index.transport import AsyncHttpTransport
    from nab_python.provider import ResolutionStrategy
    from nab_python.resolve import ResolutionResult
    from nab_python.universal.resolve import UniversalResult

__all__ = [
    "main",
]


# A pyproject.toml positional that may also be omitted to default to ./pyproject.toml.
PathArg = Annotated[Path, tyro.conf.Positional]

# Lowercase Literal types so --http-backend and --format render lowercase
# choices in --help rather than the uppercase enum names.
HttpBackend = Literal["urllib3", "httpx"]
LockFormat = Literal["pylock", "requirements", "requirements-without-hashes"]
ResolutionFlag = Literal["highest", "lowest", "lowest-direct"]

_DEFAULT_OUTPUT: dict[str, str] = {
    "pylock": "pylock.toml",
    "requirements": "requirements.txt",
    "requirements-without-hashes": "requirements.txt",
}

TUPLE_TEMPLATE_VARS = ("{python_version}", "{platform_id}")

# Conventional KeyboardInterrupt exit code: 128 + SIGINT(2).
_SIGINT_EXIT_CODE = 130

app = SubcommandApp()


def _make_transport(backend: HttpBackend) -> AsyncHttpTransport:
    # httpx is an optional extra; import lazily so a urllib3-only
    # install doesn't need it.
    if backend == "httpx":
        try:
            from nab_index.httpx_async_transport import (  # noqa: PLC0415
                HttpxAsyncTransport,
            )
        except ImportError:
            sys.stderr.write(
                "Error: httpx is not installed; run `pip install nab[httpx]`\n"
            )
            sys.exit(1)
        return HttpxAsyncTransport()

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


def _load_config(
    path: Path,
    *,
    discover_workspace: bool = True,
    anchor: datetime | None = None,
) -> NabProjectConfig:
    if not path.exists():
        sys.stderr.write(f"Error: {path} not found\n")
        sys.exit(1)

    try:
        return read_pyproject_config(
            path, discover_workspace=discover_workspace, anchor=anchor
        )
    except tomli.TOMLDecodeError as exc:
        sys.stderr.write(f"Error: {path} is not valid TOML: {exc}\n")
        sys.exit(1)
    except ConfigError as exc:
        sys.stderr.write(f"Error in [tool.nab]: {exc}\n")
        sys.exit(1)
    except WorkspaceDiscoveryError as exc:
        sys.stderr.write(f"Workspace discovery error: {exc}\n")
        sys.exit(1)


def is_stdout(output: Path | None) -> bool:
    return output is not None and str(output) == "-"


def _resolve_specific(  # noqa: PLR0913 - one wrapper per resolve_pyproject kwarg
    path: Path,
    *,
    config: NabProjectConfig,
    cache_dir: Path | None,
    offline: bool,
    transport: AsyncHttpTransport,
    failure_prefix: str,
    groups: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
    resolution_strategy: ResolutionStrategy | None = None,
) -> ResolutionResult:
    """Run the single-environment resolver and translate errors to exits."""
    try:
        return resolve_pyproject(
            path,
            transport,
            config=config,
            cache_dir=cache_dir,
            offline=offline,
            groups=groups,
            extras=extras,
            resolution_strategy=resolution_strategy,
        )
    except ResolutionError as e:
        sys.stderr.write(f"Resolution failed: {e}\n")
        sys.exit(1)
    except UnsupportedVcsError as e:
        sys.stderr.write(f"{failure_prefix}: {e}\n")
        sys.exit(1)
    except KeyError:
        sys.stderr.write(f"Error: {path} has no [project].dependencies\n")
        sys.exit(1)
    except InvalidProjectRequirementError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
    except LookupError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
    except (MissingHashError, MissingSdistError) as e:
        sys.stderr.write(f"{failure_prefix}: {e}\n")
        sys.exit(1)
    except NotImplementedError as e:
        sys.stderr.write(f"{failure_prefix}: {e}\n")
        sys.exit(1)
    except ConfigError as e:
        sys.stderr.write(f"Error in [tool.nab]: {e}\n")
        sys.exit(1)
    except HttpError as e:
        sys.stderr.write(f"{failure_prefix}: {e}\n")
        sys.exit(1)


def _resolve_universal(
    path: Path,
    *,
    config: NabProjectConfig,
    cache_dir: Path | None,
    offline: bool,
    transport: AsyncHttpTransport,
    groups: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
    resolution_strategy: ResolutionStrategy | None = None,
) -> UniversalResult:
    """Run the universal resolver, translating errors to exits.

    On a partial failure (any tuple unresolved) the per-tuple blocks
    are printed and the process exits 1, so a returned result is
    always fully successful.
    """
    try:
        result = resolve_universal_pyproject(
            path,
            config=config,
            cache_dir=cache_dir,
            transport=transport,
            offline=offline,
            groups=groups,
            extras=extras,
            resolution_strategy=resolution_strategy,
        )
    except KeyError:
        sys.stderr.write(f"Error: {path} has no [project].dependencies\n")
        sys.exit(1)
    except InvalidProjectRequirementError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
    except LookupError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
    except HttpError as e:
        sys.stderr.write(f"Cannot lock: {e}\n")
        sys.exit(1)

    if not result.success:
        _print_universal_blocks(result)
        sys.exit(1)
    return result


def _print_universal_blocks(result: UniversalResult) -> None:
    """Write per-tuple pin blocks (with FAILED markers) to stdout."""
    blocks: list[str] = []
    for tr in result.tuple_results:
        label = tr.tuple_.label
        if not tr.success:
            blocks.append(f"# {label}: FAILED")
            blocks.extend(f"#   {raw}" for raw in (tr.error or "").splitlines())
            continue
        blocks.append(f"# {label}")
        blocks.extend(f"{name}=={tr.pins[name]}" for name in sorted(tr.pins))
    sys.stdout.write("\n".join(blocks) + "\n")


# Side-effect imports: each module's @app.command decorators register the
# subcommand.  Placed at the bottom so helpers above bind before
# nab._lock / nab._download import back from this module.
from . import _download as _download_module  # noqa: E402, F401 - side-effect
from . import _lock as _lock_module  # noqa: E402, F401 - side-effect


def main() -> None:
    """Entry point for the nab command."""
    # Tyro's SubcommandApp does not surface a global ``--version`` flag,
    # so the check runs before ``app.cli()`` parses the sub-command.
    argv = sys.argv[1:]
    if argv and argv[0] in {"--version", "-V"}:
        sys.stdout.write(f"nab {__version__}\n")
        return

    try:
        app.cli(prog="nab")
    except KeyboardInterrupt:
        sys.stderr.write("Aborted.\n")
        sys.exit(_SIGINT_EXIT_CODE)
