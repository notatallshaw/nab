"""Marker algebra engine behind :class:`~nab_markersets.markersets.MarkerSet`.

A marker parses into a normalised op-tree over typed atoms, and a value atom's
denotation is packaging's own ``_eval_op``, so a marker means here what it means
there. A decision partitions each axis the tree names into cells on which every
atom is constant and evaluates the tree over their bounded product.
"""

from __future__ import annotations

__lazy_modules__ = (
    "itertools",
    "math",
    "nab_markersets._compat",
    "nab_markersets._packaging",
    "nab_markersets.errors",
    "nab_markersets.python_versions",
)

import re
import threading
import weakref
from enum import Enum, auto
from functools import lru_cache
from itertools import islice, pairwise, product
from math import prod
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol, cast

from ._compat import get_int_max_str_digits
from ._packaging import (
    InvalidMarker,
    InvalidSpecifier,
    InvalidVersion,
    Op,
    ParserSyntaxError,
    Specifier,
    UndefinedComparison,
    UndefinedEnvironmentName,
    Value,
    Variable,
    Version,
    _eval_op,
    canonicalize_name,
    parse_marker,
)
from .errors import IntractableMarkerSet, UnserializableMarkerSet
from .python_versions import supports_python_version, validate_python_environment

if TYPE_CHECKING:
    from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from typing import TypeAlias

    from ._packaging import MarkerAtom, MarkerList

    # The walkers read the parser's recursive node types without mutating them.
    MarkerOperand: TypeAlias = "Variable | Value"
    MarkerComparison: TypeAlias = "tuple[MarkerOperand, Op, MarkerOperand]"
    MarkerNode: TypeAlias = "MarkerAtom | MarkerList | str"

    # An axis is a variable's domain, named by its kind. A node key is the
    # structural key two trees of the same shape share.
    Axis: TypeAlias = "tuple[str, ...]"
    NodeKey: TypeAlias = "tuple[object, ...]"
    AtomKey: TypeAlias = "tuple[str, str, str, bool, bool, str, str, bool]"
    ClauseKey: TypeAlias = "tuple[AtomKey, ...]"
    MaskKey: TypeAlias = (
        "tuple[tuple[tuple[Axis, tuple[Atom, ...], int], ...], int, int]"
    )
    MaskTable: TypeAlias = "tuple[dict[Atom, int], int]"


# Axis kinds. Atoms on one axis share a partition and are constant on each cell.
AXIS_VALUE = "value"
AXIS_SET = "set"
AXIS_CONTAINS = "contains"

DOMAIN_VERSION = "version"
DOMAIN_STRING = "string"
DOMAIN_TWIN = "version_or_string"
DOMAIN_SET = "set"

_WITNESS_PROBE_MIN_PRODUCT_CELLS = 64
_WITNESS_PROBE_MAX_CELLS = 4
_PATTERN_BITS_PER_UNIT = 64
# Reserve slots for buffer roots, result records and loop temporaries.
_PATTERN_BUFFER_FIXED_UNITS = 64
_MASK_CACHE_MIN_CELLS = 64
_MASK_CACHE_MAX_UNITS = 16_384
_MASK_CACHE_BITS_PER_UNIT = 64
# Key/table tuple fields and dictionary entries receive a fixed reference allowance.
_MASK_CACHE_FIXED_UNITS = 14
_VERSION_GAP_MAX_UNITS = 4096
_VERSION_GAP_FIXED_UNITS = 8
_VERSION_POOL_MAX_UNITS = 4096
_VERSION_POOL_FIXED_UNITS = 12

# Every variable in packaging's marker grammar, typed to a domain. The twins
# dispatch as versions yet may hold an arbitrary string, so they also carry the
# string fall-through.
DOMAIN_REGISTRY: dict[str, str] = {
    "implementation_name": DOMAIN_STRING,
    "implementation_version": DOMAIN_TWIN,
    "os_name": DOMAIN_STRING,
    "platform_machine": DOMAIN_STRING,
    "platform_python_implementation": DOMAIN_STRING,
    "platform_release": DOMAIN_TWIN,
    "platform_system": DOMAIN_STRING,
    "platform_version": DOMAIN_STRING,
    "python_full_version": DOMAIN_VERSION,
    "python_version": DOMAIN_VERSION,
    "sys_platform": DOMAIN_STRING,
    "extra": DOMAIN_SET,
    "extras": DOMAIN_SET,
    "dependency_groups": DOMAIN_SET,
}

# Stands for both "this environment supplies no value" and "the enumeration
# found no cell", neither of which None can say.
_MISSING = object()

_MEMBERSHIP = frozenset({"in", "not in"})
_ORDERED_UNDEFINED = frozenset({"~=", "==="})
_PYTHON_RELEASE_PARTS = 3
_ROW_TRUTH_INSERTION_MAX_ENTRIES = 1024
_ROW_TRUTH_INSERTION_MAX_COMBINED_CHARS = 256


def _domain(variable: str) -> str:
    """Return a variable's domain, twins collapsed onto version."""
    kind = DOMAIN_REGISTRY[variable]
    return DOMAIN_VERSION if kind == DOMAIN_TWIN else kind


def is_version_dispatch(variable: str) -> bool:
    """Whether a variable dispatches as a version, twins included."""
    return _domain(variable) == DOMAIN_VERSION


def is_pure_version(variable: str) -> bool:
    """Whether a variable's domain is version-only, no string fall-through."""
    return DOMAIN_REGISTRY[variable] == DOMAIN_VERSION


@lru_cache(maxsize=8192)
def _apply_memoised(lhs: str, op: str, rhs: str, key: str, _limit: int) -> bool:
    """Bounded memo behind :func:`_apply`; ``_limit`` only widens the cache key."""
    return _eval_op(lhs, Op(op), rhs, key=key)


def _apply(lhs: str, op: str, rhs: str, key: str) -> bool:
    """Evaluate ``op`` on two literals under ``key``'s domain, memoised.

    The int-string limit joins the cache key: a version key parses its operands,
    so a literal that compares under one limit raises under another.
    """
    return _apply_memoised(lhs, op, rhs, key, get_int_max_str_digits())


# --------------------------------------------------------------------- version util


def _parses_version(text: str) -> bool:
    try:
        Version(text.removesuffix(".*"))
    except InvalidVersion:
        return False
    return True


def _strict_version(text: str) -> bool:
    """Whether ``text`` is a realisable version value (no ``.*`` pattern)."""
    try:
        Version(text)
    except InvalidVersion:
        return False
    return True


def derive_major_minor(full: str) -> str:
    """A1: ``python_version`` is the major.minor truncation of the full version."""
    try:
        release = Version(full).release
    except InvalidVersion:
        return full
    major = release[0]
    minor = release[1] if len(release) > 1 else 0
    return f"{major}.{minor}"


_DIGIT_RUN = re.compile(r"\d+")


def _oversized_numeric(text: str) -> bool:
    """Whether a numeric run in ``text`` overflows int-from-string parsing.

    Such a run makes packaging's ``Version`` raise a bare ``ValueError``. A zero
    limit disables the check, so nothing overflows.
    """
    limit = get_int_max_str_digits()
    if not limit:
        return False
    return any(len(run.group()) > limit for run in _DIGIT_RUN.finditer(text))


# ---------------------------------------------------------------------------- atoms


# Weak references drop entries with collected atoms. The lock prevents
# two threads from minting rival atoms for one key, which the algebra
# would read as two leaves.
_INTERNED: weakref.WeakValueDictionary[
    tuple[str, str, str, str, str, bool, bool, bool], Atom
] = weakref.WeakValueDictionary()
_INTERN_LOCK = threading.Lock()


class Atom:
    """A normalised leaf whose ``holds`` gives its denotation on one point.

    Interned on its fields, so two equal atoms are one object and equality is
    identity. The lazily minted version pool is the only mutation, and a pure
    function of those same fields, so sharing is sound.
    """

    __slots__ = (
        "__weakref__",
        "_pool_entries",
        "derive_mm",
        "kind",
        "literal",
        "op",
        "origin",
        "positive",
        "swapped",
        "variable",
    )

    kind: str
    variable: str  # axis variable (python_version lowers to python_full_version)
    origin: str  # the variable as written
    op: str
    literal: str
    swapped: bool
    positive: bool
    derive_mm: bool  # A1: evaluate on the major.minor of the point

    _pool_entries: tuple[tuple[Version, str], ...] | None

    def __new__(
        cls,
        kind: str,
        variable: str,
        origin: str,
        op: str,
        literal: str,
        *,
        swapped: bool = False,
        positive: bool = True,
        derive_mm: bool = False,
    ) -> Atom:
        """Return the interned atom for these fields, building one if none is live."""
        key = (kind, variable, origin, op, literal, swapped, positive, derive_mm)
        with _INTERN_LOCK:
            interned = _INTERNED.get(key)
            if interned is not None:
                return interned

            self = super().__new__(cls)
            self.kind = kind
            self.variable = variable
            self.origin = origin
            self.op = op
            self.literal = literal
            self.swapped = swapped
            self.positive = positive
            self.derive_mm = derive_mm
            self._pool_entries = None

            _INTERNED[key] = self
            return self

    def replaced(self, *, op: str | None = None, positive: bool | None = None) -> Atom:
        """Return the atom with ``op`` or ``positive`` swapped out, for complements."""
        return Atom(
            self.kind,
            self.variable,
            self.origin,
            self.op if op is None else op,
            self.literal,
            swapped=self.swapped,
            positive=self.positive if positive is None else positive,
            derive_mm=self.derive_mm,
        )

    def axis(self) -> tuple[str, ...]:
        """Return the axis this atom partitions and is constant on.

        A substring test on a string variable joins that variable's value axis,
        so both readings of the variable are decided on one point. On a
        version-dispatch variable it keeps a boolean axis of its own, one per
        literal, because the values that embed a literal are not enumerable
        from the literals alone.
        """
        if self.kind == AXIS_VALUE:
            return (AXIS_VALUE, self.variable)
        if self.kind == AXIS_SET:
            return (AXIS_SET, self.variable)
        if DOMAIN_REGISTRY[self.variable] == DOMAIN_STRING:
            return (AXIS_VALUE, self.variable)
        # in / not in on the same (variable, literal) share one boolean axis.
        return (AXIS_CONTAINS, self.variable, self.literal)

    def holds(self, point: object) -> bool:
        """Return the atom's truth on one point of its axis.

        A contains atom is handed the variable's value on a value axis and its
        own truth on a boolean one, so it reads whichever it is given.
        """
        if self.kind == AXIS_VALUE:
            return _holds_value(self, str(point))
        if self.kind == AXIS_SET:
            member = self.literal in cast("Container[str]", point)
            return member if self.positive else not member
        present = self.literal in point if isinstance(point, str) else bool(point)
        return present if self.positive else not present

    def pool_entries(self) -> tuple[tuple[Version, str], ...]:
        """Return the version-pool points this atom's literal seeds, minted once."""
        entries = self._pool_entries
        if entries is None:
            texts = list(_version_neighbors(self.literal))
            if self.op in _MEMBERSHIP:
                for sub in _substrings(self.literal):
                    if _parses_version(sub):
                        texts.extend(_version_neighbors(sub))
            entries = tuple((Version(text), text) for text in texts)
            self._pool_entries = entries
        return entries


def _holds_value(atom: Atom, text: str) -> bool:
    """Whether ``atom`` holds where its variable takes ``text``.

    ``derive_mm`` truncates the point to major.minor first, for the
    ``python_version`` reading of a full version, and ``swapped`` puts the
    literal on the left, which packaging's operator table is not symmetric in.
    """
    op, literal = atom.op, atom.literal
    if atom.derive_mm:
        mm = derive_major_minor(text)
        if atom.swapped:
            return _apply(literal, op, mm, key="python_version")
        return _apply(mm, op, literal, key="python_version")
    if atom.swapped:
        return _apply(literal, op, text, key=atom.variable)
    return _apply(text, op, literal, key=atom.variable)


# --------------------------------------------------------------------- the op-tree

# Two trees with the same ``key()`` have the same shape and atoms, so a decision
# recorded under one key serves the other.


class BoolConst:
    """A TRUE or FALSE constant."""

    __slots__ = ("_key", "value")

    def __init__(self, *, value: bool) -> None:
        self.value = value
        self._key: NodeKey | None = None

    def key(self) -> NodeKey:
        """Structural key."""
        if self._key is None:
            self._key = ("c", self.value)
        return self._key


class AtomLeaf:
    """A single atom."""

    __slots__ = ("_key", "atom")

    def __init__(self, atom: Atom) -> None:
        self.atom = atom
        self._key: NodeKey | None = None

    def key(self) -> NodeKey:
        """Structural key."""
        if self._key is None:
            self._key = ("a", self.atom)
        return self._key


class AndNode:
    """A conjunction of two or more non-constant formulas."""

    __slots__ = ("_key", "children")

    def __init__(self, children: tuple[Formula, ...]) -> None:
        self.children = children
        self._key: NodeKey | None = None

    def key(self) -> NodeKey:
        """Structural key."""
        if self._key is None:
            self._key = ("&", tuple(child.key() for child in self.children))
        return self._key


class OrNode:
    """A disjunction of two or more non-constant formulas."""

    __slots__ = ("_key", "children")

    def __init__(self, children: tuple[Formula, ...]) -> None:
        self.children = children
        self._key: NodeKey | None = None

    def key(self) -> NodeKey:
        """Structural key."""
        if self._key is None:
            self._key = ("|", tuple(child.key() for child in self.children))
        return self._key


class NotNode:
    """A structural complement."""

    __slots__ = ("_key", "child")

    def __init__(self, child: Formula) -> None:
        self.child = child
        self._key: NodeKey | None = None

    def key(self) -> NodeKey:
        """Structural key."""
        if self._key is None:
            self._key = ("n", self.child.key())
        return self._key


