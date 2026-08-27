"""HTML tokenizing for :pep:`503` project pages.

Imported only when an HTML project page is parsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

from typing_extensions import override

__all__ = [
    "Anchor",
    "ProjectPageParser",
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


class ProjectPageParser(HTMLParser):
    """Collect a project page's anchors and its first ``<base href>``.

    Also records whether the page declares the :pep:`629` repository version.
    """

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[Anchor] = []
        self.base_href: str | None = None
        self.declares_repository_version = False

    @override
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
