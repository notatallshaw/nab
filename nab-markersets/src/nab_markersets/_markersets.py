"""Marker algebra engine behind :class:`~nab_markersets.markersets.MarkerSet`.

A marker parses into a normalised op-tree over typed atoms, and a value atom's
denotation is packaging's own ``_eval_op``, so a marker means here what it means
there. A decision partitions each axis the tree names into cells on which every
atom is constant, enumerates their product under ``max_cells``, and evaluates
the tree once per cell.
"""

from __future__ import annotations

import re
import sys
import threading
import weakref
from functools import lru_cache
from itertools import pairwise, product
from typing import TYPE_CHECKING, NamedTuple, cast

from packaging._parser import Op, Value, Variable, parse_marker
from packaging._tokenizer import ParserSyntaxError
from packaging.markers import (
    InvalidMarker,
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
    _eval_op,
)
from packaging.specifiers import InvalidSpecifier, Specifier
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .errors import IntractableMarkerSet, UnserializableMarkerSet

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from typing import TypeAlias

    # packaging's parse tree, narrowed to what ``parse_marker`` builds. Its own
    # ``MarkerAtom`` recurses through ``Sequence``, which admits a tuple and so
    # blocks the isinstance narrowing the walkers below rely on.
    MarkerOperand: TypeAlias = "Variable | Value"
    MarkerComparison: TypeAlias = "tuple[MarkerOperand, Op, MarkerOperand]"
    MarkerNode: TypeAlias = "MarkerComparison | str | list[MarkerNode]"

    # An axis is a variable's domain, named by its kind. A node key is the
    # structural key two trees of the same shape share.
    Axis: TypeAlias = "tuple[str, ...]"
    NodeKey: TypeAlias = "tuple[object, ...]"
    AtomKey: TypeAlias = "tuple[str, str, str, bool, bool, str, str, bool]"
    ClauseKey: TypeAlias = "tuple[AtomKey, ...]"


# Axis kinds. Atoms on one axis share a partition and are constant on each cell.
AXIS_VALUE = "value"
AXIS_SET = "set"
AXIS_CONTAINS = "contains"

DOMAIN_VERSION = "version"
DOMAIN_STRING = "string"
DOMAIN_TWIN = "version_or_string"
DOMAIN_SET = "set"

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
    return _apply_memoised(lhs, op, rhs, key, sys.get_int_max_str_digits())


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
    limit = sys.get_int_max_str_digits()
    if not limit:
        return False
    return any(len(run.group()) > limit for run in _DIGIT_RUN.finditer(text))


# ---------------------------------------------------------------------------- atoms


# Weak, so an entry lives only while its atom does. The lock stops two threads
# minting rival atoms for one key, which the algebra would read as two leaves.
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
        so the substring test and the value comparison decide on one point.

        On a version-dispatch variable that test keeps a boolean axis of its own
        per literal, because the values embedding a literal are not enumerable
        from it.
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
            member = self.literal in point  # type: ignore[operator]
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


def _parse_ast(source: str | Marker) -> list[MarkerNode] | None:
    """Parse a marker with packaging; None for an empty marker."""
    if isinstance(source, Marker):
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
        return cast("list[MarkerNode]", parse_marker(source))
    except ParserSyntaxError as exc:
        # Match packaging: a malformed marker raises InvalidMarker.
        raise InvalidMarker(str(exc)) from exc


def parse(source: str | Marker) -> Formula:
    """Parse a marker into the normalised op-tree."""
    parsed = _parse_ast(source)
    return TRUE if parsed is None else _convert(parsed)


def variable_names(source: str | Marker) -> frozenset[str]:
    """Return every marker variable ``source`` names, in the parser's spelling.

    Builds no atoms, so a marker the algebra rejects still yields its names.
    """
    parsed = _parse_ast(source)
    if parsed is None:
        return frozenset()

    names: set[str] = set()
    _collect_variables(parsed, names)
    return frozenset(names)


def _collect_variables(node: list[MarkerNode], names: set[str]) -> None:
    for item in node:
        if isinstance(item, str):
            continue
        if not isinstance(item, tuple):
            _collect_variables(item, names)
            continue

        lhs, _op, rhs = item
        if isinstance(lhs, Variable):
            names.add(lhs.value)
        if isinstance(rhs, Variable):
            names.add(rhs.value)
        elif not isinstance(lhs, Variable) and rhs.value in DOMAIN_REGISTRY:
            # packaging reads a literal-vs-literal right operand as an environment key.
            names.add(rhs.value)


def _convert(node: list[MarkerNode]) -> Formula:
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
        return FALSE  # a set variable in any non-membership form is constant False.
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
            f"{sys.get_int_max_str_digits()}-digit parse limit"
        )
        raise IntractableMarkerSet(msg)