Formula = BoolConst | AtomLeaf | AndNode | OrNode | NotNode

TRUE = BoolConst(value=True)
FALSE = BoolConst(value=False)


def make_and(children: Iterable[Formula]) -> Formula:
    """Build a conjunction, folding identities and FALSE."""
    flat: list[Formula] = []
    for child in children:
        if isinstance(child, BoolConst):
            if not child.value:
                return FALSE
            continue
        if isinstance(child, AndNode):
            flat.extend(child.children)
        else:
            flat.append(child)
    if not flat:
        return TRUE
    if len(flat) == 1:
        return flat[0]
    return AndNode(tuple(flat))


def make_or(children: Iterable[Formula]) -> Formula:
    """Build a disjunction, folding identities and TRUE."""
    flat: list[Formula] = []
    for child in children:
        if isinstance(child, BoolConst):
            if child.value:
                return TRUE
            continue
        if isinstance(child, OrNode):
            flat.extend(child.children)
        else:
            flat.append(child)
    if not flat:
        return FALSE
    if len(flat) == 1:
        return flat[0]
    return OrNode(tuple(flat))


def make_not(node: Formula) -> Formula:
    """Complement a formula, folding double negation and constants."""
    if isinstance(node, BoolConst):
        return FALSE if node.value else TRUE
    if isinstance(node, NotNode):
        return node.child
    return NotNode(node)


# ------------------------------------------------------------------- construction


class MarkerLike(Protocol):
    """A packaging ``Marker`` from either copy: released, or the fork nab vendors.

    Structural, so a checker admits a ``Marker`` built by the copy the algebra
    did not bind; :func:`_is_marker` is the same test at runtime.
    """

    def evaluate(
        self,
        environment: Mapping[str, str | AbstractSet[str]] | None = None,
        context: Literal["metadata", "lock_file", "requirement"] = "metadata",
    ) -> bool: ...


def _is_marker(source: object) -> bool:
    """Whether ``source`` is a packaging ``Marker``, from either copy.

    Matched on the class rather than by ``isinstance``, because testing against
    the copy that lost the probe would mean importing its marker stack, which
    is the second copy this package exists to leave unloaded.
    """
    kind = type(source)
    return kind.__qualname__ == "Marker" and kind.__module__.endswith(
        "packaging.markers"
    )


def _parse_ast(source: object) -> Sequence[MarkerNode] | None:
    """Parse a marker with packaging; None for an empty marker."""
    if _is_marker(source):
        source = str(source)
    elif not isinstance(source, str):
        kind = type(source)
        msg = (
            "expected str or packaging.markers.Marker, got "
            f"{kind.__module__}.{kind.__qualname__}"
        )
        raise TypeError(msg)
    if not source.strip():
        return None
    try:
        return parse_marker(source)
    except ParserSyntaxError as exc:
        # Match packaging: a malformed marker raises InvalidMarker.
        raise InvalidMarker(str(exc)) from exc


def parse(source: str | MarkerLike) -> Formula:
    """Parse a marker into the normalised op-tree."""
    parsed = _parse_ast(source)
    return TRUE if parsed is None else _convert(parsed)


def parse_with_variables(
    source: str | MarkerLike,
) -> tuple[Formula, frozenset[str]]:
    """Convert a marker and collect names from its original parsed syntax."""
    parsed = _parse_ast(source)
    if parsed is None:
        return TRUE, frozenset[str]()

    tree = _convert(parsed)
    names: set[str] = set()
    _collect_variables(parsed, names)
    return tree, frozenset(names)


def variable_names(source: str | MarkerLike) -> frozenset[str]:
    """Return every marker variable ``source`` names, as parsed.

    Builds no atoms, so a marker the algebra rejects still yields its names.
    """
    parsed = _parse_ast(source)
    if parsed is None:
        return frozenset()

    names: set[str] = set()
    _collect_variables(parsed, names)
    return frozenset(names)


def _collect_variables(node: Sequence[MarkerNode], names: set[str]) -> None:
    for item in node:
        if isinstance(item, str):
            continue
        if not isinstance(item, tuple):
            # Zuban retains comparison tuples in the narrowed recursive alias.
            _collect_variables(item, names)  # zuban: ignore[arg-type]
            continue

        # parse_marker builds tuples only for comparisons, never nested sequences.
        lhs, _op, rhs = cast("MarkerComparison", item)
        if isinstance(lhs, Variable):
            names.add(lhs.value)
        if isinstance(rhs, Variable):
            names.add(rhs.value)
        elif not isinstance(lhs, Variable) and rhs.value in DOMAIN_REGISTRY:
            # packaging reads a literal-vs-literal right operand as an environment key.
            names.add(rhs.value)


def _convert(node: Sequence[MarkerNode]) -> Formula:
    or_groups: list[list[Formula]] = [[]]
    for item in node:
        if item == "or":
            or_groups.append([])
        elif item == "and":
            continue
        elif isinstance(item, list):
            or_groups[-1].append(_convert(item))
        else:
            # "and" and "or" are the only strings the grammar emits.
            or_groups[-1].append(_convert_atom(cast("MarkerComparison", item)))
    return make_or(make_and(group) for group in or_groups)


def _convert_atom(item: MarkerComparison) -> Formula:
    """Return the atom or constant one parsed comparison denotes.

    Which operand is the variable decides the atom; a comparison of two
    literals has no variable and folds to a constant.
    """
    lhs, op_node, rhs = item
    op = op_node.serialize()
    if isinstance(lhs, Variable):
        # packaging compares against the right variable's name, not its value.
        return _make_atom(lhs.value, op, rhs.value, swapped=False)
    if isinstance(rhs, Variable):
        return _make_atom(rhs.value, op, lhs.value, swapped=True)

    # packaging reads the right operand as an environment key, so a quoted
    # literal naming a known variable routes like a swapped atom, and one naming
    # none folds through the string operator table where packaging would raise
    # UndefinedEnvironmentName.
    if rhs.value in DOMAIN_REGISTRY:
        return _make_atom(rhs.value, op, lhs.value, swapped=True)
    return BoolConst(value=_apply(lhs.value, op, rhs.value, key=""))


def _make_atom(variable: str, op: str, literal: str, *, swapped: bool) -> Formula:
    if _domain(variable) == DOMAIN_SET:
        return _make_set_atom(variable, op, literal, swapped=swapped)
    if variable == "python_version":
        return _make_python_version_atom(op, literal, swapped=swapped)
    if op in _MEMBERSHIP:
        return _make_membership_atom(variable, op, literal, swapped=swapped)

    # A version axis here seeds single-segment pool points, so the probe is one.
    _reject_undefined_operator(variable, op, literal, swapped=swapped, probe="0")
    if op == "===":
        msg = f"{op!r} is undefined on {variable!r} with literal {literal!r}"
        raise UndefinedComparison(msg)
    return AtomLeaf(Atom(AXIS_VALUE, variable, variable, op, literal, swapped=swapped))


def _make_python_version_atom(op: str, literal: str, *, swapped: bool) -> Formula:
    if op in _MEMBERSHIP and swapped:
        return _make_membership_atom("python_version", op, literal, swapped=swapped)
    if op == "~=" and swapped:
        # A swapped ~= makes the environment value the specifier bound, so its
        # true region is a same-major band with no pool point at its floor.
        msg = f"{op!r} is undefined on 'python_version' with literal {literal!r}"
        raise UndefinedComparison(msg)

    # A1 lowers onto major.minor, so the probe carries two segments.
    _reject_undefined_operator(
        "python_version", op, literal, swapped=swapped, probe="1.0"
    )
    # A1: lower onto python_full_version.
    return AtomLeaf(
        Atom(
            AXIS_VALUE,
            "python_full_version",
            "python_version",
            op,
            literal,
            swapped=swapped,
            derive_mm=True,
        )
    )


def _make_membership_atom(
    variable: str, op: str, literal: str, *, swapped: bool
) -> Formula:
    if swapped:
        # "literal" in variable: the opaque contains direction.
        return AtomLeaf(
            Atom(AXIS_CONTAINS, variable, variable, op, literal, positive=op == "in")
        )
    # variable in "literal": the exact substring direction.
    return AtomLeaf(Atom(AXIS_VALUE, variable, variable, op, literal, swapped=False))


def _make_set_atom(variable: str, op: str, literal: str, *, swapped: bool) -> Formula:
    name = canonicalize_name(literal)  # PEP 685 normalisation.
    if variable == "extra":
        if op == "==":
            positive = True
        elif op == "!=":
            positive = False
        else:
            return FALSE  # every other operator on extra is constant False.
    elif op in _MEMBERSHIP and swapped:
        positive = op == "in"
    else:
        return FALSE  # every other form on a set variable is constant False.
    return AtomLeaf(Atom(AXIS_SET, variable, variable, op, name, positive=positive))


def reject_oversized_version_literals(variable: str, literals: Sequence[str]) -> None:
    """Raise before an oversized numeric component reaches packaging.

    Past ``sys.get_int_max_str_digits()`` digits, packaging's ``Version`` raises
    a bare ValueError.
    """
    if is_version_dispatch(variable) and any(
        _oversized_numeric(literal) for literal in literals
    ):
        msg = (
            "version literal numeric component exceeds the "
            f"{get_int_max_str_digits()}-digit parse limit"
        )
        raise IntractableMarkerSet(msg)


def _reject_mint_overflow(literals: Sequence[str]) -> None:
    """Reserve one digit so neighbour minting cannot overflow the parse limit.

    Minting increments a numeric component, so a run at the limit width rolls
    one digit past it: this rejects at the limit, where
    :func:`reject_oversized_version_literals` rejects past it.
    """
    limit = get_int_max_str_digits()
    if limit and any(
        len(run.group()) >= limit
        for literal in literals
        for run in _DIGIT_RUN.finditer(literal)
    ):
        msg = (
            "version literal numeric component leaves no headroom under the "
            f"{limit}-digit parse limit"
        )
        raise IntractableMarkerSet(msg)


def _reject_undefined_operator(
    variable: str, op: str, literal: str, *, swapped: bool, probe: str
) -> None:
    """Raise if ``op`` is undefined on ``literal``, tested against ``probe``."""
    if op not in _ORDERED_UNDEFINED:
        return
    reject_oversized_version_literals(variable, (literal,))
    try:
        if swapped:
            _apply(literal, op, probe, key=variable)
        else:
            _apply(probe, op, literal, key=variable)
    except UndefinedComparison as exc:
        msg = f"{op!r} is undefined on {variable!r} with literal {literal!r}"
        raise UndefinedComparison(msg) from exc


# -------------------------------------------------------- domain-partition cells


class Cell(NamedTuple):
    """A representative point of an axis's domain and its truth vector."""

    point: object
    vector: tuple[bool, ...]


class _Decision(NamedTuple):
    """An emptiness verdict and the cell work charged for reaching it."""

    empty: bool
    units: int


_CACHED_ROW_WORK_MAX_BITS = 64


class _RowWork(NamedTuple):
    """One key already owned by prepared rows and its raw cost, when known."""

    key: NodeKey
    units: int | None


class Memo:
    """The results and preparation that related decisions reuse.

    A decision makes its own unless the caller passes one to share; see
    :class:`~nab_markersets.markersets.DecisionStore`.
    """

    __slots__ = (
        "decisions",
        "gap_remaining",
        "gaps",
        "mask_remaining",
        "masks",
        "partitions",
        "pool_remaining",
        "pools",
        "row_work",
        "rows",
        "truths",
        "version_pool_seen",
        "versions",
    )

    def __init__(self) -> None:
        self.decisions: dict[tuple[NodeKey, int], _Decision] = {}
        self.gaps: dict[tuple[str, str], str | None] | None = None
        self.gap_remaining = _VERSION_GAP_MAX_UNITS
        self.version_pool_seen = False
        self.masks: dict[MaskKey, MaskTable] | None = None
        self.mask_remaining = _MASK_CACHE_MAX_UNITS
        self.partitions: dict[tuple[Axis, tuple[Atom, ...], int], list[Cell]] = {}
        self.pools: dict[frozenset[str], tuple[str, ...]] | None = None
        self.pool_remaining = _VERSION_POOL_MAX_UNITS
        self.rows: dict[NodeKey, list[_Row]] = {}
        self.row_work: _RowWork | None = None
        self.truths: dict[tuple[Atom, str], bool] = {}
        self.versions: dict[str, Version | None] = {}


def _truth(atom: Atom, point: object, memo: Memo) -> bool:
    """Return an atom's truth on one point, memoised for a value atom."""
    if atom.kind != AXIS_VALUE:
        return atom.holds(point)
    key = (atom, str(point))
    hit = memo.truths.get(key)
    if hit is None:
        hit = memo.truths[key] = atom.holds(point)
    return hit


def _pooled_version(text: str, memo: Memo) -> Version | None:
    """Return ``text`` parsed as a version, or None, memoised.

    Keyed on text alone where :func:`_apply` also keys on the int-string limit,
    because a parse-limit guard bounds every text pooled here before its read.
    """
    versions = memo.versions
    if text not in versions:
        try:
            versions[text] = Version(text)
        except InvalidVersion:
            versions[text] = None
    return versions[text]


def _substring_cost(text: str) -> int:
    """Return the iteration count of the quadratic substring loop over ``text``."""
    n = len(text)
    return n * (n + 1) // 2


def _substrings(text: str) -> list[str]:
    out = {""}
    n = len(text)
    for i in range(n):
        for j in range(i + 1, n + 1):
            out.add(text[i:j])
    return sorted(out)


