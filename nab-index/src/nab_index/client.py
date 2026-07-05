"""PyPI Simple API client using PEP 691 JSON and PEP 658/714 metadata.

Fetches package listings and wheel/sdist metadata from PyPI.
Transport-agnostic: any async HTTP client implementing the
:class:`AsyncHttpTransport` protocol can be used.
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import tarfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from packaging.utils import (
    InvalidSdistFilename,
    canonicalize_name,
    parse_sdist_filename,
)
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from packaging.utils import NormalizedName
    from typing_extensions import Self

    from .transport import AsyncHttpTransport

__all__ = [
    "DEFAULT_INDEX",
    "AsyncSimpleClient",
    "MetadataHashMismatchError",
    "SdistFile",
    "SdistHashMismatchError",
    "WheelFile",
    "extract_sdist_archive",
]

# Verification order; sha256 is pip's hash-checking baseline.
_ACCEPTED_HASH_ALGORITHMS = ("sha256", "sha384", "sha512")


class MetadataHashMismatchError(Exception):
    """Fetched PEP 658 metadata did not match its published hash."""


class SdistHashMismatchError(Exception):
    """A fetched sdist archive did not match its published hash."""


# Mirrors packaging.utils._build_tag_regex: PEP 427 build numbers start with a digit.
_BUILD_TAG_RE = re.compile(r"(\d+)(.*)", re.ASCII)
# Mirrors packaging.utils' PEP 427 project-name check (re.match, not fullmatch).
_WHEEL_NAME_RE = re.compile(r"^[\w\d._]*$", re.UNICODE)
# A wheel filename has 4 dashes, or 5 when it carries a build tag.
_WHEEL_DASHES = (4, 5)
_WHEEL_DASHES_WITH_BUILD = 5


@lru_cache(maxsize=65536)
def _intern_version(version: str) -> Version:
    """Construct a cached :class:`Version`."""
    return Version(version)


@lru_cache(maxsize=65536)
def _canonical_version(version: str) -> str:
    """Return a cached canonical version string."""
    return str(_intern_version(version))


@lru_cache(maxsize=65536)
def _intern_name(name: str) -> NormalizedName:
    """Return a cached canonical name."""
    return canonicalize_name(name)


def _parse_wheel_filename(filename: str) -> tuple[NormalizedName, str] | None:
    """Parse a wheel filename per PEP 427.

    Returns ``(canonical_name, version_string)`` or ``None`` for any
    filename packaging rejects (wrong extension, malformed, etc.).
    The version string is the canonical form produced by
    :class:`packaging.version.Version`, so trailing-zero handling
    matches what packaging records on the file; e.g. a wheel
    declaring ``2.0.0`` in its filename comes back as ``"2.0.0"``,
    not ``"2"``.

    This reproduces :func:`packaging.utils.parse_wheel_filename`'s
    name/version validation and its rejection of empty tag components,
    but discards the ``frozenset[Tag]`` that the tag parser builds and
    nab does not use.
    """
    if not filename.endswith(".whl"):
        return None

    stem = filename[:-4]
    dashes = stem.count("-")
    if dashes not in _WHEEL_DASHES:
        return None

    parts = stem.split("-", dashes - 2)
    name_part = parts[0]
    if "__" in name_part or _WHEEL_NAME_RE.match(name_part) is None:
        return None

    try:
        version = _canonical_version(parts[1])
    except InvalidVersion:
        return None

    bad_build = (
        dashes == _WHEEL_DASHES_WITH_BUILD and _BUILD_TAG_RE.match(parts[2]) is None
    )
    # No tag component may be empty (the tag triple is parts[-1]).
    empty_tag = any("" in component.split(".") for component in parts[-1].split("-"))
    if bad_build or empty_tag:
        return None

    return (_intern_name(name_part), version)


def _parse_sdist_filename(filename: str) -> tuple[NormalizedName, str] | None:
    """Parse a ``.tar.gz`` sdist filename to ``(canonical_name, version)``.

    Returns ``None`` for anything packaging rejects and for ``.zip``
    sdists, which nab does not support (gzip-tar only, and not part of
    the PEP 625 standard).

    Legacy filenames with embedded build tags (e.g. ``cffi-1.0.2-2.tar.gz``)
    parse to a surprising ``(name="cffi-1-0-2", version="2")``, so callers
    MUST drop files whose canonical name does not match the queried
    package.  See :func:`_parse_files`.
    """
    if filename.endswith(".zip"):
        return None

    try:
        name, version = parse_sdist_filename(filename)
    except InvalidSdistFilename:
        return None
    return (name, str(version))


_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"
_HTTP_NOT_FOUND = 404

DEFAULT_INDEX = "https://pypi.org/simple/"


@dataclass(frozen=True, slots=True)
class WheelFile:
    """Wheel file record returned by the Simple-API client.

    ``hashes`` is a tuple of ``(algorithm, hex_digest)`` pairs in the
    order PEP 691 declared them (tuple form keeps the dataclass
    hashable).  ``has_metadata`` says whether the index advertised a
    PEP 658/714 sidecar; :attr:`metadata_url` derives the URL lazily.

    ``local_path`` is the on-disk path of a wheel served from a local
    index, and ``None`` for one fetched from a remote index.  It lets
    downstream code use the path directly instead of reversing the
    ``file:`` URL, which is lossy across platforms.

    ``metadata_hash`` is the published ``(algorithm, hex_digest)`` for
    the PEP 658/714 sidecar, or ``None`` when the index advertised the
    sidecar without a hash.  The fetcher verifies the sidecar bytes
    against it.
    """

    filename: str
    url: str
    version: str
    requires_python: str | None
    has_metadata: bool
    upload_time: str | None
    hashes: tuple[tuple[str, str], ...] = ()
    size: int | None = None
    local_path: Path | None = None
    metadata_hash: tuple[str, str] | None = None

    @property
    def metadata_url(self) -> str | None:
        """Return the PEP 658/714 metadata URL, or None when unsupported."""
        return self.url + ".metadata" if self.has_metadata else None


@dataclass(frozen=True, slots=True)
class SdistFile:
    """A source distribution from the Simple API.

    See :class:`WheelFile` for the meaning of ``hashes``, ``size`` and
    ``local_path``.
    """

    filename: str
    url: str
    version: str
    requires_python: str | None
    upload_time: str | None
    hashes: tuple[tuple[str, str], ...] = ()
    size: int | None = None
    local_path: Path | None = None


class AsyncSimpleClient:
    """Async PyPI Simple API client.

    Uses an :class:`AsyncHttpTransport` for HTTP, so any async HTTP
    library can be plugged in.
    """

    def __init__(
        self,
        transport: AsyncHttpTransport,
        index_url: str = DEFAULT_INDEX,
    ) -> None:
        """Create a client with the given async HTTP transport."""
        self._transport = transport
        self._index_url = index_url.rstrip("/") + "/"

    async def aclose(self) -> None:
        """Close the underlying transport."""
        await self._transport.aclose()

    async def __aenter__(self) -> Self:
        """Enter the async context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit the async context manager and close the transport."""
        await self.aclose()

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        """Fetch all distribution files for a package."""
        url = f"{self._index_url}{package}/"
        response = await self._transport.get(url, headers={"Accept": _JSON_ACCEPT})
        if response.status_code == _HTTP_NOT_FOUND:
            return []
        response.raise_for_status()
        return _parse_files(response.json(), self._index_url, package)

    async def get_metadata_text(self, metadata_url: str) -> str:
        """Fetch metadata text from a known PEP 658/714 metadata URL."""
        response = await self._transport.get(metadata_url)
        response.raise_for_status()
        return response.text

    async def download(self, url: str) -> bytes:
        """Fetch a distribution artefact (wheel or sdist) as raw bytes."""
        response = await self._transport.get(url)
        response.raise_for_status()
        return response.content


def _parse_files(
    data: object, index_url: str, package: str
) -> list[WheelFile | SdistFile]:
    """Parse distribution files from a Simple API JSON response.

    ``package`` is the package the index was queried for; files whose
    parsed canonical name does not match are dropped.  PyPI hosts a
    handful of legacy sdists with embedded build tags
    (``cffi-1.0.2-2.tar.gz`` and similar) that
    :func:`packaging.utils.parse_sdist_filename` interprets as a
    different project (``cffi-1-0-2`` at version ``2``).  Without the
    name check those leak into the listing as a phantom version, and
    show up in the resolved lockfile as ``cffi==2``.

    PEP 592 ``yanked`` files are dropped unconditionally.

    A single malformed *entry* (non-dict, or missing string ``filename``
    / ``url``) is skipped so the usable entries in the same listing are
    kept.  A malformed *body* (not a JSON object, or a ``files`` value
    that is not a list) is a broken response, not an empty one, so it
    raises :class:`TypeError` rather than returning no files: an empty
    result means "package absent" to the multi-index router, which would
    otherwise fall through to a lower-priority index and risk pinning a
    different version.
    """
    expected = canonicalize_name(package)
    # PEP 691: relative URLs resolve against the package page, not the index root.
    base_url = f"{index_url}{package}/"
    files: list[WheelFile | SdistFile] = []
    if not isinstance(data, dict):
        msg = (
            f"{index_url} served a malformed Simple-API response for "
            f"{package!r}: body is {type(data).__name__}, expected a JSON object"
        )
        raise TypeError(msg)
    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        msg = (
            f"{index_url} served a malformed Simple-API response for "
            f"{package!r}: 'files' is {type(raw_files).__name__}, expected a list"
        )
        raise TypeError(msg)
    for file_info in raw_files:
        if not isinstance(file_info, dict):
            continue
        # PEP 592: ``true`` or a non-empty reason string means yanked.
        if file_info.get("yanked"):
            continue
        filename = file_info.get("filename")
        raw_url = file_info.get("url")
        if not isinstance(filename, str) or not isinstance(raw_url, str):
            continue
        parsed = _parse_file_entry(file_info, filename, raw_url, base_url, expected)
        if parsed is not None:
            files.append(parsed)

    return files


def _parse_file_entry(
    file_info: dict,
    filename: str,
    raw_url: str,
    base_url: str,
    expected: NormalizedName,
) -> WheelFile | SdistFile | None:
    """Build a file record from a validated PEP 691 entry, or None to drop it.

    ``filename`` and ``raw_url`` are the entry's already-validated string
    fields.  ``expected`` is the queried package's canonical name; files
    whose parsed name differs, or whose filename packaging does not
    recognise, are dropped (see :func:`_parse_files`).
    """
    # PyPI and most indexes emit absolute file URLs; urljoin then re-parses
    # both sides only to return the URL unchanged. Skip it for the common
    # absolute case; relative URLs still resolve against the page below.
    if raw_url.startswith(("https://", "http://")):
        file_url = raw_url
    else:
        file_url = urljoin(base_url, raw_url)

    hashes = _parse_hashes(file_info.get("hashes"))
    size = _parse_size(file_info.get("size"))
    # ``requires-python`` has only a few dozen distinct values across
    # all of PyPI (``>=3.7``, ``>=3.8`` etc.) but appears once per
    # wheel.  Interning collapses the duplicates into one shared
    # string per distinct specifier.
    requires_python_raw = file_info.get("requires-python")
    # PEP 691 mandates a string; a non-conformant index serving a number
    # would otherwise crash SpecifierSet downstream. Treat it as absent.
    requires_python = (
        sys.intern(requires_python_raw)
        if isinstance(requires_python_raw, str)
        else None
    )
    # A non-conformant index may serve a non-string ``upload-time``
    # (a JSON number or bool); drop it so the downstream datetime
    # parse never crashes.
    upload_time_raw = file_info.get("upload-time")
    upload_time = upload_time_raw if isinstance(upload_time_raw, str) else None

    wheel_parsed = _parse_wheel_filename(filename)
    if wheel_parsed is not None:
        parsed_name, version = wheel_parsed
        if parsed_name != expected:
            return None
        return WheelFile(
            filename=filename,
            url=file_url,
            version=version,
            requires_python=requires_python,
            has_metadata=_has_metadata(file_info),
            upload_time=upload_time,
            hashes=hashes,
            size=size,
            metadata_hash=_metadata_hash(file_info),
        )

    sdist_parsed = _parse_sdist_filename(filename)
    if sdist_parsed is None:
        return None
    parsed_name, version = sdist_parsed
    if parsed_name != expected:
        return None
    return SdistFile(
        filename=filename,
        url=file_url,
        version=version,
        requires_python=requires_python,
        upload_time=upload_time,
        hashes=hashes,
        size=size,
    )


def _parse_hashes(value: object) -> tuple[tuple[str, str], ...]:
    # Algo names are a tiny fixed vocabulary, so interning dedups them.
    # Both halves are lowercased: PEP 503/691 don't mandate a case, pip
    # treats them case-insensitively, and the acceptable-algorithm filter
    # and hashlib.hexdigest() both expect the lowercase form.
    if not isinstance(value, dict):
        return ()

    # The common case is a single hash; skip the list build.
    if len(value) == 1:
        ((algo, digest),) = value.items()
        if isinstance(algo, str) and isinstance(digest, str):
            return ((sys.intern(algo.lower()), digest.lower()),)
        return ()

    out: list[tuple[str, str]] = []
    for algo, digest in value.items():
        if isinstance(algo, str) and isinstance(digest, str):
            out.append((sys.intern(algo.lower()), digest.lower()))

    return tuple(out)


def _parse_size(value: object) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    return None


_LEGACY_METADATA_KEY = "dist-info-metadata"


def _metadata_value(file_info: dict) -> object:
    """Return the metadata field, applying PEP 714 key precedence.

    When ``core-metadata`` is present it wins and the legacy
    ``dist-info-metadata`` key is ignored, so ``core-metadata: false``
    means no sidecar even if a stale legacy entry lingers.  The legacy key
    applies only when ``core-metadata`` is absent.  ``data-dist-info-metadata``
    is the HTML attribute name and never appears in the JSON response.
    """
    if "core-metadata" in file_info:
        return file_info.get("core-metadata")
    return file_info.get(_LEGACY_METADATA_KEY)


def _has_metadata(file_info: dict) -> bool:
    """Return True when the file entry advertises a PEP 658/714 sidecar.

    PEP 691 allows either a ``true`` boolean (sidecar exists but no
    hashes published) or a mapping carrying the digest table.  Either
    flavour means the index will serve ``<file>.metadata``.
    """
    value = _metadata_value(file_info)
    return value is True or isinstance(value, dict)


_ACCEPTED_METADATA_HASHES: tuple[str, ...] = ("sha256", "sha384", "sha512")


def _metadata_hash(file_info: dict) -> tuple[str, str] | None:
    """Return the sidecar's published ``(algo, hex)`` to verify, or None.

    Prefers sha256, then sha384, then sha512, so a sidecar published with
    only a stronger digest is still verified. Algorithm names match
    case-insensitively. A bare ``true`` (sidecar exists, no hash) or a table
    with no accepted algorithm yields None, so no check runs.
    """
    value = _metadata_value(file_info)
    if not isinstance(value, dict):
        return None
    published = {
        algo.lower(): digest
        for algo, digest in value.items()
        if isinstance(algo, str) and isinstance(digest, str)
    }
    for algo in _ACCEPTED_METADATA_HASHES:
        digest = published.get(algo)
        if digest is not None:
            return (algo, digest.lower())
    return None


def _verify_metadata_hash(content: bytes, metadata_hash: tuple[str, str]) -> None:
    """Raise :class:`MetadataHashMismatchError` if ``content`` fails the hash."""
    algo, expected = metadata_hash
    actual = hashlib.new(algo, content).hexdigest()
    if actual != expected:
        msg = f"metadata {algo} mismatch: expected {expected}, got {actual}"
        raise MetadataHashMismatchError(msg)


def _select_artifact_hash(
    hashes: tuple[tuple[str, str], ...],
) -> tuple[str, str] | None:
    """Pick the preferred ``(algo, hex)`` to verify, or ``None`` if none qualify.

    Walks :data:`_ACCEPTED_HASH_ALGORITHMS` in order, so sha256 is preferred,
    then sha384, then sha512. An empty set or only unaccepted algorithms (md5)
    yields ``None``.
    """
    by_algo = {algo.lower(): digest.lower() for algo, digest in hashes}
    for algo in _ACCEPTED_HASH_ALGORITHMS:
        digest = by_algo.get(algo)
        if digest is not None:
            return (algo, digest)
    return None


def _verify_sdist_hash(content: bytes, sdist_hash: tuple[str, str]) -> None:
    """Raise :class:`SdistHashMismatchError` if ``content`` fails the hash."""
    algo, expected = sdist_hash
    actual = hashlib.new(algo, content).hexdigest()
    if actual != expected:
        msg = f"sdist {algo} mismatch: expected {expected}, got {actual}"
        raise SdistHashMismatchError(msg)


def _extract_sdist_files(data: bytes) -> tuple[str | None, str | None]:
    """Extract PKG-INFO and pyproject.toml from a .tar.gz sdist archive.

    Returns ``(pkg_info, pyproject_toml)``. Either may be ``None`` if
    the archive cannot be opened or the file is absent. PEP 643 static
    metadata detection requires both: PKG-INFO carries the ``Dynamic``
    field that says which values are not authoritative, and
    pyproject.toml's ``[project].dynamic`` is the static-metadata
    fallback when PKG-INFO marks dependencies dynamic.

    .zip sdists are intentionally unsupported.
    """
    try:
        return _read_tar_sdist_files(data)
    except (tarfile.TarError, OSError, UnicodeDecodeError):
        return (None, None)


def _read_tar_sdist_files(data: bytes) -> tuple[str | None, str | None]:
    pkg_infos: dict[str, str] = {}
    pyprojects: dict[str, str] = {}

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            depth, top_dir, basename = _sdist_member_top_level(member.name)
            if depth != 1:
                continue
            target = (
                pkg_infos
                if basename == "PKG-INFO"
                else pyprojects
                if basename == "pyproject.toml"
                else None
            )
            if target is None or top_dir in target:
                continue
            extracted = tar.extractfile(member)
            if extracted is not None:
                target[top_dir] = extracted.read().decode("utf-8")

    return _select_sdist_root(pkg_infos, pyprojects)


def _select_sdist_root(
    pkg_infos: dict[str, str], pyprojects: dict[str, str]
) -> tuple[str | None, str | None]:
    """Pick PKG-INFO and pyproject.toml from one ``<name>-<version>/`` root.

    A conformant sdist has a single top-level directory holding both
    files.  PKG-INFO is the defining file, so its directory is the root;
    pyproject.toml counts only when it shares that directory.  If several
    top-level directories carry a PKG-INFO the root is ambiguous, so both
    return ``None`` rather than risk pairing files from different roots.
    """
    if len(pkg_infos) != 1:
        return (None, None)
    root, pkg_info = next(iter(pkg_infos.items()))
    return (pkg_info, pyprojects.get(root))


def _sdist_member_top_level(name: str) -> tuple[int, str, str]:
    """Return ``(depth, top_dir, basename)`` for a tar member.

    Strips a single leading ``./``.  Depth 0 means the file sits at the
    archive root; depth 1 means it sits directly under a top-level
    directory, whose name is ``top_dir`` (empty at depth 0).  Anything
    deeper is reported as-is so callers can ignore it.
    """
    stripped = name.removeprefix("./")
    if not stripped or stripped.startswith("/"):
        return (-1, "", "")
    parts = stripped.split("/")
    top_dir = parts[0] if len(parts) > 1 else ""
    return (len(parts) - 1, top_dir, parts[-1])


def extract_sdist_archive(data: bytes, target_dir: Path) -> Path:
    """Extract a .tar.gz sdist into ``target_dir`` and return the source root.

    Sdists wrap their contents in a single top-level directory
    (``<name>-<version>/``).  The function refuses absolute paths and
    members whose normalised path escapes ``target_dir``; sdists from
    PyPI conform to this convention but mirrors and forks occasionally
    do not, and silently writing outside the intended directory is
    unsafe.  The first directory found at depth 1 is returned as the
    source root, falling back to ``target_dir`` itself when the archive
    is flat.
    """
    target_dir = target_dir.resolve()
    root: Path | None = None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            raw_name = member.name
            if not raw_name or raw_name.startswith(("/", "./.")) or "\\" in raw_name:
                msg = f"unsafe sdist member: {raw_name!r}"
                raise ValueError(msg)
            member_name = raw_name.removeprefix("./")
            if not member_name or member_name.startswith("/"):
                msg = f"unsafe sdist member: {raw_name!r}"
                raise ValueError(msg)
            parts = Path(member_name).parts
            if any(p in ("..", "") for p in parts):
                msg = f"unsafe sdist member: {raw_name!r}"
                raise ValueError(msg)
            tar.extract(member, target_dir, filter="data")
            if root is None and member.isdir() and len(parts) == 1:
                root = target_dir / parts[0]
            elif root is None and len(parts) >= 1:
                top = target_dir / parts[0]
                if top.is_dir():
                    root = top
    if root is None:
        return target_dir
    return root
