"""Local file:// index support for nab-index.

Two flavours, both keyed off a ``file://`` URL pointing at a directory:

* PEP 503 directory: ``<root>/<package>/index.html`` HTML listings,
  same anchor-tag shape that pip and uv recognise. Synthesised
  :class:`~nab_index.client.WheelFile` /
  :class:`~nab_index.client.SdistFile` records mirror what an HTTPS
  Simple API returns.
* Flat wheelhouse: a directory containing ``.whl`` and ``.tar.gz``
  files at the top level (pip's ``--find-links ./wheels`` shape).
  On-disk filenames are parsed for ``(name, version)`` and every
  distribution for a package is returned by ``get_files``.  ``.zip``
  sdists are ignored, matching the remote-index behaviour.

Reads run synchronously off the filesystem; the filesystem is the
cache. The async surface is a thin shim over the sync helpers so the
multi-index router can treat local and remote indexes uniformly.
"""

from __future__ import annotations

import errno
import lzma
import os
import re
import stat
import sys
import zipfile
import zlib
from email.parser import BytesParser, Parser
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import url2pathname

from packaging.utils import canonicalize_name as _canonical
from packaging.utils import parse_sdist_filename

from nab_provider.errors import IndexAccessError, UnsupportedWheelError

from ._pep503 import hash_fragment, metadata_declaration, read_page
from .client import (
    SdistFile,
    WheelFile,
    _extract_sdist_files,
    _parse_sdist_filename,
    _parse_wheel_filename,
    is_readable_filename,
)

if TYPE_CHECKING:
    from packaging.utils import NormalizedName
    from typing_extensions import Self

    from .lazy_wheel import RangeMetadataResult

__all__ = [
    "LocalIndexClient",
    "LocalIndexError",
    "MalformedLocalListingError",
    "NonLocalArtifactError",
    "UnreadableLocalIndexError",
    "UnsupportedWheelError",
    "is_file_url",
    "parse_file_url",
    "read_wheel_metadata",
    "wheel_metadata_member",
]


class LocalIndexError(IndexAccessError):
    """A ``file://`` index could not produce a listing or serve an artifact.

    Raising, rather than returning an empty listing, keeps an index that
    failed from reading as an absent package.
    """


class UnreadableLocalIndexError(LocalIndexError):
    """A path under a ``file://`` index could not be read.

    Distinct from :class:`MalformedLocalListingError` because the content was
    never seen: a wheelhouse the process cannot list, or an ``index.html`` it
    cannot stat or open, is a permission or mount fault rather than a bad
    listing.
    """


class MalformedLocalListingError(LocalIndexError):
    """A ``file://`` index's ``index.html`` or metadata sidecar does not parse."""


class NonLocalArtifactError(LocalIndexError):
    """A ``file://`` index advertised an artifact URL a local client cannot serve.

    A :pep:`503` repository page may link to absolute ``http(s)`` artifact
    URLs, so the listing is legal, but a filesystem-backed index cannot fetch
    a remote artifact.
    """


def is_file_url(url: str) -> bool:
    """Return True when ``url`` is a ``file:`` URL in either RFC 8089 spelling.

    An authority :func:`urlsplit` cannot parse, such as an unterminated IPv6
    bracket, is not one.
    """
    try:
        return urlsplit(url).scheme == "file"
    except ValueError:
        return False


def parse_file_url(url: str) -> Path:
    """Resolve a ``file://`` URL to an absolute filesystem path.

    Uses :func:`urllib.request.url2pathname` so Windows-style drive
    paths (``file:///C:/...``) and percent-encoded characters round-trip
    cleanly across platforms. An empty or ``localhost`` authority (RFC
    8089) means the local machine; any other host becomes a UNC share on
    Windows and is rejected elsewhere.  :mod:`pathlib` accepts a decoded
    null character, which names no file on any platform, so it raises
    :class:`ValueError` here instead.
    """
    parsed = urlparse(url)
    if parsed.scheme != "file":
        msg = f"expected file:// URL, got {url!r}"
        raise ValueError(msg)

    netloc = parsed.netloc
    if not netloc or netloc == "localhost":
        netloc = ""
    elif sys.platform == "win32":
        netloc = "\\\\" + netloc
    else:
        msg = f"non-local file:// URL is not supported on this platform: {url!r}"
        raise ValueError(msg)

    path = url2pathname(netloc + parsed.path)
    if "\x00" in path:
        msg = f"file:// URL decodes to a path containing a null character: {url!r}"
        raise ValueError(msg)

    return Path(path)


