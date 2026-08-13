"""PyPI Simple API client using PEP 691 JSON and PEP 658/714 metadata.

Fetches package listings and wheel/sdist metadata from PyPI. An index that
answers with the PEP 503 HTML serialization instead is read through
:mod:`nab_index._pep503`. Transport-agnostic: any async HTTP client
implementing the :class:`AsyncHttpTransport` protocol can be used.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import tarfile
import zlib
from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from packaging.utils import canonicalize_name, parse_sdist_filename
from packaging.version import Version

from nab_provider.errors import (
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    SdistHashMismatchError,
    WheelHashMismatchError,
)
from nab_provider.records import ACCEPTED_HASH_ALGORITHMS, SdistFile, WheelFile
from nab_provider.serialization import SimpleSerialization, simple_accept_header

from ._pep503 import json_listing
from .transport import IDENTITY_HEADERS, raise_unless_ok

if TYPE_CHECKING:
    from pathlib import Path

    from packaging.utils import NormalizedName
    from typing_extensions import Self

    from .transport import AsyncHttpTransport, HttpResponse

__all__ = [
    "ACCEPTED_HASH_ALGORITHMS",
    "DEFAULT_INDEX",
    "AsyncSimpleClient",
    "MalformedSimpleResponseError",
    "MetadataHashMismatchError",
    "SdistFile",
    "SdistHashMismatchError",
    "WheelFile",
    "WheelHashMismatchError",
    "extract_sdist_archive",
    "holds_unreadable_format",
    "is_readable_filename",
    "verify_sdist_hash",
]

# One decoded PEP 691 ``files`` entry.  Neither its keys nor its values
# have been checked, so every field is narrowed where it is read.
_FileEntry = Mapping[Any, object]

# The tar ``data`` filter (PEP 706) landed in 3.12 and was backported to
# 3.10.12 / 3.11.4; sdist extraction requires it (see extract_sdist_archive).
# data_filter appears with the same change, so its presence detects support.
_SUPPORTS_DATA_FILTER = hasattr(tarfile, "data_filter")


# Mirrors packaging.utils._build_tag_regex: PEP 427 build numbers start with a digit.
_BUILD_TAG_RE = re.compile(r"(\d+)(.*)", re.ASCII)
# Mirrors packaging.utils' PEP 427 project-name check. One character minimum, so
# an empty name is rejected, and ``\Z`` rather than ``$`` so a trailing newline is
# not accepted as the end of the name.
_WHEEL_NAME_RE = re.compile(r"^[\w._]+\Z", re.UNICODE)
# A wheel filename has 4 dashes, or 5 when it carries a build tag.
_WHEEL_DASHES = (4, 5)
_WHEEL_DASHES_WITH_BUILD = 5


@lru_cache(maxsize=65536)
def _canonical_version(version: str) -> str:
    """Return a cached canonical version string."""
    return str(Version(version))


@lru_cache(maxsize=65536)
def _intern_name(name: str) -> NormalizedName:
    """Return a cached canonical name."""
    return canonicalize_name(name)


def _tag_triple_is_parseable(tag_str: str) -> bool:
    """Whether ``parse_tag`` would expand this ``python-abi-platform`` triple.

    ``tag_str`` must carry exactly two dashes. Each field may be a PEP 425
    compressed set with no empty member, and an interpreter names an
    implementation and a version, so it is an identifier.
    """
    interpreters, abis, platforms = tag_str.split("-")
    if not all(part.isidentifier() for part in interpreters.split(".")):
        return False
    return all("" not in field.split(".") for field in (abis, platforms))


def _parse_wheel_filename(filename: str) -> tuple[NormalizedName, str] | None:
    """Parse a wheel filename per PEP 427.

    Returns ``(canonical_name, version_string)`` or ``None`` for any
    filename packaging rejects (wrong extension, malformed, etc.) and
    for a version digit run past CPython's int-from-string limit.
    Never raises.
    The version string is the canonical form produced by
    :class:`packaging.version.Version`, so trailing-zero handling
    matches what packaging records on the file; e.g. a wheel
    declaring ``2.0.0`` in its filename comes back as ``"2.0.0"``,
    not ``"2"``.

    This accepts what nab-provider's vendored ``parse_wheel_filename`` accepts,
    but discards the ``frozenset[Tag]`` the tag parser builds and nab does not
    use. The vendored copy is the one to match, not the released ``packaging``
    this package depends on, because the vendored tag parser ranks whatever is
    admitted here; the two can differ on an empty project name, which releases
    before 26.3 accept.

    A build tag past that same digit limit is admitted rather than rejected:
    ``parse_wheel_filename`` raises ``ValueError`` out of ``int()`` on one, but
    nab reads a build tag only to sort by it and sorts an unconvertible run
    lowest.
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
    except ValueError:
        # InvalidVersion, or int() refusing a digit run past CPython's limit.
        return None

    bad_build = (
        dashes == _WHEEL_DASHES_WITH_BUILD and _BUILD_TAG_RE.match(parts[2]) is None
    )

    # parts[-1] is the whole tag triple, so it carries the two dashes
    # _tag_triple_is_parseable unpacks on.
    if bad_build or not _tag_triple_is_parseable(parts[-1]):
        return None

    return (_intern_name(name_part), version)


