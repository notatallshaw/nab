"""Direct-URL archive requirement parsing for nab-index.

Parses a pip-style archive URL such as
``https://example.com/foo-1.0.tar.gz#sha256=<hex>&subdirectory=pkg`` into
its bare URL, the declared hashes, and any subdirectory.

The download and hash verification happen in the fetch coordinator (the
same path a remote sdist takes); which archives are permitted is a
policy decision in :mod:`nab_python.config`.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .client import _ACCEPTED_HASH_ALGORITHMS

__all__ = [
    "ArchiveRequest",
    "ArchiveRequestError",
]


class ArchiveRequestError(Exception):
    """Raised when an archive URL cannot be parsed."""


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    """Parsed representation of a direct-URL archive requirement.

    ``url`` is the archive URL with the ``#`` fragment stripped.
    ``hashes`` is the tuple of ``(algorithm, hex-digest)`` pairs read
    from the fragment.  ``subdirectory`` is the project root inside the
    extracted tree, or ``""`` for the archive root.
    """

    url: str
    hashes: tuple[tuple[str, str], ...]
    subdirectory: str = ""

    @classmethod
    def parse(cls, raw_url: str) -> ArchiveRequest:
        """Split ``raw_url`` into its URL, hashes, and subdirectory.

        The fragment holds ``&``-separated ``key=value`` parts: a
        recognised hash algorithm (see :data:`_ACCEPTED_HASH_ALGORITHMS`)
        or ``subdirectory``.  Any other key raises
        :class:`ArchiveRequestError`; requiring a hash is left to the
        config layer so the error names the offending source.
        """
        url, _, fragment = raw_url.partition("#")
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
            elif key in _ACCEPTED_HASH_ALGORITHMS:
                hashes.append((key, value.lower()))
            else:
                msg = (
                    f"unknown archive URL fragment key {key!r} in {raw_url!r};"
                    f" expected one of {', '.join(_ACCEPTED_HASH_ALGORITHMS)}"
                    " or subdirectory"
                )
                raise ArchiveRequestError(msg)

        _reject_unsafe_subdirectory(subdirectory, raw_url)
        return cls(url=url, hashes=tuple(hashes), subdirectory=subdirectory)


_SUBDIR_ROOT = "/archive-root"


def _reject_unsafe_subdirectory(subdirectory: str, raw_url: str) -> None:
    """Refuse a subdirectory that escapes the extracted tree.

    The join ``root / subdirectory`` would otherwise let an absolute path or
    a ``..`` component read a project outside the archive.  Rather than
    blocklist characters, normalise the join under a sentinel root and check
    containment; PEP 751 subdirectories are portable posix paths.
    """
    if not subdirectory:
        return
    resolved = posixpath.normpath(posixpath.join(_SUBDIR_ROOT, subdirectory))
    if posixpath.commonpath((_SUBDIR_ROOT, resolved)) != _SUBDIR_ROOT:
        msg = f"unsafe archive subdirectory {subdirectory!r} in {raw_url!r}"
        raise ArchiveRequestError(msg)
