"""Marker algebra engine: reason about PEP 508 markers as sets of environments.

The private engine behind :class:`~packaging.markersets.MarkerSet`. Parses a
marker string (or :class:`~packaging.markers.Marker`) into a normalised boolean
op-tree over typed atoms, with the packaging-faithful
``(variable, operator, literal)`` dispatch, A1 lowering of ``python_version``
onto the ``python_full_version`` axis, set-valued extras, and opaque
``contains`` atoms. The denotation of a value atom is delegated to packaging's
own ``_eval_op`` so it matches packaging exactly. Decisions run an on-demand
cell decomposition: every procedure re-partitions the referenced variables'
domains into cells on which each atom is constant, enumerates the cell product
under the ``max_cells`` guard, and evaluates the op-tree once per cell.
"""

from __future__ import annotations

import re
import sys
import threading
from itertools import pairwise, product
from typing import TYPE_CHECKING, NamedTuple, cast

from ._parser import Op, Variable, parse_marker
from ._tokenizer import ParserSyntaxError
from .markers import (
    InvalidMarker,
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
    _eval_op,
)
from .specifiers import InvalidSpecifier, Specifier
from .utils import canonicalize_name
from .version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence


class IntractableMarkerSet(ValueError):
    """The set is too complex to decide within the budget.

    Raised rather than hanging, overflowing the stack, or failing obscurely: an
    oversized cell product past ``max_cells``, a marker nested past the
    interpreter stack, or a version literal whose numeric component exceeds the
    digit parse limit. Subclasses :class:`ValueError` to match packaging's
    marker exceptions.
    """


class UnserializableMarkerSet(ValueError):
    """The set has no marker-string spelling.

    Raised, rather than emitting a wrong or masquerading string, for the empty
    set and for complements whose structure the marker grammar cannot express.
    Subclasses :class:`ValueError` to match packaging's marker exceptions.
    """


# Axis kinds. Atoms on the same axis share one cell partition and are each
# constant on every one of its cells.
AXIS_VALUE = "value"
AXIS_SET = "set"
AXIS_CONTAINS = "contains"

# Domain kinds a variable is typed through.
DOMAIN_VERSION = "version"
DOMAIN_STRING = "string"
DOMAIN_TWIN = "version_or_string"
DOMAIN_SET = "set"

# Every variable in packaging's marker grammar, typed to a domain. The twins
# ``implementation_version`` and ``platform_release`` dispatch as versions yet may
# hold an arbitrary string, so both carry a string fall-through. ``python_version``
# and ``python_full_version`` always receive a PEP 440 value, so they stay
# version-only; ``platform_version`` is a plain string.
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

_MEMBERSHIP = frozenset({"in", "not in"})
_ORDERED_UNDEFINED = frozenset({"~=", "==="})


def _domain(variable: str) -> str:
    """Return the effective domain of a variable under packaging typing."""
    kind = DOMAIN_REGISTRY[variable]
    return DOMAIN_VERSION if kind == DOMAIN_TWIN else kind


def is_version_dispatch(variable: str) -> bool:
    """Whether a variable dispatches as a version under packaging typing."""
    return _domain(variable) == DOMAIN_VERSION


def is_pure_version(variable: str) -> bool:
    """Whether a variable's domain is version-only, no string fall-through."""
    return DOMAIN_REGISTRY[variable] == DOMAIN_VERSION


