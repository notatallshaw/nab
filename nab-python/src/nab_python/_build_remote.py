"""BUILD_REMOTE path: fetch, extract, and build a remote sdist.

The half of the remote-build rung that touches the world.  The provider picks
the sdist and checks what the build declared;
:func:`build_remote_sdist` is what fetches the archive, extracts it into scratch
space and hands the tree to a :pep:`517` backend.

Reached only through
:meth:`~nab_python.fetch_port.FetchPort.request_built_metadata`, so a host that
builds sdists its own way runs none of it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from nab_index.client import extract_sdist_archive

from .errors import UnsupportedSdistError

if TYPE_CHECKING:
    from .config import NabProjectConfig
    from .fetch_port import FetchPort
    from .metadata import WheelMetadata


def build_remote_sdist(
    port: FetchPort,
    package: str,
    version: str,
    url: str,
    sdist_hashes: tuple[tuple[str, str], ...],
    build_config: NabProjectConfig | None,
) -> WheelMetadata:
    """Download the sdist at ``url``, extract it, and build it.

    ``package`` is the canonical package name and ``version`` its string form,
    which together key the archive slot in the store.  A fetch that recorded an
    integrity failure re-raises it; a fetch that produced nothing, an archive
    that will not extract, and a backend that fails all raise
    :class:`~nab_python.errors.UnsupportedSdistError`.
    """
    # Imported in-function so tests can patch the module attribute, and to
    # keep ``_build.runner`` (and the ``build`` package behind it) off the
    # import path of a resolve that never invokes a backend.  Hoisting it
    # would also close the resolve-builds-resolve loop described in
    # :func:`nab_python._build.env.NabBuildEnv._resolve_and_download`.
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

    with tempfile.TemporaryDirectory(
        prefix="nab-build-remote-", ignore_cleanup_errors=True
    ) as td:
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
