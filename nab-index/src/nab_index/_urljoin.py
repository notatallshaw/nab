"""Relative URL resolution across supported standard-library implementations."""

from __future__ import annotations

__lazy_modules__ = ()

from typing import TYPE_CHECKING
from urllib.parse import urljoin as _stdlib_urljoin
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["join_url"]


def _legacy_join_url(base_url: str, reference: str) -> str:
    """Clear an explicit empty query on runtimes that otherwise inherit it."""
    joined = _stdlib_urljoin(base_url, reference)
    _, marker, query = reference.partition("#")[0].partition("?")
    if not marker or query.replace("\t", "").replace("\r", "").replace("\n", ""):
        return joined

    base = urlsplit(base_url)
    without_query = urlunsplit((base.scheme, base.netloc, base.path, "", base.fragment))
    return _stdlib_urljoin(without_query, reference)


# Keep corrected standard libraries on their existing per-link call path.
join_url: Callable[[str, str], str]
if urlsplit(_stdlib_urljoin("https://example.invalid/file?inherited=1", "?")).query:
    join_url = _legacy_join_url
else:
    join_url = _stdlib_urljoin
