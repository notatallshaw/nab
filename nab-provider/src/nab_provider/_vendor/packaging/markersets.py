"""A PEP 508 marker as the set of environments it selects.

:class:`MarkerSet` is the marker-side counterpart of
:class:`~packaging.ranges.VersionRange`. It holds the states a marker string
cannot: the full set of an absent marker, the empty set of a contradiction, and
complements the grammar cannot spell.

:meth:`~MarkerSet.to_marker_string` converts back, returning ``None`` for the
full set and raising for the sets above that have no spelling.
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

# Resource caps, not semantic parameters: no answer depends on their value, so
# neither reaches the public surface. `_MAX_CELLS` bounds one decision and
# `_MAX_WORK` the greedy loop `simplify` runs over many of them.
_MAX_CELLS = 100_000
_MAX_WORK = 100_000_000

__all__ = [
    "DecisionStore",
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
    """Report a tree walk's :class:`RecursionError` as :class:`IntractableMarkerSet`.

    A walk recurses as deep as the marker nests, so a deeply nested marker
    exhausts the stack rather than the cell budget.
    """

    @wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(*args, **kwargs)
        except RecursionError as exc:
            msg = "marker nests too deeply to decide"
            raise IntractableMarkerSet(msg) from exc

    return wrapper


DecisionStore = _markersets.Memo
"""Scratch several decisions can share, for one piece of work.