def _apply(lhs: str, op: str, rhs: str, key: str) -> bool:
    return _eval_op(lhs, Op(op), rhs, key=key)


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

    A component wider than the interpreter's int-from-string limit makes
    packaging's ``Version`` raise a bare ``ValueError`` on parse. A zero limit
    disables the check, so nothing overflows.
    """
    limit = sys.get_int_max_str_digits()
    if not limit:
        return False
    return any(len(run.group()) > limit for run in _DIGIT_RUN.finditer(text))


# ---------------------------------------------------------------------------- atoms


# A value atom's denotation is memoised per atom, so the cache lives and dies
# with the atom's tree. The bound stops a long-lived atom evaluated against
# ever-new points from accumulating them. The oversized-value guards run
# uncached before any ``holds`` call, so a warm cache never bypasses them.
_HOLDS_CACHE_LIMIT = 1024


class Atom:
    """A normalised leaf whose ``holds`` gives its denotation on one point.

    Immutable once constructed, apart from two private caches minted lazily. A
    value atom memoises ``holds`` per point text, since decisions re-evaluate
    the same points across cell partitions; a version atom mints its pool
    entries once. Equality and hashing run off a field tuple precomputed at
    construction; the caches never take part.
    """

    __slots__ = (
        "_hash",
        "_holds_cache",
        "_key",
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
    origin: str  # the variable as written, for env lookup and serialisation
    op: str
    literal: str
    swapped: bool
    positive: bool
    derive_mm: bool  # A1: evaluate on the major.minor of the point

    _key: tuple[str, str, str, str, str, bool, bool, bool]
    _hash: int
    _holds_cache: dict[str, bool]
    _pool_entries: tuple[tuple[Version, str], ...] | None

    def __init__(
        self,
        kind: str,
        variable: str,
        origin: str,
        op: str,
        literal: str,
        *,
        swapped: bool = False,
        positive: bool = True,
        derive_mm: bool = False,
    ) -> None:
        self.kind = kind
        self.variable = variable
        self.origin = origin
        self.op = op
        self.literal = literal
        self.swapped = swapped
        self.positive = positive
        self.derive_mm = derive_mm
        self._key = (kind, variable, origin, op, literal, swapped, positive, derive_mm)
        self._hash = hash(self._key)
        self._holds_cache = {}
        self._pool_entries = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Atom):
            return NotImplemented
        return self._key == other._key

    def __hash__(self) -> int:
        return self._hash

    def replaced(self, *, op: str | None = None, positive: bool | None = None) -> Atom:
        """Return a copy with ``op`` or ``positive`` swapped out, for complements."""
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
        """Return the axis this atom partitions and is constant on."""
        if self.kind == AXIS_VALUE:
            return (AXIS_VALUE, self.variable)
        if self.kind == AXIS_SET:
            return (AXIS_SET, self.variable)
        # in / not in on the same (variable, literal) share one boolean axis.
        return (AXIS_CONTAINS, self.variable, self.literal)

    def holds(self, point: object) -> bool:
        """Return the atom's truth on one point of its axis."""
        if self.kind == AXIS_VALUE:
            text = str(point)
            cache = self._holds_cache
            if text in cache:
                return cache[text]
            result = _holds_value(self, text)
            if len(cache) < _HOLDS_CACHE_LIMIT:
                cache[text] = result
            return result
        if self.kind == AXIS_SET:
            member = self.literal in point  # type: ignore[operator]
            return member if self.positive else not member
        return bool(point) if self.positive else not bool(point)

    def pool_entries(self) -> tuple[tuple[Version, str], ...]:
        """The parsed version-pool points this atom seeds, minted lazily once.

        A pure property of the literal: its version neighbours, plus the
        neighbours of its version-parseable substrings for a membership atom.
        """
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


class BoolConst:
    """A first-class TRUE/FALSE, produced eagerly wherever a combination collapses."""

    __slots__ = ("value",)

    def __init__(self, value: bool) -> None:
        self.value = value


class AtomLeaf:
    """A single atom."""

    __slots__ = ("atom",)

    def __init__(self, atom: Atom) -> None:
        self.atom = atom


class AndNode:
    """A conjunction of two or more non-constant formulas."""

    __slots__ = ("children",)

    def __init__(self, children: tuple[Formula, ...]) -> None:
        self.children = children


class OrNode:
    """A disjunction of two or more non-constant formulas."""

    __slots__ = ("children",)

    def __init__(self, children: tuple[Formula, ...]) -> None:
        self.children = children


class NotNode:
    """A structural complement, negated per cell at decision time."""

    __slots__ = ("child",)

    def __init__(self, child: Formula) -> None:
        self.child = child


Formula = BoolConst | AtomLeaf | AndNode | OrNode | NotNode

TRUE = BoolConst(value=True)
FALSE = BoolConst(value=False)


def make_and(children: Iterable[Formula]) -> Formula:
    """Build a conjunction, folding identities and the FALSE annihilator."""
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
    """Build a disjunction, folding identities and the TRUE absorber."""
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
    """Structural complement with eager double-negation and constant folding."""
    if isinstance(node, BoolConst):
        return FALSE if node.value else TRUE
    if isinstance(node, NotNode):
        return node.child
    return NotNode(node)


# ------------------------------------------------------------------- construction


def _parse_ast(source: str | Marker) -> list | None:
    """Dispatch a marker to packaging's parser, or None for an empty marker."""
    if isinstance(source, Marker):
        source = str(source)
    elif not isinstance(source, str):
        msg = f"expected str or Marker, got {type(source).__name__}"
        raise TypeError(msg)
    if not source.strip():
        return None
    try:
        return parse_marker(source)
    except ParserSyntaxError as exc:
        # A malformed marker raises the public InvalidMarker, as packaging does,
        # not the tokenizer's internal syntax error.
        raise InvalidMarker(str(exc)) from exc


