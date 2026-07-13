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

import re
import zipfile
from email.parser import BytesParser, Parser
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ._naming import canonical as _canonical
from .client import (
    SdistFile,
    WheelFile,
    _extract_sdist_files,
    _parse_sdist_filename,
    _parse_wheel_filename,
)

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = [
    "LocalIndexClient",
    "UnsupportedWheelError",
    "parse_file_url",
    "read_wheel_metadata",
]


class UnsupportedWheelError(Exception):
    """A local wheel's ``.dist-info`` contradicts its own filename.

    Raised when a wheel carries more than one top-level ``.dist-info``
    directory, or a single one whose name does not canonicalise to the
    distribution named by the wheel's filename.
    """


def parse_file_url(url: str) -> Path:
    """Resolve a ``file://`` URL to an absolute filesystem path.

    Uses :func:`urllib.request.url2pathname` so Windows-style drive
    paths (``file:///C:/...``) and percent-encoded characters round-trip
    cleanly across platforms.
    """
    parsed = urlparse(url)
    if parsed.scheme != "file":
        msg = f"expected file:// URL, got {url!r}"
        raise ValueError(msg)
    raw = parsed.path
    if parsed.netloc:
        raw = f"//{parsed.netloc}{raw}"
    return Path(url2pathname(raw))


_REQUIRES_PYTHON_ATTR = "data-requires-python"
_YANKED_ATTR = "data-yanked"
_CORE_METADATA_ATTR = "data-core-metadata"
_LEGACY_METADATA_ATTR = "data-dist-info-metadata"


def _html_advertises_metadata(value: str | None) -> bool:
    """Return True when a metadata-sidecar attribute value advertises a sidecar.

    PEP 658/714 set the value to ``true`` (sidecar exists, no hash) or
    ``<algo>=<hexdigest>``.  Mirrors the JSON path's ``true``/digest
    semantics in :func:`nab_index.client._has_metadata`.
    """
    if value is None:
        return False
    if value == "true":
        return True
    algo, sep, digest = value.partition("=")
    return bool(sep and algo and digest)


