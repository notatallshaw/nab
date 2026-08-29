"""The two failures the marker algebra reports.

Both subclass :class:`ValueError`, matching packaging's marker exceptions, so a
caller that already catches ``ValueError`` around marker handling keeps working.
"""

from __future__ import annotations

__all__ = ["IntractableMarkerSet", "UnserializableMarkerSet"]


def __dir__() -> list[str]:
    return __all__


class IntractableMarkerSet(ValueError):
    """The set is too complex to decide within the budget.

    Raised rather than hanging or failing obscurely: a cell product past the
    internal cap, a marker nested past the interpreter stack, or a version
    literal whose numeric component exceeds the digit parse limit.
    """


class UnserializableMarkerSet(ValueError):
    """The set has no marker-string spelling.

    Raised, rather than emitting a string for some other set, for the empty set
    and for complements the marker grammar cannot express.
    """