def _resolve_served_path(url: str) -> Path:
    """Resolve a served-artifact URL to a local path.

    :func:`parse_file_url` raises :class:`ValueError` for an ``http(s)`` or
    non-local ``file://`` URL; re-raise it as :class:`NonLocalArtifactError` so
    the fetch fails through the index-error path.
    """
    try:
        return parse_file_url(url)
    except ValueError as exc:
        msg = f"local file:// index cannot serve artifact {url!r}"
        raise NonLocalArtifactError(msg) from exc


def _read_served_bytes(path: Path, kind: str) -> bytes:
    """Read a served local artifact's bytes, mapping a read failure.

    A missing or unreadable file raises :class:`UnreadableLocalIndexError` so
    the fetch fails through the index-error path, matching a remote index's
    404 rather than a raw :class:`OSError`.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        msg = f"cannot read local {kind} {path}: {exc}"
        raise UnreadableLocalIndexError(msg) from exc


# The errnos pathlib itself treats as "no such file".
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})

# Windows: drive not ready, invalid name, symlink loop.
_ABSENT_WINERRORS = frozenset({21, 123, 1921})


def _stat_mode(path: Path) -> int | None:
    """Return ``path``'s ``st_mode``, or ``None`` when nothing is there.

    Only an error meaning the path is absent gives ``None``, so a broken entry
    skips instead of failing the listing it sits in. Anything else raises: from
    Python 3.14 ``Path.exists`` and ``Path.is_file`` swallow every
    :class:`OSError`, so an unreadable path would read as absent.
    """
    try:
        return path.stat().st_mode
    except OSError as exc:
        if (
            exc.errno in _ABSENT_ERRNOS
            or getattr(exc, "winerror", None) in _ABSENT_WINERRORS
        ):
            return None
        raise


def _is_file(path: Path) -> bool:
    """Return whether ``path`` is an existing regular file."""
    mode = _stat_mode(path)
    return mode is not None and stat.S_ISREG(mode)


def _is_dir(path: Path) -> bool:
    """Return whether ``path`` is an existing directory."""
    mode = _stat_mode(path)
    return mode is not None and stat.S_ISDIR(mode)


_FLAT_EXTS = re.compile(r"\.(whl|tar\.gz)$", re.IGNORECASE)


def _scan_pep503_directory(
    package_dir: Path,
    canonical: str,
) -> tuple[list[WheelFile | SdistFile], bool]:
    """Parse ``<package>/index.html`` and return file records.

    ``package_dir`` has to be absolute, because the page's URI is the base
    for its relative links.

    The second element says the page linked a file in a format nab does
    not read, which tells a page of ``.zip`` sdists from an empty one.
    """
    index_html = package_dir / "index.html"
    if not _is_file(index_html):
        return [], False

    try:
        text = index_html.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{index_html} is not valid UTF-8: {exc}"
        raise MalformedLocalListingError(msg) from exc

    anchors, base_href = read_page(text)

    # RFC 3986 section 5.1.3: the base is the URI the page was read from, not
    # its realpath.
    base_url = index_html.as_uri()
    if base_href is not None:
        # A base href every relative anchor resolves against, so one that
        # cannot be parsed leaves the whole page's targets unknown. Fail
        # loudly rather than fall back to the page URL, which would resolve
        # each link to a different file than the page names.
        try:
            base_url = urljoin(base_url, base_href)
        except ValueError as exc:
            msg = f"{index_html} has an unparseable <base href>: {exc}"
            raise MalformedLocalListingError(msg) from exc

    files: list[WheelFile | SdistFile] = []
    unreadable = False

    for anchor in anchors:
        # PEP 592: a yanked link never reaches the listing.
        if anchor.yanked:
            continue

        filename, file_url, local_path, hashes = _resolve_local_link(
            anchor.href, base_url
        )
        if filename is None:
            continue
        if not is_readable_filename(filename):
            unreadable = True
            continue

        record = _make_record(
            filename,
            file_url,
            local_path,
            anchor.requires_python,
            hashes,
            canonical,
            has_metadata=metadata_declaration(anchor.metadata) is not None,
        )
        if record is not None:
            files.append(record)
    return files, unreadable


def _resolve_local_link(
    href: str,
    base_url: str,
) -> tuple[str | None, str, Path | None, tuple[tuple[str, str], ...]]:
    """Resolve an anchor href to ``(filename, url, local_path, hashes)``.

    ``base_url`` is the page's ``<base href>`` when it carries one, else the
    ``index.html`` URL.  An href is a URL reference, so only its path
    component names the artefact, and the target may sit outside the package
    directory: the standard mirror layout links to a shared
    ``../../packages/`` tree.

    The href's hash fragment is surfaced as the file record's ``hashes``
    tuple so the lockfile writer has something to round-trip.

    ``local_path`` is the artefact's on-disk path when the href names
    a local file, and ``None`` for an ``http``/``https`` href.  It is
    carried so downstream code never has to reverse the ``file:`` URL.
    """
    href_no_frag, _, fragment = href.partition("#")
    hashes = hash_fragment(fragment)

    # A malformed authority (an unterminated IPv6 bracket) makes these raise,
    # so the drop guard has to start here rather than at the path resolution
    # below.  urljoin leaves an href alone when its scheme differs from the
    # page's, so the split round trip is what drops a tab, CR or LF.
    try:
        url = urlunsplit(urlsplit(urljoin(base_url, href_no_frag)))
        parsed = urlparse(url)
    except ValueError:
        return (None, href_no_frag, None, hashes)

    if parsed.scheme in {"http", "https"}:
        filename = unquote(parsed.path.rsplit("/", 1)[-1]) or None
        return (filename, url, None, hashes)

    # Drop an anchor naming no local file rather than fail the whole listing.
    try:
        path = parse_file_url(url)
    except ValueError:
        return (None, url, None, hashes)

    return (path.name, url, path, hashes)


def _scan_flat_wheelhouse(
    root: Path,
    package: str,
) -> tuple[list[WheelFile | SdistFile], bool]:
    """Find all dists for ``package`` in a flat directory of files.

    Entries are sorted because the listing order breaks ties between dists at
    one version, and ``iterdir`` order comes from the filesystem.

    The second element says the directory holds a ``.zip`` sdist for
    ``package``, a format nab does not read.  One directory serves every
    package, so a file that does not name ``package`` says nothing about it.
    """
    canonical = _canonical(package)
    files: list[WheelFile | SdistFile] = []
    unreadable = False

    for entry in sorted(root.iterdir()):
        if not _is_file(entry):
            continue
        if _FLAT_EXTS.search(entry.name) is None:
            unreadable = unreadable or _is_zip_sdist(entry.name, canonical)
            continue
        requires_python = _flat_requires_python(entry, canonical)
        record = _make_record(
            entry.name,
            entry.as_uri(),
            entry,
            requires_python,
            (),
            canonical,
            has_metadata=False,
        )
        if record is not None:
            files.append(record)
    return files, unreadable


def _is_zip_sdist(filename: str, canonical: str) -> bool:
    """Whether ``filename`` is a ``.zip`` sdist belonging to ``canonical``."""
    if not filename.endswith(".zip"):
        return False
    try:
        name, _ = parse_sdist_filename(filename)
    except ValueError:
        # InvalidSdistFilename, or int() refusing a digit run past CPython's limit.
        return False
    return name == canonical


def _flat_requires_python(entry: Path, canonical: str) -> str | None:
    """Read a flat-wheelhouse dist's ``Requires-Python``; not in the filename."""
    wheel = _parse_wheel_filename(entry.name)
    if wheel is not None:
        if wheel[0] != canonical:
            return None
        return _read_wheel_requires_python(entry, canonical)
    sdist = _parse_sdist_filename(entry.name)
    if sdist is not None and sdist[0] == canonical:
        return _read_sdist_requires_python(entry)
    return None