A run over related sets re-decides the same tree shapes and re-partitions the
same axes. Passing one store to those decisions keeps that work, and passing
none is always correct: answers never depend on it. It grows with what it has read, so drop it
when the work is done, and do not share one across threads.
"""


class MarkerSet:
    """A set of environments: the denotation of a PEP 508 marker. Immutable.

    A caller builds one only through the factories (:meth:`from_marker`,
    :meth:`full`, :meth:`empty`); calling ``MarkerSet(...)`` raises
    :class:`TypeError`.

    :meth:`intersection`, :meth:`union` and :meth:`complement` always return a
    ``MarkerSet``. :meth:`to_marker_string` can refuse at the marker-grammar
    boundary, and :meth:`simplify` there or on an empty ``within``. ``==`` is
    identity; :meth:`equivalent` is semantic.

    :meth:`is_empty`, :meth:`is_full`, :meth:`equivalent_within`,
    :meth:`witness`, :meth:`simplify` and :meth:`to_marker_string` take an
    optional ``store``, which shares scratch across a run of related decisions.
    """

    __slots__ = ("_tree",)

    def __new__(cls, *args: object, **kwargs: object) -> MarkerSet:  # noqa: PYI034
        raise TypeError(
            "cannot create 'MarkerSet' instances directly; use "
            "MarkerSet.from_marker(), MarkerSet.full(), or "
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

        :raises packaging.markers.InvalidMarker: for a string that is not a
            valid PEP 508 marker.
        :raises IntractableMarkerSet: if a ``~=`` or ``===`` version literal
            overruns the integer-string limit, or the marker nests past the
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
    def is_empty(self, *, store: DecisionStore | None = None) -> bool:
        """Whether no environment satisfies this set (the marker is a contradiction).

        Not exact on one construction. A substring test on a version-dispatch
        variable is decided as its own free boolean, because the values
        embedding a literal are not enumerable from it.

        The set then reads larger than it is, so ``True`` is safe and ``False``
        is the weak answer. Every predicate but :meth:`witness` and
        :meth:`evaluate` reduces to this one and inherits that gap.

        :raises IntractableMarkerSet: if deciding the set exceeds the internal
            cell budget, if a version literal overruns the integer-string limit,
            or if the marker nests past the stack.
        """
        return _markersets.is_empty(self._tree, _MAX_CELLS, store)

    @_bounded
    def is_full(self, *, store: DecisionStore | None = None) -> bool:
        """Whether every environment satisfies this set: ``(~self).is_empty()``.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.is_empty(_markersets.make_not(self._tree), _MAX_CELLS, store)

    @_bounded
    def is_disjoint(self, other: MarkerSet) -> bool:
        """Whether this set and ``other`` share no environment.

        ``(self & other).is_empty()``.
        """
        return _markersets.is_empty(
            _markersets.make_and((self._tree, other._tree)), _MAX_CELLS
        )

    @_bounded
    def is_subset(self, other: MarkerSet) -> bool:
        """Whether every environment in this set is in ``other``.

        ``(self & ~other).is_empty()``, the set reading of implication.
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

        Containment both ways. ``==`` stays identity because there is no cheap
        canonical form to key it on.

        The two decisions read the same trees, so they share one memo.
        """
        store = _markersets.Memo()
        return _markersets.is_empty(
            _markersets.make_and((self._tree, _markersets.make_not(other._tree))),
            _MAX_CELLS,
            store,
        ) and _markersets.is_empty(
            _markersets.make_and((other._tree, _markersets.make_not(self._tree))),
            _MAX_CELLS,
            store,
        )

    @_bounded
    def equivalent_within(
        self, other: MarkerSet, within: MarkerSet, *, store: DecisionStore | None = None
    ) -> bool:
        """Whether the sets denote the same environments on every point of ``within``.

        Deciding each row of ``within`` under its own pins keeps a wide
        multi-platform universe decidable, whereas complementing ``within`` as a
        whole does not. Use :meth:`equivalent` when the universe is full.
        """
        return _markersets.equivalent_within_rows(
            self._tree, other._tree, within._tree, _MAX_CELLS, store
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

        A variable ``env`` omits stays in the result, unless
        ``on_unknown_variable="error"`` asks for a :class:`ValueError` instead.

        :raises ValueError: for an unknown ``on_unknown_variable``, or for an
            unprovided variable under ``"error"``.
        :raises IntractableMarkerSet: if a version literal or value overruns
            the integer-string limit on a variable ``env`` supplies, or the
            marker nests past the stack.
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
        """Whether a full environment is in the set (set variables take sets).

        Exact: no cell decomposition runs, so the gap :meth:`is_empty` carries
        does not apply.

        :raises packaging.markers.UndefinedEnvironmentName: for a variable
            ``env`` does not supply.
        :raises IntractableMarkerSet: if a version literal or value overruns the
            integer-string limit.
        """
        _markersets.reject_oversized_literals(self._tree, env)
        return _markersets.evaluate_tree(self._tree, env)

    @_bounded
    def witness(
        self, *, store: DecisionStore | None = None
    ) -> dict[str, str | frozenset[str]] | None:
        """Return an environment in this set, or ``None`` when none is found.

        Never wrong: the environment is checked against the set before it is
        returned, so it does not inherit the gap :meth:`is_empty` carries.

        ``None`` does not prove the set empty. The search enumerates the cells
        :meth:`is_empty` decides on, so a set whose only environments lie
        outside them is inhabited and still yields ``None``.
        """
        return _markersets.witness(self._tree, _MAX_CELLS, store)

    # ---- simplification

    @_bounded
    def simplify(
        self, *, within: MarkerSet, store: DecisionStore | None = None
    ) -> MarkerSet:
        """Return a set that agrees with this one on every point of ``within``.

        Pass the union of a lock's declared environments as ``within``, or
        :meth:`full` for a context-free factoring.

        Clauses and then atoms are dropped greedily, so the result is not the
        smallest equivalent set, and a factored input whose clauses are all
        needed comes back expanded.

        :raises ValueError: if ``within`` is the empty set, which makes every set
            vacuously equivalent.
        :raises UnserializableMarkerSet: for a complement the grammar cannot
            negate, such as ``~(python_version >= "3.9")``.
        :raises IntractableMarkerSet: see :meth:`is_empty`, plus the work budget
            the greedy loop runs under.
        """
        if _markersets.universe_is_empty(within._tree, _MAX_CELLS, store):
            msg = "within must not be the empty set"
            raise ValueError(msg)
        return self._wrap(
            _markersets.simplify_within(
                self._tree, within._tree, _MAX_CELLS, _MAX_WORK, store
            )
        )

    # ---- serialisation

    @_bounded
    def to_marker_string(self, *, store: DecisionStore | None = None) -> str | None:
        """Return a marker string denoting this set, or ``None`` for the full set.

        Two kinds raise rather than getting a string for some other set: the
        empty set, and one whose complement the grammar cannot negate.
        ``~(python_version == "3.9")`` is spellable and
        ``~(python_version >= "3.9")`` is not.

        What is returned is parsed back and checked equivalent first.
        """
        if store is None:
            store = _markersets.Memo()
        if self.is_full(store=store):
            return None
        if self.is_empty(store=store):
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
