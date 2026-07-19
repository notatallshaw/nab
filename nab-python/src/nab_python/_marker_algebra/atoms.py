"""Typed atoms and domain-partition primitives for the marker algebra.

Parses a marker string (or :class:`Marker`) into a normalised boolean op-tree
over typed atoms, with the packaging-faithful ``(variable, operator, literal)``
dispatch, A1 lowering of ``python_version`` onto the ``python_full_version``
axis, set-valued extras, and opaque ``contains`` atoms. The denotation of a
value atom is delegated to packaging's own ``_eval_op`` so it matches packaging
exactly. The decision engine in :mod:`.engine` consumes this tree and reuses the
per-axis cell partition and per-atom evaluation defined here.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from .._vendor.packaging._parser import Op, Variable, parse_marker
from .._vendor.packaging._tokenizer import ParserSyntaxError
from .._vendor.packaging.markers import (
    InvalidMarker,
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
    _eval_op,
)
from .._vendor.packaging.utils import canonicalize_name
from .._vendor.packaging.version import InvalidVersion, Version
from .errors import ComplexityLimitError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

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


@dataclass(frozen=True)
class Atom:
    """A normalised leaf whose ``holds`` gives its denotation on one point."""

    kind: str
    variable: str  # axis variable (python_version lowers to python_full_version)
    origin: str  # the variable as written, for variables()/serialisation
    op: str
    literal: str
    swapped: bool = False
    positive: bool = True
    derive_mm: bool = False  # A1: evaluate on the major.minor of the point

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
            return _holds_value(self, point)
        if self.kind == AXIS_SET:
            member = self.literal in point  # type: ignore[operator]
            return member if self.positive else not member
        return bool(point) if self.positive else not bool(point)


def _holds_value(atom: Atom, value: object) -> bool:
    op, literal = atom.op, atom.literal
    text = str(value)
    if atom.derive_mm:
        mm = derive_major_minor(text)
        if atom.swapped:
            return _apply(literal, op, mm, key="python_version")
        return _apply(mm, op, literal, key="python_version")
    if atom.swapped:
        return _apply(literal, op, text, key=atom.variable)
    return _apply(text, op, literal, key=atom.variable)


# --------------------------------------------------------------------- the op-tree


@dataclass(frozen=True)
class BoolConst:
    """A first-class TRUE/FALSE, produced eagerly wherever a combination collapses."""

    value: bool


@dataclass(frozen=True)
class AtomLeaf:
    """A single atom."""

    atom: Atom


@dataclass(frozen=True)
class AndNode:
    """A conjunction of two or more non-constant formulas."""

    children: tuple[Formula, ...]


@dataclass(frozen=True)
class OrNode:
    """A disjunction of two or more non-constant formulas."""

    children: tuple[Formula, ...]


@dataclass(frozen=True)
class NotNode:
    """A structural complement, negated per cell at decision time."""

    child: Formula


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


def parse(source: str | Marker) -> Formula:
    """Parse a marker string or :class:`Marker` into the normalised op-tree."""
    if isinstance(source, Marker):
        source = str(source)
    elif not isinstance(source, str):
        msg = f"expected str or Marker, got {type(source).__name__}"
        raise TypeError(msg)
    if not source.strip():
        return TRUE
    try:
        parsed = parse_marker(source)
    except ParserSyntaxError as exc:
        # A malformed marker raises the public InvalidMarker, as packaging does,
        # not the tokenizer's internal syntax error.
        raise InvalidMarker(str(exc)) from exc
    return _convert(parsed)


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
    return AtomLeaf(Atom(AXIS_VALUE, variable, variable, op, literal, swapped=swapped))


def _make_python_version_atom(op: str, literal: str, *, swapped: bool) -> Formula:
    if op in _MEMBERSHIP and swapped:
        # "literal" in python_version is the opaque contains direction.
        return AtomLeaf(
            Atom(
                AXIS_CONTAINS,
                "python_version",
                "python_version",
                op,
                literal,
                positive=op == "in",
            )
        )
    # python_version A1-lowers to major.minor, so the swapped decision RHS is
    # always two-segment; a two-segment probe matches its validity.
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
    ComplexityLimitError here.
    """
    if is_version_dispatch(variable) and any(
        _oversized_numeric(literal) for literal in literals
    ):
        msg = (
            "version literal numeric component exceeds the "
            f"{sys.get_int_max_str_digits()}-digit parse limit"
        )
        raise ComplexityLimitError(msg)


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


@dataclass(frozen=True)
class Cell:
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


def _between(low: str, high: str) -> str | None:
    vlow, vhigh = Version(low), Version(high)
    for candidate in (
        f"{low}.post0",
        f"{low}+m",
        f"{low}.dev1",
        f"{low}.1",
        f"{low}a1",
    ):
        if not _strict_version(candidate):
            continue
        if vlow < Version(candidate) < vhigh:
            return candidate
    return None


def _version_pool(
    literals: Sequence[str], *, elevate_epoch: bool, max_cells: int
) -> list[str]:
    pool = ["0", "0.dev0", "99999"]
    for literal in literals:
        pool.extend(_version_neighbors(literal))

    parsed: list[tuple[Version, str]] = []
    seen: set[str] = set()
    for text in pool:
        if text in seen:
            continue
        seen.add(text)
        parsed.append((Version(text), text))
    parsed.sort()

    extra: list[str] = []
    for (vlow, slow), (vhigh, shigh) in pairwise(parsed):
        if vlow == vhigh:
            continue
        mid = _between(slow, shigh)
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
                raise ComplexityLimitError(msg)
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
            raise ComplexityLimitError(msg)
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
        raise ComplexityLimitError(msg)

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
                raise ComplexityLimitError(msg)
            candidates.extend(_membership_candidates(atom))

    if is_version_dispatch(variable):
        candidates.extend(
            _version_pool(
                literals,
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
        raise ComplexityLimitError(msg)

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
        raise ComplexityLimitError(msg)
    subsets = [
        frozenset(names[i] for i in range(count) if mask & (1 << i))
        for mask in range(1 << count)
    ]
    return _reduce_cells(subsets, atoms, max_cells)


def partition_boolean_axis(atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
    """Cells of an opaque boolean (contains) axis: the two truth values."""
    return _reduce_cells((False, True), atoms, max_cells)


def partition_axis(axis: tuple, atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
    """Partition one axis's domain into cells on which every atom is constant."""
    kind = axis[0]
    if kind == AXIS_VALUE:
        return partition_value_axis(axis[1], atoms, max_cells)
    if kind == AXIS_SET:
        return partition_set_axis(atoms, max_cells)
    return partition_boolean_axis(atoms, max_cells)


def guarded_product_size(sizes: Iterable[int], max_cells: int) -> int:
    """Multiply per-axis cell counts, raising past the guard."""
    total = 1
    for size in sizes:
        total *= size
        if total > max_cells:
            msg = f"cell product exceeds max_cells={max_cells}"
            raise ComplexityLimitError(msg)
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
