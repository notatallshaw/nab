"""URL forms suitable for diagnostic and serialized output."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

__all__ = ["strip_userinfo"]


def strip_userinfo(url: str) -> str:
    """Remove credential userinfo while retaining SSH logins and URL components."""
    parts = urlsplit(url)
    userinfo, sep, host = parts.netloc.rpartition("@")
    if not sep:
        return url

    # SSH uses the login name to identify the remote account.
    if parts.scheme.endswith("ssh"):
        login = userinfo.split(":", 1)[0]
        host = f"{login}@{host}"

    return urlunsplit(parts._replace(netloc=host))
