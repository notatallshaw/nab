"""The public :class:`MarkerSet`: a marker's denotation as a set of environments.

A :class:`MarkerSet` is the denotation of a PEP 508 marker as a set of
environments, the marker-side counterpart of :class:`~packaging.ranges.VersionRange`.
It holds the states a marker string cannot (the universal set of an absent
marker, the empty set of a contradiction, and complements the grammar cannot
spell), and reconciles with the grammar at exactly one boundary,
:meth:`~MarkerSet.to_marker_string`, which may return ``None`` or raise. The
engine is on-demand cell decomposition, guarded by ``max_cells``.
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

DEFAULT_MAX_CELLS = 100_000

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
    decorates report it as :class:`IntractableMarkerSet`, the one bounded
    failure the algebra promises on pathological input.
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
    """A set of environments, the denotation of a marker. Immutable.

    Built with :meth:`from_marker` / :meth:`true` / :meth:`false`, combined with
    ``&`` / ``|`` / :meth:`complement`, and queried with the decision procedures.
    ``max_cells`` bounds the on-demand cell decomposition every decision runs; it
    is a resource cap carried on the set, and the left operand's cap governs a
    combined decision.
    """

    __slots__ = ("_max_cells", "_tree")

    def __init__(self, tree: Formula, max_cells: int) -> None:
        self._tree = tree
        self._max_cells = max_cells

    # ---- construction

    @classmethod
    @_bounded
    def from_marker(
        cls, marker: str | Marker, *, max_cells: int = DEFAULT_MAX_CELLS
    ) -> MarkerSet:
        """Build the set of a marker string or :class:`Marker`."""
        _check_max_cells(max_cells)
        return cls(_markersets.parse(marker), max_cells)

    @classmethod
    def true(cls, *, max_cells: int = DEFAULT_MAX_CELLS) -> MarkerSet:
        """Return the universal set: every environment (an absent marker)."""
        _check_max_cells(max_cells)
        return cls(_markersets.TRUE, max_cells)

    @classmethod
    def false(cls, *, max_cells: int = DEFAULT_MAX_CELLS) -> MarkerSet:
        """Return the empty set: no environment."""
        _check_max_cells(max_cells)
        return cls(_markersets.FALSE, max_cells)

    # ---- algebra

    def __and__(self, other: MarkerSet) -> MarkerSet:
        return MarkerSet(
            _markersets.make_and((self._tree, other._tree)), self._max_cells
        )

    def __or__(self, other: MarkerSet) -> MarkerSet:
        return MarkerSet(
            _markersets.make_or((self._tree, other._tree)), self._max_cells
        )

    def complement(self) -> MarkerSet:
        """Return the set of environments this set excludes."""
        return MarkerSet(_markersets.make_not(self._tree), self._max_cells)

    # ---- decision procedures

    @_bounded
    def is_empty(self) -> bool:
        """Whether the set is unsatisfiable."""
        return _markersets.is_empty(self._tree, self._max_cells)

    @_bounded
    def is_tautology(self) -> bool:
        """Whether the set is every environment."""
        return _markersets.is_empty(
            _markersets.make_not(self._tree), self._max_cells
        )

    @_bounded
    def is_disjoint(self, other: MarkerSet) -> bool:
        """Whether the two sets share no environment."""
        return _markersets.is_empty(
            _markersets.make_and((self._tree, other._tree)), self._max_cells
        )

    @_bounded
    def implies(self, other: MarkerSet) -> bool:
        """Whether every environment in this set is in ``other``."""
        return _markersets.is_empty(
            _markersets.make_and((self._tree, _markersets.make_not(other._tree))),
            self._max_cells,
        )

    @_bounded
    def equivalent(self, other: MarkerSet) -> bool:
        """Whether the two sets denote the same environments."""
        return _markersets.is_empty(
            _markersets.make_and((self._tree, _markersets.make_not(other._tree))),
            self._max_cells,
        ) and _markersets.is_empty(
            _markersets.make_and((other._tree, _markersets.make_not(self._tree))),
            self._max_cells,
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
        return MarkerSet(
            _markersets.restrict_tree(self._tree, env), self._max_cells
        )

    @_bounded
    def membership_literals(self) -> frozenset[tuple[str, str]]:
        """Return the ``(variable, canonical name)`` set-memberships the set tests."""
        return _markersets.membership_literals_of(self._tree)

    # ---- evaluation and witness

    @_bounded
    def evaluate(self, env: Mapping[str, str | AbstractSet[str]]) -> bool:
        """Whether a full environment is in the set (extras are sets)."""
        _markersets.reject_oversized_literals(self._tree, env)
        return _markersets.evaluate_tree(self._tree, env)

    @_bounded
    def witness(self) -> dict[str, str | frozenset[str]] | None:
        """Return a satisfying environment, or ``None`` when none is found.

        ``None`` is returned for the empty set. The search over ``contains``
        atoms is incomplete, so ``None`` may also be returned for a non-empty
        set when the concrete-string constraints on one variable (a value atom,
        one or more ``contains`` atoms, or a mix) have no jointly realisable
        cell representative. ``python_version`` and ``python_full_version``
        share one axis, so those constraints can sit on different variables.
        """
        return _markersets.witness(self._tree, self._max_cells)

    # ---- serialisation

    @_bounded
    def to_marker_string(self) -> str | None:
        """Return a marker string that re-parses to an equivalent set, or ``None``.

        ``None`` means the universal set (no marker). The empty set, and any set
        whose complement structure the marker grammar cannot express, raise
        :class:`UnserializableMarkerSet` rather than emit a wrong string. The
        produced string is verified equivalent to this set before it is returned.
        """
        if self.is_tautology():
            return None
        if self.is_empty():
            msg = "the empty set has no marker string"
            raise UnserializableMarkerSet(msg)

        text = _markersets.serialize(_markersets.to_nnf(self._tree))
        rebuilt = MarkerSet.from_marker(text, max_cells=self._max_cells)
        if not self.equivalent(rebuilt):  # pragma: no cover
            # A last-resort guard: the per-atom complements are sound by
            # construction, so a non-equivalent round-trip is unreachable.
            msg = "serialisation is not round-trip sound"
            raise UnserializableMarkerSet(msg)
        return text

    @_bounded
    def __repr__(self) -> str:
        return f"MarkerSet({self._tree!r})"


def _check_max_cells(max_cells: int) -> None:
    if max_cells < 1:
        msg = f"max_cells must be >= 1, got {max_cells!r}"
        raise ValueError(msg)
