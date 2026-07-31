"""PEP 503 HTML project-page reading.

:pep:`691` allows an index to answer a JSON request with the HTML
serialization, so the ``file://`` index reader and the remote Simple-API
client both have to read the same anchor-tag shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

__all__ = [
    "Anchor",
    "hash_fragment",
    "json_listing",
    "metadata_declaration",
    "read_page",
]

_REQUIRES_PYTHON_ATTR = "data-requires-python"
_YANKED_ATTR = "data-yanked"
_CORE_METADATA_ATTR = "data-core-metadata"
_LEGACY_METADATA_ATTR = "data-dist-info-metadata"
_UPLOAD_TIME_ATTR = "data-upload-time"
_REPOSITORY_VERSION_META = "pypi:repository-version"

# HTML's ASCII whitespace set: a URL attribute's value may be surrounded by it.
_HTML_WHITESPACE = "\t\n\f\r "


@dataclass(frozen=True, slots=True)
class Anchor:
    """One ``<a>`` link on a project page.

    ``metadata`` is the :pep:`714` ``data-core-metadata`` value, falling back
    to the legacy ``data-dist-info-metadata`` when only that is set, and
    ``None`` when the anchor declares neither.

    ``upload_time`` is the ``data-upload-time`` value. No specification
    covers it (:pep:`700` defines ``upload-time`` for the JSON serialization
    only), so an index is free to omit it, and PyPI and download.pytorch.org
    both do. It is read because it is the only upload time an HTML page can
    carry.
    """

    href: str
    requires_python: str | None
    metadata: str | None
    yanked: bool
    upload_time: str | None


def _anchor(attrs: list[tuple[str, str | None]]) -> Anchor | None:
    """Build an :class:`Anchor` from a tag's attributes, or ``None`` if hrefless."""
    href: str | None = None
    requires_python: str | None = None
    yanked = False
    core_metadata: str | None = None
    legacy_metadata: str | None = None
    upload_time: str | None = None

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
        elif name == _UPLOAD_TIME_ATTR:
            upload_time = value

    if href is None:
        return None

    metadata = core_metadata if core_metadata is not None else legacy_metadata
    return Anchor(
        href.strip(_HTML_WHITESPACE), requires_python, metadata, yanked, upload_time
    )


class _ProjectPageParser(HTMLParser):
    """Collect a project page's anchors and its first ``<base href>``.

    Also records whether the page declares the :pep:`629` repository version.
    """

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[Anchor] = []
        self.base_href: str | None = None
        self.declares_repository_version = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            for name, value in attrs:
                if name == "name" and value == _REPOSITORY_VERSION_META:
                    self.declares_repository_version = True
                    break
            return

        if tag == "base":
            if self.base_href is None:
                for name, value in attrs:
                    if name == "href":
                        self.base_href = value
                        break
            return

        if tag != "a":
            return
        anchor = _anchor(attrs)
        if anchor is not None:
            self.anchors.append(anchor)


def _parse(text: str) -> _ProjectPageParser:
    """Run the page parser over ``text``."""
    parser = _ProjectPageParser()
    parser.feed(text)
    return parser


def read_page(text: str) -> tuple[list[Anchor], str | None]:
    """Return a project page's anchors and its ``<base href>``, if it has one.

    Relative anchors resolve against the base href rather than the page
    directory, matching how pip and uv read a Simple-repository page.
    """
    parser = _parse(text)
    return (parser.anchors, parser.base_href)


def metadata_declaration(value: str | None) -> bool | dict[str, str] | None:
    """Translate a metadata-sidecar attribute value into its PEP 691 JSON form.

    :pep:`658`/:pep:`714` set the value to ``true`` (sidecar exists, no
    published hash) or ``<algo>=<hexdigest>``.  Anything else declares no
    sidecar and yields ``None``.
    """
    if value is None:
        return None
    if value == "true":
        return True
    algo, sep, digest = value.partition("=")
    if sep and algo and digest:
        return {algo: digest}
    return None


def hash_fragment(fragment: str) -> tuple[tuple[str, str], ...]:
    """Parse one ``algo=digest`` URL fragment into the ``hashes`` tuple shape.

    :pep:`503` carries the artefact's hash in the URL fragment; it is the only
    place the hash appears when the index has not opted into :pep:`691` JSON.
    """
    if not fragment:
        return ()
    algo, sep, digest = fragment.partition("=")
    if not sep or not algo or not digest:
        return ()
    return ((algo.lower(), digest.lower()),)


def _file_entry(anchor: Anchor, base_url: str) -> dict[str, object] | None:
    """Render one anchor as a PEP 691 file entry, or ``None`` if it names no file.

    An href with a malformed authority (an unterminated IPv6 bracket) makes
    both the join and the split raise, so it is dropped like an href that
    names no file rather than failing the whole listing.
    """
    try:
        url, _, fragment = urljoin(base_url, anchor.href).partition("#")
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    except ValueError:
        return None
    if not filename:
        return None

    entry: dict[str, object] = {"filename": filename, "url": url}
    if anchor.requires_python is not None:
        entry["requires-python"] = anchor.requires_python

    hashes = dict(hash_fragment(fragment))
    if hashes:
        entry["hashes"] = hashes

    metadata = metadata_declaration(anchor.metadata)
    if metadata is not None:
        entry["core-metadata"] = metadata

    if anchor.upload_time is not None:
        entry["upload-time"] = anchor.upload_time
    if anchor.yanked:
        entry["yanked"] = True

    return entry


def json_listing(text: str, page_url: str) -> bytes:
    """Re-serialize a PEP 503 project page as a PEP 691 JSON listing body.

    Hrefs are resolved here, against the page's ``<base href>`` when it has
    one and otherwise against ``page_url``, so the cached body stands on its
    own.

    Raises :class:`ValueError` when the page's ``<base href>`` cannot be
    parsed. Every relative anchor resolves against it, so the whole page's
    targets are unknown; the caller maps this to its own malformed-listing
    error. A single unparseable anchor is dropped instead.

    Also raises :class:`ValueError` for a page carrying neither a link nor
    the :pep:`629` ``pypi:repository-version`` marker, since nothing in it
    says it is a project page. An empty listing means "package absent" to
    the multi-index router (see :func:`nab_index.client._parse_files`), so a
    site error page served with a 200 would otherwise fall through to a
    lower-priority index and risk pinning a different version.
    """
    parser = _parse(text)
    anchors, base_href = parser.anchors, parser.base_href
    if not anchors and not parser.declares_repository_version:
        msg = (
            "body has no links and no PEP 629 repository-version marker, "
            "so it is not a project page"
        )
        raise ValueError(msg)
    base_url = page_url
    if base_href is not None:
        try:
            base_url = urljoin(page_url, base_href)
        except ValueError as exc:
            msg = f"unparseable <base href> {base_href!r}: {exc}"
            raise ValueError(msg) from exc
    entries = (_file_entry(anchor, base_url) for anchor in anchors)
    files = [entry for entry in entries if entry is not None]
    return json.dumps({"files": files}).encode("utf-8")
