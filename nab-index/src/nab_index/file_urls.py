"""File-URL helpers without index-client imports."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import ParseResult, unquote, urlparse, urlsplit

__all__ = ["is_file_url", "parse_file_url"]


def is_file_url(url: str) -> bool:
    """Return True for either RFC 8089 form of a ``file:`` URL.

    An authority :func:`urlsplit` cannot parse, such as an unterminated IPv6
    bracket, is not one.
    """
    try:
        return urlsplit(url).scheme == "file"
    except ValueError:
        return False


def parse_file_url(url: str) -> Path:
    """Return the filesystem path named by a ``file:`` URL.

    POSIX percent escapes use the filesystem encoding with surrogateescape.
    Windows paths use :func:`urllib.request.url2pathname`.
    Empty and ``localhost`` authorities name local paths (RFC 8089).
    Other hosts name UNC shares on Windows and are rejected elsewhere.

    Raise :class:`ValueError` for decoded null characters or paths rejected by
    :func:`urllib.request.url2pathname`.
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
    url_path = parsed.path

    if not netloc or netloc == "localhost":
        netloc = ""
        if url_path.startswith("//") and not _is_windows_drive_root(url_path):
            # Preserve the path root through url2pathname's authority parsing.
            url_path = "/%2F" + url_path[2:]
    elif sys.platform == "win32":
        netloc = "\\\\" + netloc
    else:
        msg = f"non-local file:// URL is not supported on this platform: {url!r}"
        raise ValueError(msg)

    try:
        if sys.platform == "win32":
            path = url2pathname(netloc + url_path)
        else:
            path = unquote(
                url_path,
                encoding=sys.getfilesystemencoding(),
                errors="surrogateescape",
            )
    except OSError as exc:
        msg = f"file:// URL {url!r} does not name a path: {exc}"
        raise ValueError(msg) from exc

    if "\x00" in path:
        msg = f"file:// URL decodes to a path containing a null character: {url!r}"
        raise ValueError(msg)

    return Path(path)


def _is_windows_drive_root(url_path: str) -> bool:
    """Check the drive prefix of a ``//``-rooted Windows path."""
    letter, colon = url_path[2:3], url_path[3:4]
    return (
        sys.platform == "win32"
        and colon == ":"
        and letter.isascii()
        and letter.isalpha()
    )