def _reject_mint_overflow(literals: Sequence[str]) -> None:
    """Reserve one digit so neighbour minting cannot overflow the parse limit.

    Minting increments a numeric component, so a run at the limit width rolls
    one digit past it: this rejects at the limit, where
    :func:`reject_oversized_version_literals` rejects past it.
    """
    limit = sys.get_int_max_str_digits()
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


class Memo:
    """The verdicts, partitions, atom truths and version parses a decision re-reads.

    A decision makes its own unless the caller passes one to share; see
    :class:`~nab_markersets.DecisionStore`.
    """

    __slots__ = ("decisions", "partitions", "truths", "versions")

    def __init__(self) -> None:
        self.decisions: dict[tuple[NodeKey, int], _Decision] = {}
        self.partitions: dict[tuple[Axis, tuple[Atom, ...], int], list[Cell]] = {}
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

    for suffix in (".dev0", "a0", ".post0", ".1", "+l"):
        candidate = f"{base}{suffix}"
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
    """Mint the points that separate the literal's version reading from its string reading.

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

    Release ordering runs ahead of pre, post, dev and local, so extending
    ``vlow``'s release by one outranks it whatever suffix it carries.

    Padding to the wider release keeps the result under ``vhigh`` whenever the
    two releases differ, that being the deepest place ``vhigh`` can differ.

    Where the ends share a release, or across an epoch boundary, ``vhigh`` does
    not bound it and the caller's ordering check rejects the candidate.
    """
    low = vlow.release
    width = len(low) if vlow.epoch != vhigh.epoch else max(len(low), len(vhigh.release))
    parts = (*low, *(0,) * (width - len(low)), 1)
    prefix = f"{vlow.epoch}!" if vlow.epoch else ""
    return prefix + ".".join(str(part) for part in parts)


def _between(vlow: Version, low: str, vhigh: Version, memo: Memo) -> str | None:
    """Return a point strictly between two adjacent pool points, or None.

    An exclusive comparison excludes its own bound's post, local, pre and dev
    variants. Where the two points' releases differ, only a candidate whose own
    release differs from both lands between them, so the plain release is tried
    first.

    The suffixed candidates fill a band whose ends share a release.
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


def _version_pool(
    entries: Iterable[tuple[Version, str]],
    *,
    elevate_epoch: bool,
    max_cells: int,
    memo: Memo,
) -> list[str]:
    parsed: list[tuple[Version, str]] = list(_POOL_ANCHORS)
    seen: set[str] = {text for _, text in _POOL_ANCHORS}
    for version, text in entries:
        if text in seen:
            continue
        seen.add(text)
        parsed.append((version, text))
    parsed.sort()

    extra: list[str] = []
    for (vlow, slow), (vhigh, _shigh) in pairwise(parsed):
        if vlow == vhigh:
            continue
        mid = _between(vlow, slow, vhigh, memo)
        if mid is not None:
            extra.append(mid)

    base = [text for _, text in parsed] + extra
    if not elevate_epoch:
        return base
    return _elevate_epochs(base, parsed, max_cells)


def _elevate_epochs(
    base: list[str], parsed: Sequence[tuple[Version, str]], max_cells: int
) -> list[str]:
    # A1 lowers python_version here, so major.minor and full ordering diverge across
    # an epoch boundary: 1!3.9 truncates to 3.9 yet outranks 3.14. Each point needs a
    # twin in every epoch the pool reaches, and the pool already carries one above
    # the top literal, so the range runs two past it.
    epochs = {version.epoch for version, _ in parsed}
    targets = range(1, max(epochs) + 2)

    elevated = list(base)
    for epoch in targets:
        for text in base:
            elevated.append(f"{epoch}!{text}")
            if len(elevated) > max_cells:
                msg = f"version pool exceeds max_cells={max_cells}"
                raise IntractableMarkerSet(msg)
    return elevated


def _membership_candidates(atom: Atom, memo: Memo) -> list[str]:
    subs = _substrings(atom.literal)
    if atom.derive_mm:
        # A1 tests a full version's major.minor; only version substrings are realisable.
        return [
            s for s in subs if _pooled_version(s.removesuffix(".*"), memo) is not None
        ]
    return subs


def _mixes_mm_and_full(atoms: Sequence[Atom]) -> bool:
    """Whether the axis carries both A1-lowered and direct version atoms."""
    return any(atom.derive_mm for atom in atoms) and any(
        not atom.derive_mm for atom in atoms
    )


def _dedupe_candidates(
    candidates: Iterable[str], *, pure_version: bool, max_cells: int, memo: Memo
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        # A pure Version axis holds only PEP 440 versions.
        if pure_version and _pooled_version(candidate, memo) is None:
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
    can run past printable ASCII. Any unused character serves.
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

    A separator none of ``literals`` uses joins one subset, so every occurrence
    in the point falls in one piece.

    The point then embeds that subset and what the subset embeds, equals no
    literal, and is a substring of none. Every combination the atoms realise
    together is some subset, so the axis reaches all of them.
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
        candidates.extend(
            _version_pool(
                entries,
                elevate_epoch=_mixes_mm_and_full(atoms),
                max_cells=max_cells,
                memo=memo,
            )
        )
    return _dedupe_candidates(
        candidates,
        pure_version=raw_kind == DOMAIN_VERSION,
        max_cells=max_cells,
        memo=memo,
    )


def _reduce_cells(
    points: Iterable[object], atoms: Sequence[Atom], max_cells: int, memo: Memo
) -> list[Cell]:
    points = list(points)

    if len(points) * len(atoms) > max_cells:
        msg = (
            f"axis work {len(points)}x{len(atoms)} atoms exceeds max_cells={max_cells}"
        )
        raise IntractableMarkerSet(msg)

    representatives: dict[tuple[bool, ...], object] = {}
    for point in points:
        vector = tuple(_truth(atom, point, memo) for atom in atoms)
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
        _value_candidates(variable, atoms, max_cells, memo), atoms, max_cells, memo
    )


def partition_set_axis(atoms: Sequence[Atom], max_cells: int, memo: Memo) -> list[Cell]:
    """Cells of a set axis: the powerset over the mentioned names (guarded)."""
    names = _mentioned_names(atoms)
    count = len(names)
    if (1 << count) > max_cells:
        msg = f"set powerset over {count} names exceeds max_cells={max_cells}"
        raise IntractableMarkerSet(msg)
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


# ``max_cells`` bounds one decision, the meter a whole simplify. Thread-local and
# unset outside a simplify, so every other decision stays unmetered.
_work_meter = threading.local()


def charge_work(units: int) -> None:
    """Charge ``units`` of cell work to the running simplify's meter, if any."""
    remaining = getattr(_work_meter, "remaining", None)
    if remaining is None:
        return
    remaining -= units
    _work_meter.remaining = remaining
    if remaining < 0:
        msg = "simplification work exceeds max_work"
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
    return atom.holds(atom.literal in _require(env, atom.variable))  # type: ignore[operator]


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


