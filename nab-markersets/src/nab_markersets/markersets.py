"""A PEP 508 marker as the set of environments it selects.

:class:`MarkerSet` is the marker-side counterpart of
:class:`packaging.ranges.VersionRange`. It holds the states a marker string
cannot: the full set of an absent marker, the empty set of a contradiction, and
complements the grammar cannot spell. :meth:`~MarkerSet.to_marker_string` is the
way back, and it is partial.

Build a set with :meth:`MarkerSet.from_marker`, :meth:`MarkerSet.full` or
:meth:`MarkerSet.empty`; combine with ``&``, ``|``, ``~`` and ``-``; and query
with the decision procedures.
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from . import _markersets
from ._compat import override
from ._markersets import variable_names
from .errors import IntractableMarkerSet, UnserializableMarkerSet

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from collections.abc import Set as AbstractSet

    from ._markersets import Formula
    from ._packaging import Marker

# Resource caps, not semantic parameters: no answer depends on their value, so
# neither reaches the public surface. `_MAX_CELLS` bounds one decision and
# `_MAX_WORK` the greedy loop `simplify` runs over many of them.
_MAX_CELLS = 100_000
_MAX_WORK = 100_000_000

__all__ = ["DecisionStore", "MarkerSet", "variable_names"]


def __dir__() -> list[str]:
    return __all__


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _bounded(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Report a walk's :class:`RecursionError` as the algebra's bounded failure.

    A tree walk recurses as deep as the marker nests, so pathological input
    exhausts the stack where the cell budget would otherwise have caught it.
    """

    @wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(*args, **kwargs)
        except RecursionError as exc:
            msg = "marker nests too deeply to decide"
            raise IntractableMarkerSet(msg) from exc

    return wrapper


class DecisionStore(_markersets.Memo):
    """Scratch several decisions can share, for one piece of work.

    Related sets repartition the same axes, and one store keeps that work.
    Passing none is always correct: answers never depend on it. It grows with
    what it has read, so drop it when the work is done, and do not share one
    across threads.
    """

    __slots__ = ()


