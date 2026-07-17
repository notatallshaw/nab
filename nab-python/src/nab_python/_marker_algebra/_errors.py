"""Exceptions raised by the marker algebra."""

from __future__ import annotations


class ComplexityLimitExceeded(Exception):  # noqa: N818
    """A decision procedure exceeded the ``max_cells`` guard.

    Raised loudly instead of hanging: the caller sees a bounded failure rather
    than an unbounded cell enumeration.
    """


class UnserializableSet(Exception):  # noqa: N818
    """A set has no marker-string spelling.

    Raised, rather than emitting a wrong or masquerading string, for the empty
    set and for complements whose structure the marker grammar cannot express.
    """
