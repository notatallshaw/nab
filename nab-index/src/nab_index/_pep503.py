"""PEP 503 HTML project-page reading.

:pep:`691` allows an index to answer a JSON request with the HTML
serialization, so the ``file://`` index reader and the remote Simple-API
client both have to read the same anchor-tag shape.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlsplit

from nab_provider.digest import is_hex_digest

if TYPE_CHECKING:
    from ._html_page import Anchor, ProjectPageParser

__all__ = [
    "hash_fragment",
    "json_listing",
    "metadata_declaration",
    "read_page",
]


def _parse(text: str) -> ProjectPageParser:
    """Run the page parser over ``text``."""
    # Deferred to keep html.parser off the CLI's import path.
    from ._html_page import ProjectPageParser  # noqa: PLC0415

    parser = ProjectPageParser()
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
    """Read a URL fragment's hash declarations into the ``hashes`` tuple shape.

    :pep:`503` carries the artefact's hash in the URL fragment; it is the only
    place the hash appears when the index has not opted into :pep:`691` JSON.
    The fragment is a ``&``-separated list of ``key=value`` parts, so a hash
    can sit beside ``egg`` or ``subdirectory`` in any order.  A part keyed by
    a ``hashlib`` algorithm and carrying a hex digest is a hash; anything else
    is not.
    """
    hashes: list[tuple[str, str]] = []

    for part in fragment.split("&"):
        algo, _, digest = part.partition("=")
        algo = algo.lower()
        if algo in hashlib.algorithms_guaranteed and is_hex_digest(digest):
            hashes.append((algo, digest.lower()))

    return tuple(hashes)


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
