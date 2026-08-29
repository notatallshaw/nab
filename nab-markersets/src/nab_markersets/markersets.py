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

    from packaging.markers import Marker

    from ._markersets import Formula

# The cell budget every decision runs under: a resource cap, not a semantic
# parameter, so it is private and never reaches the public surface. No result
# depends on its value; a set too complex to decide within it raises
# IntractableMarkerSet.
_MAX_CELLS = 100_000

# The total cell work one `simplify` may spend. `_MAX_CELLS` bounds a single
# decision, this bounds the greedy loop that issues them. A runaway guard rather
# than a tuning knob: the widest marker in nab's own CI locks spends 4.1 million.
_MAX_WORK = 100_000_000

__all__ = ["DecisionStore", "MarkerSet", "variable_names"]


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


class DecisionStore(_markersets.Memo):
    """Scratch several decisions can share, for one piece of work.

    A decision partitions the axes its atoms sit on and reads each atom on the
    resulting cells; a run over related sets repeats most of that. Passing one
    store to those decisions keeps the work, and passing none is always correct.

    Answers never depend on it. It grows with what it has read, so drop it when
    the work is done, and do not share one across threads. Only the object is
    API; what it holds is internal.
    """

    __slots__ = ()


class MarkerSet:
    """A set of environments: the denotation of a PEP 508 marker. Immutable.

    Instances come only from the factories (:meth:`from_marker`, :meth:`full`,
    :meth:`empty`); calling ``MarkerSet(...)`` raises :class:`TypeError`.

    The algebra is closed under :meth:`intersection`, :meth:`union`,
    :meth:`complement` and :meth:`difference`, which always return a
    ``MarkerSet``. :meth:`simplify` and :meth:`to_marker_string` are the two that
    can refuse, both at the marker-grammar boundary. ``==`` is structural;
    :meth:`equivalent` is semantic.

    The decision procedures partition each variable a set names into cells on
    which every atom is constant, and read the set once per cell. That is exact
    except on two constructions, and they err in opposite directions, so neither
    verdict is safe on its own.

    A substring test is decided as its own free boolean, independent of the
    variable's value, so the set reads larger than it is:

    >>> both = 'os_name == "posix" and "posix" not in os_name'
    >>> MarkerSet.from_marker(both).is_empty()
    False

    Only points around a set's own version literals enter the partition, so a
    band between two adjacent literals holds no representative and the set reads
    smaller than it is:

    >>> band = MarkerSet.from_marker(
    ...     'platform_release > "6" and platform_release < "6.1"'
    ... )
    >>> band.is_empty(), band.evaluate({"platform_release": "6.0.1"})
    (True, True)

    :meth:`witness` and :meth:`evaluate` inherit neither.
    """

    __slots__ = ("_tree",)

    #: The normalised op-tree this set denotes.
    _tree: Formula

    def __new__(cls, *_args: object, **_kwargs: object) -> MarkerSet:
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
        instance = object.__new__(cls)
        # SLF001 reads `instance` as a foreign object; it is a `cls`.
        instance._tree = tree  # noqa: SLF001
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

        Structural, so a set parsed twice from one marker compares equal and can
        key a dict:

        >>> MarkerSet.from_marker('sys_platform == "linux"') == MarkerSet.from_marker(
        ...     'sys_platform == "linux"'
        ... )
        True

        Two spellings of one set do not: ``a & b`` is unequal to ``b & a``, and
        ``a | ~a`` to :meth:`full`. :meth:`equivalent` decides whether two sets
        denote the same environments.
        """
        if not isinstance(other, MarkerSet):
            return NotImplemented
        return self._tree.key() == other._tree.key()

    @override
    def __hash__(self) -> int:
        """Hash the key :meth:`__eq__` compares, so equal sets hash alike."""
        return hash(self._tree.key())

    # ---- decision procedures

    @_bounded
    def is_empty(self, *, store: DecisionStore | None = None) -> bool:
        """Whether no environment satisfies this set (the marker is a contradiction).

        Not exact: :class:`MarkerSet` names the two constructions it misreads and
        which way each one errs. Every predicate below reduces to this one.

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
        """Whether every environment in ``other`` is in this set."""
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

        The row-restricted counterpart of :meth:`equivalent`, deciding each of
        ``within``'s rows under its pins so it stays decidable on wide
        multi-platform universes. A universe of :meth:`full` reduces it to plain
        :meth:`equivalent`, which is the cheaper call when that is the universe.

        :raises IntractableMarkerSet: see :meth:`is_empty`.
        """
        return _markersets.equivalent_within_rows(
            self._tree, other._tree, within._tree, _MAX_CELLS, store
        )

    # ---- restriction

    @_bounded
    def restrict(self, env: Mapping[str, str | AbstractSet[str]]) -> MarkerSet:
        """Substitute the variables ``env`` provides, leaving the rest.

        A variable ``env`` omits stays in the result. :meth:`evaluate` is the
        total counterpart, which raises on a variable it was not given.

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

        Exact: no cell decomposition runs, so the gaps :meth:`is_empty` carries
        do not apply.

        >>> MarkerSet.from_marker('extra == "gpu"').evaluate(
        ...     {"extra": frozenset({"cpu", "gpu"})}
        ... )
        True

        :raises packaging.markers.UndefinedEnvironmentName: if the marker
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

        Never wrong: the environment is evaluated against the set before it is
        returned, so it inherits neither gap :meth:`is_empty` carries. ``None``
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
        """Return a smaller set that agrees with this one on every point of ``within``.

        ``within`` is the universe the result must agree over: pass the union of
        a lock's declared environments for a universe-aware result, or
        :meth:`full` for a context-free factoring. Clauses and atoms come off
        greedily to a fixpoint, so the result is small rather than minimal.

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
            cell budget, if the whole run exceeds the internal work budget, or
            if the marker nests past the stack.
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

        ``None`` means no marker is needed. That is the opposite of
        :meth:`packaging.ranges.VersionRange.to_specifier_set`, whose ``None``
        means unspellable: a specifier set spells both its extremes and a marker
        string spells neither, so the two put their sentinel in different places.

        A set the grammar cannot spell raises rather than emit a string for some
        other set, and what is returned is parsed back and checked equivalent
        first. Two sets raise: the empty set, and one holding the complement of a
        version comparison.

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
            # A last-resort guard: the per-atom complements are sound by
            # construction, so a non-equivalent round-trip is unreachable.
            msg = "serialisation is not round-trip sound"
            raise UnserializableMarkerSet(msg)
        return text

    @override
    def __repr__(self) -> str:
        """Return a short summary of the set, for debugging."""
        return f"<{type(self).__name__} {_markersets.describe(self._tree)!r}>"