class MarkerSet:
    """A set of environments: the denotation of a PEP 508 marker. Immutable.

    A caller builds one only through the factories (:meth:`from_marker`,
    :meth:`full`, :meth:`empty`); calling ``MarkerSet(...)`` raises
    :class:`TypeError`.

    The algebra is closed under :meth:`intersection`, :meth:`union`,
    :meth:`complement` and :meth:`difference`, which always return a
    ``MarkerSet``. :meth:`simplify` and :meth:`to_marker_string` are the two that
    can refuse, both at the marker-grammar boundary. ``==`` is structural;
    :meth:`equivalent` is semantic.

    The decision procedures partition each axis a set names into cells on which
    every atom is constant, and read the set once per cell. That is exact except
    on one construction, where the set reads larger than it is.

    A substring test on a version-dispatch variable (``python_version``,
    ``python_full_version``, ``platform_release``, ``implementation_version``)
    is decided as its own free boolean, independent of the variable's value,
    because the versions that embed a literal are not enumerable from the
    literal. On a string variable the two readings share one axis, and there
    the contradiction is decided:

    >>> opaque = 'python_version == "3.9" and "9" not in python_version'
    >>> MarkerSet.from_marker(opaque).is_empty()
    False
    >>> decided = 'os_name == "posix" and "posix" not in os_name'
    >>> MarkerSet.from_marker(decided).is_empty()
    True

    So ``True`` from :meth:`is_empty` is safe and ``False`` is the weak answer.
    :meth:`witness` and :meth:`evaluate` do not inherit it.
    """

    __slots__ = ("_tree",)

    #: The normalised op-tree this set denotes.
    _tree: Formula

    # NoReturn is the truthful annotation and mypy then reads every class-level
    # access as Never.
    def __new__(cls, *_args: object, **_kwargs: object) -> MarkerSet:  # noqa: PYI034
        """Refuse direct construction, naming the three factories instead."""
        msg = (
            "cannot create 'MarkerSet' instances directly; use "
            "MarkerSet.from_marker(), MarkerSet.full(), or "
            "MarkerSet.empty() instead"
        )
        raise TypeError(msg)

    @classmethod
    def _wrap(cls, tree: Formula) -> MarkerSet:
        """Return a set wrapping ``tree``, bypassing the :meth:`__new__` refusal."""
        self = object.__new__(cls)
        self._tree = tree
        return self

    # ---- construction

    @classmethod
    @_bounded
    def from_marker(cls, marker: str | Marker) -> MarkerSet:
        """Return the set of environments a marker denotes.

        A ``Marker`` argument has to come from the copy of packaging the algebra
        bound; ``str(marker)`` is the spelling that always works. A blank string
        is the absent marker and gives the full set, where packaging refuses it.

        :raises InvalidMarker: packaging's, if ``marker`` is a string the
            grammar rejects.
        :raises IntractableMarkerSet: if the marker nests past the stack, or a
            ``~=`` literal overruns the interpreter's integer-string limit.
            Under the other operators that surfaces on the first decision.
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

    def difference(self, other: MarkerSet) -> MarkerSet:
        """Return the set of environments in this set and not in ``other``."""
        return self._wrap(
            _markersets.make_and((self._tree, _markersets.make_not(other._tree)))
        )

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

    def __sub__(self, other: object) -> MarkerSet:
        """Operator alias for :meth:`difference`."""
        if not isinstance(other, MarkerSet):
            return NotImplemented
        return self.difference(other)

    # ---- identity

    @override
    def __eq__(self, other: object) -> bool:
        """Whether ``other`` was built from the same tree over the same atoms.

        Structural, so a set parsed twice from one marker compares equal:

        >>> MarkerSet.from_marker('sys_platform == "linux"') == MarkerSet.from_marker(
        ...     'sys_platform == "linux"'
        ... )
        True

        Two spellings of one set do not: ``a & b`` is unequal to ``b & a``, and
        ``a | ~a`` to :meth:`full`. :meth:`equivalent` is the semantic test.
        """
        if not isinstance(other, MarkerSet):
            return NotImplemented
        return self._tree.key() == other._tree.key()

    @override
    def __hash__(self) -> int:
        """Hash the key :meth:`__eq__` compares, so equal sets hash alike.

        Deliberately not :func:`_bounded`: ``dict`` and ``set`` call this where
        the caller wrote no marker code, so a tree nested past the stack should
        surface as CPython's own :class:`RecursionError` rather than as the
        algebra's failure.
        """
        return hash(self._tree.key())

    # ---- decision procedures

    @_bounded
    def is_empty(self, *, store: DecisionStore | None = None) -> bool:
        """Whether no environment satisfies this set (the marker is a contradiction).

        Not exact: :class:`MarkerSet` names the construction it misreads and
        which way it errs. Every other decision procedure reduces to this one,
        so all of them inherit that gap.

        :raises IntractableMarkerSet: if deciding the set exceeds the internal
            cell budget, or the marker nests past the stack.
        """
        return _markersets.is_empty(self._tree, _MAX_CELLS, store)

    @_bounded
    def is_full(self, *, store: DecisionStore | None = None) -> bool:
        """Whether every environment satisfies this set (the marker is a tautology).

        ``(~self).is_empty()``.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.is_empty(_markersets.make_not(self._tree), _MAX_CELLS, store)

    @_bounded
    def is_disjoint(
        self, other: MarkerSet, *, store: DecisionStore | None = None
    ) -> bool:
        """Whether this set and ``other`` share no environment.

        ``(self & other).is_empty()``.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.is_empty(
            _markersets.make_and((self._tree, other._tree)), _MAX_CELLS, store
        )

    @_bounded
    def is_subset(
        self, other: MarkerSet, *, store: DecisionStore | None = None
    ) -> bool:
        """Whether every environment in this set is in ``other``.

        ``(self & ~other).is_empty()``, the set reading of ``self`` implies
        ``other``.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.is_empty(
            _markersets.make_and((self._tree, _markersets.make_not(other._tree))),
            _MAX_CELLS,
            store,
        )

    def is_superset(
        self, other: MarkerSet, *, store: DecisionStore | None = None
    ) -> bool:
        """Whether every environment in ``other`` is in this set.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return other.is_subset(self, store=store)

    @_bounded
    def equivalent(
        self, other: MarkerSet, *, store: DecisionStore | None = None
    ) -> bool:
        """Whether the two sets denote the same environments.

        Containment both ways, which ``==`` cannot cheaply provide. The two
        decisions read the same two trees, so they share a store whether or not
        one is passed.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        if store is None:
            store = DecisionStore()
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
        """Whether the two sets denote the same environments inside ``within``.

        Deciding each row of ``within`` under its own pins keeps a wide
        multi-platform universe decidable, where complementing the whole matrix
        at once does not. Use :meth:`equivalent` when the universe is full. An
        empty ``within`` makes every pair equivalent; :meth:`simplify` refuses
        it instead.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.equivalent_within_rows(
            self._tree, other._tree, within._tree, _MAX_CELLS, store
        )

    # ---- restriction

    @_bounded
    def restrict(self, env: Mapping[str, str | AbstractSet[str]]) -> MarkerSet:
        """Substitute the variables ``env`` provides, leaving the rest.

        A variable ``env`` omits stays in the result, where :meth:`evaluate`
        would raise.

        >>> both = MarkerSet.from_marker(
        ...     'sys_platform == "linux" and python_version >= "3.11"'
        ... )
        >>> both.restrict({"sys_platform": "linux"}).to_marker_string()
        'python_version >= "3.11"'

        :raises IntractableMarkerSet: if a version literal or value overruns the
            integer-string limit, or the marker nests past the stack.
        """
        _markersets.reject_oversized_literals(self._tree, env)
        return self._wrap(_markersets.restrict_tree(self._tree, env))

    @_bounded
    def set_memberships(self) -> frozenset[tuple[str, str]]:
        """Return the ``(variable, canonical name)`` pairs on set-valued variables.

        Only ``extra``, ``extras`` and ``dependency_groups`` hold sets, so a
        substring test on a string variable is not one of these.

        >>> sorted(MarkerSet.from_marker('extra == "GPU"').set_memberships())
        [('extra', 'gpu')]
        """
        return _markersets.membership_literals_of(self._tree)

    # ---- evaluation and witness

    @_bounded
    def evaluate(self, env: Mapping[str, str | AbstractSet[str]]) -> bool:
        """Whether a full environment is in the set (set variables take sets).

        Exact: no cell decomposition runs, so the gap :meth:`is_empty` carries
        does not apply.

        >>> MarkerSet.from_marker('extra == "gpu"').evaluate(
        ...     {"extra": frozenset({"cpu", "gpu"})}
        ... )
        True

        :raises UndefinedEnvironmentName: packaging's, if the marker
            references a variable ``env`` does not supply.
        :raises IntractableMarkerSet: if a version literal or value overruns the
            integer-string limit, or the marker nests past the stack.
        """
        _markersets.reject_oversized_literals(self._tree, env)
        return _markersets.evaluate_tree(self._tree, env)

    def __contains__(self, env: Mapping[str, str | AbstractSet[str]]) -> bool:
        """Operator alias for :meth:`evaluate`, so ``env in marker_set`` reads."""
        return self.evaluate(env)

    @_bounded
    def witness(
        self, *, store: DecisionStore | None = None
    ) -> dict[str, str | frozenset[str]] | None:
        """Return an environment in this set, or ``None`` when none is found.

        Never wrong: the environment is checked against the set before it is
        returned, so it does not inherit the gap :meth:`is_empty` carries. ``None``
        is weaker than empty, because the search reads the same partition.

        >>> minor = MarkerSet.from_marker('python_version >= "3.11"')
        >>> exact = MarkerSet.from_marker('python_full_version >= "3.11.0"')
        >>> (minor & ~exact).witness()["python_full_version"]
        '3.11.0.dev0'

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.witness(self._tree, _MAX_CELLS, store)

    # ---- simplification

    @_bounded
    def simplify(
        self, *, within: MarkerSet, store: DecisionStore | None = None
    ) -> MarkerSet:
        """Return a set that agrees with this one on every point of ``within``.

        Pass the union of a lock's declared environments as ``within`` for a
        universe-aware result, or :meth:`full` for a context-free factoring.
        Clauses and atoms come off greedily to a fixpoint, so the result is not
        the smallest equivalent set, and a factored input whose clauses are all
        needed comes back expanded.

        >>> wide = MarkerSet.from_marker(
        ...     'python_version == "3.10" or python_version == "3.11"'
        ...     ' or python_version >= "3.10" and platform_system != "Linux"'
        ... )
        >>> supported = MarkerSet.from_marker(
        ...     'python_version >= "3.9" and python_version < "3.12"'
        ... )
        >>> wide.simplify(within=supported).to_marker_string()
        'python_version == "3.10" or python_version == "3.11"'

        :raises ValueError: if ``within`` is the empty set, which makes every set
            vacuously equivalent.
        :raises UnserializableMarkerSet: if the set holds the complement of a
            version comparison, which has no negation in the grammar.
        :raises IntractableMarkerSet: if deciding a removal exceeds the internal
            cell budget, if the greedy loop exceeds the internal work budget,
            or if the marker nests past the stack.
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

        ``None`` means no marker is needed. A set the grammar cannot spell
        raises rather than emit a string for some other set, and what is
        returned is parsed back and checked equivalent first.

        >>> MarkerSet.from_marker('os_name == "posix"').to_marker_string()
        'os_name == "posix"'
        >>> MarkerSet.full().to_marker_string() is None
        True

        :raises UnserializableMarkerSet: for the empty set, and for a complement
            no marker operator spells.
        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        if store is None:
            store = DecisionStore()
        if self.is_full(store=store):
            return None
        if self.is_empty(store=store):
            msg = "the empty set has no marker string"
            raise UnserializableMarkerSet(msg)

        text = _markersets.serialize(_markersets.to_nnf(self._tree))
        rebuilt = MarkerSet.from_marker(text)
        if not self.equivalent(rebuilt):  # pragma: no cover
            # Unreachable: the per-atom complements are sound by construction.
            msg = "serialisation is not round-trip sound"
            raise UnserializableMarkerSet(msg)
        return text

    @override
    def __repr__(self) -> str:
        """Return a short summary of the set."""
        return f"<{type(self).__name__} {_markersets.describe(self._tree)!r}>"