def _read_sdist_requires_python(sdist_path: Path) -> str | None:
    """Return ``Requires-Python`` from an sdist's PKG-INFO, or ``None``."""
    try:
        data = sdist_path.read_bytes()
    except OSError:
        return None

    pkg_info, _ = _extract_sdist_files(data)
    if pkg_info is None:
        return None

    value = Parser().parsestr(pkg_info, headersonly=True).get("Requires-Python")
    return value if isinstance(value, str) else None


def _read_wheel_requires_python(wheel_path: Path, expected: str) -> str | None:
    """Return ``Requires-Python`` from a wheel's METADATA, or ``None``."""
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            member = wheel_metadata_member(archive.namelist(), expected)
            if member is None:
                return None
            raw = archive.read(member)
    except (
        zipfile.BadZipFile,
        OSError,
        UnsupportedWheelError,
        zlib.error,
        lzma.LZMAError,
        RuntimeError,
    ):
        return None

    value = BytesParser().parsebytes(raw, headersonly=True).get("Requires-Python")
    return value if isinstance(value, str) else None


def _make_record(
    filename: str,
    file_url: str,
    local_path: Path | None,
    requires_python: str | None,
    hashes: tuple[tuple[str, str], ...],
    expected: str,
    *,
    has_metadata: bool,
) -> WheelFile | SdistFile | None:
    """Build a file record, or ``None`` for unusable filenames.

    Files whose parsed canonical name does not match ``expected`` are
    dropped; see :func:`nab_index.client._parse_files` for the
    phantom-version failure this prevents.
    """
    parsed = _parse_wheel_filename(filename)
    if parsed is not None:
        parsed_name, version = parsed
        if parsed_name != expected:
            return None
        return WheelFile(
            filename=filename,
            url=file_url,
            version=version,
            requires_python=requires_python,
            has_metadata=has_metadata,
            upload_time=None,
            hashes=hashes,
            local_path=local_path,
        )
    parsed = _parse_sdist_filename(filename)
    if parsed is not None:
        parsed_name, version = parsed
        if parsed_name != expected:
            return None
        return SdistFile(
            filename=filename,
            url=file_url,
            version=version,
            requires_python=requires_python,
            upload_time=None,
            hashes=hashes,
            local_path=local_path,
        )
    return None