def parse(source: str | Marker) -> Formula:
    """Parse a marker string or :class:`Marker` into the normalised op-tree."""
    parsed = _parse_ast(source)
    return TRUE if parsed is None else _convert(parsed)


def variable_names(source: str | Marker) -> frozenset[str]:
    """Return every marker variable ``source`` names, as written.

    Walks the parsed marker collecting the variables its operands name, without
    building atoms, so a marker the algebra rejects at construction still yields
    its names. The result over-approximates semantic support.
    """
    parsed = _parse_ast(source)
    if parsed is None:
        return frozenset()

    names: set[str] = set()
    _collect_variables(parsed, names)
    return frozenset(names)


def _collect_variables(node: list, names: set[str]) -> None:
    for item in node:
        if isinstance(item, str):
            continue
        if isinstance(item, list):
            _collect_variables(item, names)
        else:
            lhs, _op, rhs = item
            if isinstance(lhs, Variable):
                names.add(lhs.value)
            if isinstance(rhs, Variable):
                names.add(rhs.value)
            elif not isinstance(lhs, Variable) and rhs.value in DOMAIN_REGISTRY:
                # packaging reads a literal-vs-literal comparison's right operand
                # as an environment key when it names a variable.
                names.add(rhs.value)


def _convert(node: list) -> Formula:
    or_groups: list[list[Formula]] = [[]]
    for item in node:
        if item == "or":
            or_groups.append([])
        elif item == "and":
            continue
        elif isinstance(item, list):
            or_groups[-1].append(_convert(item))
        else:
            or_groups[-1].append(_convert_atom(item))
    return make_or(make_and(group) for group in or_groups)


def _convert_atom(item: tuple) -> Formula:
    lhs, op_node, rhs = item
    op = op_node.serialize()
    if isinstance(lhs, Variable):
        # Variable-vs-variable keys off the left variable and treats the right
        # variable's name as the literal, matching packaging.
        return _make_atom(lhs.value, op, rhs.value, swapped=False)
    if isinstance(rhs, Variable):
        return _make_atom(rhs.value, op, lhs.value, swapped=True)

    # Neither side is a Variable node. packaging reads the right operand as an
    # environment key, so a quoted literal naming a known variable routes like a
    # swapped variable atom. A right operand naming no known variable folds via
    # the string operator table (packaging raises UndefinedEnvironmentName at
    # evaluate; the algebra evaluates, a documented divergence).
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

    # These axes seed single-segment pool points, so a single-segment probe drives
    # the swapped-operator validity check.
    _reject_undefined_operator(variable, op, literal, swapped=swapped, probe="0")
    if op == "===":
        msg = f"{op!r} is undefined on {variable!r} with literal {literal!r}"
        raise UndefinedComparison(msg)
    return AtomLeaf(Atom(AXIS_VALUE, variable, variable, op, literal, swapped=swapped))


def _make_python_version_atom(op: str, literal: str, *, swapped: bool) -> Formula:
    if op in _MEMBERSHIP and swapped:
        return _make_membership_atom("python_version", op, literal, swapped=swapped)
    if op == "~=" and swapped:
        # A swapped ~= makes the environment value the specifier bound, so the
        # true region is the same-major lower-minor band the version pool never
        # seeds; reject it, as the other version-dispatch axes do.
        msg = f"{op!r} is undefined on 'python_version' with literal {literal!r}"
        raise UndefinedComparison(msg)

    # A1-lowering maps python_version onto major.minor, so a two-segment probe
    # matches the validity of the non-swapped ~= and === forms.
    _reject_undefined_operator(
        "python_version", op, literal, swapped=swapped, probe="1.0"
    )
    # A1: lower onto python_full_version, evaluated on the major.minor of the point.
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
    """Raise before a numeric component past the parse limit reaches packaging.

    A numeric component over sys.get_int_max_str_digits() digits makes
    packaging's Version raise a bare ValueError; convert it to the bounded
    IntractableMarkerSet here.
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

    Cell decomposition mints version neighbours by incrementing one numeric
    component, so a run at the limit width rolls to one digit past it and
    makes packaging's Version raise a bare ValueError. Reject one digit early.
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
    """One piece of an axis's domain: a representative point and its truth vector."""

    point: object
    vector: tuple[bool, ...]


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
    out = [base]

    # Bumps stay in the literal's own epoch: the release bump of 1!3.9 is 1!3.10,
    # which outranks it, not 3.10, which sorts below and leaves the band above the
    # literal (1!4.0, 2!0) with no representative.
    prefix = f"{epoch}!" if epoch else ""
    bumps = [prefix + ".".join(str(x) for x in (*release[:-1], release[-1] + 1))]
    if len(release) > 1:
        bumps.append(f"{prefix}{major}.{release[1] + 1}")
    bumps.append(f"{prefix}{major + 1}")
    if epoch:
        # The band above a non-zero-epoch literal continues into the next epoch
        # (2!0 outranks every 1!* release), beyond any same-epoch bump.
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

    An exclusive comparison against a suffixed literal excludes the literal's own
    lower-precedence variants, so the adjacent point is the next or previous
    suffix of the same release, which no release bump reaches.
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


