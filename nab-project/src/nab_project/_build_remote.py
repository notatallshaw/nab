"""BUILD_REMOTE path: fetch, extract, and build a remote sdist.

The provider picks the sdist and checks what the build declared;
:func:`build_remote_sdist` does the rest, ending at a :pep:`517` backend run
over a tree in scratch space.

Reached only through
:meth:`~nab_provider.fetch_port.FetchPort.request_built_metadata`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from nab_index.client import extract_sdist_archive
from nab_provider.errors import UnsupportedSdistError

if TYPE_CHECKING:
    from nab_provider.fetch_port import FetchPort
    from nab_provider.metadata import WheelMetadata

    from .inputs import ResolveInputs


def build_remote_sdist(
    port: FetchPort,
    package: str,
    version: str,
    url: str,
    sdist_hashes: tuple[tuple[str, str], ...],
    build_config: ResolveInputs | None,
) -> WheelMetadata:
    """Download the sdist at ``url``, extract it, and build it.

    ``package`` is the canonical package name and ``version`` its string form,
    which together key the archive slot in the store.  An integrity failure the
    fetch recorded is re-raised; every other failure raises
    :class:`~nab_provider.errors.UnsupportedSdistError`.
    """
    # Imported in-function so tests can patch the module attribute, and to keep
    # ``_build.runner`` (and the ``build`` package behind it) off the import
    # path of a resolve that never invokes a backend.
    from . import build_backend
    from .build_backend import BuildBackendError

    event = port.request_sdist_archive(package, version, url, sdist_hashes)
    event.wait()

    integrity_error = port.index.get_sdist_archive_error(package, version)
    if integrity_error is not None:
        raise integrity_error

    data = port.index.get_sdist_archive(package, version)
    if data is None:
        msg = (
            f"{package}=={version} build-remote requested but sdist archive"
            f" fetch from {url} failed"
        )
        raise UnsupportedSdistError(msg)

    try:
        scratch = tempfile.TemporaryDirectory(
            prefix="nab-build-remote-", ignore_cleanup_errors=True
        )
    except OSError as exc:
        msg = (
            f"{package}=={version} build-remote could not create a temporary"
            f" build directory: {exc}"
        )
        raise UnsupportedSdistError(msg) from exc

    with scratch as td:
        try:
            source_dir = extract_sdist_archive(data, Path(td))
        except ValueError as exc:
            msg = f"{package}=={version} sdist archive could not be extracted: {exc}"
            raise UnsupportedSdistError(msg) from exc

        try:
            return build_backend.extract_metadata(
                source_dir,
                config=build_config,
                offline=port.offline,
            )
        except BuildBackendError as exc:
            msg = f"{package}=={version} build-remote backend failed: {exc}"
            raise UnsupportedSdistError(msg) from exc
