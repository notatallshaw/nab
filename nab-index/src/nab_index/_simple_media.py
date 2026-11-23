"""Media types for Simple API listing requests and responses."""

from __future__ import annotations

from nab_provider.serialization import SimpleSerialization

# Prefer JSON to HTML during negotiation. The HTML pin also accepts text/html,
# the PEP 691 alias for the original HTML media type.
_ACCEPT: dict[SimpleSerialization, str] = {
    SimpleSerialization.NEGOTIATE: (
        "application/vnd.pypi.simple.v1+json, "
        "application/vnd.pypi.simple.v1+html;q=0.2, "
        "text/html;q=0.01"
    ),
    SimpleSerialization.JSON: "application/vnd.pypi.simple.v1+json",
    SimpleSerialization.HTML: "application/vnd.pypi.simple.v1+html, text/html;q=0.01",
}


def simple_accept_header(serialization: SimpleSerialization) -> str:
    """Return the ``Accept`` header for a listing request under ``serialization``."""
    return _ACCEPT[serialization]


def is_html_listing(content_type: str | None) -> bool:
    """Return True when a Content-Type names an HTML Simple-API serialization.

    Covers :pep:`503`'s ``text/html`` and :pep:`691`'s
    ``application/vnd.pypi.simple.vN+html``.
    """
    if content_type is None:
        return False
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type == "text/html" or media_type.endswith("+html")
