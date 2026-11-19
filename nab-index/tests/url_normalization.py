"""Reference result from the standard library URL parser."""

from urllib.parse import urljoin, urlsplit, urlunsplit

BASE = "https://index.example/simple/pkg/"


def expected_url(raw: str) -> str | None:
    try:
        joined = raw if raw.startswith(("https://", "http://")) else urljoin(BASE, raw)
        normalized = urlunsplit(urlsplit(joined))
        normalized.encode()
    except ValueError:
        return None
    return normalized
