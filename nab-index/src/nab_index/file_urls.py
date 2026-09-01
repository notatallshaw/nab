"""File-URL helpers, in a module that imports no index client.

:mod:`nab_index.local_index` reads listings and wheels, so importing it pulls
in :mod:`zipfile` and the HTTP client.  Deciding whether a configured index URL
is a ``file:`` one needs none of that, and that check runs while a command line
is still being read, so the two live apart.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import ParseResult, urlparse, urlsplit

__all__ = ["is_file_url", "parse_file_url"]


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
    """Resolve a ``file://`` URL to the filesystem path it names.

    Uses :func:`urllib.request.url2pathname` so Windows-style drive
    paths (``file:///C:/...``) and percent-encoded characters round-trip
    cleanly across platforms. An empty or ``localhost`` authority (RFC
    8089) means the local machine; any other host becomes a UNC share on
    Windows and is rejected elsewhere.  :mod:`pathlib` accepts a decoded
    null character, which names no file on any platform, so it raises
    :class:`ValueError` here instead.
    """
    return _parsed_file_url_path(urlparse(url), url)


def _parsed_file_url_path(parsed: ParseResult, url: str) -> Path:
    """:func:`parse_file_url` for a caller that already holds the parse.

    ``url`` is the string ``parsed`` came from and is quoted in the errors.
    """
    if parsed.scheme != "file":
        msg = f"expected file:// URL, got {url!r}"
        raise ValueError(msg)

    # Deferred to keep urllib.request off the CLI's import path.
    from urllib.request import url2pathname  # noqa: PLC0415

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