def _cell_space(node: Formula, max_cells: int, store: Memo) -> _CellSpace:
    """Partition each axis a tree mentions and charge the enumeration it implies."""
    atoms = collect_atoms(node)
    grouped = _atoms_by_axis(atoms)
    axes = list(grouped)
    atomlists = [grouped[axis] for axis in axes]
    partitions = [
        partition_axis(axis, axis_atoms, max_cells, store)
        for axis, axis_atoms in zip(axes, atomlists, strict=True)
    ]

    if not axes:
        return _CellSpace(axes, atomlists, partitions, 0)

    # Repeated atoms inflate the per-cell walk without inflating the cell
    # product, so the guard counts leaf occurrences too.
    units = guarded_product_size(
        (*(len(part) for part in partitions), len(atoms)), max_cells
    )
    charge_work(units)
    return _CellSpace(axes, atomlists, partitions, units)


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
    return _Decision(
        next(_enumerate_cells(node, space), _MISSING) is _MISSING, space.units
    )


def is_empty(node: Formula, max_cells: int, store: Memo | None = None) -> bool:
    """Whether a tree denotes the empty set.

    A verdict ``store`` holds is reused and charged again, so the work budget
    does not depend on the memo.
    """
    if store is None:
        return _decide_empty(node, max_cells, Memo()).empty

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
        return atom.holds(atom.literal in value)  # type: ignore[operator]
    return atom.holds(value)


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
    # Excluded middle holds for an unswapped ==/!= on a pure-version axis alone.
    # An ordered comparison has the prerelease hole, a twin may hold a
    # non-version, and a swapped literal is the specifier bound.
    if op in ("==", "!=") and is_pure_version(var) and not atom.swapped:
        return AtomLeaf(atom.replaced(op="!=" if op == "==" else "=="))
    msg = f"no marker string spells the complement of {_render_atom(atom)}"
    raise UnserializableMarkerSet(msg)


def _flip_string_op(atom: Atom, flipped: str) -> Formula:
    """Return ``atom`` under ``flipped``, refusing if that leaves the string table.

    An equality specifier accepts a wildcard and a local version where an
    ordered one does not, so a flip can make the atom dispatch as a version and
    denote something else.
    """
    if is_version_dispatch(atom.variable) and _builds_specifier(flipped, atom.literal):
        msg = f"no marker string spells the complement of {_render_atom(atom)}"
        raise UnserializableMarkerSet(msg)
    return AtomLeaf(atom.replaced(op=flipped))


