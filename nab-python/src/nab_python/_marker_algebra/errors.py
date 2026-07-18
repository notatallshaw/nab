"""Exceptions raised by the marker algebra."""

from __future__ import annotations


class ComplexityLimitError(Exception):
    """A decision procedure hit a resource guard on pathological input.

    Raised loudly instead of hanging or overflowing the stack: the caller sees a
    bounded failure rather than an unbounded cell enumeration under the
    ``max_cells`` guard, or a marker nested too deeply for the tree walk.
    """


class UnserializableSetError(Exception):
    """A set has no marker-string spelling.

    Raised, rather than emitting a wrong or masquerading string, for the empty
    set and for complements whose structure the marker grammar cannot express.
    """
