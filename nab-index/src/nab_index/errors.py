"""The error root every index backend fails through."""

from __future__ import annotations

__all__ = ["IndexAccessError"]


class IndexAccessError(Exception):
    """An index could not produce a usable answer.

    A remote index fails with :class:`~nab_index.transport.HttpError`, a
    ``file://`` index with :class:`~nab_index.local_index.LocalIndexError`.
    Catching this covers both without naming a backend.
    """