def _parse_sdist_filename(filename: str) -> tuple[NormalizedName, str] | None:
    """Parse a ``.tar.gz`` sdist filename to ``(canonical_name, version)``.

    Returns ``None`` for anything packaging rejects, for a version digit
    run past CPython's int-from-string limit, and for ``.zip`` sdists,
    which nab does not support (gzip-tar only, and not part of the PEP 625
    standard).  Never raises.

    Legacy filenames with embedded build tags (e.g. ``cffi-1.0.2-2.tar.gz``)
    parse to a surprising ``(name="cffi-1-0-2", version="2")``, so callers
    MUST drop files whose canonical name does not match the queried
    package.  See :func:`_parse_files`.
    """
    if filename.endswith(".zip"):
        return None

    try:
        name, version = parse_sdist_filename(filename)
    except ValueError:
        # InvalidSdistFilename, or int() refusing a digit run past CPython's limit.
        return None
    return (name, str(version))


def holds_unreadable_format(data: object) -> bool:
    """Whether a Simple-API body offers a file nab cannot read.

    nab reads wheels and ``.tar.gz`` sdists, so a page of ``.zip`` sdists
    or ``.exe`` installers parses to no files at all.  A body that is not
    a list of file entries answers ``False``.
    """
    if not isinstance(data, dict):
        return False
    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        return False

    for file_info in raw_files:
        if not isinstance(file_info, dict) or file_info.get("yanked"):
            continue
        filename = file_info.get("filename")
        if isinstance(filename, str) and not is_readable_filename(filename):
            return True
    return False


def is_readable_filename(filename: str) -> bool:
    """Whether ``filename`` names a wheel or a ``.tar.gz`` sdist."""
    return (
        _parse_wheel_filename(filename) is not None
        or _parse_sdist_filename(filename) is not None
    )


_HTTP_NOT_FOUND = 404

DEFAULT_INDEX = "https://pypi.org/simple/"


def _header(response: HttpResponse, key: str) -> str | None:
    """Case-insensitive header lookup.

    The :class:`HttpResponse` Protocol only promises a plain
    :class:`Mapping`. Both real transports (httpx, urllib3) return
    case-insensitive header containers, but we don't rely on
    that here so a plain-dict fake also works.
    """
    headers = response.headers
    target = key.lower()
    for name, value in headers.items():
        if name.lower() == target:
            return value
    return None


def _is_html_listing(content_type: str | None) -> bool:
    """Return True when a Content-Type names an HTML Simple-API serialization.

    Covers :pep:`503`'s ``text/html`` and :pep:`691`'s
    ``application/vnd.pypi.simple.vN+html``.
    """
    if content_type is None:
        return False
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type == "text/html" or media_type.endswith("+html")


