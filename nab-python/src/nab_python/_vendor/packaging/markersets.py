"""The public :class:`MarkerSet`: a marker's denotation as a set of environments.

A :class:`MarkerSet` is the denotation of a PEP 508 marker as a set of
environments, the marker-side counterpart of
:class:`~packaging.ranges.VersionRange`. It holds the states a marker string
cannot: the full set of an absent marker, the empty set of a contradiction, and
complements the grammar cannot spell. It reconciles with the grammar at exactly
one boundary, :meth:`~MarkerSet.to_marker_string`, which may return ``None`` or
raise :class:`UnserializableMarkerSet`.

Build one with :meth:`~packaging.markers.Marker.to_set`,
:meth:`MarkerSet.from_marker`, :meth:`MarkerSet.full`, or :meth:`MarkerSet.empty`;
combine with :meth:`intersection`, :meth:`union`, and :meth:`complement` (or
``&`` / ``|`` / ``~``); and query with the decision procedures. Equality is
identity; :meth:`equivalent` tests whether two sets denote the same
environments, since there is no cheap canonical form to key ``==`` on.
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from . import _markersets
from ._markersets import (
    IntractableMarkerSet,
    UnserializableMarkerSet,
    variable_names,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from collections.abc import Set as AbstractSet
    from typing import Literal

    from ._markersets import Formula
    from .markers import Marker

# The cell budget every decision runs under: a resource cap, not a semantic
# parameter, so it is private and never reaches the public surface. No result
# depends on its value; a set too complex to decide within it raises
# IntractableMarkerSet.
_MAX_CELLS = 100_000

# The total cell work one `simplify` may spend. `_MAX_CELLS` bounds a single
# decision, this bounds the greedy loop that issues them. A runaway guard rather
# than a tuning knob: the widest marker in nab's own CI locks spends 4.1 million.
_MAX_WORK = 100_000_000

__all__ = [
    "IntractableMarkerSet",
    "MarkerSet",
    "UnserializableMarkerSet",
    "variable_names",
]


def __dir__() -> list[str]:
    return __all__


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _bounded(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Report stack exhaustion on a deeply nested tree as the resource guard.

    A tree walk recurses as deep as the marker nests, so a marker nested past the
    interpreter's stack raises :class:`RecursionError`. The public methods it
    decorates report it as :class:`IntractableMarkerSet`, the one bounded failure
    the algebra promises on pathological input.
    """

    @wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(*args, **kwargs)
        except RecursionError as exc:
            msg = "marker nests too deeply to decide"
            raise IntractableMarkerSet(msg) from exc

    return wrapper