def _between(vlow: Version, low: str, vhigh: Version) -> str | None:
    for candidate in (
        f"{low}.post0",
        f"{low}+m",
        f"{low}.dev1",
        f"{low}.1",
        f"{low}a1",
    ):
        try:
            parsed = Version(candidate)
        except InvalidVersion:
            continue
        if vlow < parsed < vhigh:
            return candidate
    return None


# The fixed points anchoring every pool below and above the literal bands.
_POOL_ANCHORS: tuple[tuple[Version, str], ...] = tuple(
    (Version(text), text) for text in ("0", "0.dev0", "99999")
)


def _version_pool(
    entries: Iterable[tuple[Version, str]], *, elevate_epoch: bool, max_cells: int
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
        mid = _between(vlow, slow, vhigh)
        if mid is not None:
            extra.append(mid)

    base = [text for _, text in parsed] + extra
    if not elevate_epoch:
        return base
    return _elevate_epochs(base, parsed, max_cells)


def _elevate_epochs(
    base: list[str], parsed: Sequence[tuple[Version, str]], max_cells: int
) -> list[str]:
    # A1 lowers python_version onto this axis, so major.minor and full ordering
    # diverge across an epoch boundary: Version("1!3.9") truncates to "3.9" yet
    # outranks "3.14". Each point needs an epoch-bearing twin for every band up to
    # one epoch above the top literal, covering gap epochs no literal names.
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


def _membership_candidates(atom: Atom) -> list[str]:
    subs = _substrings(atom.literal)
    if atom.derive_mm:
        # A1 membership tests the major.minor of a full version, so realisable
        # points are the substrings of the literal that are themselves versions.
        return [s for s in subs if _parses_version(s)]
    return subs


def _mixes_mm_and_full(atoms: Sequence[Atom]) -> bool:
    """Whether the axis carries both A1-lowered and direct version atoms."""
    return any(atom.derive_mm for atom in atoms) and any(
        not atom.derive_mm for atom in atoms
    )


def _dedupe_candidates(
    candidates: Iterable[str], *, pure_version: bool, max_cells: int
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        # A pure Version axis holds only PEP 440 versions, so a non-version
        # candidate is unrealisable there. The twins keep every non-version
        # candidate, including the OTHER-cell representative.
        if pure_version and not _strict_version(candidate):
            continue
        seen.add(candidate)
        ordered.append(candidate)
        if len(ordered) > max_cells:
            msg = f"value candidate set exceeds max_cells={max_cells}"
            raise IntractableMarkerSet(msg)
    return ordered


def _other_representative(literals: Sequence[str]) -> str:
    """Return a string equal to no literal on the axis and parsing as no version.

    One character longer than the longest literal, so no literal can equal it,
    and built from a filler that never forms a PEP 440 version so the point
    lands in the arbitrary-string region rather than a version cell.
    """
    width = max((len(literal) for literal in literals), default=0) + 1
    return "z" * width


def _reduce_work_exceeds(
    variable: str, literals: Sequence[str], atom_count: int, max_cells: int
) -> bool:
    """Whether the axis's guaranteed reduce work already exceeds ``max_cells``.

    Every distinct literal is one point, so its count times the atom count is a
    lower bound on the ``_reduce_cells`` work. A pure-version axis keeps only
    version-parseable literals; the twins and string fields keep every distinct
    literal.
    """
    pure = is_pure_version(variable)
    seen: set[str] = set()
    for literal in literals:
        key = literal.removesuffix(".*") if pure else literal
        if key in seen or (pure and not _strict_version(key)):
            continue
        seen.add(key)
        if len(seen) * atom_count > max_cells:
            return True
    return False


def _value_candidates(
    variable: str, atoms: Sequence[Atom], max_cells: int
) -> list[str]:
    literals = [atom.literal for atom in atoms]

    reject_oversized_version_literals(variable, literals)
    if _reduce_work_exceeds(variable, literals, len(atoms), max_cells):
        msg = f"axis work over {len(atoms)} atoms exceeds max_cells={max_cells}"
        raise IntractableMarkerSet(msg)

    candidates: list[str] = []
    raw_kind = DOMAIN_REGISTRY[variable]

    # The OTHER cell (equal to no literal and not a version) exists only where the
    # domain admits arbitrary strings: string fields and the twins.
    if raw_kind in (DOMAIN_STRING, DOMAIN_TWIN):
        candidates.append(_other_representative(literals))
    candidates.extend(literals)

    # Cap the substring enumeration across the whole axis, not each literal alone,
    # so a set of long distinct literals fails loudly first.
    spent = 0
    for atom in atoms:
        if atom.op in _MEMBERSHIP:
            spent += _substring_cost(atom.literal)
            if spent > max_cells:
                msg = f"substring enumeration exceeds max_cells={max_cells}"
                raise IntractableMarkerSet(msg)
            candidates.extend(_membership_candidates(atom))

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
            )
        )
    return _dedupe_candidates(
        candidates, pure_version=raw_kind == DOMAIN_VERSION, max_cells=max_cells
    )


