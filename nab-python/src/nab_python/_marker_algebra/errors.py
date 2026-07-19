"""Exceptions raised by the marker algebra."""

from __future__ import annotations


class ComplexityLimitError(Exception):
    """A marker operation hit a resource guard on pathological input.

    Raised loudly instead of hanging, overflowing the stack, or failing
    obscurely: the caller sees a bounded failure rather than an unbounded cell
    enumeration under the ``max_cells`` guard, a marker nested too deeply for the
    tree walk, or a version literal whose numeric component exceeds the digit
    parse limit.
    """


class UnserializableSetError(Exception):
    """A set has no marker-string spelling.

    Raised, rather than emitting a wrong or masquerading string, for the empty
    set and for complements whose structure the marker grammar cannot express.
    """