def _version_neighbors(text: str) -> list[str]:
    base = text.removesuffix(".*")
    try:
        version = Version(base)
    except InvalidVersion:
        return []

    release = version.release
    epoch = version.epoch
    major = release[0]
    release_str = ".".join(str(part) for part in release)
    pre_part = f"{version.pre[0]}{version.pre[1]}" if version.pre is not None else ""
    out = [base, *_equal_twins(version)]

    # Bumps stay in the literal's epoch: 1!3.9 bumps to 1!3.10, not 3.10, which sorts
    # below and leaves the band above the literal unrepresented.
    prefix = f"{epoch}!" if epoch else ""
    bumps = [prefix + ".".join(str(x) for x in (*release[:-1], release[-1] + 1))]
    if len(release) > 1:
        bumps.append(f"{prefix}{major}.{release[1] + 1}")
    bumps.append(f"{prefix}{major + 1}")
    if epoch:
        # The band above a non-zero-epoch literal runs into the next epoch,
        # beyond any same-epoch bump.
        bumps.append(f"{epoch + 1}!0")

    for bump in bumps:
        out.append(bump)
        out.append(f"{bump}.dev0")

    out.extend(_suffix_neighbors(version, release_str, pre_part))

    # A suffix after a local label stays local; public neighbours need their own stem.
    stems = (base, version.public) if version.local is not None else (base,)
    for stem in stems:
        for suffix in (".dev0", "a0", ".post0", ".1", "+l"):
            candidate = f"{stem}{suffix}"
            if _strict_version(candidate):
                out.append(candidate)
    return out


def _suffix_neighbors(version: Version, release_str: str, pre_part: str) -> list[str]:
    """Mint the points adjacent to a pre/post/dev literal.

    An exclusive comparison excludes the literal's own lower-precedence
    variants, so the adjacent point is the next or previous suffix of the same
    release, which no release bump reaches.
    """
    out: list[str] = []
    epoch = version.epoch
    if version.pre is not None:
        letter, number = version.pre
        out.append(str(Version(f"{epoch}!{release_str}{letter}{number + 1}")))

    if version.post is not None:
        out.append(
            str(Version(f"{epoch}!{release_str}{pre_part}.post{version.post + 1}"))
        )

    if version.dev is not None:
        post_part = f".post{version.post}" if version.post is not None else ""
        stem = f"{epoch}!{release_str}{pre_part}{post_part}"
        out.append(str(Version(f"{stem}.dev{version.dev + 1}")))
        if version.dev > 0:
            out.append(str(Version(f"{stem}.dev{version.dev - 1}")))
    return out


def _equal_twins(version: Version) -> list[str]:
    """Mint the points separating the literal's version reading from its string one.

    ``in``/``not in`` and an invalid specifier fall through to a raw string
    test, so separating that reading from PEP 440 matching needs the release
    zero-padded. A local-tagged literal also needs the local-stripped release,
    since a specifier built from a public point ignores a local label.
    """
    padded = version.__replace__(release=(*version.release, 0))
    if version.local is None:
        return [str(padded)]
    return [version.public, str(padded)]


def _release_between(vlow: Version, vhigh: Version) -> str:
    """Return a plain release ranking above every variant of ``vlow``.

    Release ordering runs ahead of pre, post, dev and local, so ``vlow``'s
    release extended by one outranks ``vlow`` whatever suffix it carries. There
    is no lowest such release: another zero can always be padded in. Within one
    epoch the padding runs to the wider of the two releases, which is the
    deepest place ``vhigh`` can differ, so the result stays under it whenever
    the two releases differ at all. Across an epoch boundary ``vhigh``'s release
    does not bound the candidate, so ``vlow``'s own width is enough. The caller
    checks the ordering either way.
    """
    low = vlow.release
    width = len(low) if vlow.epoch != vhigh.epoch else max(len(low), len(vhigh.release))
    parts = (*low, *(0,) * (width - len(low)), 1)
    prefix = f"{vlow.epoch}!" if vlow.epoch else ""
    return prefix + ".".join(str(part) for part in parts)


def _between(vlow: Version, low: str, vhigh: Version, memo: Memo) -> str | None:
    """Return a point strictly between two adjacent pool points, or None.

    The plain release comes first because an exclusive ordered comparison
    excludes its own bound's post, local, pre and dev variants, so a band whose
    two ends share a release is the only one the suffixed candidates can fill.
    Where the ends differ, a candidate must have a release distinct from
    both ends; suffixes of ``vlow`` are rejected.
    """
    for candidate in (
        _release_between(vlow, vhigh),
        f"{low}.post0",
        f"{low}+m",
        f"{low}.dev1",
        f"{low}.1",
        f"{low}a1",
    ):
        parsed = _pooled_version(candidate, memo)
        if parsed is not None and vlow < parsed < vhigh:
            return candidate
    return None


# The fixed low and high points every pool starts from.
_POOL_ANCHORS: tuple[tuple[Version, str], ...] = tuple(
    (Version(text), text) for text in ("0", "0.dev0", "99999")
)


def _cached_version_gap(
    vlow: Version, low: str, vhigh: Version, high: str, memo: Memo
) -> str | None:
    """Reuse completed gap candidates within the operation's later pools."""
    # Pool endpoint text preserves the full release width, unlike Version equality.
    key = (low, high)
    if memo.gaps is not None:
        try:
            return memo.gaps[key]
        except KeyError:
            pass

    result = _between(vlow, low, vhigh, memo)
    # Units cover retained references and text characters, not bytes.
    units = _VERSION_GAP_FIXED_UNITS + len(low) + len(high) + len(result or "")
    if units <= memo.gap_remaining:
        if memo.gaps is None:
            memo.gaps = {}
        memo.gaps[key] = result
        memo.gap_remaining -= units
    return result


def _pool_cache_key(seen: set[str], memo: Memo) -> frozenset[str] | None:
    """Return an endpoint key whose extra references fit the pool budget."""
    # Retained keys remain eligible after the budget is spent.
    ceiling = _VERSION_POOL_MAX_UNITS if memo.pools is not None else memo.pool_remaining
    if _VERSION_POOL_FIXED_UNITS + len(seen) > ceiling:
        return None
    return frozenset(seen)


def _remember_version_pool(memo: Memo, key: frozenset[str], points: list[str]) -> None:
    """Retain completed pool text within the operation's reference/character budget."""
    units = _VERSION_POOL_FIXED_UNITS + len(key) + len(points)
    if units > memo.pool_remaining:
        return
    units += sum(map(len, key)) + sum(map(len, points))
    if units > memo.pool_remaining:
        return
    if memo.pools is None:
        memo.pools = {}
    memo.pools[key] = tuple(points)
    memo.pool_remaining -= units


def _version_pool(
    entries: Iterable[tuple[Version, str]],
    *,
    memo: Memo,
) -> list[str]:
    """Build ordered candidates from endpoints paired with their full version text."""
    parsed: list[tuple[Version, str]] = list(_POOL_ANCHORS)
    seen: set[str] = {text for _, text in _POOL_ANCHORS}
    for version, text in entries:
        if text in seen:
            continue
        seen.add(text)
        parsed.append((version, text))

    # Defer cache setup until the operation needs a second pool.
    reuse = memo.version_pool_seen
    key = _pool_cache_key(seen, memo) if reuse else None
    if key is not None and memo.pools is not None:
        cached = memo.pools.get(key)
        if cached is not None:
            return list(cached)

    parsed.sort()
    memo.version_pool_seen = True
    extra: list[str] = []
    for (vlow, slow), (vhigh, shigh) in pairwise(parsed):
        if vlow == vhigh:
            continue
        if reuse:
            mid = _cached_version_gap(vlow, slow, vhigh, shigh, memo)
        else:
            mid = _between(vlow, slow, vhigh, memo)
        if mid is not None:
            extra.append(mid)

    result = [text for _, text in parsed] + extra
    if key is not None:
        _remember_version_pool(memo, key, result)
    return result


def _membership_candidates(atom: Atom, memo: Memo) -> list[str]:
    subs = _substrings(atom.literal)
    if atom.derive_mm:
        # A1 tests a full version's major.minor; only version substrings are realisable.
        return [
            s for s in subs if _pooled_version(s.removesuffix(".*"), memo) is not None
        ]
    return subs