def _reduce_cells(
    points: Iterable[object], atoms: Sequence[Atom], max_cells: int
) -> list[Cell]:
    points = list(points)

    # The truth vector costs one holds() per atom per point; guard that product so
    # an axis carrying many atoms fails loudly.
    if len(points) * len(atoms) > max_cells:
        msg = (
            f"axis work {len(points)}x{len(atoms)} atoms exceeds max_cells={max_cells}"
        )
        raise IntractableMarkerSet(msg)

    representatives: dict[tuple, object] = {}
    for point in points:
        vector = tuple(atom.holds(point) for atom in atoms)
        representatives.setdefault(vector, point)
        # Every one of the 2**len(atoms) truth vectors now has a representative;
        # the remaining points can only repeat one.
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
    variable: str, atoms: Sequence[Atom], max_cells: int
) -> list[Cell]:
    """Cells of a version/string value axis."""
    return _reduce_cells(
        _value_candidates(variable, atoms, max_cells), atoms, max_cells
    )


def partition_set_axis(atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
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
    return _reduce_cells(subsets, atoms, max_cells)


def partition_boolean_axis(atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
    """Cells of an opaque boolean (contains) axis: the two truth values."""
    return _reduce_cells((False, True), atoms, max_cells)


def _partition_axis(axis: tuple, atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
    kind = axis[0]
    if kind == AXIS_VALUE:
        return partition_value_axis(axis[1], atoms, max_cells)
    if kind == AXIS_SET:
        return partition_set_axis(atoms, max_cells)
    return partition_boolean_axis(atoms, max_cells)


# One simplify re-partitions the same axes with the same atoms over and over.
# The cache lives only for that span and is thread-local, so a concurrent
# simplify never shares a store.
_partition_cache = threading.local()

# ``max_cells`` bounds one decision, and the greedy fixpoint issues one per
# clause and per atom per universe row, so the meter is what bounds the run:
# every cell enumeration charges the work it is about to do. Thread-local, and
# unset outside a simplify so every other decision stays unmetered.
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


def partition_axis(axis: tuple, atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
    """Partition one axis's domain into cells on which every atom is constant."""
    store: dict | None = getattr(_partition_cache, "store", None)
    if store is None:
        return _partition_axis(axis, atoms, max_cells)
    key = (axis, tuple(atoms), max_cells)
    cached = store.get(key)
    if cached is None:
        cached = _partition_axis(axis, atoms, max_cells)
        store[key] = cached
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
    """Normalise a set-variable value: a str is one name, PEP 685 canonical."""
    if isinstance(value, str):
        return frozenset({canonicalize_name(value)}) if value else frozenset()
    return frozenset(canonicalize_name(name) for name in value)  # type: ignore[union-attr]


def _require(env: Mapping[str, object], key: str) -> object:
    """Look a referenced variable up, matching packaging's missing-key contract."""
    try:
        return env[key]
    except KeyError:
        raise UndefinedEnvironmentName(key) from None


def evaluate_atom(atom: Atom, env: Mapping[str, object]) -> bool:
    """Evaluate one atom against a full environment (extras are sets).

    A referenced variable absent from ``env`` raises
    :class:`UndefinedEnvironmentName` on every axis, matching packaging and
    keeping the missing-key behaviour uniform across scalars and sets. A
    ``python_version`` atom reads ``python_full_version`` in preference to
    ``python_version``, so an environment supplying both keys is read through
    ``python_full_version``.
    """
    if atom.kind == AXIS_VALUE:
        if atom.derive_mm and "python_full_version" not in env:
            # A1 lowers python_version onto python_full_version; honour an env
            # that supplies only the python_version key (the written variable).
            return atom.holds(_require(env, "python_version"))
        return atom.holds(_require(env, atom.variable))
    if atom.kind == AXIS_SET:
        return atom.holds(as_name_set(_require(env, atom.origin)))
    return atom.holds(atom.literal in _require(env, atom.variable))  # type: ignore[operator]


_MISSING = object()


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
    """Raise the bounded guard for an oversized version literal or env value.

    Runs before either could reach packaging's Version and raise a bare
    ValueError.
    """
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


def unprovided_variables(node: Formula, env: Mapping[str, object]) -> set[str]:
    """Return the referenced variables an environment supplies no value for."""
    return {
        atom.origin
        for atom in collect_atoms(node)
        if _atom_env_value(atom, env) is _MISSING
    }


def _atoms_by_axis(atoms: list[Atom]) -> dict[tuple[str, ...], list[Atom]]:
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


def _satisfying_cells(
    node: Formula, max_cells: int
) -> Iterator[dict[tuple[str, ...], Cell]]:
    atoms = collect_atoms(node)
    grouped = _atoms_by_axis(atoms)
    if not grouped:
        if _eval_cell(node, {}):
            yield {}
        return

    axes = list(grouped)
    atomlists = [grouped[axis] for axis in axes]
    partitions = [
        partition_axis(axis, atoms, max_cells)
        for axis, atoms in zip(axes, atomlists, strict=True)
    ]

    # The enumeration walks the whole op-tree once per cell, so guard the cell
    # product times the leaf-occurrence count: a marker that repeats atoms inflates
    # the walk without inflating the distinct-atom count or the cell product.
    leaf_occurrences = len(atoms)
    charge_work(
        guarded_product_size(
            (*(len(part) for part in partitions), leaf_occurrences), max_cells
        )
    )

    for combo in product(*partitions):
        truth: dict[Atom, bool] = {
            atom: value
            for atoms, cell in zip(atomlists, combo, strict=True)
            for atom, value in zip(atoms, cell.vector, strict=True)
        }
        if _eval_cell(node, truth):
            yield dict(zip(axes, combo, strict=True))


def is_empty(node: Formula, max_cells: int) -> bool:
    """Whether a tree denotes the empty set."""
    return next(_satisfying_cells(node, max_cells), _MISSING) is _MISSING


def witness(node: Formula, max_cells: int) -> dict[str, str | frozenset[str]] | None:
    """Return a concrete environment satisfying a tree, or ``None`` if none is found.

    The returned environment is verified against the tree before it is returned.
    ``None`` is returned for the empty set. The search over ``contains`` atoms
    is incomplete, so ``None`` may also be returned for a non-empty set when the
    concrete-string constraints on one variable (a value atom, one or more
    ``contains`` atoms, or a mix) have no jointly realisable cell representative.
    ``python_version`` and ``python_full_version`` share one axis, so those
    constraints can sit on different variables.
    """
    for cell in _satisfying_cells(node, max_cells):
        env = _materialize(cell)
        if evaluate_tree(node, env):
            return env
    return None


def _materialize(
    cell: Mapping[tuple[str, ...], Cell],
) -> dict[str, str | frozenset[str]]:
    env: dict[str, str | frozenset[str]] = {}
    contains: dict[str, list[tuple[str, bool]]] = {}
    for axis, piece in cell.items():
        kind = axis[0]
        if kind == AXIS_VALUE:
            env[axis[1]] = str(piece.point)
        elif kind == AXIS_SET:
            env[axis[1]] = frozenset(piece.point)  # type: ignore[arg-type]
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
    """Substitute the provided variables, leaving the rest as a residual."""
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
    # Excluded middle holds only for ==/!= on a pure-version axis; ordered
    # comparisons have the prerelease hole, and the twins can hold a non-version,
    # so neither complements to a single atom.
    if op in ("==", "!=") and is_pure_version(var) and not atom.swapped:
        return AtomLeaf(atom.replaced(op="!=" if op == "==" else "=="))
    msg = f"cannot complement version atom on {var!r}"
    raise UnserializableMarkerSet(msg)


def _complement_string(atom: Atom, op: str) -> Formula:
    if op in ("==", ">=", "<="):
        return AtomLeaf(atom.replaced(op="!="))
    if op == "!=":
        return AtomLeaf(atom.replaced(op="=="))
    if op == "in":
        return AtomLeaf(atom.replaced(op="not in"))
    if op == "not in":
        return AtomLeaf(atom.replaced(op="in"))
    # < and > are constant-false on a string variable, so the complement is all.
    return TRUE


def _complement_leaf(atom: Atom) -> Formula:
    if atom.kind in (AXIS_SET, AXIS_CONTAINS):
        return AtomLeaf(atom.replaced(positive=not atom.positive))
    op, var = atom.op, atom.variable
    if is_version_dispatch(var) and _builds_specifier(op, atom.literal):
        return _complement_version(atom, op, var)
    return _complement_string(atom, op)


def to_nnf(node: Formula) -> Formula:
    """Push complements down to the leaves (negation normal form)."""
    if isinstance(node, AtomLeaf):
        return node
    if isinstance(node, AndNode):
        return make_and(to_nnf(child) for child in node.children)
    if isinstance(node, OrNode):
        return make_or(to_nnf(child) for child in node.children)
    if isinstance(node, NotNode):
        return _negate(node.child)
    msg = "a bare constant cannot reach to_nnf"  # pragma: no cover
    raise RuntimeError(msg)  # pragma: no cover


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
    """Spell a literal as a marker string, picking the quote the grammar allows.

    A PEP 508 literal is delimited by one quote style and cannot contain that
    style, so a literal carrying a double-quote is spelled with single quotes.
    """
    if '"' not in literal:
        return f'"{literal}"'
    if "'" not in literal:
        return f"'{literal}'"
    # A literal carrying both quote styles has no marker spelling. Marker
    # literals only ever arrive through the exclusive-quote grammar, so a value
    # holding both is unreachable from any parsed input.
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
    """A short human summary of a set, for :func:`repr`. Total and never raises.

    Renders the constant sets as words and any other set as its marker string,
    falling back to a placeholder for a complement the grammar cannot spell. It
    never exposes the private op-tree and never raises, so a ``MarkerSet`` is
    always safe to print, including inside a traceback.
    """
    if isinstance(node, BoolConst):
        return "universe" if node.value else "empty"
    try:
        return serialize(to_nnf(node))
    except UnserializableMarkerSet:
        return "unrepresentable"


# ------------------------------------------------------------------- simplify


def _atom_key(atom: Atom) -> tuple[str, str, str, bool, bool, str, str, bool]:
    """A total order over atoms, for a deterministic factored serialisation."""
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


def _clause_key(clause: frozenset[Atom]) -> tuple:
    return tuple(_atom_key(atom) for atom in sorted(clause, key=_atom_key))


def _to_clauses(node: Formula, max_cells: int) -> list[frozenset[Atom]]:
    """Distribute an NNF tree into a disjunction of atom-set clauses (DNF).

    An AND of ORs expands multiplicatively, so the running clause count is held
    to ``max_cells`` and a pathological non-DNF input raises
    :class:`IntractableMarkerSet`.
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


def _equivalent_within(
    left: Formula, right: Formula, universe: Formula, max_cells: int
) -> bool:
    """Whether two trees denote the same set on every point of ``universe``.

    The whole-matrix reference oracle: it complements ``universe`` in one shot,
    so it overruns ``max_cells`` on a wide multi-platform universe.
    :func:`equivalent_within_rows` decides the same verdict per row instead.
    """
    return is_empty(
        make_and((left, universe, make_not(right))), max_cells
    ) and is_empty(make_and((right, universe, make_not(left))), max_cells)


class _Row(NamedTuple):
    """One universe row: its entailed pins and residual bound."""

    pins: dict[str, str]
    bound: Formula


def _row_pins(disjunct: Formula) -> dict[str, str]:
    """The exact-string equality pins a universe row entails.

    Only a top-level ``==`` value conjunct pins; a nested or negated atom never
    does.  Only a :data:`DOMAIN_STRING` variable pins: on a version-dispatch
    variable ``==`` is PEP 440 equality, so ``platform_release == "5.10"`` still
    admits ``"5.10.0"``, and substituting the literal would answer that
    variable's string-reading atoms at the wrong point.  Those stay in the
    residual bound.
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
    """Split a universe into rows: the top-level disjuncts of its NNF.

    Each row keeps its entailed pins and the residual bound left after
    restricting the disjunct by them. Purely structural, no algebra.
    """
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
) -> bool:
    """Whether ``left`` agrees with the per-row restriction of the constant right.

    Restricting each operand to a row's pins complements over that row's residual
    rather than the whole-matrix product.
    """
    for row, right in zip(rows, right_by_row, strict=True):
        left_r = restrict_tree(left, row.pins)
        if not is_empty(make_and((left_r, row.bound, make_not(right))), max_cells):
            return False
        if not is_empty(make_and((right, row.bound, make_not(left_r))), max_cells):
            return False
    return True


def universe_is_empty(universe: Formula, max_cells: int) -> bool:
    """Whether a universe admits no environment, decided per row.

    A union is empty iff every top-level disjunct is, so each is tested alone,
    staying on one row's product instead of the whole-matrix complement.
    """
    nnf = universe if isinstance(universe, BoolConst) else to_nnf(universe)
    disjuncts = nnf.children if isinstance(nnf, OrNode) else (nnf,)
    return all(is_empty(disjunct, max_cells) for disjunct in disjuncts)


def equivalent_within_rows(
    left: Formula, right: Formula, universe: Formula, max_cells: int
) -> bool:
    """Whether two trees agree on every point of ``universe``, decided per row.

    The row-restricted dual of :func:`_equivalent_within`, deciding the same
    verdict but staying decidable on wide multi-platform universes. A universe of
    ``TRUE`` reduces it to plain global equivalence.
    """
    rows = _decompose_rows(universe)
    right_by_row = [restrict_tree(right, row.pins) for row in rows]
    return _rows_equivalent(left, rows, right_by_row, max_cells)


def _dedupe(clauses: list[frozenset[Atom]]) -> list[frozenset[Atom]]:
    seen: set[frozenset[Atom]] = set()
    out: list[frozenset[Atom]] = []
    for clause in clauses:
        if clause not in seen:
            seen.add(clause)
            out.append(clause)
    return out


def _drop_clauses(
    clauses: list[frozenset[Atom]],
    rows: Sequence[_Row],
    original_by_row: Sequence[Formula],
    max_cells: int,
) -> list[frozenset[Atom]]:
    kept = list(clauses)
    for clause in sorted(clauses, key=_clause_key):
        trial = [other for other in kept if other != clause]
        if _rows_equivalent(_disjunction(trial), rows, original_by_row, max_cells):
            kept = trial
    return kept


def _drop_atoms(
    clauses: list[frozenset[Atom]],
    rows: Sequence[_Row],
    original_by_row: Sequence[Formula],
    max_cells: int,
) -> list[frozenset[Atom]]:
    working = [set(clause) for clause in clauses]
    for clause in working:
        for atom in sorted(clause, key=_atom_key):
            clause.discard(atom)
            trial = _disjunction(frozenset(current) for current in working)
            if not _rows_equivalent(trial, rows, original_by_row, max_cells):
                clause.add(atom)
    return [frozenset(clause) for clause in working]


def _canonical(clauses: list[frozenset[Atom]]) -> tuple:
    return tuple(sorted(_clause_key(clause) for clause in clauses))


def simplify_within(
    node: Formula, universe: Formula, max_cells: int, max_work: int
) -> Formula:
    """Return the smallest tree equivalent to ``node`` on every point of ``universe``.

    Greedy: drop each clause, then each atom, whose removal preserves
    within-universe equivalence, to a fixpoint, then factor the atoms common to
    every surviving clause into a leading conjunction, verifying each removal
    per universe row so a wide multi-platform universe stays decidable. A
    universe of ``TRUE`` reduces it to a context-free factoring, and a total
    atom order fixes the output.

    ``max_cells`` bounds one decision and ``max_work`` bounds the whole run,
    because a wide matrix runs many cheap decisions where a large membership
    powerset runs few expensive ones. Either overrun raises
    :class:`IntractableMarkerSet`.
    """
    nnf = node if isinstance(node, BoolConst) else to_nnf(node)
    clauses = _dedupe(_to_clauses(nnf, max_cells))
    original = _disjunction(clauses)
    rows = _decompose_rows(universe)
    original_by_row = [restrict_tree(original, row.pins) for row in rows]
    previous_store = getattr(_partition_cache, "store", None)
    previous_work = getattr(_work_meter, "remaining", None)
    _partition_cache.store = {}
    _work_meter.remaining = max_work
    try:
        while True:
            before = _canonical(clauses)
            clauses = _drop_clauses(clauses, rows, original_by_row, max_cells)
            clauses = _dedupe(_drop_atoms(clauses, rows, original_by_row, max_cells))
            if _canonical(clauses) == before:
                break
    finally:
        _partition_cache.store = previous_store
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
