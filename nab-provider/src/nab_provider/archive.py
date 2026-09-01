"""Direct-URL archive requirement parsing.

Parses a pip-style archive URL such as
``https://example.com/foo-1.0.tar.gz#sha256=<hex>&subdirectory=pkg`` into
its bare URL, the declared hashes, and any subdirectory.

The download happens in the fetch coordinator, which reads the URL by its
own scheme; which archives are permitted is a policy decision in
:mod:`nab.config.model`.
"""

from __future__ import annotations

from ._value import SlottedValue
from .digest import is_hex_digest
from .records import ACCEPTED_HASH_ALGORITHMS
from .subdir import subdirectory_escapes

__all__ = [
    "ArchiveRequest",
    "ArchiveRequestError",
]


class ArchiveRequestError(Exception):
    """Raised when an archive URL cannot be parsed."""


class ArchiveRequest(SlottedValue):
    """Parsed representation of a direct-URL archive requirement.

    ``url`` is the archive URL with the ``#`` fragment stripped.
    ``hashes`` is the tuple of ``(algorithm, hex-digest)`` pairs read
    from the fragment.  ``subdirectory`` is the project root inside the
    extracted tree, or ``""`` for the archive root.
    """

    __slots__ = ("hashes", "subdirectory", "url")
    __match_args__ = ("url", "hashes", "subdirectory")

    def __init__(
        self,
        url: str,
        hashes: tuple[tuple[str, str], ...],
        subdirectory: str = "",
    ) -> None:
        """Record one parsed archive URL."""
        self.url = url
        self.hashes = hashes
        self.subdirectory = subdirectory

    @property
    def has_usable_hash(self) -> bool:
        """Whether every declared hash carries a non-empty digest.

        PEP 751 requires an archive hash, so a URL with no hash fragment or a
        present algorithm with an empty digest (``#sha256=``) has none.
        """
        return bool(self.hashes) and all(digest for _, digest in self.hashes)

    @classmethod
    def parse(cls, raw_url: str) -> ArchiveRequest:
        """Split ``raw_url`` into its URL, hashes, and subdirectory.

        The fragment holds ``&``-separated ``key=value`` parts: a
        recognised hash algorithm (see :data:`ACCEPTED_HASH_ALGORITHMS`)
        or ``subdirectory``.  An unescaped space in the URL, an unrecognised
        fragment key, or a digest that is not hexadecimal raises
        :class:`ArchiveRequestError`; requiring a hash at all is left to the
        config layer so the error names the
        offending source.
        """
        url, _, fragment = raw_url.partition("#")
        if " " in url:
            msg = (
                f"archive URL {url!r} contains an unescaped space;"
                " percent-encode spaces as %20"
            )
            raise ArchiveRequestError(msg)

        hashes: list[tuple[str, str]] = []
        subdirectory = ""

        for part in fragment.split("&"):
            if not part:
                continue
            key, sep, value = part.partition("=")
            if not sep:
                msg = f"malformed archive URL fragment {part!r} in {raw_url!r}"
                raise ArchiveRequestError(msg)
            if key == "subdirectory":
                subdirectory = value
            elif key in ACCEPTED_HASH_ALGORITHMS:
                # An empty digest is the missing-hash case, left to config.
                if value and not is_hex_digest(value):
                    msg = f"{key} digest {value!r} is not hexadecimal in {raw_url!r}"
                    raise ArchiveRequestError(msg)

                hashes.append((key, value.lower()))
            else:
                msg = (
                    f"unknown archive URL fragment key {key!r} in {raw_url!r};"
                    f" expected one of {', '.join(ACCEPTED_HASH_ALGORITHMS)}"
                    " or subdirectory"
                )
                raise ArchiveRequestError(msg)

        _reject_unsafe_subdirectory(subdirectory, raw_url)
        return cls(url=url, hashes=tuple(hashes), subdirectory=subdirectory)


def _reject_unsafe_subdirectory(subdirectory: str, raw_url: str) -> None:
    """Refuse a subdirectory that escapes the extracted tree."""
    if subdirectory_escapes(subdirectory):
        msg = f"unsafe archive subdirectory {subdirectory!r} in {raw_url!r}"
        raise ArchiveRequestError(msg)