def read_wheel_metadata(wheel_path: Path) -> str | None:
    """Return a wheel's ``<name>-<version>.dist-info/METADATA`` text.

    The ``.dist-info`` directory must name the wheel's own distribution
    (taken from its filename); a wheel with several top-level ``.dist-info``
    directories, or one naming a different distribution, raises
    :class:`UnsupportedWheelError` rather than reading another package's
    metadata.  Returns ``None`` when the archive cannot be parsed, its name is
    not a wheel filename, or it carries no METADATA member.  A wheel that
    cannot be opened raises :class:`UnreadableLocalIndexError` instead, so a
    permission or mount fault is not read as a wheel that declares nothing.
    """
    parsed = _parse_wheel_filename(wheel_path.name)
    if parsed is None:
        return None
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            member = wheel_metadata_member(zf.namelist(), parsed[0])
            if member is None:
                return None
            return zf.read(member).decode("utf-8")
    except OSError as exc:
        msg = f"cannot read local wheel {wheel_path}: {exc}"
        raise UnreadableLocalIndexError(msg) from exc
    except (
        zipfile.BadZipFile,
        UnicodeDecodeError,
        zlib.error,
        lzma.LZMAError,
        RuntimeError,
    ):
        return None


def wheel_metadata_member(names: list[str], expected: str) -> str | None:
    """Return ``expected``'s own top-level ``*.dist-info/METADATA`` member.

    ``expected`` is the wheel's canonical name.  Both the local wheel reader
    and the HTTP range reader select the METADATA member through this one
    helper, so the two paths agree on what counts as a wheel's own metadata.
    Returns ``None`` when no top-level ``.dist-info`` holds a METADATA file.
    Raises
    :class:`UnsupportedWheelError` when the wheel carries several top-level
    ``.dist-info`` directories, or a single one whose name does not
    canonicalise to ``expected``.
    """
    info_dirs = sorted(
        {
            head
            for head, sep, _ in (name.partition("/") for name in names)
            if sep and head.endswith(".dist-info")
        }
    )
    if not info_dirs:
        return None

    if len(info_dirs) > 1:
        joined = ", ".join(info_dirs)
        msg = f"wheel for {expected!r} has multiple .dist-info directories: {joined}"
        raise UnsupportedWheelError(msg)

    info_dir = info_dirs[0]
    if _dist_info_name(info_dir) != expected:
        msg = (
            f"wheel for {expected!r} carries .dist-info directory {info_dir!r} "
            f"for a different distribution"
        )
        raise UnsupportedWheelError(msg)

    member = f"{info_dir}/METADATA"
    return member if member in names else None


def _dist_info_name(info_dir: str) -> str:
    """Return the canonical distribution name from a ``.dist-info`` dir name."""
    stem = info_dir.removesuffix(".dist-info")
    return _canonical(stem.rsplit("-", 1)[0])


