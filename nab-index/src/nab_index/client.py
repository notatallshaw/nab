"""PyPI Simple API client using PEP 691 JSON and PEP 658/714 metadata.

Fetches package listings and wheel/sdist metadata from PyPI.
Transport-agnostic: any async HTTP client implementing the
:class:`AsyncHttpTransport` protocol can be used.
"""

from __future__ import annotations

import io
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)

if TYPE_CHECKING:
    from packaging.utils import NormalizedName
    from typing_extensions import Self

    from .transport import AsyncHttpTransport

__all__ = [
    "DEFAULT_INDEX",
    "AsyncSimpleClient",
    "SdistFile",
    "WheelFile",
    "extract_sdist_archive",
]


def _parse_wheel_filename(filename: str) -> tuple[NormalizedName, str] | None:
    """Parse a wheel filename per PEP 427.

    Returns ``(canonical_name, version_string)`` or ``None`` for any
    filename packaging rejects (wrong extension, malformed, etc.).
    The version string is the canonical form produced by
    :class:`packaging.version.Version`, so trailing-zero handling
    matches what packaging records on the file; e.g. a wheel
    declaring ``2.0.0`` in its filename comes back as ``"2.0.0"``,
    not ``"2"``.
    """
    if not filename.endswith(".whl"):
        return None
    try:
        name, version, _build, _tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        return None
    return (name, str(version))


def _parse_sdist_filename(filename: str) -> tuple[NormalizedName, str] | None:
    """Parse an sdist filename.  Supports ``.tar.gz`` and ``.zip``.

    Returns ``(canonical_name, version_string)`` or ``None`` for any
    filename packaging rejects.  Note that legacy filenames with
    embedded build tags (e.g. ``cffi-1.0.2-2.tar.gz``) parse to a
    surprising ``(name="cffi-1-0-2", version="2")`` tuple per
    :func:`packaging.utils.parse_sdist_filename`'s last-dash split;
    callers MUST drop files whose canonical name does not match the
    package they queried.  See :func:`_parse_files`.
    """
    try:
        name, version = parse_sdist_filename(filename)
    except InvalidSdistFilename:
        return None
    return (name, str(version))


_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"

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
    data: dict, index_url: str, package: str
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
    """
    expected = canonicalize_name(package)
    files: list[WheelFile | SdistFile] = []
    for file_info in data.get("files", []):
        # PEP 592: ``true`` or a non-empty reason string means yanked.
        if file_info.get("yanked"):
            continue
        filename = file_info["filename"]

        file_url = file_info["url"]
        if not file_url.startswith("http"):
            file_url = index_url + file_url

        hashes = _parse_hashes(file_info.get("hashes"))
        size = _parse_size(file_info.get("size"))
        # ``requires-python`` has only a few dozen distinct values across
        # all of PyPI (``>=3.7``, ``>=3.8`` etc.) but appears once per
        # wheel.  Interning collapses the duplicates into one shared
        # string per distinct specifier.
        requires_python_raw = file_info.get("requires-python")
        requires_python = (
            sys.intern(requires_python_raw)
            if isinstance(requires_python_raw, str)
            else requires_python_raw
        )

        wheel_parsed = _parse_wheel_filename(filename)
        if wheel_parsed is not None:
            parsed_name, version = wheel_parsed
            if parsed_name != expected:
                continue
            files.append(
                WheelFile(
                    filename=filename,
                    url=file_url,
                    version=version,
                    requires_python=requires_python,
                    has_metadata=_has_metadata(file_info),
                    upload_time=file_info.get("upload-time"),
                    hashes=hashes,
                    size=size,
                )
            )
            continue

        sdist_parsed = _parse_sdist_filename(filename)
        if sdist_parsed is not None:
            parsed_name, version = sdist_parsed
            if parsed_name != expected:
                continue
            files.append(
                SdistFile(
                    filename=filename,
                    url=file_url,
                    version=version,
                    requires_python=requires_python,
                    upload_time=file_info.get("upload-time"),
                    hashes=hashes,
                    size=size,
                )
            )

    return files


def _parse_hashes(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    out: list[tuple[str, str]] = []
    for algo, digest in value.items():
        if isinstance(algo, str) and isinstance(digest, str):
            # Hash algo names are drawn from a fixed vocabulary
            # (``sha256``, ``md5``, ``blake2b``...) but appear once per
            # file; interning collapses the duplicates into the handful
            # actually used.
            out.append((sys.intern(algo), digest))
    return tuple(out)


def _parse_size(value: object) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _has_metadata(file_info: dict) -> bool:
    """Return True when the file entry advertises a PEP 658/714 sidecar.

    PEP 691 allows either a ``true`` boolean (sidecar exists but no
    hashes published) or a mapping carrying the digest table.  Either
    flavour means the index will serve ``<file>.metadata``.
    """
    for key in ("core-metadata", "data-dist-info-metadata"):
        value = file_info.get(key)
        if value is True or isinstance(value, dict):
            return True
    return False


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
    pkg_info: str | None = None
    pyproject_toml: str | None = None

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            depth, basename = _sdist_member_top_level(member.name)
            if depth > 1:
                continue
            if pkg_info is None and basename == "PKG-INFO":
                extracted = tar.extractfile(member)
                if extracted is not None:
                    pkg_info = extracted.read().decode("utf-8")
            elif pyproject_toml is None and basename == "pyproject.toml":
                extracted = tar.extractfile(member)
                if extracted is not None:
                    pyproject_toml = extracted.read().decode("utf-8")

            if pkg_info is not None and pyproject_toml is not None:
                break

    return (pkg_info, pyproject_toml)


def _sdist_member_top_level(name: str) -> tuple[int, str]:
    """Return ``(depth, basename)`` for a tar member relative to the sdist root.

    Strips a single leading ``./``.  Depth 0 means the file sits at the
    archive root (rare); depth 1 means it sits directly under the
    conventional ``<name>-<version>/`` top-level directory.  Anything
    deeper is reported as-is so callers can ignore it.
    """
    stripped = name.removeprefix("./")
    if not stripped or stripped.startswith("/"):
        return (-1, "")
    parts = stripped.split("/")
    return (len(parts) - 1, parts[-1])


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
