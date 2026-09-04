"""The failures the algebra raises on its own, beside packaging's.

A marker the grammar rejects still raises packaging's ``InvalidMarker`` and an
operator it cannot decide packaging's ``UndefinedComparison``, both
:class:`ValueError`, which the two below match. An environment missing a
variable raises packaging's ``UndefinedEnvironmentName``, a :class:`KeyError`.
Those three come from whichever copy :mod:`nab_markersets._packaging` bound, so
catch :class:`ValueError` or :class:`KeyError` rather than naming a module.
"""

from __future__ import annotations

__all__ = ["IntractableMarkerSet", "UnserializableMarkerSet"]


def __dir__() -> list[str]:
    return __all__


class IntractableMarkerSet(ValueError):
    """The set is too complex to decide within the budget.

    Raised rather than hanging or failing obscurely, for any budget the
    algebra runs under: the cell caps, the work meter :meth:`simplify` runs
    the greedy loop under, the interpreter's stack, and the digit parse
    limit.
    """


class UnserializableMarkerSet(ValueError):
    """The set has no marker-string representation.

    Raised, rather than emitting a string for some other set, for the empty set
    and for complements the marker grammar cannot express.
    """