class MarkerSet:
    """A set of environments: the denotation of a PEP 508 marker. Immutable.

    Instances are created only through the factories (:meth:`from_marker`,
    :meth:`full`, :meth:`empty`, or :meth:`~packaging.markers.Marker.to_set`);
    calling ``MarkerSet(...)`` raises :class:`TypeError`.

    The algebra is closed under :meth:`intersection`, :meth:`union`, and
    :meth:`complement`. It is total on the set, so those always return a
    ``MarkerSet``; only :meth:`to_marker_string` is partial, at the marker-grammar
    boundary. ``==`` is identity; :meth:`equivalent` is semantic equality.
    """

    __slots__ = ("_tree",)

    def __new__(cls, *args: object, **kwargs: object) -> MarkerSet:  # noqa: PYI034
        raise TypeError(
            "cannot create 'MarkerSet' instances directly; use "
            "Marker.to_set(), MarkerSet.from_marker(), MarkerSet.full(), or "
            "MarkerSet.empty() instead"
        )

    @classmethod
    def _wrap(cls, tree: Formula) -> MarkerSet:
        """Internal factory; wraps a built op-tree, bypassing :meth:`__new__`."""
        instance = object.__new__(cls)
        instance._tree = tree
        return instance

    # ---- construction

    @classmethod
    @_bounded
    def from_marker(cls, marker: str | Marker) -> MarkerSet:
        """Return the set of environments a marker denotes.

        :raises packaging.markers.InvalidMarker: if ``marker`` is a string that is
            not a valid PEP 508 marker.
        :raises IntractableMarkerSet: if a version literal overruns the
            interpreter's integer-string limit, or the marker nests past the
            stack.
        """
        return cls._wrap(_markersets.parse(marker))

    @classmethod
    def full(cls) -> MarkerSet:
        """Return the full set: every environment (an absent, always-true marker)."""
        return cls._wrap(_markersets.TRUE)

    @classmethod
    def empty(cls) -> MarkerSet:
        """Return the empty set: no environment (a contradiction)."""
        return cls._wrap(_markersets.FALSE)

    # ---- algebra

    def intersection(self, other: MarkerSet) -> MarkerSet:
        """Return the set of environments in both this set and ``other``."""
        return self._wrap(_markersets.make_and((self._tree, other._tree)))

    def union(self, other: MarkerSet) -> MarkerSet:
        """Return the set of environments in either this set or ``other``."""
        return self._wrap(_markersets.make_or((self._tree, other._tree)))

    def complement(self) -> MarkerSet:
        """Return the set of environments this set excludes."""
        return self._wrap(_markersets.make_not(self._tree))

    def __and__(self, other: object) -> MarkerSet:
        """Operator alias for :meth:`intersection`."""
        if not isinstance(other, MarkerSet):
            return NotImplemented
        return self.intersection(other)

    def __or__(self, other: object) -> MarkerSet:
        """Operator alias for :meth:`union`."""
        if not isinstance(other, MarkerSet):
            return NotImplemented
        return self.union(other)

    def __invert__(self) -> MarkerSet:
        """Operator alias for :meth:`complement`."""
        return self.complement()

    # ---- decision procedures

    @_bounded
    def is_empty(self) -> bool:
        """Whether no environment satisfies this set (the marker is a contradiction).

        :raises IntractableMarkerSet: if deciding the set exceeds the internal
            cell budget, or the marker nests past the stack.
        """
        return _markersets.is_empty(self._tree, _MAX_CELLS)

    @_bounded
    def is_full(self) -> bool:
        """Whether every environment satisfies this set (the marker is a tautology).

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.is_empty(_markersets.make_not(self._tree), _MAX_CELLS)

    @_bounded
    def is_disjoint(self, other: MarkerSet) -> bool:
        """Whether this set and ``other`` share no environment.

        Equivalent to ``(self & other).is_empty()``.
        """
        return _markersets.is_empty(
            _markersets.make_and((self._tree, other._tree)), _MAX_CELLS
        )

    @_bounded
    def is_subset(self, other: MarkerSet) -> bool:
        """Whether every environment in this set is in ``other``.

        The set-algebra reading of ``self`` implies ``other``.
        """
        return _markersets.is_empty(
            _markersets.make_and((self._tree, _markersets.make_not(other._tree))),
            _MAX_CELLS,
        )

    def is_superset(self, other: MarkerSet) -> bool:
        """Whether every environment in ``other`` is in this set."""
        return other.is_subset(self)

    @_bounded
    def equivalent(self, other: MarkerSet) -> bool:
        """Whether the two sets denote the same environments.

        The semantic equality ``==`` cannot cheaply provide, so it is a method.
        """
        return _markersets.is_empty(
            _markersets.make_and((self._tree, _markersets.make_not(other._tree))),
            _MAX_CELLS,
        ) and _markersets.is_empty(
            _markersets.make_and((other._tree, _markersets.make_not(self._tree))),
            _MAX_CELLS,
        )

    @_bounded
    def equivalent_within(self, other: MarkerSet, within: MarkerSet) -> bool:
        """Whether the two sets denote the same environments on every point of ``within``.

        The row-restricted counterpart of :meth:`equivalent`, deciding each of
        ``within``'s rows under its pins so it stays decidable on wide
        multi-platform universes. A universe of :meth:`full` reduces it to plain
        :meth:`equivalent`.
        """
        return _markersets.equivalent_within_rows(
            self._tree, other._tree, within._tree, _MAX_CELLS
        )

    # ---- restriction and projection

    @_bounded
    def restrict(
        self,
        env: Mapping[str, str | AbstractSet[str]],
        *,
        on_unknown_variable: Literal["residual", "error"] = "residual",
    ) -> MarkerSet:
        """Substitute the provided variables, returning a residual set.

        With ``on_unknown_variable="error"`` a referenced variable absent from
        ``env`` raises :class:`ValueError`; with ``"residual"`` (the default) it
        is left in the residual set.

        :raises ValueError: for an unknown ``on_unknown_variable``, or for a
            referenced-but-unprovided variable under ``"error"``.
        :raises IntractableMarkerSet: if a version literal or value overruns the
            integer-string limit, or the marker nests past the stack.
        """
        if on_unknown_variable not in ("residual", "error"):
            msg = (
                "on_unknown_variable must be 'residual' or 'error', "
                f"got {on_unknown_variable!r}"
            )
            raise ValueError(msg)
        if on_unknown_variable == "error":
            missing = _markersets.unprovided_variables(self._tree, env)
            if missing:
                msg = f"restrict() has no value for {sorted(missing)}"
                raise ValueError(msg)
        _markersets.reject_oversized_literals(self._tree, env)
        return self._wrap(_markersets.restrict_tree(self._tree, env))

    @_bounded
    def membership_literals(self) -> frozenset[tuple[str, str]]:
        """Return the ``(variable, canonical name)`` set-memberships the set tests."""
        return _markersets.membership_literals_of(self._tree)

    # ---- evaluation and witness

    @_bounded
    def evaluate(self, env: Mapping[str, str | AbstractSet[str]]) -> bool:
        """Whether a full environment is in the set (membership variables are sets).

        :raises packaging.markers.UndefinedEnvironmentName: if the marker
            references a variable ``env`` does not supply.
        :raises IntractableMarkerSet: if a version literal or value overruns the
            integer-string limit.
        """
        _markersets.reject_oversized_literals(self._tree, env)
        return _markersets.evaluate_tree(self._tree, env)

    @_bounded
    def witness(self) -> dict[str, str | frozenset[str]] | None:
        """Return a satisfying environment, or ``None`` when none is found.

        ``None`` is returned for the empty set. The search over ``contains``
        atoms is incomplete, so ``None`` may also be returned for a non-empty set
        when the concrete-string constraints on one variable (a value atom, one
        or more ``contains`` atoms, or a mix) have no jointly realisable cell
        representative. ``python_version`` and ``python_full_version`` share one
        axis, so those constraints can sit on different variables.
        """
        return _markersets.witness(self._tree, _MAX_CELLS)

    # ---- simplification

    @_bounded
    def simplify(self, *, within: MarkerSet) -> MarkerSet:
        """Return the smallest set equivalent to this one on every point of ``within``.

        ``within`` is the universe the result must agree with this set over: pass
        the union of a lock's declared environments for universe-aware
        simplification, or :meth:`full` for a context-free factoring.

        :raises ValueError: if ``within`` is the empty set, which makes every set
            vacuously equivalent.
        :raises IntractableMarkerSet: if deciding a removal exceeds the internal
            cell budget, if the whole run exceeds the internal work budget, or
            if the marker nests past the stack.
        """
        if _markersets.universe_is_empty(within._tree, _MAX_CELLS):
            msg = "within must not be the empty set"
            raise ValueError(msg)
        return self._wrap(
            _markersets.simplify_within(
                self._tree, within._tree, _MAX_CELLS, _MAX_WORK
            )
        )

    # ---- serialisation

    @_bounded
    def to_marker_string(self) -> str | None:
        """Return a marker string that re-parses to an equivalent set, or ``None``.

        ``None`` means the full set (no marker needed). The empty set, and any set
        whose complement structure the marker grammar cannot express, raise
        :class:`UnserializableMarkerSet` rather than emit a wrong string. The
        produced string is verified equivalent to this set before it is returned.
        """
        if self.is_full():
            return None
        if self.is_empty():
            msg = "the empty set has no marker string"
            raise UnserializableMarkerSet(msg)

        text = _markersets.serialize(_markersets.to_nnf(self._tree))
        rebuilt = MarkerSet.from_marker(text)
        if not self.equivalent(rebuilt):  # pragma: no cover
            # A last-resort guard: the per-atom complements are sound by
            # construction, so a non-equivalent round-trip is unreachable.
            msg = "serialisation is not round-trip sound"
            raise UnserializableMarkerSet(msg)
        return text

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {_markersets.describe(self._tree)!r}>"