def _listing_body(
    response: HttpResponse,
    index_url: str,
    package: str,
    serialization: SimpleSerialization,
) -> bytes:
    """Return a listing response's body as PEP 691 JSON bytes.

    The served Content-Type picks the decoder. An HTML page is re-serialized
    so the parser and the cache only ever see one shape; any other body is
    passed through untouched. A pinned index that answers in the other
    serialization raises instead.

    An HTML page's hrefs resolve against the URL that served the page, which
    is not the requested one when the index redirected.
    """
    body = response.content
    content_type = _header(response, "content-type")
    is_html = _is_html_listing(content_type)

    if serialization is not SimpleSerialization.NEGOTIATE and is_html != (
        serialization is SimpleSerialization.HTML
    ):
        served = (
            f"Content-Type {content_type!r}"
            if content_type is not None
            else "no Content-Type"
        )
        instead = (
            f" set serialization = {SimpleSerialization.HTML.value!r},"
            if is_html
            else ""
        )
        msg = (
            f"{index_url} served {package!r} with {served}, but this index is"
            f" pinned to serialization = {serialization.value!r}."
            f"  Drop the pin,{instead} or set url to an endpoint that serves"
            f" {serialization.value}."
        )
        raise MalformedSimpleResponseError(msg)

    if not is_html:
        return body

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = (
            f"{index_url} served a malformed Simple-API response for "
            f"{package!r}: HTML body is not valid UTF-8"
        )
        raise MalformedSimpleResponseError(msg) from exc

    try:
        return json_listing(text, response.url)
    except ValueError as exc:
        msg = (
            f"{index_url} served a malformed Simple-API response for {package!r}: {exc}"
        )
        raise MalformedSimpleResponseError(msg) from exc


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
        """Fetch all distribution files for a package.

        A body ``json.loads`` rejects becomes a
        :class:`MalformedSimpleResponseError`, not a raw decode error.
        """
        url = f"{self._index_url}{package}/"
        accept = simple_accept_header(SimpleSerialization.NEGOTIATE)
        response = await self._transport.get(url, headers={"Accept": accept})
        if response.status_code == _HTTP_NOT_FOUND:
            return []
        raise_unless_ok(response, url)
        body = _listing_body(
            response, self._index_url, package, SimpleSerialization.NEGOTIATE
        )

        try:
            data = json.loads(body)
        except ValueError as exc:
            msg = (
                f"{self._index_url} served a malformed Simple-API response for "
                f"{package!r}: body is not valid JSON"
            )
            raise MalformedSimpleResponseError(msg) from exc
        return _parse_files(data, self._index_url, package, page_url=response.url)

    async def get_metadata_text(self, metadata_url: str) -> str:
        """Fetch metadata text from a known PEP 658/714 metadata URL."""
        response = await self._transport.get(metadata_url)
        raise_unless_ok(response, metadata_url)
        return response.text

    async def download(self, url: str) -> bytes:
        """Fetch a distribution artefact (wheel or sdist) as raw bytes."""
        response = await self._transport.get(url, headers=IDENTITY_HEADERS)
        raise_unless_ok(response, url)
        return response.content