class LocalIndexClient:
    """File-system-backed index client.

    Speaks the same surface as :class:`CachedAsyncSimpleClient` so the
    multi-index router can use it without branches.  Each ``get_files``
    call auto-detects layout: PEP 503 if ``<root>/<package>/index.html``
    exists, flat wheelhouse otherwise.  Mixed roots work; the choice is
    re-made per package.
    """

    def __init__(self, index_url: str) -> None:
        """Hold the absolute root path for ``index_url``.

        A ``file:`` URL may be cwd-relative; artefact URLs have to be absolute.
        Dropping the dot segments must not follow symlinks, so a page keeps the
        path it was reached by.
        """
        root = parse_file_url(index_url)
        self._root = Path(os.path.abspath(root))  # noqa: PTH100
        self._unreadable_only: set[str] = set()

    async def aclose(self) -> None:
        """No-op; nothing to release."""

    async def __aenter__(self) -> Self:
        """Return self for ``async with``."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """No-op exit."""

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        """Return all distribution files known for ``package``.

        An index directory or listing the process cannot read raises
        :class:`UnreadableLocalIndexError` so the listing fails through the
        index-error path rather than raising a raw :class:`OSError` or
        returning an empty list that reads as "package absent".
        """
        canonical = _canonical(package)
        package_dir = self._root / canonical

        try:
            if _is_file(package_dir / "index.html"):
                files, unreadable = _scan_pep503_directory(package_dir, canonical)
            elif not _is_dir(self._root):
                files, unreadable = [], False
            else:
                files, unreadable = _scan_flat_wheelhouse(self._root, package)
        except OSError as exc:
            msg = f"cannot read local index {self._root}: {exc}"
            raise UnreadableLocalIndexError(msg) from exc

        if not files and unreadable:
            self._unreadable_only.add(package)
        return files

    def served_unreadable_only(self, package: str) -> bool:
        """Whether a listing for ``package`` held only files nab cannot read."""
        return package in self._unreadable_only

    async def get_metadata_text(
        self,
        package: str,  # noqa: ARG002 - matches CachedAsyncSimpleClient signature
        version: str,  # noqa: ARG002
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,  # noqa: ARG002
    ) -> str | None:
        """Return metadata text for a wheel sitting on disk.

        A wheel that publishes a sidecar is asked for at the sidecar's URL; one
        that does not is asked for at its own, and its METADATA is read out of
        the wheel.  A wheel that is not a readable zip, carries no METADATA
        member, or whose ``.dist-info`` names another distribution answers
        ``None``.  One the process cannot open raises
        :class:`UnreadableLocalIndexError`, like a sidecar.

        The on-disk sidecar is trusted, so ``metadata_hash`` is accepted only
        to match the remote client signature and is not verified.  A missing
        or unreadable sidecar raises :class:`UnreadableLocalIndexError` and a
        non-UTF-8 one :class:`MalformedLocalListingError`, so no raw
        :class:`OSError` or :class:`UnicodeDecodeError` escapes.
        """
        path = _resolve_served_path(metadata_url)
        if path.suffix == ".whl":
            try:
                return read_wheel_metadata(path)
            except UnsupportedWheelError:
                return None
        data = _read_served_bytes(path, "metadata sidecar")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"{path} is not valid UTF-8: {exc}"
            raise MalformedLocalListingError(msg) from exc

    async def get_sdist_files(
        self,
        package: str,  # noqa: ARG002 - matches CachedAsyncSimpleClient signature
        version: str,  # noqa: ARG002
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),  # noqa: ARG002
    ) -> tuple[str | None, str | None]:
        """Return ``(pkg_info, pyproject_toml)`` extracted from the sdist.

        On-disk archives are trusted, so ``sdist_hashes`` matches the remote
        client signature but is not verified.
        """
        path = _resolve_served_path(sdist_url)
        return _extract_sdist_files(_read_served_bytes(path, "sdist"))

    async def get_sdist_archive(
        self,
        package: str,  # noqa: ARG002 - matches CachedAsyncSimpleClient signature
        version: str,  # noqa: ARG002
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),  # noqa: ARG002
    ) -> bytes:
        """Return the raw bytes of an sdist archive sitting on disk.

        On-disk archives are trusted, so ``sdist_hashes`` matches the remote
        client signature but is not verified.
        """
        path = _resolve_served_path(sdist_url)
        return _read_served_bytes(path, "sdist")

    async def get_range_metadata(
        self,
        package: str,  # noqa: ARG002 - matches CachedAsyncSimpleClient signature
        version: str,  # noqa: ARG002
        wheel_url: str,  # noqa: ARG002
        canonical_name: NormalizedName,  # noqa: ARG002
        wheel_hashes: tuple[tuple[str, str], ...] = (),  # noqa: ARG002
    ) -> RangeMetadataResult:
        """Return the no-source result.

        A local wheel is read through the resolver's ``local_path`` branch, not
        over HTTP, so there is no range read to perform here.
        """
        # Imported inside the method to break the lazy_wheel <-> local_index
        # import cycle: lazy_wheel imports this module's shared member selector.
        from .lazy_wheel import RangeMetadataResult, RangeOutcome  # noqa: PLC0415

        return RangeMetadataResult(None, RangeOutcome.UNSUPPORTED)