def _complement_string(atom: Atom, op: str) -> Formula:
    """Complement an atom packaging reads through the string operator table.

    A swapped atom on a version-dispatch variable is not the table's to
    complement, because packaging builds its specifier from the environment
    value.

    Such an atom compares as a version wherever that value parses as one, and
    through the table only on the rest.

    The table folds ``<`` and ``>`` to false and ``<=`` and ``>=`` to equality,
    so no ordered comparison complements to another.
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

    A leaf and a constant are already in normal form. A tree can normalise to a
    constant too: the string operator table folds ``<`` and ``>`` to false, so
    complementing such an atom yields ``TRUE``.
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
    """Summarise a set for :func:`repr`, without exposing the op-tree."""
    if isinstance(node, BoolConst):
        return "universe" if node.value else "empty"
    try:
        nnf = to_nnf(node)
        if isinstance(nnf, BoolConst):
            return "universe" if nnf.value else "empty"
        return serialize(nnf)
    except UnserializableMarkerSet:
        return "unrepresentable"
    except RecursionError:
        # repr owes its caller a string, not the depth of the tree handed to it.
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
    return make_and(AtomLeaf(atom) for atom in clause)


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


def _decompose_rows(universe: Formula) -> list[_Row]:
    """Split a universe into rows: each top-level NNF disjunct with its pins."""
    nnf = universe if isinstance(universe, BoolConst) else to_nnf(universe)
    disjuncts = nnf.children if isinstance(nnf, OrNode) else (nnf,)
    rows: list[_Row] = []
    for disjunct in disjuncts:
        pins = _row_pins(disjunct)
        rows.append(_Row(pins, restrict_tree(disjunct, pins)))
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
        left_r = restrict_tree(left, row.pins)
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
    nnf = universe if isinstance(universe, BoolConst) else to_nnf(universe)
    disjuncts = nnf.children if isinstance(nnf, OrNode) else (nnf,)
    shared = Memo() if store is None else store
    return all(is_empty(disjunct, max_cells, shared) for disjunct in disjuncts)


def equivalent_within_rows(
    left: Formula,
    right: Formula,
    universe: Formula,
    max_cells: int,
    store: Memo | None = None,
) -> bool:
    """Whether two trees agree on every point of ``universe``, decided per row."""
    rows = _decompose_rows(universe)
    right_by_row = [restrict_tree(right, row.pins) for row in rows]
    return _rows_equivalent(
        left, rows, right_by_row, max_cells, Memo() if store is None else store
    )


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
        left_r = restrict_tree(left, row.pins)
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
        left_r = restrict_tree(left, row.pins)
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


def simplify_within(
    node: Formula,
    universe: Formula,
    max_cells: int,
    max_work: int,
    store: Memo | None = None,
) -> Formula:
    """Return a tree equivalent to ``node`` on every point of ``universe``.

    Greedy: expand to clauses, drop each clause and then each atom whose removal
    preserves within-universe equivalence, to a fixpoint, then factor the atoms
    common to every survivor into a leading conjunction. Each removal is decided
    per universe row, which is what keeps a wide multi-platform universe
    decidable, and a total atom order fixes the output.

    The result is not the smallest equivalent tree, and need not be smaller than
    ``node``: the clause expansion runs first, so a factored input whose clauses
    are all needed comes back expanded.

    ``max_cells`` bounds one decision. ``max_work`` meters the greedy loop,
    where a wide matrix runs many cheap decisions and a large membership
    powerset runs few expensive ones. Either overrun raises
    :class:`IntractableMarkerSet`.
    """
    nnf = to_nnf(node)
    clauses = _dedupe(_to_clauses(nnf, max_cells))
    original = _disjunction(clauses)
    rows = _decompose_rows(universe)
    original_by_row = [restrict_tree(original, row.pins) for row in rows]
    if store is None:
        store = Memo()
    previous_work = getattr(_work_meter, "remaining", None)
    _work_meter.remaining = max_work
    try:
        while True:
            before = _canonical(clauses)
            clauses = _drop_clauses(clauses, rows, original_by_row, max_cells, store)
            clauses = _dedupe(
                _drop_atoms(clauses, rows, original_by_row, max_cells, store)
            )
            if _canonical(clauses) == before:
                break
    finally:
        _work_meter.remaining = previous_work
    if not clauses:
        return FALSE
    clauses = sorted(clauses, key=_clause_key)
    common = frozenset.intersection(*clauses)
    residual = sorted((clause - common for clause in clauses), key=_clause_key)
    lead = [AtomLeaf(atom) for atom in sorted(common, key=_atom_key)]
    inner = make_or(
        _clause_formula(sorted(clause, key=_atom_key)) for clause in residual
    )
    return make_and([*lead, inner])