class _Pep503Parser(HTMLParser):
    """Collect ``<a href="..." data-requires-python="...">`` entries.

    Anchors carrying ``data-yanked`` (PEP 592) are dropped at parse
    time so the listing never surfaces them.  The PEP 714
    ``data-core-metadata`` attribute (or legacy ``data-dist-info-metadata``)
    is read so a wheel's advertised sidecar is honoured, matching the PEP
    691 JSON behaviour in :func:`nab_index.client._parse_files`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str | None, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href: str | None = None
        requires_python: str | None = None
        yanked = False
        core_metadata: str | None = None
        legacy_metadata: str | None = None
        for name, value in attrs:
            if name == "href":
                href = value
            elif name == _REQUIRES_PYTHON_ATTR:
                requires_python = value
            elif name == _YANKED_ATTR:
                yanked = True
            elif name == _CORE_METADATA_ATTR:
                core_metadata = value
            elif name == _LEGACY_METADATA_ATTR:
                legacy_metadata = value
        if href is not None and not yanked:
            advertised = core_metadata if core_metadata is not None else legacy_metadata
            self.links.append(
                (href, requires_python, _html_advertises_metadata(advertised))
            )


_FLAT_EXTS = re.compile(r"\.(whl|tar\.gz)$", re.IGNORECASE)


def _scan_pep503_directory(
    package_dir: Path,
    canonical: str,
) -> list[WheelFile | SdistFile]:
    """Parse ``<package>/index.html`` and return file records."""
    index_html = package_dir / "index.html"
    if not index_html.exists():
        return []
    parser = _Pep503Parser()
    parser.feed(index_html.read_text(encoding="utf-8"))
    files: list[WheelFile | SdistFile] = []
    for href, requires_python, has_metadata in parser.links:
        filename, file_url, local_path, hashes = _resolve_local_link(href, package_dir)
        if filename is None:
            continue
        record = _make_record(
            filename,
            file_url,
            local_path,
            requires_python,
            hashes,
            canonical,
            has_metadata=has_metadata,
        )
        if record is not None:
            files.append(record)
    return files


def _resolve_local_link(
    href: str,
    package_dir: Path,
) -> tuple[str | None, str, Path | None, tuple[tuple[str, str], ...]]:
    """Resolve an anchor href to ``(filename, url, local_path, hashes)``.

    PEP 503 carries the artefact's hash in the URL fragment as
    ``#<algo>=<hexdigest>``; this is the only place the hash appears
    when the index has not opted into PEP 691 JSON.  The fragment is
    parsed here and surfaced as the file record's ``hashes`` tuple so
    the lockfile writer has something to round-trip.

    ``local_path`` is the artefact's on-disk path when the href names
    a local file, and ``None`` for an ``http``/``https`` href.  It is
    carried so downstream code never has to reverse the ``file:`` URL.
    """
    href_no_frag, _, fragment = href.partition("#")
    hashes = _parse_pep503_hash_fragment(fragment)
    parsed = urlparse(href_no_frag)

    if parsed.scheme in {"http", "https"}:
        filename = unquote(parsed.path.rsplit("/", 1)[-1]) or None
        return (filename, href_no_frag, None, hashes)
    if parsed.scheme == "file":
        path = parse_file_url(href_no_frag)
        return (path.name, href_no_frag, path, hashes)

    # A relative href resolves against the package page wherever it points;
    # the standard mirror layout links to a shared ../../packages/ tree, so the
    # target legitimately sits outside the package directory.
    target = (package_dir.resolve() / unquote(href_no_frag)).resolve()
    return (target.name, target.as_uri(), target, hashes)


def _parse_pep503_hash_fragment(fragment: str) -> tuple[tuple[str, str], ...]:
    """Parse one ``algo=digest`` fragment into the WheelFile.hashes shape."""
    if not fragment:
        return ()
    algo, sep, digest = fragment.partition("=")
    if not sep or not algo or not digest:
        return ()
    return ((algo.lower(), digest.lower()),)


def _scan_flat_wheelhouse(
    root: Path,
    package: str,
) -> list[WheelFile | SdistFile]:
    """Find all dists for ``package`` in a flat directory of files.

    Entries are sorted because the listing order breaks ties between dists at
    one version, and ``iterdir`` order comes from the filesystem.
    """
    canonical = _canonical(package)
    files: list[WheelFile | SdistFile] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            continue
        if _FLAT_EXTS.search(entry.name) is None:
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
    return files


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
            member = _wheel_metadata_member(archive.namelist(), expected)
            if member is None:
                return None
            raw = archive.read(member)
    except (zipfile.BadZipFile, OSError, UnsupportedWheelError):
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
    metadata.  Returns ``None`` when the file is not a readable zip, its name
    is not a wheel filename, or it carries no METADATA member.
    """
    parsed = _parse_wheel_filename(wheel_path.name)
    if parsed is None:
        return None
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            member = _wheel_metadata_member(zf.namelist(), parsed[0])
            if member is None:
                return None
            return zf.read(member).decode("utf-8")
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError):
        return None


def _wheel_metadata_member(names: list[str], expected: str) -> str | None:
    """Return ``expected``'s own top-level ``*.dist-info/METADATA`` member.

    ``expected`` is the wheel's canonical name from its filename.  Returns
    ``None`` when no top-level ``.dist-info`` holds a METADATA file.  Raises
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
        """Hold the resolved root path for ``index_url``."""
        self._root = parse_file_url(index_url)

    async def aclose(self) -> None:
        """No-op; nothing to release."""

    async def __aenter__(self) -> Self:
        """Return self for ``async with``."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """No-op exit."""

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        """Return all distribution files known for ``package``."""
        canonical = _canonical(package)
        package_dir = self._root / canonical
        if (package_dir / "index.html").is_file():
            return _scan_pep503_directory(package_dir, canonical)

        if not self._root.is_dir():
            return []
        return _scan_flat_wheelhouse(self._root, package)

    async def get_metadata_text(
        self,
        package: str,  # noqa: ARG002 - matches CachedAsyncSimpleClient signature
        version: str,  # noqa: ARG002
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,  # noqa: ARG002
    ) -> str:
        """Return PEP 658 metadata text for a wheel sitting on disk.

        The on-disk sidecar is trusted, so ``metadata_hash`` is accepted
        only to match the remote client signature and is not verified.
        """
        path = parse_file_url(metadata_url)
        return path.read_text(encoding="utf-8")

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
        path = parse_file_url(sdist_url)
        return _extract_sdist_files(path.read_bytes())

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
        path = parse_file_url(sdist_url)
        return path.read_bytes()
