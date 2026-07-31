"""Local-source, VCS-source, and archive-source materialisation.

A ``LocalSource`` becomes the only candidate for a package: PyPI is
not consulted.  A ``VcsSource`` clones the repo and an ``ArchiveSource``
downloads and hash-verifies a ``.tar.gz`` and extracts it; both reuse the
``LocalSource`` extraction path.  Each produces a single synthetic
``SdistFile`` whose version is read from ``[project].version``.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from nab_index.archive import ArchiveRequest
from nab_index.client import SdistFile, extract_sdist_archive, verify_sdist_hash
from nab_index.vcs import VcsCloneError, VcsRequest

from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from .._vendor.packaging.version import Version
    from ..metadata import WheelMetadata
    from ..provider import ArchiveSource, LocalSource, Provider, VcsSource

_EXTRACTED_MARKER = ".nab-extracted"
_HASHES_MARKER = ".nab-hashes"


def index_local_sources(
    provider: Provider,  # noqa: ARG001  (signature parity with index_vcs_sources)
    sources: list[LocalSource],
) -> dict[str, LocalSource]:
    """Validate ``LocalSource`` entries and return a canonical-name map.

    Admitted at every :class:`~nab_python.provider.BuildPolicy` level; the
    policy only governs whether the backend may run when the static
    pyproject read returns nothing usable (see
    :func:`extract_source_metadata`).
    """
    if not sources:
        return {}
    out: dict[str, LocalSource] = {}
    for src in sources:
        canonical = canonicalize_name(src.name)
        if canonical in out:
            msg = f"duplicate local source for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out


def materialize_local_source(
    provider: Provider,
    normalized: str,
    source: LocalSource,
) -> list[tuple[Version, SdistFile]]:
    """Read metadata from ``source`` and seed caches with one synthetic version.

    Static path: ``extract_static_metadata`` reads ``pyproject.toml``
    directly.  Backend path: requires :attr:`BuildPolicy.BUILD_LOCAL`
    or looser; raises :class:`UnsupportedSdistError` otherwise.
    """
    path = Path(source.path)
    if source.subdirectory:
        path = path / source.subdirectory
    descriptor = f"local source {source.name!r}"
    metadata = extract_source_metadata(
        provider,
        path,
        descriptor=descriptor,
        package=canonicalize_name(source.name),
        kind="local",
    )
    return seed_synthetic_listing(provider, normalized, path, metadata, descriptor)


def extract_source_metadata(
    provider: Provider,
    path: Path,
    *,
    descriptor: str,
    package: str,
    kind: str,
) -> WheelMetadata:
    """Read metadata from a directory; gates the backend path on policy.

    ``kind`` is ``"local"`` for :class:`LocalSource` directories
    (admitted at :attr:`BuildPolicy.BUILD_LOCAL` and above); ``"vcs"``
    for :class:`VcsSource` clones and ``"archive"`` for extracted
    :class:`ArchiveSource` trees both build only at
    :attr:`BuildPolicy.BUILD_REMOTE`, like a remote sdist.
    """
    # Imported in-function so tests can patch the module attribute.
    from .. import build_backend
    from ..build_backend import BuildBackendError, extract_static_metadata
    from ..provider import BuildPolicy, UnsupportedSdistError

    metadata = extract_static_metadata(path)
    if metadata is not None:
        return metadata
    effective = provider.effective_build_policy_for_source(package)
    if kind == "local":
        allowed = {BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE}
        minimum = BuildPolicy.BUILD_LOCAL
    else:
        allowed = {BuildPolicy.BUILD_REMOTE}
        minimum = BuildPolicy.BUILD_REMOTE
    if effective not in allowed:
        provider.stats.excluded_by_build_policy += 1
        msg = (
            f"{descriptor} at {path} has dynamic metadata; building requires"
            f" BuildPolicy.{minimum.name} but the effective policy is"
            f" {effective.value}"
        )
        raise UnsupportedSdistError(msg)
    try:
        return build_backend.extract_metadata(
            path,
            config=provider.build_config,
            offline=provider.coordinator.offline,
        )
    except BuildBackendError as exc:
        msg = f"{descriptor}: {exc}"
        raise UnsupportedSdistError(msg) from exc


def seed_synthetic_listing(
    provider: Provider,
    normalized: str,
    path: Path,
    metadata: WheelMetadata,
    descriptor: str,
) -> list[tuple[Version, SdistFile]]:
    """Produce a one-version listing for a materialised source."""
    # The source's own [project].name must canonicalise to the requested name;
    # otherwise it declares a different project, and pinning it here would carry
    # the wrong version and dependencies.
    actual = canonicalize_name(metadata.name)
    if actual != normalized:
        from ..provider import SourceNameMismatchError

        msg = (
            f"{descriptor} declares package {normalized!r} but its"
            f" [project].name is {actual!r} (at {path}); a source declared for"
            f" one name must not provide a different project"
        )
        raise SourceNameMismatchError(msg)

    synthetic_file = SdistFile(
        filename=f"{normalized}-{metadata.version}.tar.gz",
        url=path.as_uri(),
        version=str(metadata.version),
        requires_python=(
            str(metadata.requires_python)
            if metadata.requires_python is not None
            else None
        ),
        upload_time=None,
        local_path=path,
    )
    version = metadata.version
    provider.metadata_cache[(normalized, version)] = metadata
    return [(version, synthetic_file)]


def index_vcs_sources(
    provider: Provider,
    sources: list[VcsSource],
) -> dict[str, VcsSource]:
    """Validate VCS sources and return a canonical-name map.

    Admitted at every :class:`~nab_python.provider.BuildPolicy` level; the
    policy only governs whether the backend may run on the clone (see
    :func:`extract_source_metadata`).  ``VcsPolicy.BLOCK`` still refuses
    any declaration up-front because that is an independent decision
    about whether VCS fetching is permitted at all.

    Each URL is passed through :func:`admit_vcs_url` so the scheme,
    repo, and pin allowlists apply to ``[[tool.nab.vcs-sources]]``
    just like project-root direct-URL requirements.
    """
    # Late import: ``provider`` imports this module at module load.
    from .._vcs_admission import admit_vcs_url
    from ..provider import VcsPolicy

    if not sources:
        return {}

    if provider.vcs_config.policy is VcsPolicy.BLOCK:
        msg = (
            "vcs_sources require VcsPolicy.ALLOW; current policy is"
            f" {provider.vcs_config.policy.value}.  Set vcs_config to"
            " a permissive VcsConfig before declaring sources."
        )
        raise ValueError(msg)

    out: dict[str, VcsSource] = {}
    for src in sources:
        admit_vcs_url(src.url, provider.vcs_config)
        canonical = canonicalize_name(src.name)
        if canonical in out or canonical in provider.local_sources:
            msg = f"duplicate source declared for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out


def materialize_vcs_source(
    provider: Provider,
    normalized: str,
    source: VcsSource,
) -> list[tuple[Version, SdistFile]]:
    """Clone ``source`` and materialise it via the same path as a LocalSource."""
    # Imported in-function so tests can patch the module attribute.
    from nab_index import vcs as _vcs

    from ..provider import UnsupportedSdistError

    if provider.vcs_cache_dir is None:
        msg = (
            f"vcs source {source.name!r} declared but no"
            f" vcs_cache_dir was supplied to Provider"
        )
        raise UnsupportedSdistError(msg)
    try:
        request = VcsRequest.parse(source.url)
        clone = _vcs.prepare_clone(
            provider.vcs_cache_dir,
            request,
            require_pin=provider.vcs_config.require_pin,
            offline=provider.coordinator.offline,
        )
    except VcsCloneError as exc:
        msg = f"vcs source {source.name!r}: {exc}"
        raise UnsupportedSdistError(msg) from exc
    provider.vcs_pins[canonicalize_name(source.name)] = clone.commit_sha
    path = clone.path / clone.subdirectory if clone.subdirectory else clone.path
    descriptor = f"vcs source {source.name!r}"
    metadata = extract_source_metadata(
        provider,
        path,
        descriptor=descriptor,
        package=canonicalize_name(source.name),
        kind="vcs",
    )
    return seed_synthetic_listing(provider, normalized, path, metadata, descriptor)


def index_archive_sources(
    provider: Provider,
    sources: list[ArchiveSource],
) -> dict[str, ArchiveSource]:
    """Validate archive sources and return a canonical-name map.

    Admitted at every :class:`~nab_python.provider.BuildPolicy` level; the
    policy only governs whether the backend may run on the extracted tree
    (see :func:`extract_source_metadata`).  There is no ``VcsPolicy``-style
    gate: the download is hash-verified, and which archive URLs are
    permitted is decided at config parse.
    """
    if not sources:
        return {}
    out: dict[str, ArchiveSource] = {}
    for src in sources:
        # Guarantee a usable hash at the provider layer (config parse already
        # checks it, but a directly-built Provider would otherwise IndexError
        # when materialisation reads the first digest, or verify an empty one).
        if not ArchiveRequest.parse(src.url).has_usable_hash:
            msg = f"archive source {src.name!r} has no hash in its URL: {src.url!r}"
            raise ValueError(msg)

        canonical = canonicalize_name(src.name)
        if (
            canonical in out
            or canonical in provider.local_sources
            or canonical in provider.vcs_sources
        ):
            msg = f"duplicate source declared for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out


def _fetch_archive_bytes(
    provider: Provider,
    source: ArchiveSource,
    request: ArchiveRequest,
) -> bytes:
    """Return the hash-verified bytes of ``source``'s archive.

    Raises before returning if the fetch recorded a failure, produced no
    bytes, or the bytes fail their hash.  The coordinator reads the declared
    URL without verifying it, so every declared hash is checked here.
    """
    from ..provider import UnsupportedSdistError

    canonical = canonicalize_name(source.name)
    digest = request.hashes[0][1]
    index = provider.coordinator.index

    event = provider.coordinator.request_direct_archive(canonical, digest, request.url)
    event.wait()

    failure = index.get_sdist_archive_error(canonical, digest)
    if failure is not None:
        msg = f"archive source {source.name!r}: {failure}"
        raise UnsupportedSdistError(msg) from failure

    data = index.get_sdist_archive(canonical, digest)
    if data is None:
        msg = f"archive source {source.name!r}: download from {request.url} failed"
        raise UnsupportedSdistError(msg)

    for pinned_hash in request.hashes:
        verify_sdist_hash(data, pinned_hash)

    return data


def _prepare_archive_tree(
    provider: Provider,
    source: ArchiveSource,
) -> tuple[Path, ArchiveRequest]:
    """Return the extracted tree's root and the parsed request for ``source``.

    The cached tree is used with no download, offline runs included, only when
    the record left at extraction covers every hash this resolve declares.
    Otherwise the archive is downloaded and checked against the whole
    declaration, so adding a hash re-verifies rather than trusting the tree.
    """
    from ..provider import UnsupportedSdistError

    cache_dir = provider.archive_cache_dir
    if cache_dir is None:
        msg = (
            f"archive source {source.name!r} declared but no"
            f" archive_cache_dir was supplied to Provider"
        )
        raise UnsupportedSdistError(msg)

    request = ArchiveRequest.parse(source.url)

    # The version is unknown until the tree is extracted, so key the cache by
    # the first declared digest: unique and known up-front.  The fragment
    # only carries accepted algorithms, so every pair is verifiable.
    digest = request.hashes[0][1]
    target = cache_dir / digest

    declared = set(request.hashes)
    if (target / _EXTRACTED_MARKER).is_file() and declared <= _verified_hashes(target):
        return _extracted_root(target), request

    data = _fetch_archive_bytes(provider, source, request)
    return _extract_archive(cache_dir, digest, data, request.hashes), request


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
def materialize_archive_source(
    provider: Provider,
    normalized: str,
    source: ArchiveSource,
) -> list[tuple[Version, SdistFile]]:  # pragma: no cover (tar data filter)
    """Materialise ``source`` from its extracted tree, downloading if needed.

    Every hash ``source`` declares is checked: against the downloaded bytes in
    :func:`_fetch_archive_bytes`, or against the record the extraction left when
    the cached tree is reused.  A tampered archive therefore fails the resolve
    loudly rather than being used and pinned unverified.  The extracted tree
    then takes the same path as a LocalSource.
    """
    root, request = _prepare_archive_tree(provider, source)

    # Read the extracted tree's metadata as a local source and seed one candidate.
    path = root / request.subdirectory if request.subdirectory else root
    descriptor = f"archive source {source.name!r}"
    metadata = extract_source_metadata(
        provider,
        path,
        descriptor=descriptor,
        package=canonicalize_name(source.name),
        kind="archive",
    )
    return seed_synthetic_listing(provider, normalized, path, metadata, descriptor)


def _extract_archive(
    cache_dir: Path,
    digest: str,
    data: bytes,
    verified: tuple[tuple[str, str], ...],
) -> Path:  # pragma: no cover (tar data filter; see materialize_archive_source)
    """Extract ``data`` under ``cache_dir`` keyed by ``digest``; return the root.

    ``verified`` names the hashes the caller checked ``data`` against, recorded
    beside the completion marker so a later resolve reuses the tree only for a
    declaration those hashes cover.

    Idempotent, like :func:`prepare_clone`: a tree another run published
    between the caller's cache check and this call is reused rather than
    re-extracted.  A fresh extraction lands in a temporary sibling and is
    renamed into place once its completion marker is written, so the cache
    path never holds a partial tree.
    """
    from ..provider import UnsupportedSdistError

    target = cache_dir / digest
    marker = target / _EXTRACTED_MARKER

    if not marker.is_file():
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=cache_dir, prefix=f"{digest}.", suffix=".tmp"))

        try:
            try:
                root = extract_sdist_archive(data, tmp)
            except ValueError as exc:
                msg = f"archive could not be extracted: {exc}"
                raise UnsupportedSdistError(msg) from exc

            # An empty root name means a flat archive.  Compare against the
            # resolved temp dir, since extract_sdist_archive resolves symlinks
            # and relative cache dirs.
            root_name = root.name if root != tmp.resolve() else ""

            record = "\n".join(
                f"{algorithm}={hex_digest}" for algorithm, hex_digest in verified
            )
            (tmp / _HASHES_MARKER).write_text(record, encoding="utf-8")
            (tmp / _EXTRACTED_MARKER).write_text(root_name, encoding="utf-8")

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
    """Return the source root inside the extracted tree at ``target``.

    An empty marker records a flat archive, whose root is the tree itself.
    """
    root_name = (target / _EXTRACTED_MARKER).read_text(encoding="utf-8").strip()

    # Resolve so the file URI works even for a relative cache dir.
    base = target / root_name if root_name else target
    return base.resolve()
