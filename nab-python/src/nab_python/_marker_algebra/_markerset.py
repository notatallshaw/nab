"""The public :class:`MarkerSet`: a marker's denotation as a set of environments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import _atoms, _engine
from ._errors import UnserializableSet

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

    from .._vendor.packaging.markers import Marker
    from ._atoms import Formula

DEFAULT_MAX_CELLS = 100_000


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
    def from_marker(
        cls, marker: str | Marker, *, max_cells: int = DEFAULT_MAX_CELLS
    ) -> MarkerSet:
        """Build the set of a marker string or :class:`Marker`."""
        _check_max_cells(max_cells)
        return cls(_atoms.parse(marker), max_cells)

    @classmethod
    def true(cls, *, max_cells: int = DEFAULT_MAX_CELLS) -> MarkerSet:
        """Return the universal set: every environment (an absent marker)."""
        _check_max_cells(max_cells)
        return cls(_atoms.TRUE, max_cells)

    @classmethod
    def false(cls, *, max_cells: int = DEFAULT_MAX_CELLS) -> MarkerSet:
        """Return the empty set: no environment."""
        _check_max_cells(max_cells)
        return cls(_atoms.FALSE, max_cells)

    # ---- algebra

    def __and__(self, other: MarkerSet) -> MarkerSet:
        return MarkerSet(_atoms.make_and((self._tree, other._tree)), self._max_cells)

    def __or__(self, other: MarkerSet) -> MarkerSet:
        return MarkerSet(_atoms.make_or((self._tree, other._tree)), self._max_cells)

    def complement(self) -> MarkerSet:
        """Return the set of environments this set excludes."""
        return MarkerSet(_atoms.make_not(self._tree), self._max_cells)

    # ---- decision procedures

    def is_empty(self) -> bool:
        """Whether the set is unsatisfiable."""
        return _engine.is_empty(self._tree, self._max_cells)

    def is_tautology(self) -> bool:
        """Whether the set is every environment."""
        return _engine.is_empty(_atoms.make_not(self._tree), self._max_cells)

    def is_disjoint(self, other: MarkerSet) -> bool:
        """Whether the two sets share no environment."""
        return _engine.is_empty(
            _atoms.make_and((self._tree, other._tree)), self._max_cells
        )

    def implies(self, other: MarkerSet) -> bool:
        """Whether every environment in this set is in ``other``."""
        return _engine.is_empty(
            _atoms.make_and((self._tree, _atoms.make_not(other._tree))),
            self._max_cells,
        )

    def equivalent(self, other: MarkerSet) -> bool:
        """Whether the two sets denote the same environments."""
        return self.implies(other) and other.implies(self)

    # ---- restriction and projection

    def restrict(
        self,
        env: Mapping[str, str | AbstractSet[str]],
        *,
        on_unknown_variable: str = "residual",
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
            missing = _engine.unprovided_variables(self._tree, env)
            if missing:
                msg = f"restrict() has no value for {sorted(missing)}"
                raise ValueError(msg)
        return MarkerSet(_engine.restrict_tree(self._tree, env), self._max_cells)

    def variables(self) -> frozenset[str]:
        """Return the variables the set references, as written."""
        return _engine.variables_of(self._tree)

    def membership_literals(self) -> frozenset[tuple[str, str]]:
        """Return the ``(variable, canonical name)`` set-memberships the set tests."""
        return _engine.membership_literals_of(self._tree)

    # ---- evaluation and witness

    def evaluate(self, env: Mapping[str, str | AbstractSet[str]]) -> bool:
        """Whether a full environment is in the set (extras are sets)."""
        return _engine.evaluate_tree(self._tree, env)

    def witness(self) -> dict[str, str | frozenset[str]] | None:
        """Return a satisfying environment, or ``None`` when none is found."""
        return _engine.witness(self._tree, self._max_cells)

    # ---- serialisation

    def to_marker_string(self) -> str | None:
        """Return a marker string that re-parses to an equivalent set, or ``None``.

        ``None`` means the universal set (no marker). The empty set, and any set
        whose complement structure the marker grammar cannot express, raise
        :class:`UnserializableSet` rather than emit a wrong string. The produced
        string is verified equivalent to this set before it is returned.
        """
        if self.is_tautology():
            return None
        if self.is_empty():
            msg = "the empty set has no marker string"
            raise UnserializableSet(msg)
        text = _engine.serialize(_engine.to_nnf(self._tree))
        rebuilt = MarkerSet.from_marker(text, max_cells=self._max_cells)
        if not self.equivalent(rebuilt):  # pragma: no cover
            # A last-resort guard: the per-atom complements are sound by
            # construction, so a non-equivalent round-trip is unreachable.
            msg = "serialisation is not round-trip sound"
            raise UnserializableSet(msg)
        return text

    def __repr__(self) -> str:
        return f"MarkerSet({self._tree!r})"


def _check_max_cells(max_cells: int) -> None:
    if max_cells < 1:
        msg = f"max_cells must be >= 1, got {max_cells!r}"
        raise ValueError(msg)
