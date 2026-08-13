"""Materialising the local, VCS and archive sources a project declares.

The half of source handling that touches the world: it reads a directory,
clones a repository, downloads and extracts an archive, and hands the tree to a
:pep:`517` backend when the static read yields nothing and the policy permits
it.  :mod:`nab_provider._provider.sources` keeps the other half, which validates
the declarations and turns one materialised tree into a synthetic listing.

Reached only through
:meth:`~nab_provider.fetch_port.FetchPort.request_source_listing`, so a host that
owns its own source handling runs none of it.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from nab_index import vcs
from nab_index.client import extract_sdist_archive, verify_sdist_hash
from nab_provider.archive import ArchiveRequest
from nab_provider.errors import SourceBuildPolicyError, UnsupportedSdistError
from nab_provider.policy import (
    BuildPolicy,
    LocalSource,
    SourceMaterialization,
    VcsSource,
)
from nab_provider.vcs_request import VcsCloneError, VcsRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from nab_provider.fetch_port import FetchPort
    from nab_provider.metadata import WheelMetadata
    from nab_provider.policy import ArchiveSource, SourceRequest

    from .config import NabProjectConfig

# The archive names everything under _TREE_DIR, so the cache's own bookkeeping
# sits beside that directory rather than inside it.
_TREE_DIR = "tree"
_COMPLETE_MARKER = ".nab-complete"
_HASHES_MARKER = ".nab-hashes"


class _SourceCopyError(Exception):
    """Raised when a cached source cannot be copied for a backend build."""


class _CopyWithHardlinks:
    """Copy regular files while retaining source hard-link groups."""

    def __init__(self) -> None:
        self._destinations: dict[tuple[int, int], Path] = {}

    def __call__(self, source: str, destination: str) -> str:
        source_path = Path(source)
        source_stat = source_path.stat(follow_symlinks=False)
        if source_stat.st_nlink <= 1 or not stat.S_ISREG(source_stat.st_mode):
            return shutil.copy2(source_path, destination)

        key = (source_stat.st_dev, source_stat.st_ino)
        existing = self._destinations.get(key)
        if existing is None:
            copied = shutil.copy2(source_path, destination)
            self._destinations[key] = Path(copied)
            return copied

        os.link(existing, destination)
        return destination


@contextmanager
def _source_for_build(path: Path, persistent_root: Path | None) -> Iterator[Path]:
    """Yield the matching path in a disposable copy of a persistent source tree."""
    if persistent_root is None:
        yield path
        return

    relative_path = path.relative_to(persistent_root)
    try:
        temporary = tempfile.TemporaryDirectory(
            prefix="nab-source-build-", ignore_cleanup_errors=True
        )
    except OSError as exc:
        msg = f"could not create a temporary build tree for {persistent_root}: {exc}"
        raise _SourceCopyError(msg) from exc

    with temporary as temporary_path:
        try:
            build_root = shutil.copytree(
                persistent_root,
                Path(temporary_path) / persistent_root.name,
                symlinks=True,
                copy_function=_CopyWithHardlinks(),
            )
        except OSError as exc:
            msg = f"could not copy cached source tree at {persistent_root}: {exc}"
            raise _SourceCopyError(msg) from exc
        yield build_root / relative_path


def materialize_source(
    port: FetchPort,
    request: SourceRequest,
    build_config: NabProjectConfig | None,
) -> SourceMaterialization:
    """Materialise ``request``'s declared source and read its metadata.

    Dispatches on the declaration's own type: a directory is read where it
    stands, a repository is cloned, an archive is downloaded and extracted.
    All three end at the same directory read, so the kinds differ only in how
    the directory comes to exist.
    """
    source = request.source
    if isinstance(source, LocalSource):
        return _materialize_local(request, source, build_config, port=port)
    if isinstance(source, VcsSource):
        return _materialize_vcs(request, source, build_config, port=port)
    return _materialize_archive(request, source, build_config, port=port)


def _materialize_local(
    request: SourceRequest,
    source: LocalSource,
    build_config: NabProjectConfig | None,
    *,
    port: FetchPort,
) -> SourceMaterialization:
    """Read metadata from the directory ``source`` names."""
    path = Path(source.path)
    if source.subdirectory:
        path = path / source.subdirectory
    metadata = extract_source_metadata(
        path,
        descriptor=source.descriptor,
        policy=request.build_policy,
        kind="local",
        offline=port.offline,
        build_config=build_config,
    )
    return SourceMaterialization(path=path, metadata=metadata, commit_sha=None)


def _materialize_vcs(
    request: SourceRequest,
    source: VcsSource,
    build_config: NabProjectConfig | None,
    *,
    port: FetchPort,
) -> SourceMaterialization:
    """Clone ``source`` and read the checkout the same way as a directory."""
    if request.vcs_cache_dir is None:
        msg = (
            f"vcs source {source.name!r} declared but no"
            f" vcs_cache_dir was supplied to Provider"
        )
        raise UnsupportedSdistError(msg)
    try:
        parsed = VcsRequest.parse(source.url)
        clone = vcs.prepare_clone(
            request.vcs_cache_dir,
            parsed,
            require_pin=request.require_pin,
            offline=port.offline,
        )
    except VcsCloneError as exc:
        msg = f"vcs source {source.name!r}: {exc}"
        raise UnsupportedSdistError(msg) from exc

    # The cache dir can be relative, and a file URI needs an absolute path.
    root = clone.path.resolve()
    path = root / clone.subdirectory if clone.subdirectory else root
    metadata = extract_source_metadata(
        path,
        descriptor=source.descriptor,
        policy=request.build_policy,
        kind="vcs",
        offline=port.offline,
        build_config=build_config,
        persistent_root=root,
    )
    return SourceMaterialization(
        path=path, metadata=metadata, commit_sha=clone.commit_sha
    )


def extract_source_metadata(
    path: Path,
    *,
    descriptor: str,
    policy: BuildPolicy,
    kind: str,
    offline: bool,
    build_config: NabProjectConfig | None,
    persistent_root: Path | None = None,
) -> WheelMetadata:
    """Read metadata from a directory; gates the backend path on ``policy``.

    ``kind`` is ``"local"`` for :class:`LocalSource` directories (admitted at
    :attr:`BuildPolicy.BUILD_LOCAL` and above); ``"vcs"`` for :class:`VcsSource`
    clones and ``"archive"`` for extracted :class:`ArchiveSource` trees both
    build only at :attr:`BuildPolicy.BUILD_REMOTE`, like a remote sdist.

    ``persistent_root`` identifies the complete cached clone or archive tree.
    Dynamic builds receive a disposable copy of that root; local-source builds
    keep using the caller's path.

    An unreadable ``pyproject.toml`` is reported as a read failure at every
    policy level: the build path cannot read it either, so calling it dynamic
    metadata would blame the policy for a permission error.
    """
    # Imported in-function so tests can patch the module attribute, and to
    # keep ``_build.runner`` (and the ``build`` package behind it) off the
    # import path of a resolve that never invokes a backend.  Hoisting it
    # would also close the resolve-builds-resolve loop described in
    # :func:`nab_python._build.env.NabBuildEnv._resolve_and_download`.
    from . import build_backend
    from .build_backend import BuildBackendError, extract_static_metadata

    try:
        metadata = extract_static_metadata(path)
    except BuildBackendError as exc:
        msg = f"{descriptor}: {exc}"
        raise UnsupportedSdistError(msg) from exc

    if metadata is not None:
        return metadata

    if kind == "local":
        allowed = {BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE}
        minimum = BuildPolicy.BUILD_LOCAL
    else:
        allowed = {BuildPolicy.BUILD_REMOTE}
        minimum = BuildPolicy.BUILD_REMOTE
    if policy not in allowed:
        msg = (
            f"{descriptor} at {path} has dynamic metadata; building requires"
            f" build-policy '{minimum.value}' but the effective policy is"
            f" '{policy.value}'"
        )
        raise SourceBuildPolicyError(msg)
    try:
        with _source_for_build(path, persistent_root) as build_path:
            return build_backend.extract_metadata(
                build_path,
                config=build_config,
                offline=offline,
            )
    except (BuildBackendError, _SourceCopyError) as exc:
        msg = f"{descriptor}: {exc}"
        raise UnsupportedSdistError(msg) from exc


def _fetch_archive_bytes(
    package: str,
    source: ArchiveSource,
    request: ArchiveRequest,
    *,
    port: FetchPort,
) -> bytes:
    """Return the hash-verified bytes of ``source``'s archive.

    Raises before returning if the fetch recorded a failure, produced no
    bytes, or the bytes fail their hash.  The port reads the declared URL
    without verifying it, so every declared hash is checked here.
    """
    digest = request.hashes[0][1]
    index = port.index

    event = port.request_direct_archive(package, digest, request.url)
    event.wait()

    failure = index.get_sdist_archive_error(package, digest)
    if failure is not None:
        msg = f"archive source {source.name!r}: {failure}"
        raise UnsupportedSdistError(msg) from failure

    data = index.get_sdist_archive(package, digest)
    if data is None:
        msg = f"archive source {source.name!r}: download from {request.url} failed"
        raise UnsupportedSdistError(msg)

    for pinned_hash in request.hashes:
        verify_sdist_hash(data, pinned_hash)

    return data


def _prepare_archive_tree(
    request: SourceRequest,
    source: ArchiveSource,
    *,
    port: FetchPort,
) -> tuple[Path, ArchiveRequest]:
    """Return the extracted tree's root and the parsed request for ``source``.

    The cached tree is used with no download, offline runs included, only when
    the record left at extraction covers every hash this resolve declares.
    Otherwise the archive is downloaded and checked against the whole
    declaration, so adding a hash re-verifies rather than trusting the tree.
    """
    cache_dir = request.archive_cache_dir
    if cache_dir is None:
        msg = (
            f"archive source {source.name!r} declared but no"
            f" archive_cache_dir was supplied to Provider"
        )
        raise UnsupportedSdistError(msg)

    parsed = ArchiveRequest.parse(source.url)

    # The version is unknown until the tree is extracted, so key the cache by
    # the first declared digest: unique and known up-front.  The fragment
    # only carries accepted algorithms, so every pair is verifiable.
    digest = parsed.hashes[0][1]
    target = cache_dir / digest

    declared = set(parsed.hashes)
    if (target / _COMPLETE_MARKER).is_file() and declared <= _verified_hashes(target):
        return _extracted_root(target), parsed

    data = _fetch_archive_bytes(request.package, source, parsed, port=port)
    return _extract_archive(cache_dir, digest, data, parsed.hashes), parsed


def _verified_hashes(target: Path) -> set[tuple[str, str]]:
    """Return the hashes the tree at ``target`` was verified against.

    The record is written with the completion marker, so a tree with no record
    covers nothing and is refetched rather than trusted.
    """
    record = target / _HASHES_MARKER
    if not record.is_file():
        return set()

    lines = record.read_text(encoding="utf-8").splitlines()
    return {
        (algorithm, hex_digest)
        for algorithm, _, hex_digest in (line.partition("=") for line in lines)
    }


# Extraction (this function and _extract_archive) is excluded from coverage: a
# cold archive requires the PEP 706 tar data filter (Python 3.10.12+), but
# GitHub Actions setup-python installs 3.10.11 on macOS-arm64 and Windows
# (python.org stopped shipping 3.10 installers after 3.10.11, and
# actions/python-versions builds those OSes from installers), so no 3.10.12+
# artifact exists for them.  The extraction tests skip there, leaving these
# lines unhit on those two runners.  The cache-hit test and the
# download-and-verify guards live in _prepare_archive_tree and
# _fetch_archive_bytes above so they stay gated on every runner.  Remove the
# pragmas when that 3.10 cell is dropped, sourced from python-build-standalone,
# or 3.10 reaches EOL (2026-10).
def _materialize_archive(
    request: SourceRequest,
    source: ArchiveSource,
    build_config: NabProjectConfig | None,
    *,
    port: FetchPort,
) -> SourceMaterialization:  # pragma: no cover (tar data filter)
    """Materialise ``source`` from its extracted tree, downloading if needed.

    Every hash ``source`` declares is checked: against the downloaded bytes in
    :func:`_fetch_archive_bytes`, or against the record the extraction left when
    the cached tree is reused.  A tampered archive therefore fails the resolve
    loudly rather than being used and pinned unverified.  The extracted tree
    then takes the same path as a LocalSource.
    """
    root, parsed = _prepare_archive_tree(request, source, port=port)

    # Read the extracted tree's metadata as a local source and seed one candidate.
    path = root / parsed.subdirectory if parsed.subdirectory else root
    metadata = extract_source_metadata(
        path,
        descriptor=source.descriptor,
        policy=request.build_policy,
        kind="archive",
        offline=port.offline,
        build_config=build_config,
        persistent_root=root,
    )
    return SourceMaterialization(path=path, metadata=metadata, commit_sha=None)


def _extract_archive(
    cache_dir: Path,
    digest: str,
    data: bytes,
    verified: tuple[tuple[str, str], ...],
) -> Path:  # pragma: no cover (tar data filter; see _materialize_archive)
    """Extract ``data`` under ``cache_dir`` keyed by ``digest``; return the root.

    The archive's root is published at ``_TREE_DIR`` inside the entry, so no
    name the archive chose reaches the entry's top level.

    ``verified`` names the hashes the caller checked ``data`` against, recorded
    beside the completion marker so a later resolve reuses the tree only for a
    declaration those hashes cover.

    Idempotent, like :func:`prepare_clone`: a tree another run published
    between the caller's cache check and this call is reused rather than
    re-extracted.  A fresh extraction lands in a temporary sibling and is
    renamed into place once its completion marker is written, so the cache
    path never holds a partial tree.
    """
    target = cache_dir / digest
    marker = target / _COMPLETE_MARKER

    if not marker.is_file():
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=cache_dir, prefix=f"{digest}.", suffix=".tmp"))

        try:
            unpacked = tmp / "unpacked"
            unpacked.mkdir()

            try:
                root = extract_sdist_archive(data, unpacked)
            except ValueError as exc:
                msg = f"archive could not be extracted: {exc}"
                raise UnsupportedSdistError(msg) from exc

            # A flat archive's root is the extraction dir itself, so the move
            # takes that dir and leaves nothing behind to remove.
            root.replace(tmp / _TREE_DIR)
            with suppress(FileNotFoundError):
                unpacked.rmdir()

            record = "\n".join(
                f"{algorithm}={hex_digest}" for algorithm, hex_digest in verified
            )
            (tmp / _HASHES_MARKER).write_text(record, encoding="utf-8")
            (tmp / _COMPLETE_MARKER).touch()

            try:
                tmp.rename(target)
            except OSError as exc:
                # The cache path is taken.  A marker there means another run
                # got there first, so keep its tree; without one it is a partial
                # left by an interrupted run, so wipe it and retry the rename.
                if not marker.is_file():
                    shutil.rmtree(target, ignore_errors=True)
                    with suppress(OSError):
                        tmp.rename(target)
                if not marker.is_file():
                    msg = f"extracted archive could not be moved into place: {exc}"
                    raise UnsupportedSdistError(msg) from exc
        finally:
            # A successful rename leaves nothing here; any other exit, an
            # interrupt included, would leak the temp tree.
            shutil.rmtree(tmp, ignore_errors=True)

    return _extracted_root(target)


def _extracted_root(target: Path) -> Path:
    """Return the source root inside the extracted tree at ``target``."""
    # Resolve so the file URI works even for a relative cache dir.
    return (target / _TREE_DIR).resolve()
