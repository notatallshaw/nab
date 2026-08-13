"""The failures a fetch reports back to the resolving side.

Every one of these is raised by whoever does the IO and caught by code that
does none, so the type has to be nameable from both sides without the
resolving side importing an HTTP client.
"""

from __future__ import annotations

__all__ = [
    "HttpError",
    "IndexAccessError",
    "MetadataHashMismatchError",
    "SdistHashMismatchError",
    "UnsupportedWheelError",
    "WheelHashMismatchError",
]


class IndexAccessError(Exception):
    """An index could not produce a usable answer.

    A remote index fails with :class:`HttpError`, a ``file://`` index with
    :class:`~nab_index.local_index.LocalIndexError`.  Catching this covers
    both without naming a backend.
    """


class HttpError(IndexAccessError):
    """A request failed, or answered with a status the caller cannot use.

    Transports raise this from ``get`` and ``raise_for_status`` so callers
    can handle index failures without importing a specific HTTP backend.
    """


class MetadataHashMismatchError(Exception):
    """Fetched PEP 658 metadata did not match its published hash."""


class SdistHashMismatchError(Exception):
    """A fetched sdist archive did not match its published hash."""


class WheelHashMismatchError(Exception):
    """A range-recovered wheel's bytes did not match its published hash."""


class UnsupportedWheelError(Exception):
    """A wheel's ``.dist-info`` contradicts its own filename.

    Raised when a wheel carries more than one top-level ``.dist-info``
    directory, or a single one whose name does not canonicalise to the
    distribution named by the wheel's filename.
    """