def _parse_files(
    data: object, index_url: str, package: str, *, page_url: str | None = None
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

    ``page_url`` is the URL the project page was retrieved from, the base a
    relative entry resolves against. ``None`` falls back to the page URL
    built from ``index_url`` and ``package``.

    PEP 592 ``yanked`` files are dropped unconditionally.

    A single malformed *entry* (non-dict, missing string ``filename`` /
    ``url``, or a ``url`` that cannot be used) is skipped so the usable
    entries in the same listing are kept.  A malformed *body* (not a JSON
    object, or a ``files`` value that is not a list) is a broken response,
    not an empty one, so it raises :class:`MalformedSimpleResponseError`
    rather than returning no files: an empty result means "package absent"
    to the multi-index router, which would otherwise fall through to a
    lower-priority index and risk pinning a different version.
    """
    expected = canonicalize_name(package)
    # PEP 691: relative URLs resolve against the package page, not the index root.
    base_url = page_url if page_url is not None else f"{index_url}{package}/"
    files: list[WheelFile | SdistFile] = []
    if not isinstance(data, dict):
        msg = (
            f"{index_url} served a malformed Simple-API response for "
            f"{package!r}: body is {type(data).__name__}, expected a JSON object"
        )
        raise MalformedSimpleResponseError(msg)
    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        msg = (
            f"{index_url} served a malformed Simple-API response for "
            f"{package!r}: 'files' is {type(raw_files).__name__}, expected a list"
        )
        raise MalformedSimpleResponseError(msg)
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


def _resolve_file_url(raw_url: str, base_url: str) -> str | None:
    """Return the entry's absolute URL, or None when it is not usable.

    PEP 691 allows a relative ``url``, which resolves against the package
    page.  ``urlsplit`` rejects a netloc it cannot parse, such as an
    unbalanced bracket in an IPv6 host, and ``encode`` rejects a string
    with no UTF-8 form, such as one holding an unpaired surrogate; both
    signal it with a ``ValueError``.

    ``urlsplit`` deletes a tab, CR or LF anywhere in a URL, so the split
    round trip stores the URL a later parse of the record would yield.
    """
    try:
        # urljoin resolves a same-scheme URL against the base, which would
        # hand ``https:///pkg.whl`` the base's authority.
        absolute = (
            raw_url
            if raw_url.startswith(("https://", "http://"))
            else urljoin(base_url, raw_url)
        )
        file_url = urlunsplit(urlsplit(absolute))

        # Encode only to reject a string with no UTF-8 form.
        file_url.encode()
    except ValueError:
        return None

    return file_url


def _parse_file_entry(
    file_info: _FileEntry,
    filename: str,
    raw_url: str,
    base_url: str,
    expected: NormalizedName,
) -> WheelFile | SdistFile | None:
    """Build a file record from a validated PEP 691 entry, or None to drop it.

    ``filename`` and ``raw_url`` are the entry's already-validated string
    fields.  ``expected`` is the queried package's canonical name; files
    whose parsed name differs, whose filename packaging does not
    recognise, or whose URL cannot be used are dropped (see
    :func:`_parse_files`).
    """
    file_url = _resolve_file_url(raw_url, base_url)
    if file_url is None:
        return None

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
    # An empty digest carries no integrity claim and can never match a real
    # file, so it is dropped rather than recorded and later failed against.
    if not isinstance(value, dict):
        return ()

    # The common case is a single hash; skip the list build.
    if len(value) == 1:
        ((algo, digest),) = value.items()
        if isinstance(algo, str) and isinstance(digest, str) and digest:
            return ((sys.intern(algo.lower()), digest.lower()),)
        return ()

    out: list[tuple[str, str]] = []
    for algo, digest in value.items():
        if isinstance(algo, str) and isinstance(digest, str) and digest:
            out.append((sys.intern(algo.lower()), digest.lower()))

    return tuple(out)


def _parse_size(value: object) -> int | None:
    # bool is an int subclass, so reject it explicitly rather than read True as 1.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


_LEGACY_METADATA_KEY = "dist-info-metadata"


def _metadata_value(file_info: _FileEntry) -> object:
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


def _has_metadata(file_info: _FileEntry) -> bool:
    """Return True when the file entry advertises a PEP 658/714 sidecar.

    PEP 691 allows either a ``true`` boolean (sidecar exists but no
    hashes published) or a mapping carrying the digest table.  Either
    flavour means the index will serve ``<file>.metadata``.
    """
    value = _metadata_value(file_info)
    return value is True or isinstance(value, dict)


def _metadata_hash(file_info: _FileEntry) -> tuple[str, str] | None:
    """Return the sidecar's published ``(algo, hex)`` to verify, or None.

    A bare ``true`` (sidecar exists, no hash), an empty digest, or a table with
    no accepted algorithm yields None, so no check runs.
    """
    value = _metadata_value(file_info)
    if not isinstance(value, dict):
        return None

    published = tuple(
        (algo, digest)
        for algo, digest in value.items()
        if isinstance(algo, str) and isinstance(digest, str)
    )
    return _select_artifact_hash(published)


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

    Walks :data:`ACCEPTED_HASH_ALGORITHMS` in order, so sha256 is preferred,
    then sha384, then sha512. An empty set, an empty digest, or only unaccepted
    algorithms (md5) yields ``None``.
    """
    by_algo = {algo.lower(): digest.lower() for algo, digest in hashes}
    for algo in ACCEPTED_HASH_ALGORITHMS:
        digest = by_algo.get(algo)
        if digest:
            return (algo, digest)
    return None


def verify_sdist_hash(content: bytes, sdist_hash: tuple[str, str]) -> None:
    """Raise :class:`SdistHashMismatchError` if ``content`` fails the hash."""
    algo, expected = sdist_hash
    actual = hashlib.new(algo, content).hexdigest()
    if actual != expected:
        msg = f"sdist {algo} mismatch: expected {expected}, got {actual}"
        raise SdistHashMismatchError(msg)


def _extract_sdist_files(data: bytes) -> tuple[str | None, str | None]:
    """Extract PKG-INFO and pyproject.toml from a .tar.gz sdist archive.

    Returns ``(pkg_info, pyproject_toml)``. Either may be ``None`` if
    the archive cannot be read or the file is absent. PEP 643 static
    metadata detection requires both: PKG-INFO carries the ``Dynamic``
    field that says which values are not authoritative, and
    pyproject.toml's ``[project].dynamic`` is the static-metadata
    fallback when PKG-INFO marks dependencies dynamic.

    .zip sdists are intentionally unsupported.
    """
    try:
        return _read_tar_sdist_files(data)
    except (
        tarfile.TarError,
        OSError,
        UnicodeDecodeError,
        KeyError,
        EOFError,
        zlib.error,
        # tarfile resolves a link member by recursing on its target, so a
        # cycle of links only ends at the recursion limit.
        RecursionError,
    ):
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


# The tar data filter (PEP 706) this extraction requires ships in 3.10.12 /
# 3.11.4 / 3.12. The guard below raises on older patch releases and never
# extracts, so the CI cell still on 3.10.11 exercises only one side of it and
# cannot reach the extraction path. Drop the pragma when that cell moves to a
# build carrying the filter or 3.10 reaches EOL (2026-10).
def extract_sdist_archive(
    data: bytes, target_dir: Path
) -> Path:  # pragma: no cover (tar data filter)
    """Extract a .tar.gz sdist into ``target_dir`` and return the source root.

    Anything the extractor cannot read raises :class:`ValueError`: a corrupt or
    truncated stream, a tar that will not open, and a member the tar ``data``
    filter (:pep:`706`) refuses.  The filter refuses any member that would write
    outside ``target_dir`` (absolute paths, ``..``, escaping links), is a special
    file (device node, FIFO), or is a hard link whose target the archive does not
    carry.  A lone top-level directory that wraps every member is the source
    root; otherwise (top-level files, as in a flat sdist, or several top-level
    directories) the root is ``target_dir``.

    The data filter is required; a Python that lacks it (before 3.10.12 /
    3.11.4 / 3.12) is unsupported and extraction raises.
    """
    if not _SUPPORTS_DATA_FILTER:
        msg = (
            "extracting an sdist archive requires the tar data filter;"
            " upgrade to Python 3.10.12+ / 3.11.4+ / 3.12+"
        )
        raise ValueError(msg)

    target_dir = target_dir.resolve()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(target_dir, filter="data")
    except tarfile.FilterError as exc:
        msg = f"unsafe sdist member: {exc}"
        raise ValueError(msg) from exc
    except KeyError as exc:
        msg = f"broken link in sdist member: {exc}"
        raise ValueError(msg) from exc
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        # gzip raises BadGzipFile (an OSError) on a bad header, a bare EOFError on
        # a truncated stream, and zlib.error on a corrupt deflate block; none of
        # them is a TarError.
        msg = f"unreadable sdist archive: {exc}"
        raise ValueError(msg) from exc

    # A lone wrapping directory is the source root; top-level files (a flat
    # sdist) leave it at target_dir. Read from disk, not member names, so a
    # sanitised name cannot mislead the choice.
    entries = list(target_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        return entries[0]
    return target_dir