def _dedupe_candidates(
    candidates: Iterable[str], *, variable: str, max_cells: int, memo: Memo
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        # Python axes exclude versions no supported target can report.
        if is_pure_version(variable):
            version = _pooled_version(candidate, memo)
            if version is None or not supports_python_version(
                version, variable=variable
            ):
                continue
        seen.add(candidate)
        ordered.append(candidate)
        if len(ordered) > max_cells:
            msg = f"value candidate set exceeds max_cells={max_cells}"
            raise IntractableMarkerSet(msg)
    return ordered


def _other_representative(literals: Sequence[str]) -> str:
    """Return a string equal to no literal on the axis and parsing as no version."""
    width = max((len(literal) for literal in literals), default=0) + 1
    return "z" * width


def _unused_character(literals: Iterable[str]) -> str:
    """Return a character none of ``literals`` uses, counting up from ``!``.

    A marker literal can hold any character its quote style admits, so the walk
    can run past printable ASCII; the point only has to be a string no atom on
    the axis matches by accident, and any unused character serves.
    """
    used = {char for literal in literals for char in literal}
    code = ord("!")
    while chr(code) in used:
        code += 1
    return chr(code)


def _contains_candidates(
    atoms: Sequence[Atom], literals: Sequence[str], max_cells: int
) -> list[str]:
    """Mint a point for each substring pattern the axis's contains atoms allow.

    A separator none of ``literals`` uses joins one subset of the substring
    literals, so every occurrence inside the point falls in one piece: it
    embeds that subset and whatever the subset embeds, equals no literal, and
    is a substring of none. Any string embeds what one subset does, so with the
    literals and their substrings the axis reaches every combination its atoms
    can realise together.
    """
    names: list[str] = []
    seen: set[str] = set()
    for atom in atoms:
        if atom.kind == AXIS_CONTAINS and atom.literal not in seen:
            seen.add(atom.literal)
            names.append(atom.literal)
    if not names:
        return []

    count = len(names)
    if (1 << count) > max_cells:
        msg = f"substring subsets over {count} literals exceeds max_cells={max_cells}"
        raise IntractableMarkerSet(msg)

    separator = _unused_character(literals)
    return [
        separator + separator.join(names[i] for i in range(count) if mask & (1 << i))
        for mask in range(1 << count)
    ]


def _reduce_work_exceeds(
    variable: str, literals: Sequence[str], atom_count: int, max_cells: int, memo: Memo
) -> bool:
    """Whether the axis's guaranteed reduce work already exceeds ``max_cells``.

    Every distinct literal is one point, so its count times the atom count
    lower-bounds the ``_reduce_cells`` work. A pure-version axis counts only the
    version-parseable literals.
    """
    pure = is_pure_version(variable)
    seen: set[str] = set()
    for literal in literals:
        key = literal.removesuffix(".*") if pure else literal
        if key in seen or (pure and _pooled_version(key, memo) is None):
            continue
        seen.add(key)
        if len(seen) * atom_count > max_cells:
            return True
    return False


def _python_release_aliases(
    entries: Sequence[tuple[Version, str]],
) -> list[tuple[Version, str]]:
    """Seed supported releases equivalent to zero-padded Python bounds."""
    aliases = []
    for version, _text in entries:
        release = version.release
        if len(release) > _PYTHON_RELEASE_PARTS and not any(
            release[_PYTHON_RELEASE_PARTS:]
        ):
            alias = version.__replace__(release=release[:_PYTHON_RELEASE_PARTS])
            aliases.append((alias, str(alias)))
    return aliases


def _value_candidates(
    variable: str, atoms: Sequence[Atom], max_cells: int, memo: Memo
) -> list[str]:
    literals = [atom.literal for atom in atoms]

    reject_oversized_version_literals(variable, literals)
    if _reduce_work_exceeds(variable, literals, len(atoms), max_cells, memo):
        msg = f"axis work over {len(atoms)} atoms exceeds max_cells={max_cells}"
        raise IntractableMarkerSet(msg)

    candidates: list[str] = []
    raw_kind = DOMAIN_REGISTRY[variable]

    # The OTHER cell exists only where the domain admits arbitrary strings.
    if raw_kind in (DOMAIN_STRING, DOMAIN_TWIN):
        candidates.append(_other_representative(literals))
    candidates.extend(literals)

    # Cap substring enumeration across the whole axis, not per literal.
    spent = 0
    for atom in atoms:
        if atom.kind == AXIS_VALUE and atom.op in _MEMBERSHIP:
            spent += _substring_cost(atom.literal)
            if spent > max_cells:
                msg = f"substring enumeration exceeds max_cells={max_cells}"
                raise IntractableMarkerSet(msg)
            candidates.extend(_membership_candidates(atom, memo))

    candidates.extend(_contains_candidates(atoms, literals, max_cells))

    if is_version_dispatch(variable):
        _reject_mint_overflow(literals)
        entries: list[tuple[Version, str]] = []
        for atom in atoms:
            entries.extend(atom.pool_entries())
        if raw_kind == DOMAIN_VERSION:
            entries.extend(_python_release_aliases(entries))
        candidates.extend(
            _version_pool(
                entries,
                memo=memo,
            )
        )
    return _dedupe_candidates(
        candidates,
        variable=variable,
        max_cells=max_cells,
        memo=memo,
    )


def _scalar_truth_vector(
    atoms: Sequence[Atom], point: object, memo: Memo
) -> tuple[bool, ...]:
    """Read a scalar point's value-atom vector through the existing truth cache."""
    truths = memo.truths
    text = str(point)
    vector = []
    for atom in atoms:
        key = (atom, text)
        value = truths.get(key)
        if value is None:
            value = truths[key] = atom.holds(point)
        vector.append(value)
    return tuple(vector)


def _check_axis_work(point_count: int, atom_count: int, max_cells: int) -> None:
    """Refuse point-by-atom work before building its truth vectors."""
    if point_count * atom_count > max_cells:
        msg = (
            f"axis work {point_count}x{atom_count} atoms exceeds max_cells={max_cells}"
        )
        raise IntractableMarkerSet(msg)


def _reduce_cells(
    points: Iterable[object],
    atoms: Sequence[Atom],
    max_cells: int,
    memo: Memo,
    *,
    value_points: bool = False,
) -> list[Cell]:
    """Return one cell per distinct truth vector ``atoms`` take over ``points``.

    Points agreeing on every atom decide the same way, so the first of them
    stands for the rest.

    ``value_points`` permits reuse of owned plain-string candidates.
    """
    if not value_points or type(points) is not list:
        points = list(points)

    _check_axis_work(len(points), len(atoms), max_cells)

    scalar = (
        value_points
        and len(atoms) > 1
        and all(atom.kind == AXIS_VALUE for atom in atoms)
    )
    representatives: dict[tuple[bool, ...], object] = {}
    for point in points:
        vector = (
            _scalar_truth_vector(atoms, point, memo)
            if scalar
            else tuple(_truth(atom, point, memo) for atom in atoms)
        )
        representatives.setdefault(vector, point)
        # With all 2**len(atoms) vectors represented, later points only repeat one.
        if len(representatives) == 1 << len(atoms):
            break
    return [Cell(point, vector) for vector, point in representatives.items()]


def _mentioned_names(atoms: Sequence[Atom]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for atom in atoms:
        if atom.literal not in seen:
            seen.add(atom.literal)
            names.append(atom.literal)
    return names


def partition_value_axis(
    variable: str, atoms: Sequence[Atom], max_cells: int, memo: Memo
) -> list[Cell]:
    """Cells of a version/string value axis."""
    return _reduce_cells(
        _value_candidates(variable, atoms, max_cells, memo),
        atoms,
        max_cells,
        memo,
        value_points=True,
    )


def _checked_set_names(atoms: Sequence[Atom], max_cells: int) -> list[str]:
    """Return a set axis's names after its powerset and vector-work guards."""
    names = _mentioned_names(atoms)
    count = len(names)
    if (1 << count) > max_cells:
        msg = f"set powerset over {count} names exceeds max_cells={max_cells}"
        raise IntractableMarkerSet(msg)
    _check_axis_work(1 << count, len(atoms), max_cells)
    return names


def partition_set_axis(atoms: Sequence[Atom], max_cells: int, memo: Memo) -> list[Cell]:
    """Cells of a set axis: the powerset over the mentioned names (guarded)."""
    names = _checked_set_names(atoms, max_cells)
    count = len(names)
    subsets = [
        frozenset(names[i] for i in range(count) if mask & (1 << i))
        for mask in range(1 << count)
    ]
    return _reduce_cells(subsets, atoms, max_cells, memo)


def partition_boolean_axis(
    atoms: Sequence[Atom], max_cells: int, memo: Memo
) -> list[Cell]:
    """Cells of an opaque boolean (contains) axis: the two truth values."""
    return _reduce_cells((False, True), atoms, max_cells, memo)


def _partition_axis(
    axis: Axis, atoms: Sequence[Atom], max_cells: int, memo: Memo
) -> list[Cell]:
    kind = axis[0]
    if kind == AXIS_VALUE:
        return partition_value_axis(axis[1], atoms, max_cells, memo)
    if kind == AXIS_SET:
        return partition_set_axis(atoms, max_cells, memo)
    return partition_boolean_axis(atoms, max_cells, memo)


class _WorkMeter(threading.local):
    """Per-thread remaining logical work, or None for unmetered operations."""

    # Slots on threading.local subclasses would share this field between threads.
    remaining: int | None = None


_work_meter = _WorkMeter()


def charge_work(units: int) -> None:
    """Charge logical work to the current bounded operation, if any."""
    remaining = _work_meter.remaining
    if remaining is None:
        return
    remaining -= units
    _work_meter.remaining = remaining
    if remaining < 0:
        msg = "marker operation work exceeds max_work"
        raise IntractableMarkerSet(msg)


def partition_axis(
    axis: Axis, atoms: Sequence[Atom], max_cells: int, store: Memo
) -> list[Cell]:
    """Partition one axis's domain into cells on which every atom is constant.

    Atoms are keyed in order: a :class:`Cell` vector lines up positionally with them.
    """
    key = (axis, tuple(atoms), max_cells)
    cached = store.partitions.get(key)
    if cached is None:
        cached = _partition_axis(axis, atoms, max_cells, store)
        store.partitions[key] = cached
    return cached


def guarded_product_size(sizes: Iterable[int], max_cells: int) -> int:
    """Multiply per-axis cell counts, raising past the guard."""
    total = 1
    for size in sizes:
        total *= size
        if total > max_cells:
            msg = f"cell product exceeds max_cells={max_cells}"
            raise IntractableMarkerSet(msg)
    return total


# ------------------------------------------------------------------- evaluation


def prepare_environment(env: Mapping[str, object]) -> Mapping[str, object]:
    """Repair legacy CPython full versions for evaluation and restriction."""
    full = env.get("python_full_version")
    if full is None or full == "":
        return env
    if isinstance(full, str) and full.endswith("+"):
        return {**env, "python_full_version": f"{full}local"}
    return env


def as_name_set(value: object) -> frozenset[str]:
    """Normalise a set-variable value: a non-empty str is one PEP 685 name."""
    if isinstance(value, str):
        return frozenset({canonicalize_name(value)}) if value else frozenset()
    return frozenset(canonicalize_name(name) for name in cast("Iterable[str]", value))


def _require(env: Mapping[str, object], key: str) -> object:
    """Look a referenced variable up, matching packaging's missing-key contract."""
    try:
        return env[key]
    except KeyError:
        raise UndefinedEnvironmentName(key) from None


def evaluate_atom(atom: Atom, env: Mapping[str, object]) -> bool:
    """Evaluate one atom against a full environment (extras are sets)."""
    if atom.kind == AXIS_VALUE:
        if atom.derive_mm and "python_full_version" not in env:
            # A1 lowers onto python_full_version; fall back to the written key.
            return atom.holds(_require(env, "python_version"))
        return atom.holds(_require(env, atom.variable))
    if atom.kind == AXIS_SET:
        return atom.holds(as_name_set(_require(env, atom.origin)))
    names = cast("Container[str]", _require(env, atom.variable))
    return atom.holds(atom.literal in names)


# --------------------------------------------------------------------- walking


def _walk(node: Formula, out: list[Atom]) -> None:
    if isinstance(node, AtomLeaf):
        out.append(node.atom)
    elif isinstance(node, NotNode):
        _walk(node.child, out)
    elif isinstance(node, (AndNode, OrNode)):
        for child in node.children:
            _walk(child, out)


def collect_atoms(node: Formula) -> list[Atom]:
    """Every atom mentioned by a tree, in encounter order."""
    if isinstance(node, AtomLeaf):
        return [node.atom]
    if isinstance(node, BoolConst):
        return []
    out: list[Atom] = []
    _walk(node, out)
    return out


def reject_oversized_literals(node: Formula, env: Mapping[str, object]) -> None:
    """Raise the bounded guard for an oversized literal or env value in a tree."""
    for atom in collect_atoms(node):
        value = _atom_env_value(atom, env)
        if value is not _MISSING:
            reject_oversized_version_literals(atom.variable, (atom.literal, str(value)))


def membership_literals_of(node: Formula) -> frozenset[tuple[str, str]]:
    """Return the ``(variable, canonical name)`` set-memberships a tree tests."""
    return frozenset(
        (atom.origin, atom.literal)
        for atom in collect_atoms(node)
        if atom.kind == AXIS_SET
    )


def _atoms_by_axis(atoms: list[Atom]) -> dict[tuple[str, ...], list[Atom]]:
    """Group the distinct atoms by axis, keeping encounter order."""
    grouped: dict[tuple[str, ...], list[Atom]] = {}
    seen: dict[tuple[str, ...], set[Atom]] = {}
    for atom in atoms:
        axis = atom.axis()
        known = seen.setdefault(axis, set())
        if atom not in known:
            known.add(atom)
            grouped.setdefault(axis, []).append(atom)
    return grouped


# ------------------------------------------------------------------- decisions


def _eval_cell(node: Formula, truth: Mapping[Atom, bool]) -> bool:
    if isinstance(node, BoolConst):
        return node.value
    if isinstance(node, AtomLeaf):
        return truth[node.atom]
    if isinstance(node, NotNode):
        return not _eval_cell(node.child, truth)
    if isinstance(node, AndNode):
        return all(_eval_cell(child, truth) for child in node.children)
    return any(_eval_cell(child, truth) for child in node.children)


class _CellSpace(NamedTuple):
    """One decision's axes, their atoms and cells, and the work charged."""

    axes: list[tuple[str, ...]]
    atomlists: list[list[Atom]]
    partitions: list[list[Cell]]
    units: int
    max_cells: int


def _cell_partitions(
    axes: Sequence[Axis],
    atomlists: Sequence[Sequence[Atom]],
    leaf_count: int,
    max_cells: int,
    store: Memo,
) -> tuple[list[list[Cell]], int]:
    """Return partitions and work, deferring cold sets past the product guard."""
    partitions: list[list[Cell]] = []
    deferred: dict[int, int] | None = None
    for axis, axis_atoms in zip(axes, atomlists, strict=True):
        if axis[0] == AXIS_SET:
            cached = store.partitions.get((axis, tuple(axis_atoms), max_cells))
            if cached is None:
                names = _checked_set_names(axis_atoms, max_cells)
                if deferred is None:
                    deferred = {}
                # Distinct mentioned names give distinct membership vectors.
                deferred[len(partitions)] = 1 << len(names)
                partitions.append([])
                continue
            partitions.append(cached)
        else:
            partitions.append(partition_axis(axis, axis_atoms, max_cells, store))

    # Repeated atoms inflate the per-cell walk without inflating the cell
    # product, so the guard counts leaf occurrences too.
    sizes = [len(part) for part in partitions]
    if deferred is not None:
        for index, size in deferred.items():
            sizes[index] = size
    units = guarded_product_size((*sizes, leaf_count), max_cells)

    if deferred is not None:
        for index in deferred:
            partitions[index] = partition_axis(
                axes[index], atomlists[index], max_cells, store
            )

    return partitions, units


def _cell_space(node: Formula, max_cells: int, store: Memo) -> _CellSpace:
    """Partition each axis a tree mentions and charge the enumeration it implies."""
    atoms = collect_atoms(node)
    grouped = _atoms_by_axis(atoms)
    axes = list(grouped)
    atomlists = [grouped[axis] for axis in axes]
    if not axes:
        return _CellSpace(axes, atomlists, [], 0, max_cells)

    partitions, units = _cell_partitions(axes, atomlists, len(atoms), max_cells, store)
    charge_work(units)
    return _CellSpace(axes, atomlists, partitions, units, max_cells)


def _enumerate_cells(
    node: Formula, space: _CellSpace
) -> Iterator[dict[tuple[str, ...], Cell]]:
    """Yield the cells of ``space`` on which the tree holds."""
    for combo in product(*space.partitions):
        truth: dict[Atom, bool] = {
            atom: value
            for atoms, cell in zip(space.atomlists, combo, strict=True)
            for atom, value in zip(atoms, cell.vector, strict=True)
        }
        if _eval_cell(node, truth):
            yield dict(zip(space.axes, combo, strict=True))


def _decide_empty(node: Formula, max_cells: int, store: Memo) -> _Decision:
    """Decide emptiness, returning the verdict with the work charged for it."""
    space = _cell_space(node, max_cells, store)
    size = prod(len(partition) for partition in space.partitions)
    if size >= _WITNESS_PROBE_MIN_PRODUCT_CELLS and _has_early_witness(node, space):
        return _Decision(empty=False, units=space.units)
    if size < _MASK_CACHE_MIN_CELLS:
        truth, full = _compile_cell_truth_masks(space, size)
    else:
        truth, full = _cell_truth_masks(space, size, store=store)
    return _Decision(not _eval_mask(node, truth, full), space.units)


def _has_early_witness(node: Formula, space: _CellSpace) -> bool:
    """Look for a quick witness before building a large product's truth masks."""
    for combo in islice(product(*space.partitions), _WITNESS_PROBE_MAX_CELLS):
        truth = {
            atom: value
            for atoms, cell in zip(space.atomlists, combo, strict=True)
            for atom, value in zip(atoms, cell.vector, strict=True)
        }
        if _eval_cell(node, truth):
            return True
    return False


def _compile_cell_truth_masks(space: _CellSpace, size: int) -> MaskTable:
    """Encode atom truths over the Cartesian cell product, one bit per cell."""
    full = (1 << size) - 1
    truth: dict[Atom, int] = {}
    stride = size
    for atoms, cells in zip(space.atomlists, space.partitions, strict=True):
        period = stride
        stride //= len(cells)
        run = (1 << stride) - 1
        # The quotient repeats this axis's period across preceding axes.
        repeat = full // ((1 << period) - 1)
        for column, atom in enumerate(atoms):
            pattern = 0
            for index, cell in enumerate(cells):
                if cell.vector[column]:
                    pattern |= run << (index * stride)
            truth[atom] = pattern * repeat
    return truth, full


def _mask_cache_key(space: _CellSpace, size: int) -> MaskKey:
    """Identify an admitted partition's ordered axes and predicate columns."""
    layout = tuple(
        (axis, tuple(atoms), len(cells))
        for axis, atoms, cells in zip(
            space.axes, space.atomlists, space.partitions, strict=True
        )
    )
    return layout, size, space.max_cells


def _remember_masks(store: Memo, key: MaskKey, table: MaskTable) -> None:
    """Bound cache-owned container references and mask bits for this operation."""
    layout, size, max_cells = key
    truth, full = table
    units = (
        _MASK_CACHE_FIXED_UNITS
        + len(layout)
        + sum(3 + len(axis) + len(atoms) for axis, atoms, _ in layout)
        + 2 * len(truth)
    )
    if units > store.mask_remaining:
        return

    # A unit covers one reference or up to 64 integer payload bits, not bytes.
    units += sum(
        max(
            1,
            (mask.bit_length() + _MASK_CACHE_BITS_PER_UNIT - 1)
            // _MASK_CACHE_BITS_PER_UNIT,
        )
        for mask in truth.values()
    )
    units += sum(
        max(
            1,
            (value.bit_length() + _MASK_CACHE_BITS_PER_UNIT - 1)
            // _MASK_CACHE_BITS_PER_UNIT,
        )
        for value in (
            full,
            size,
            max_cells,
            *(cells for _, _, cells in layout),
        )
    )
    if units > store.mask_remaining:
        return

    if store.masks is None:
        store.masks = {}
    store.masks[key] = table
    store.mask_remaining -= units


def _cell_truth_masks(
    space: _CellSpace, size: int, *, store: Memo | None = None
) -> MaskTable:
    """Reuse predicates in the operation store that prepared this space."""
    if (
        store is None
        or size < _MASK_CACHE_MIN_CELLS
        or (store.masks is None and store.mask_remaining == 0)
    ):
        return _compile_cell_truth_masks(space, size)

    key = _mask_cache_key(space, size)
    if store.masks is not None:
        cached = store.masks.get(key)
        if cached is not None:
            return cached

    table = _compile_cell_truth_masks(space, size)
    _remember_masks(store, key, table)
    return table


def _eval_mask(node: Formula, truth: Mapping[Atom, int], full: int) -> int:
    """Return the cells on which a formula holds, bounded by ``full``."""
    if isinstance(node, BoolConst):
        return full if node.value else 0
    if isinstance(node, AtomLeaf):
        return truth[node.atom]
    if isinstance(node, NotNode):
        return full ^ _eval_mask(node.child, truth, full)
    if isinstance(node, AndNode):
        result = full
        for child in node.children:
            result &= _eval_mask(child, truth, full)
            if not result:
                break
    else:
        result = 0
        for child in node.children:
            result |= _eval_mask(child, truth, full)
            if result == full:
                break
    return result


def is_empty(node: Formula, max_cells: int, store: Memo | None = None) -> bool:
    """Whether a tree denotes the empty set.

    A verdict ``store`` holds is reused and charged again, so the work budget
    does not depend on the memo.
    """
    if store is None:
        one_off = Memo()
        one_off.mask_remaining = 0
        return _decide_empty(node, max_cells, one_off).empty

    key = (node.key(), max_cells)
    decided = store.decisions.get(key)
    if decided is None:
        decided = store.decisions[key] = _decide_empty(node, max_cells, store)
    else:
        charge_work(decided.units)

    return decided.empty


def witness(
    node: Formula, max_cells: int, store: Memo | None = None
) -> dict[str, str | frozenset[str]] | None:
    """Return an environment satisfying a tree, or ``None`` when none is found.

    Each candidate is evaluated against the tree before it is returned, so a
    result is never wrong; ``None`` is weaker than empty, because the search
    reads the same cells the decisions do.
    """
    memo = Memo() if store is None else store
    for cell in _enumerate_cells(node, _cell_space(node, max_cells, memo)):
        env = _materialize(cell)
        try:
            validate_python_environment(env)
        except InvalidVersion:
            continue
        if evaluate_tree(node, env):
            return env
    return None


def _materialize(
    cell: Mapping[tuple[str, ...], Cell],
) -> dict[str, str | frozenset[str]]:
    """Turn one cell into the environment :func:`witness` returns."""
    env: dict[str, str | frozenset[str]] = {}
    contains: dict[str, list[tuple[str, bool]]] = {}
    for axis, piece in cell.items():
        kind = axis[0]
        if kind == AXIS_VALUE:
            env[axis[1]] = str(piece.point)
        elif kind == AXIS_SET:
            env[axis[1]] = frozenset(cast("Iterable[str]", piece.point))
        else:
            contains.setdefault(axis[1], []).append((axis[2], bool(piece.point)))

    for variable, items in contains.items():
        if variable in env:
            continue
        if variable == "python_version" and "python_full_version" in env:
            env[variable] = derive_major_minor(str(env["python_full_version"]))
            continue
        env[variable] = "".join(sorted(lit for lit, present in items if present))
    if "python_full_version" in env and "python_version" not in env:
        env["python_version"] = derive_major_minor(str(env["python_full_version"]))
    return env


# --------------------------------------------------------------------- restrict


def _atom_env_value(atom: Atom, env: Mapping[str, object]) -> object:
    """Return the env value the atom reads, or ``_MISSING`` when unprovided."""
    if atom.derive_mm:
        for key in ("python_full_version", "python_version"):
            if key in env:
                return env[key]
        return _MISSING
    key = atom.origin if atom.kind == AXIS_SET else atom.variable
    return env.get(key, _MISSING)


def _restrict_value(atom: Atom, env: Mapping[str, object]) -> bool | None:
    """Return the atom's truth under ``env``, or ``None`` when unprovided."""
    value = _atom_env_value(atom, env)
    if value is _MISSING:
        return None
    if atom.kind == AXIS_SET:
        return atom.holds(as_name_set(value))
    if atom.kind == AXIS_CONTAINS:
        return atom.holds(atom.literal in cast("Container[str]", value))
    return atom.holds(value)


def _restrict_row_value(
    atom: Atom, env: Mapping[str, object], store: Memo
) -> bool | None:
    """Read a row predicate through cached truths for string pins."""
    if (
        atom.kind == AXIS_VALUE
        and not atom.derive_mm
        and DOMAIN_REGISTRY[atom.variable] == DOMAIN_STRING
    ):
        value = env.get(atom.variable, _MISSING)
        if value is _MISSING:
            return None
        if type(value) is str:
            key = (atom, value)
            resolved = store.truths.get(key)
            if resolved is None:
                resolved = atom.holds(value)
                if (
                    len(store.truths) < _ROW_TRUTH_INSERTION_MAX_ENTRIES
                    and len(value) + len(atom.literal)
                    <= _ROW_TRUTH_INSERTION_MAX_COMBINED_CHARS
                ):
                    store.truths[key] = resolved
            return resolved
    return _restrict_value(atom, env)


def _restrict_cover_atom(atom: Atom, env: Mapping[str, object], store: Memo) -> Formula:
    """Restrict a cover predicate, wrapping unresolved atoms."""
    resolved = _restrict_row_value(atom, env, store)
    if resolved is None:
        return AtomLeaf(atom)
    return TRUE if resolved else FALSE


def _restrict_atom(leaf: AtomLeaf, env: Mapping[str, object]) -> Formula:
    resolved = _restrict_value(leaf.atom, env)
    if resolved is None:
        return leaf
    return TRUE if resolved else FALSE


def restrict_tree(node: Formula, env: Mapping[str, object]) -> Formula:
    """Substitute the variables ``env`` provides, leaving the rest."""
    if isinstance(node, BoolConst):
        return node
    if isinstance(node, AtomLeaf):
        return _restrict_atom(node, env)
    if isinstance(node, NotNode):
        return make_not(restrict_tree(node.child, env))
    if isinstance(node, AndNode):
        return make_and(restrict_tree(child, env) for child in node.children)
    return make_or(restrict_tree(child, env) for child in node.children)


def _restrict_row_tree(
    node: Formula, env: Mapping[str, object], store: Memo
) -> Formula:
    """Restrict a row tree through its operation-owned string truths."""
    if isinstance(node, BoolConst):
        return node
    if isinstance(node, AtomLeaf):
        resolved = _restrict_row_value(node.atom, env, store)
        return node if resolved is None else (TRUE if resolved else FALSE)
    if isinstance(node, NotNode):
        return make_not(_restrict_row_tree(node.child, env, store))
    if isinstance(node, AndNode):
        return make_and(
            _restrict_row_tree(child, env, store) for child in node.children
        )
    return make_or(_restrict_row_tree(child, env, store) for child in node.children)


# ------------------------------------------------------------------- evaluate


def evaluate_tree(node: Formula, env: Mapping[str, object]) -> bool:
    """Evaluate a tree against a full environment (extras are sets)."""
    if isinstance(node, BoolConst):
        return node.value
    if isinstance(node, AtomLeaf):
        return evaluate_atom(node.atom, env)
    if isinstance(node, NotNode):
        return not evaluate_tree(node.child, env)
    if isinstance(node, AndNode):
        return all(evaluate_tree(child, env) for child in node.children)
    return any(evaluate_tree(child, env) for child in node.children)


# ------------------------------------------------------------------ serialise


def _builds_specifier(op: str, literal: str) -> bool:
    try:
        Specifier(f"{op}{literal}")
    except InvalidSpecifier:
        return False
    return True


def _complement_version(atom: Atom, op: str, var: str) -> Formula:
    """Return the negation of a version atom, or refuse when nothing spells it."""
    # Excluded middle holds for an unswapped ==/!= on a pure-version axis alone.
    # An ordered comparison has the prerelease hole, a twin may hold a
    # non-version, and a swapped literal is the specifier bound.
    if op in ("==", "!=") and is_pure_version(var) and not atom.swapped:
        return AtomLeaf(atom.replaced(op="!=" if op == "==" else "=="))
    msg = f"no marker string spells the complement of {_render_atom(atom)}"
    raise UnserializableMarkerSet(msg)


def _flip_string_op(atom: Atom, flipped: str) -> Formula:
    """Return ``atom`` under ``flipped``, refusing if that leaves the string table.

    The atom reaches the string table only while its literal builds no specifier
    under its own operator, and an equality specifier accepts a wildcard and a
    local version where an ordered one does not, so the flipped atom can
    dispatch as a version and denote something else.
    """
    if is_version_dispatch(atom.variable) and _builds_specifier(flipped, atom.literal):
        msg = f"no marker string spells the complement of {_render_atom(atom)}"
        raise UnserializableMarkerSet(msg)
    return AtomLeaf(atom.replaced(op=flipped))


def _complement_string(atom: Atom, op: str) -> Formula:
    """Complement an atom packaging reads through the string operator table.

    That table folds ``<`` and ``>`` to false and ``<=`` and ``>=`` to equality,
    so no ordered comparison complements to another ordered comparison.

    A swapped atom on a version-dispatch variable does not reach the table at
    all: packaging builds its specifier from the environment value, so the atom
    compares as a version wherever that value parses as one, and the table's
    reading holds only on the rest.
    """
    if atom.swapped and is_version_dispatch(atom.variable):
        msg = f"no marker string spells the complement of {_render_atom(atom)}"
        raise UnserializableMarkerSet(msg)
    if op in ("==", ">=", "<="):
        return _flip_string_op(atom, "!=")
    if op == "!=":
        return _flip_string_op(atom, "==")
    if op == "in":
        return _flip_string_op(atom, "not in")
    if op == "not in":
        return _flip_string_op(atom, "in")
    # Only < and > are left, and the table reads both as false.
    return TRUE


def _complement_leaf(atom: Atom) -> Formula:
    if atom.kind in (AXIS_SET, AXIS_CONTAINS):
        return AtomLeaf(atom.replaced(positive=not atom.positive))
    op, var = atom.op, atom.variable
    if is_version_dispatch(var) and _builds_specifier(op, atom.literal):
        return _complement_version(atom, op, var)
    return _complement_string(atom, op)


def to_nnf(node: Formula) -> Formula:
    """Push complements down to the leaves (negation normal form).

    A leaf and a constant are already in normal form. A tree can also normalise
    to a constant, because complementing an atom the string operator table
    folds to false yields one.
    """
    if isinstance(node, AndNode):
        return make_and(to_nnf(child) for child in node.children)
    if isinstance(node, OrNode):
        return make_or(to_nnf(child) for child in node.children)
    if isinstance(node, NotNode):
        return _negate(node.child)
    return node


def _negate(node: Formula) -> Formula:
    if isinstance(node, AtomLeaf):
        return _complement_leaf(node.atom)
    if isinstance(node, AndNode):
        return make_or(_negate(child) for child in node.children)
    if isinstance(node, OrNode):
        return make_and(_negate(child) for child in node.children)
    if isinstance(node, NotNode):
        return to_nnf(node.child)
    msg = "a bare constant cannot reach _negate"  # pragma: no cover
    raise RuntimeError(msg)  # pragma: no cover


def _quote(literal: str) -> str:
    """Quote a literal for a marker string.

    A PEP 508 literal cannot contain its own delimiter.
    """
    if '"' not in literal:
        return f'"{literal}"'
    if "'" not in literal:
        return f"'{literal}'"
    # The grammar admits no literal holding both quote styles.
    msg = f"literal {literal!r} has no marker-string quoting"  # pragma: no cover
    raise UnserializableMarkerSet(msg)  # pragma: no cover


def _render_atom(atom: Atom) -> str:
    if atom.kind == AXIS_SET and atom.origin == "extra":
        op = "==" if atom.positive else "!="
        return f"extra {op} {_quote(atom.literal)}"
    if atom.kind in (AXIS_SET, AXIS_CONTAINS):
        op = "in" if atom.positive else "not in"
        return f"{_quote(atom.literal)} {op} {atom.origin}"
    if atom.swapped:
        return f"{_quote(atom.literal)} {atom.op} {atom.origin}"
    return f"{atom.origin} {atom.op} {_quote(atom.literal)}"


def _paren(node: Formula) -> str:
    if isinstance(node, AtomLeaf):
        return _render_atom(node.atom)
    return f"({serialize(node)})"


def serialize(node: Formula) -> str:
    """Render a negation-normal-form tree to a marker string."""
    if isinstance(node, AtomLeaf):
        return _render_atom(node.atom)
    if isinstance(node, AndNode):
        return " and ".join(_paren(child) for child in node.children)
    if isinstance(node, OrNode):
        return " or ".join(_paren(child) for child in node.children)
    msg = "a bare constant has no marker-atom spelling"  # pragma: no cover
    raise RuntimeError(msg)  # pragma: no cover


def describe(node: Formula) -> str:
    """Summarise a set for :func:`repr`, without exposing the op-tree.

    Total: a constant, a set no marker string spells, and a tree nested past the
    stack each get a word, so every set can be printed.
    """
    try:
        nnf = to_nnf(node)
        if isinstance(nnf, BoolConst):
            return "universe" if nnf.value else "empty"
        return serialize(nnf)
    except UnserializableMarkerSet:
        return "unrepresentable"
    except RecursionError:
        # The two walks are the only recursion under repr, which owes its
        # caller a string rather than the depth of the tree it was handed.
        return "too deeply nested"


# ------------------------------------------------------------------- simplify


def _atom_key(atom: Atom) -> AtomKey:
    """Total order over atoms, so a factored serialisation is stable."""
    return (
        atom.origin,
        atom.op,
        atom.literal,
        atom.swapped,
        atom.positive,
        atom.kind,
        atom.variable,
        atom.derive_mm,
    )


def _clause_key(clause: frozenset[Atom]) -> ClauseKey:
    return tuple(_atom_key(atom) for atom in sorted(clause, key=_atom_key))


def _to_clauses(node: Formula, max_cells: int) -> list[frozenset[Atom]]:
    """Distribute an NNF tree into a disjunction of atom-set clauses (DNF).

    An AND of ORs expands multiplicatively, so that product is held to
    ``max_cells`` and a pathological non-DNF input raises
    :class:`IntractableMarkerSet`. A plain OR chain is not counted.
    """
    if isinstance(node, BoolConst):
        return [frozenset()] if node.value else []
    if isinstance(node, AtomLeaf):
        return [frozenset((node.atom,))]
    if isinstance(node, OrNode):
        clauses: list[frozenset[Atom]] = []
        for child in node.children:
            clauses.extend(_to_clauses(child, max_cells))
        return clauses
    and_node = cast("AndNode", node)
    product: list[frozenset[Atom]] = [frozenset()]
    for child in and_node.children:
        child_clauses = _to_clauses(child, max_cells)
        product = [left | right for left in product for right in child_clauses]
        if len(product) > max_cells:
            msg = f"DNF clause count exceeds max_cells={max_cells}"
            raise IntractableMarkerSet(msg)
    return product


def _clause_formula(clause: Iterable[Atom]) -> Formula:
    """Build a clause in the stable atom order used by decision keys."""
    return make_and(AtomLeaf(atom) for atom in sorted(clause, key=_atom_key))


def _disjunction(clauses: Iterable[frozenset[Atom]]) -> Formula:
    return make_or(_clause_formula(clause) for clause in clauses)


class _Row(NamedTuple):
    """One universe row: its entailed pins and residual bound."""

    pins: dict[str, str]
    bound: Formula


def _row_pins(disjunct: Formula) -> dict[str, str]:
    """Return the exact-string equality pins a universe row entails.

    Only an unswapped top-level ``==`` on a :data:`DOMAIN_STRING` variable
    pins: version-dispatch ``==`` is PEP 440 equality, so
    ``platform_release == "5.10"`` still admits ``"5.10.0"``.
    """
    if isinstance(disjunct, AtomLeaf):
        conjuncts: tuple[Formula, ...] = (disjunct,)
    elif isinstance(disjunct, AndNode):
        conjuncts = disjunct.children
    else:
        conjuncts = ()
    pins: dict[str, str] = {}
    for child in conjuncts:
        if not isinstance(child, AtomLeaf):
            continue
        atom = child.atom
        if (
            atom.kind == AXIS_VALUE
            and atom.op == "=="
            and not atom.swapped
            and _domain(atom.variable) == DOMAIN_STRING
        ):
            pins[atom.variable] = atom.literal
    return pins


def _universe_disjuncts(universe: Formula) -> tuple[Formula, ...]:
    """Return a universe's top-level disjuncts, in NNF where it has one.

    NNF exposes the disjuncts a complement hides, but a version complement has
    no NNF. The rows are only a split of the universe, so the tree itself
    stands in for one, split on whatever top-level ``or`` it carries: any split
    of a union is sound, and the pins a top-level conjunct entails do not
    depend on the normal form.
    """
    try:
        node = to_nnf(universe)
    except UnserializableMarkerSet:
        node = universe
    return node.children if isinstance(node, OrNode) else (node,)


def _decompose_rows(
    universe: Formula, store: Memo, *, raw_work: int | None = None
) -> list[_Row]:
    """Split a universe into pinned rows, retaining one owned raw traversal cost."""
    key = universe.key()
    rows = store.rows.get(key)
    units = (
        raw_work
        if raw_work is not None and raw_work.bit_length() <= _CACHED_ROW_WORK_MAX_BITS
        else None
    )
    if rows is None:
        rows = []
        for disjunct in _universe_disjuncts(universe):
            pins = _row_pins(disjunct)
            rows.append(_Row(pins, _restrict_row_tree(disjunct, pins, store)))
        store.rows[key] = rows
        store.row_work = _RowWork(key, units)
    elif units is not None:
        record = store.row_work
        if record is not None and record.key is key and record.units is None:
            store.row_work = _RowWork(key, units)
    return rows


def _rows_equivalent(
    left: Formula,
    rows: Sequence[_Row],
    right_by_row: Sequence[Formula],
    max_cells: int,
    store: Memo,
) -> bool:
    """Whether ``left`` agrees with the already-restricted right on every row.

    Restricting to a row's pins complements over that row's residual rather than
    the whole-matrix product.
    """
    for row, right in zip(rows, right_by_row, strict=True):
        left_r = _restrict_row_tree(left, row.pins, store)
        if isinstance(left_r, BoolConst) and left_r is right:
            # Retain the zero-work decision and negative-budget refusal.
            is_empty(FALSE, max_cells, store)
            charge_work(0)
            continue
        if not is_empty(
            make_and((left_r, row.bound, make_not(right))), max_cells, store
        ):
            return False
        if not is_empty(
            make_and((right, row.bound, make_not(left_r))), max_cells, store
        ):
            return False
    return True


def universe_is_empty(
    universe: Formula, max_cells: int, store: Memo | None = None
) -> bool:
    """Whether a universe admits no environment: every top-level disjunct is empty."""
    shared = Memo() if store is None else store
    return all(
        is_empty(disjunct, max_cells, shared)
        for disjunct in _universe_disjuncts(universe)
    )


def equivalent_within_rows(
    left: Formula,
    right: Formula,
    universe: Formula,
    max_cells: int,
    store: Memo | None = None,
    *,
    max_work: int | None = None,
) -> bool:
    """Compare original trees per row, sharing an existing work budget if present."""
    shared = Memo() if store is None else store
    previous_work = _work_meter.remaining
    if previous_work is None:
        _work_meter.remaining = max_work
    try:
        left_nodes = _charge_formula_work(left)
        right_nodes = _charge_formula_work(right)
        if _work_meter.remaining is None:
            _charge_formula_work(universe)
        else:
            _charge_universe_work(universe, shared)
        rows = _decompose_rows(universe, shared)
        if all(
            isinstance(row.bound, BoolConst) and not row.bound.value for row in rows
        ):
            return True
        result = _independent_rows_equivalent(
            left, right, rows, left_nodes + right_nodes, max_cells, shared
        )
        if result is not None:
            return result
        right_by_row = [_restrict_row_tree(right, row.pins, shared) for row in rows]
        return _rows_equivalent(left, rows, right_by_row, max_cells, shared)
    finally:
        if previous_work is None:
            _work_meter.remaining = previous_work


def _dedupe(clauses: list[frozenset[Atom]]) -> list[frozenset[Atom]]:
    seen: set[frozenset[Atom]] = set()
    out: list[frozenset[Atom]] = []
    for clause in clauses:
        if clause not in seen:
            seen.add(clause)
            out.append(clause)
    return out


def _rows_within(
    left: Formula,
    rows: Sequence[_Row],
    right_by_row: Sequence[Formula],
    max_cells: int,
    store: Memo,
) -> bool:
    """Whether ``left`` stays inside the already-restricted right on every row."""
    for row, right in zip(rows, right_by_row, strict=True):
        left_r = _restrict_row_tree(left, row.pins, store)
        if not is_empty(
            make_and((left_r, row.bound, make_not(right))), max_cells, store
        ):
            return False
    return True


def _rows_cover(
    left: Formula,
    rows: Sequence[_Row],
    right_by_row: Sequence[Formula],
    max_cells: int,
    store: Memo,
) -> bool:
    """Whether ``left`` still reaches everything the already-restricted right does."""
    for row, right in zip(rows, right_by_row, strict=True):
        left_r = _restrict_row_tree(left, row.pins, store)
        if not is_empty(
            make_and((right, row.bound, make_not(left_r))), max_cells, store
        ):
            return False
    return True


def _drop_clauses(
    clauses: list[frozenset[Atom]],
    rows: Sequence[_Row],
    original_by_row: Sequence[Formula],
    max_cells: int,
    store: Memo,
) -> list[frozenset[Atom]]:
    """Drop every clause the rest of the disjunction already covers.

    Removing one only narrows the candidate, so only the cover direction can
    break.
    """
    kept = list(clauses)
    for clause in sorted(clauses, key=_clause_key):
        trial = [other for other in kept if other != clause]
        if _rows_cover(_disjunction(trial), rows, original_by_row, max_cells, store):
            kept = trial
    return kept


def _drop_atoms(
    clauses: list[frozenset[Atom]],
    rows: Sequence[_Row],
    original_by_row: Sequence[Formula],
    max_cells: int,
    store: Memo,
) -> list[frozenset[Atom]]:
    """Drop every atom its clause does not need to stay within the original.

    Removing one only widens that clause, so only that clause can reach past
    the original. The others are unchanged and already within, so testing the
    widened clause alone is enough.
    """
    working = [set(clause) for clause in clauses]
    for clause in working:
        for atom in sorted(clause, key=_atom_key):
            clause.discard(atom)
            widened = _clause_formula(frozenset(clause))
            if not _rows_within(widened, rows, original_by_row, max_cells, store):
                clause.add(atom)
    return [frozenset(clause) for clause in working]


def _canonical(clauses: list[frozenset[Atom]]) -> tuple[ClauseKey, ...]:
    return tuple(sorted(_clause_key(clause) for clause in clauses))


class _RowPattern(NamedTuple):
    """Source-predicate truth bits and emptiness for one eligible row."""

    nonempty: bool
    bits: int


class _RowStatus(Enum):
    """Distinguish varying predicates from a refused row admission."""

    VARYING = auto()
    OVER_CAP = auto()


class _CoverCube(NamedTuple):
    """A retained conjunction and the observed patterns it covers."""

    atoms: frozenset[Atom]
    mask: int


class _CoverInput(NamedTuple):
    """An NNF tree, its ordered predicates and its evaluation leaf count."""

    tree: Formula
    atoms: tuple[Atom, ...]
    leaf_count: int


class _RowTruth(NamedTuple):
    """Source columns and the residual universe for one admitted row."""

    columns: tuple[int, ...]
    within: int
    size: int


class _PreparedRow:
    """A varying row's temporary lookup and charges for this cover attempt."""

    __slots__ = (
        "evaluation_units",
        "masks",
        "restricted",
        "setup_units",
        "size",
        "within",
    )

    def __init__(
        self,
        *,
        restricted: Sequence[Formula],
        masks: MaskTable,
        within: int,
        size: int,
        setup_units: int,
        evaluation_units: int,
    ) -> None:
        self.restricted = restricted
        self.masks = masks
        self.within = within
        self.size = size

        self.setup_units = setup_units
        self.evaluation_units = evaluation_units


def _canonical_cover_atom(atom: Atom) -> Atom:
    """Match the operator encoding that serializing and reparsing a predicate uses."""
    if atom.kind == AXIS_SET and atom.origin == "extra":
        op = "==" if atom.positive else "!="
    elif atom.kind in (AXIS_SET, AXIS_CONTAINS):
        op = "in" if atom.positive else "not in"
    else:
        return atom
    return atom if atom.op == op else atom.replaced(op=op)


def _replace_cover_atoms(node: Formula, replacements: Mapping[Atom, Atom]) -> Formula:
    """Replace predicates in a nonconstant NNF tree by equivalent encodings."""
    if isinstance(node, AtomLeaf):
        return AtomLeaf(replacements.get(node.atom, node.atom))
    if isinstance(node, AndNode):
        return make_and(
            _replace_cover_atoms(child, replacements) for child in node.children
        )
    assert isinstance(node, OrNode)
    return make_or(_replace_cover_atoms(child, replacements) for child in node.children)


def _prepare_cover(node: Formula) -> _CoverInput:
    """Collect canonical predicates and price their traversal and ordering."""
    leaves = collect_atoms(node)
    charge_work(len(leaves))
    unique = set(leaves)
    charge_work(len(unique) * max(1, len(unique).bit_length()))
    replacements = {}
    for atom in unique:
        canonical = _canonical_cover_atom(atom)
        if canonical is not atom:
            replacements[atom] = canonical
    if replacements:
        node = _replace_cover_atoms(node, replacements)
        unique = {replacements.get(atom, atom) for atom in unique}
    return _CoverInput(node, tuple(sorted(unique, key=_atom_key)), len(leaves))


def _charge_formula_work(node: Formula) -> int:
    """Price a raw walk before preparation and return its node count."""
    nodes = 0
    pending = [node]
    while pending:
        charge_work(1)
        current = pending.pop()
        nodes += 1
        if isinstance(current, AtomLeaf):
            atom = current.atom
            charge_work(
                len(atom.origin)
                + len(atom.op)
                + len(atom.literal)
                + len(atom.kind)
                + len(atom.variable)
            )
        elif isinstance(current, NotNode):
            pending.append(current.child)
        elif isinstance(current, (AndNode, OrNode)):
            charge_work(len(current.children))
            pending.extend(current.children)
    return nodes


def _charge_universe_work(universe: Formula, store: Memo) -> int:
    """Price a raw universe traversal inside the active work budget."""
    record = store.row_work
    if record is not None:
        key = universe._key  # noqa: SLF001 - key() can traverse an uncharged tree.
        if record.key is key and record.units is not None:
            charge_work(record.units)
            return record.units
    before = _work_meter.remaining
    assert before is not None
    _charge_formula_work(universe)
    after = _work_meter.remaining
    assert after is not None
    return before - after


def _fixed_row_pattern(
    atoms: Sequence[Atom], row: _Row, max_cells: int, store: Memo
) -> _RowPattern | _RowStatus | _PreparedRow:
    """Return a fixed pattern, a prepared varying row, or a row status."""
    charge_work(len(atoms) + 1)
    restricted = [_restrict_cover_atom(atom, row.pins, store) for atom in atoms]
    residual = [atom for node in restricted for atom in collect_atoms(node)]
    bound_atoms = collect_atoms(row.bound)
    bound_axes = {atom.axis() for atom in bound_atoms}
    if any(atom.axis() not in bound_axes for atom in residual):
        return _RowStatus.VARYING

    partition_atoms = dict.fromkeys((*residual, *bound_atoms))
    try:
        space = _cell_space(
            make_and(AtomLeaf(atom) for atom in partition_atoms), max_cells, store
        )
    except IntractableMarkerSet:
        remaining = _work_meter.remaining
        if remaining is not None and remaining < 0:
            raise
        return _RowStatus.OVER_CAP
    count = prod(map(len, space.partitions))
    if count < _MASK_CACHE_MIN_CELLS:
        truth, full = _compile_cell_truth_masks(space, count)
    else:
        truth, full = _cell_truth_masks(space, count, store=store)
    charge_work(count * (len(bound_atoms) + 1))
    within = _eval_mask(row.bound, truth, full)
    if not within:
        return _RowPattern(nonempty=False, bits=0)

    pattern = 0
    for index, node in enumerate(restricted):
        charge_work(count)
        value = _eval_mask(node, truth, full) & within
        if value == within:
            pattern |= 1 << index
        elif value:
            setup = (
                len(atoms)
                + 1
                + len(residual)
                + len(bound_atoms)
                + len(partition_atoms)
                + space.units
            )
            evaluation = count * (len(bound_atoms) + 1 + len(restricted))
            return _PreparedRow(
                restricted=restricted,
                masks=(truth, full),
                within=within,
                size=count,
                setup_units=setup,
                evaluation_units=evaluation,
            )
    return _RowPattern(nonempty=True, bits=pattern)


def _cube_mask(atoms: Iterable[Atom], truth: Mapping[Atom, int], full: int) -> int:
    """Intersect the predicates retained in a source-atom cube."""
    covered = full
    for atom in atoms:
        covered &= truth[atom]
    return covered


def _grow_cover_cube(
    atoms: Sequence[Atom],
    truth: Mapping[Atom, int],
    full: int,
    forbidden: int,
    point: int,
) -> _CoverCube:
    """Drop predicates while the cube avoids every forbidden observed pattern."""
    size = full.bit_length()
    charge_work(size * (len(atoms) + 1))
    # Every satisfied NNF clause is contained in the initial cube.
    cube = [atom for atom in atoms if truth[atom] & point]
    covered = _cube_mask(cube, truth, full)
    for atom in tuple(cube):
        charge_work(size * (len(cube) + 1))
        trial = [other for other in cube if other != atom]
        widened = _cube_mask(trial, truth, full)
        if not widened & forbidden:
            cube = trial
            covered = widened
    return _CoverCube(frozenset(cube), covered)


def _remove_covered_cubes(cubes: Sequence[_CoverCube], full: int) -> list[_CoverCube]:
    """Remove a cube only while the other retained cubes cover all its patterns."""
    size = full.bit_length()
    charge_work(size * (len(cubes) + 1) * max(1, len(cubes).bit_length()))
    kept = sorted(cubes, key=lambda cube: _clause_key(cube.atoms))
    for cube in tuple(kept):
        charge_work(size * (len(kept) + 1))
        others = 0
        for other in kept:
            if other is not cube:
                others |= other.mask
        if not cube.mask & ~others:
            kept.remove(cube)
    return kept


def _factor_clauses(clauses: Sequence[frozenset[Atom]]) -> Formula:
    """Render clauses in stable order with their common atoms factored out."""
    if not clauses:
        return FALSE
    common: frozenset[Atom] = frozenset.intersection(*clauses)
    residual = sorted((clause - common for clause in clauses), key=_clause_key)
    lead = [AtomLeaf(atom) for atom in sorted(common, key=_atom_key)]
    return make_and([*lead, _disjunction(residual)])


def _pattern_truth_columns(
    atoms: Sequence[Atom], patterns: Sequence[int]
) -> dict[Atom, int]:
    """Transpose observed predicate bits into columns for the mask evaluator."""
    charge_work(len(patterns) * len(atoms))
    truth = dict.fromkeys(atoms, 0)
    for index, pattern in enumerate(patterns):
        for position, atom in enumerate(atoms):
            if pattern & (1 << position):
                truth[atom] |= 1 << index
    return truth


def _cover_patterns(
    source: _CoverInput, patterns: AbstractSet[int]
) -> list[_CoverCube]:
    """Extract irredundant cubes over the observed predicate patterns."""
    atoms = source.atoms
    size = len(patterns)
    charge_work(size * max(1, size.bit_length()))
    truth = _pattern_truth_columns(atoms, sorted(patterns))
    full = (1 << size) - 1
    charge_work(size * (source.leaf_count + 1))
    selected = _eval_mask(source.tree, truth, full)

    cubes: list[_CoverCube] = []
    required = selected
    while required:
        cube = _grow_cover_cube(
            atoms, truth, full, full ^ selected, required & -required
        )
        cubes.append(cube)
        required &= full ^ cube.mask
    kept = _remove_covered_cubes(cubes, full)
    charge_work(size * (sum(len(cube.atoms) for cube in kept) + 1))
    return kept


def _project_patterns(
    patterns: AbstractSet[int] | Sequence[int],
    atoms: Sequence[Atom],
    retained: Sequence[Atom],
) -> set[int]:
    """Project every observed pattern onto the predicates a cover retains."""
    charge_work(len(patterns) * (len(atoms) + len(retained) + 1))
    positions = {atom: index for index, atom in enumerate(atoms)}
    columns = [positions[atom] for atom in retained]
    projected = set()
    for pattern in patterns:
        compact = 0
        for index, position in enumerate(columns):
            if pattern & (1 << position):
                compact |= 1 << index
        projected.add(compact)
    return projected


def _stabilize_cover(source: _CoverInput, patterns: AbstractSet[int]) -> Formula:
    """Extract a stable cover by projecting away discarded predicate columns."""
    while True:
        cubes = _cover_patterns(source, patterns)
        result = _factor_clauses([cube.atoms for cube in cubes])
        refined = _prepare_cover(result)
        if not refined.atoms or len(refined.atoms) == len(source.atoms):
            return result
        patterns = _project_patterns(patterns, source.atoms, refined.atoms)
        source = refined


def _row_cover(
    source: _CoverInput, rows: Sequence[_Row], max_cells: int, store: Memo
) -> Formula | None:
    """Build a stable cover by projecting out predicates the cover no longer uses."""
    if isinstance(source.tree, BoolConst):
        return source.tree
    patterns: set[int] = set()
    for index, row in enumerate(rows):
        pattern = _fixed_row_pattern(source.atoms, row, max_cells, store)
        if pattern is _RowStatus.OVER_CAP:
            break
        if pattern is _RowStatus.VARYING:
            return _varying_row_cover(
                source, rows, max_cells, store, start=index, patterns=patterns
            )
        if isinstance(pattern, _PreparedRow):
            columns = _prepared_row_columns(pattern, max_cells)
            # The split budget excludes the prepared lookup.
            del pattern
            if columns is None or not _add_row_patterns(
                columns, patterns, atom_count=len(source.atoms), max_cells=max_cells
            ):
                break
            del columns
            return _varying_row_cover(
                source, rows, max_cells, store, start=index + 1, patterns=patterns
            )
        if pattern.nonempty:
            patterns.add(pattern.bits)
            if len(patterns) * len(source.atoms) > max_cells:
                break
    else:
        return _stabilize_cover(source, patterns)
    return None


def _set_axis_work_fits(atoms: list[Atom], max_cells: int) -> bool:
    """Reject set axes whose required column work already exceeds the cap."""
    charge_work(len(atoms))
    for axis, predicates in _atoms_by_axis(atoms).items():
        if axis[0] == AXIS_SET:
            names = {atom.literal for atom in predicates}
            if (1 << len(names)) * len(predicates) > max_cells:
                return False
    return True


def _row_pattern_columns(
    atoms: Sequence[Atom],
    row: _Row,
    max_cells: int,
    store: Memo,
    *,
    max_buffer_units: int | None = None,
) -> _RowTruth | None:
    """Compile a row's source columns and release its temporary lookup map."""
    charge_work(len(atoms) + 1)
    restricted = [_restrict_cover_atom(atom, row.pins, store) for atom in atoms]
    residual = [atom for node in restricted for atom in collect_atoms(node)]
    bound_atoms = collect_atoms(row.bound)
    charge_work(len(residual) + len(bound_atoms))
    partition_atoms = list(dict.fromkeys((*residual, *bound_atoms)))
    if not _set_axis_work_fits(partition_atoms, max_cells):
        return None
    try:
        space = _cell_space(
            make_and(AtomLeaf(atom) for atom in partition_atoms), max_cells, store
        )
    except IntractableMarkerSet:
        remaining = _work_meter.remaining
        if remaining is not None and remaining < 0:
            raise
        return None
    count = prod(map(len, space.partitions))
    if count * (len(atoms) + 1) > max_cells:
        return None
    # The partition lookup survives while source columns and residual masks form.
    if (
        max_buffer_units is not None
        and 2 * len(partition_atoms)
        + (len(partition_atoms) + len(atoms) + 3) * _integer_payload_units(count)
        > max_buffer_units
    ):
        return None
    if count < _MASK_CACHE_MIN_CELLS:
        truth, full = _compile_cell_truth_masks(space, count)
    else:
        truth, full = _cell_truth_masks(space, count, store=store)
    charge_work(count * (len(bound_atoms) + 1))
    within = _eval_mask(row.bound, truth, full)
    if not within:
        return _RowTruth((), 0, count)
    charge_work(count * len(restricted))
    columns = tuple(_eval_mask(node, truth, full) for node in restricted)
    return _RowTruth(columns, within, count)


def _prepared_row_columns(prepared: _PreparedRow, max_cells: int) -> _RowTruth | None:
    """Build prepared columns with the full preparation work charge."""
    charge_work(prepared.setup_units)
    if prepared.size * (len(prepared.restricted) + 1) > max_cells:
        return None
    charge_work(prepared.evaluation_units)
    truth, full = prepared.masks
    columns = tuple(_eval_mask(node, truth, full) for node in prepared.restricted)
    return _RowTruth(columns, prepared.within, prepared.size)


def _pattern_buffer_units(
    *, columns: int, cell_units: int, prefix_units: int, regions: int, existing: int
) -> int:
    """Price additional container slots and 64-bit integer payload units."""
    # Old regions and doubled output need at most three records per input.
    records = 3 * regions * (3 + cell_units + prefix_units)
    sets = (existing + 2 * regions) * (1 + prefix_units)
    temporaries = (columns + 6) * cell_units + 3 * prefix_units
    return _PATTERN_BUFFER_FIXED_UNITS + columns + records + sets + temporaries


def _split_row_patterns(
    row: _RowTruth, existing: int, max_cells: int
) -> set[int] | None:
    """Enumerate every source truth pattern, including source-false witnesses."""
    if not row.within:
        return set()
    cell_units = max(1, (row.size + _PATTERN_BITS_PER_UNIT) // _PATTERN_BITS_PER_UNIT)
    prefix_units = max(
        1, (len(row.columns) + _PATTERN_BITS_PER_UNIT - 1) // _PATTERN_BITS_PER_UNIT
    )
    minimum = _pattern_buffer_units(
        columns=len(row.columns),
        cell_units=cell_units,
        prefix_units=prefix_units,
        regions=1,
        existing=existing,
    )
    if minimum > max_cells:
        return None
    regions = [(0, row.within)]
    for position, value in enumerate(row.columns):
        units = _pattern_buffer_units(
            columns=len(row.columns),
            cell_units=cell_units,
            prefix_units=prefix_units,
            regions=len(regions),
            existing=existing,
        )
        if units > max_cells:
            return None
        charge_work(
            row.size
            + len(regions) * (2 * cell_units + 2 * prefix_units + 3)
            + prefix_units
        )
        bit = 1 << position
        complement = ~value
        refined = []
        for prefix, region in regions:
            positive = region & value
            negative = region & complement
            if positive:
                refined.append((prefix | bit, positive))
            if negative:
                refined.append((prefix, negative))
        regions = refined
    charge_work(len(regions) * (len(row.columns) + prefix_units + 1))
    return {prefix for prefix, _ in regions}


def _add_row_patterns(
    columns: _RowTruth, patterns: set[int], *, atom_count: int, max_cells: int
) -> bool:
    """Add one row's observed patterns within the aggregate predicate cap."""
    observed = _split_row_patterns(columns, len(patterns), max_cells)
    if observed is None:
        return False
    patterns.update(observed)
    return len(patterns) * atom_count <= max_cells


def _varying_row_cover(
    source: _CoverInput,
    rows: Sequence[_Row],
    max_cells: int,
    store: Memo,
    *,
    start: int,
    patterns: set[int],
) -> Formula | None:
    """Extend the accumulated patterns with all remaining rows."""
    independent = _independent_cover(source, rows, max_cells, store)
    if independent is not None:
        return independent
    if not _extend_row_patterns(
        source.atoms, rows, max_cells, store, start=start, patterns=patterns
    ):
        return None
    result = _stabilize_cover(source, patterns)
    refined = _prepare_cover(result)
    if refined.atoms != source.atoms:
        independent = _independent_cover(refined, rows, max_cells, store)
        if independent is not None:
            return independent
    return result


def _simplify_greedy(
    nnf: Formula,
    rows: Sequence[_Row],
    max_cells: int,
    store: Memo,
) -> Formula:
    """Remove an NNF tree's clauses and atoms to a fixpoint over the universe's rows."""
    clauses = _dedupe(_to_clauses(nnf, max_cells))
    original = _disjunction(clauses)
    original_by_row = [_restrict_row_tree(original, row.pins, store) for row in rows]
    while True:
        before = _canonical(clauses)
        clauses = _drop_clauses(clauses, rows, original_by_row, max_cells, store)
        clauses = _dedupe(_drop_atoms(clauses, rows, original_by_row, max_cells, store))
        if _canonical(clauses) == before:
            break
    return _factor_clauses(clauses)


def simplify_within(
    node: Formula,
    universe: Formula,
    max_cells: int,
    max_work: int,
    store: Memo | None = None,
) -> Formula:
    """Return a tree equivalent to ``node`` on every point of ``universe``.

    Admitted row truth patterns allow a direct cover.
    Other inputs use greedy clause and atom removal.
    Neither path guarantees the smallest tree.

    ``max_cells`` bounds each decision and the direct truth-table payload.
    ``max_work`` covers the direct attempt and greedy fallback together.
    """
    shared = Memo() if store is None else store
    previous_work = _work_meter.remaining
    _work_meter.remaining = max_work
    try:
        _charge_formula_work(node)
        universe_work = _charge_universe_work(universe, shared)
        source = _prepare_cover(to_nnf(node))
        rows = (
            ()
            if isinstance(source.tree, BoolConst)
            else _decompose_rows(universe, shared, raw_work=universe_work)
        )
        direct = _row_cover(source, rows, max_cells, shared)
        if direct is not None:
            return direct
        greedy = _simplify_greedy(source.tree, rows, max_cells, shared)
        refined = _prepare_cover(greedy)
        if len(refined.atoms) < len(source.atoms):
            direct = _row_cover(refined, rows, max_cells, shared)
            if direct is not None:
                return direct
        return greedy
    finally:
        _work_meter.remaining = previous_work


class _FreeSetAxis(NamedTuple):
    """Predicates on a free set axis and the remaining row predicates."""

    members: tuple[Atom, ...]
    others: tuple[Atom, ...]


def _free_set_axis(atoms: Sequence[Atom], rows: Sequence[_Row]) -> _FreeSetAxis | None:
    """Find a set domain whose observations can vary independently in every row."""
    charge_work(len(atoms) + len(rows))
    members = tuple(atom for atom in atoms if atom.kind == AXIS_SET)
    axes = {atom.axis() for atom in members}
    if len(axes) != 1:
        return None
    variable = next(iter(axes))[1]
    for row in rows:
        _charge_formula_work(row.bound)
        bound_atoms = collect_atoms(row.bound)
        charge_work(len(row.pins) + len(bound_atoms))
        if variable in row.pins or any(
            atom.variable == variable for atom in bound_atoms
        ):
            return None
    return _FreeSetAxis(members, tuple(atom for atom in atoms if atom.kind != AXIS_SET))


def _extend_row_patterns(
    atoms: Sequence[Atom],
    rows: Sequence[_Row],
    max_cells: int,
    store: Memo,
    *,
    start: int,
    patterns: set[int],
) -> bool:
    """Retain all admitted observations, including those making the source false."""
    for index in range(start, len(rows)):
        columns = _row_pattern_columns(atoms, rows[index], max_cells, store)
        if columns is None:
            return False
        if not _add_row_patterns(
            columns, patterns, atom_count=len(atoms), max_cells=max_cells
        ):
            return False
        del columns
    return True


def _integer_payload_units(bits: int) -> int:
    """Price integer payloads, including the carry bit while building a full mask."""
    return max(1, (bits + _PATTERN_BITS_PER_UNIT) // _PATTERN_BITS_PER_UNIT)


def _independent_cover_units(
    axis: _FreeSetAxis, names: int, assignments: int, scalar_patterns: int
) -> int:
    """Bound pattern, sort and column buffers in slots plus integer payload units."""
    members, others = len(axis.members), len(axis.others)
    prefix = _integer_payload_units(members)
    mask = _integer_payload_units(assignments)
    return (
        _PATTERN_BUFFER_FIXED_UNITS
        + members
        + others
        + names * (4 + prefix)
        + assignments * (4 + 2 * prefix)
        + scalar_patterns * (4 + 2 * _integer_payload_units(others))
        + 2 * (members + others)
        + (members + 3) * mask
        + (others + 3) * _integer_payload_units(scalar_patterns)
    )


def _membership_patterns(
    axis: _FreeSetAxis, scalar_patterns: int, max_cells: int
) -> list[int] | None:
    """Enumerate realizable assignments, keeping opposite tests of one name linked."""
    count = _set_assignment_count(axis.members, max_cells)
    if count is None:
        return None
    names = count.bit_length() - 1
    if _independent_cover_units(axis, names, count, scalar_patterns) > max_cells:
        return None
    charge_work(count * (len(axis.members) + max(1, count.bit_length())))
    groups: dict[str, int] = {}
    negative = 0
    for index, atom in enumerate(axis.members):
        groups[atom.literal] = groups.get(atom.literal, 0) | (1 << index)
        if not atom.positive:
            negative |= 1 << index
    patterns: list[int] = [negative]
    for mask in groups.values():
        patterns += [pattern ^ mask for pattern in patterns]
    patterns.sort()
    return patterns


def _set_assignment_count(atoms: Sequence[Atom], max_cells: int) -> int | None:
    """Admit the powerset's column count before allocating positioned integers."""
    charge_work(len(atoms))
    names = {atom.literal for atom in atoms}
    if len(names) >= max_cells.bit_length():
        return None
    count = 1 << len(names)
    return count if count * (len(atoms) + 1) <= max_cells else None


def _replace_observed_atoms(node: Formula, values: Mapping[Atom, bool]) -> Formula:
    """Substitute observations in a nonconstant NNF tree to obtain a cofactor."""
    if isinstance(node, AtomLeaf):
        value = values.get(node.atom)
        return node if value is None else (TRUE if value else FALSE)
    if isinstance(node, AndNode):
        return make_and(
            _replace_observed_atoms(child, values) for child in node.children
        )
    assert isinstance(node, OrNode)
    return make_or(_replace_observed_atoms(child, values) for child in node.children)


def _cofactor_cover(
    node: Formula, patterns: AbstractSet[int] | Sequence[int], atoms: Sequence[Atom]
) -> Formula:
    """Normalize a cofactor over every observation of its retained domain."""
    source = _prepare_cover(node)
    if isinstance(source.tree, BoolConst):
        return source.tree
    return _stabilize_cover(source, _project_patterns(patterns, atoms, source.atoms))


class _CofactorWitness(NamedTuple):
    """A shared nonzero membership function and a scalar assignment selecting it."""

    members: int
    scalar: int


def _common_membership_cofactor(
    source: _CoverInput,
    axis: _FreeSetAxis,
    scalar_patterns: AbstractSet[int],
    member_patterns: Sequence[int],
) -> _CofactorWitness | None:
    """Recognize AND separability by comparing every nonzero cofactor."""
    count = len(member_patterns)
    truth = _pattern_truth_columns(axis.members, member_patterns)
    full = (1 << count) - 1
    selected_members = 0
    representative_scalar = 0
    for pattern in scalar_patterns:
        charge_work(count * (source.leaf_count + len(axis.others) + 1))
        truth.update(
            (atom, full if pattern & (1 << index) else 0)
            for index, atom in enumerate(axis.others)
        )
        selected = _eval_mask(source.tree, truth, full)
        if selected:
            if selected_members and selected_members != selected:
                return None
            selected_members = selected
            representative_scalar = pattern
    return _CofactorWitness(selected_members, representative_scalar)


def _independent_cover(
    source: _CoverInput, rows: Sequence[_Row], max_cells: int, store: Memo
) -> Formula | None:
    """Factor f(m,s) when every nonzero membership cofactor is the same function."""
    axis = _free_set_axis(source.atoms, rows)
    if axis is None:
        return None
    scalar_patterns: set[int] = set()
    if not _extend_row_patterns(
        axis.others, rows, max_cells, store, start=0, patterns=scalar_patterns
    ):
        return None
    member_patterns = _membership_patterns(axis, len(scalar_patterns), max_cells)
    if member_patterns is None:
        return None
    witness = _common_membership_cofactor(
        source, axis, scalar_patterns, member_patterns
    )
    if witness is None:
        return None
    if not witness.members:
        return FALSE
    representative_member = member_patterns[
        (witness.members & -witness.members).bit_length() - 1
    ]
    charge_work(2 * (source.leaf_count + len(source.atoms)))
    member_tree = _replace_observed_atoms(
        source.tree,
        {
            atom: bool(witness.scalar & (1 << index))
            for index, atom in enumerate(axis.others)
        },
    )
    scalar_tree = _replace_observed_atoms(
        source.tree,
        {
            atom: bool(representative_member & (1 << index))
            for index, atom in enumerate(axis.members)
        },
    )
    return make_and(
        (
            _cofactor_cover(member_tree, member_patterns, axis.members),
            _cofactor_cover(scalar_tree, scalar_patterns, axis.others),
        )
    )


def _comparison_member_units(axis: _FreeSetAxis, member_cells: int) -> int:
    """Reserve tables and lookup masks; recursive evaluation uses the work bound."""
    member_count, other_count = len(axis.members), len(axis.others)
    return (
        _PATTERN_BUFFER_FIXED_UNITS
        + 4 * (member_count + other_count)
        + (2 * member_count + 5) * _integer_payload_units(member_cells)
    )


class _ComparisonBuffers(NamedTuple):
    """Admitted set cells and scalar-column payload allowance for one comparison."""

    member_cells: int
    scalar_units: int


def _comparison_buffers(
    axis: _FreeSetAxis, max_cells: int
) -> _ComparisonBuffers | None:
    """Reserve the free set table and lookup before preparing scalar row columns."""
    cells = _set_assignment_count(axis.members, max_cells)
    if cells is None:
        return None
    scalar_units = max_cells - _comparison_member_units(axis, cells)
    return _ComparisonBuffers(cells, scalar_units) if scalar_units >= 0 else None


def _independent_rows_equivalent(
    left: Formula,
    right: Formula,
    rows: Sequence[_Row],
    node_count: int,
    max_cells: int,
    store: Memo,
) -> bool | None:
    """Compare original formulas on complete independent set and row domains."""
    atoms = tuple(dict.fromkeys((*collect_atoms(left), *collect_atoms(right))))
    axis = _free_set_axis(atoms, rows)
    if axis is None:
        return None
    buffers = _comparison_buffers(axis, max_cells)
    if buffers is None:
        return None
    membership: _RowTruth | None = None
    for row in rows:
        if isinstance(row.bound, BoolConst) and not row.bound.value:
            continue
        columns = _row_pattern_columns(
            axis.others, row, max_cells, store, max_buffer_units=buffers.scalar_units
        )
        if columns is None:
            return None
        if not columns.within:
            continue
        if membership is None:
            membership = _row_pattern_columns(
                axis.members,
                _Row({}, TRUE),
                max_cells,
                store,
                max_buffer_units=2 * len(axis.members)
                + (2 * len(axis.members) + 3)
                * _integer_payload_units(buffers.member_cells),
            )
            # A free set axis has exactly the preadmitted powerset of observations.
            assert membership is not None
            assert membership.size == buffers.member_cells
        if not _compare_independent_row(
            left, right, axis, membership, columns, node_count
        ):
            return False
        del columns
    return True


def _compare_independent_row(
    left: Formula,
    right: Formula,
    axis: _FreeSetAxis,
    membership: _RowTruth,
    columns: _RowTruth,
    node_count: int,
) -> bool:
    """Evaluate both original trees for every admitted scalar and set assignment."""
    charge_work(len(axis.members) + len(axis.others))
    truth = dict(zip(axis.members, membership.columns, strict=True))
    full = (1 << membership.size) - 1
    remaining = columns.within
    while remaining:
        charge_work(
            membership.size * (node_count + len(axis.others) + 1)
            + columns.size * (len(axis.others) + 1)
        )
        point = remaining & -remaining
        remaining ^= point
        truth.update(
            (atom, full if value & point else 0)
            for atom, value in zip(axis.others, columns.columns, strict=True)
        )
        if _eval_mask(left, truth, full) != _eval_mask(right, truth, full):
            return False
    return True
