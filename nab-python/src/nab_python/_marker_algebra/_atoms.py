"""Typed atoms and domain-partition primitives for the marker algebra.

Parses a marker string (or :class:`Marker`) into a normalised boolean op-tree
over typed atoms, with the packaging-faithful ``(variable, operator, literal)``
dispatch, A1 lowering of ``python_version`` onto the ``python_full_version``
axis, set-valued extras, and opaque ``contains`` atoms. The denotation of a
value atom is delegated to packaging's own ``_eval_op`` so it matches packaging
exactly. Every representation consumes this tree and reuses the per-axis cell
partition and per-atom evaluation defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from .._vendor.packaging._parser import Op, Variable, parse_marker
from .._vendor.packaging.markers import (
    Marker,
    UndefinedComparison,
    _eval_op,
)
from .._vendor.packaging.utils import canonicalize_name
from .._vendor.packaging.version import InvalidVersion, Version
from ._errors import ComplexityLimitExceeded

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

# The single registry the layer types variables through. Fourteen variables,
# matching packaging's marker grammar. The twins (``platform_release`` and
# ``platform_version``) are version-typed with a string fall-through in
# packaging: ``platform_release`` dispatches as a version, ``platform_version``
# as a string, but both may hold arbitrary strings.
DOMAIN_REGISTRY: dict[str, str] = {
    "implementation_name": DOMAIN_STRING,
    "implementation_version": DOMAIN_VERSION,
    "os_name": DOMAIN_STRING,
    "platform_machine": DOMAIN_STRING,
    "platform_python_implementation": DOMAIN_STRING,
    "platform_release": DOMAIN_TWIN,
    "platform_system": DOMAIN_STRING,
    "platform_version": DOMAIN_TWIN,
    "python_full_version": DOMAIN_VERSION,
    "python_version": DOMAIN_VERSION,
    "sys_platform": DOMAIN_STRING,
    "extra": DOMAIN_SET,
    "extras": DOMAIN_SET,
    "dependency_groups": DOMAIN_SET,
}

_MEMBERSHIP = frozenset({"in", "not in"})
_SENTINEL = "zzz-no-literal-equals-this"
_ORDERED_UNDEFINED = frozenset({"~=", "==="})


def _domain(variable: str) -> str:
    """Return the effective domain of a variable under packaging typing."""
    kind = DOMAIN_REGISTRY[variable]
    if kind == DOMAIN_TWIN:
        return DOMAIN_VERSION if variable == "platform_release" else DOMAIN_STRING
    return kind


def _version_dispatch(variable: str) -> bool:
    return _domain(variable) == DOMAIN_VERSION


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


def _derive_major_minor(full: str) -> str:
    """A1: ``python_version`` is the major.minor truncation of the full version."""
    try:
        release = Version(full).release
    except InvalidVersion:
        return full
    major = release[0]
    minor = release[1] if len(release) > 1 else 0
    return f"{major}.{minor}"


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
        mm = _derive_major_minor(text)
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
    return _convert(parse_marker(source))


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
        # Covers variable-vs-variable too: packaging keys off the left variable
        # and treats the right variable's name as the literal (faithful).
        return _make_atom(lhs.value, op, rhs.value, swapped=False)
    if isinstance(rhs, Variable):
        return _make_atom(rhs.value, op, lhs.value, swapped=True)
    # Const-vs-const: packaging raises here; evaluating by the operator is the
    # useful reading. The string table applies, so ~= and === raise.
    return BoolConst(value=_apply(lhs.value, op, rhs.value, key=""))


def _make_atom(variable: str, op: str, literal: str, *, swapped: bool) -> Formula:
    if _domain(variable) == DOMAIN_SET:
        return _make_set_atom(variable, op, literal, swapped=swapped)
    if variable == "python_version":
        return _make_python_version_atom(op, literal, swapped=swapped)
    if op in _MEMBERSHIP:
        return _make_membership_atom(variable, op, literal, swapped=swapped)
    _reject_undefined_operator(variable, op, literal, swapped=swapped)
    return AtomLeaf(Atom(AXIS_VALUE, variable, variable, op, literal, swapped=swapped))


def _make_python_version_atom(op: str, literal: str, *, swapped: bool) -> Formula:
    if op in _MEMBERSHIP and swapped:
        # "literal" in python_version is the opaque contains direction (Tier 2).
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
        # "literal" in variable: the opaque contains direction (Tier 2).
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


def _reject_undefined_operator(
    variable: str, op: str, literal: str, *, swapped: bool
) -> None:
    if op not in _ORDERED_UNDEFINED:
        return
    probe = "1.0"
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


def _substrings(text: str, max_cells: int) -> list[str]:
    out = {""}
    for i in range(len(text)):
        for j in range(i + 1, len(text) + 1):
            out.add(text[i:j])
            if len(out) > max_cells:
                msg = f"substring enumeration exceeds max_cells={max_cells}"
                raise ComplexityLimitExceeded(msg)
    return sorted(out)


def _version_neighbours(text: str) -> list[str]:
    base = text.removesuffix(".*")
    try:
        release = Version(base).release
    except InvalidVersion:
        return []
    out = [base]
    major = release[0]
    bumps = [".".join(str(x) for x in (*release[:-1], release[-1] + 1))]
    if len(release) > 1:
        bumps.append(f"{major}.{release[1] + 1}")
    bumps.append(f"{major + 1}")
    for bump in bumps:
        out.append(bump)
        out.append(f"{bump}.dev0")
    for suffix in (".dev0", "a0", ".post0", ".1", "+l"):
        candidate = f"{base}{suffix}"
        if _strict_version(candidate):
            out.append(candidate)
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


def _version_pool(literals: Sequence[str]) -> list[str]:
    pool = ["0", "0.dev0", "99999"]
    for literal in literals:
        pool.extend(_version_neighbours(literal))
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
    return [text for _, text in parsed] + extra


def _membership_candidates(atom: Atom, max_cells: int) -> list[str]:
    subs = _substrings(atom.literal, max_cells)
    if atom.derive_mm:
        # A1 membership tests the major.minor of a full version, so realisable
        # points are the substrings of the literal that are themselves versions.
        return [s for s in subs if _parses_version(s)]
    return subs


def _wants_versions(variable: str, literals: Sequence[str]) -> bool:
    if _version_dispatch(variable):
        return True
    return any(_parses_version(literal) for literal in literals)


def _dedupe_candidates(
    candidates: Iterable[str], *, pure_version: bool, max_cells: int
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        # A pure Version axis holds only PEP 440 versions, so a non-version
        # candidate is unrealisable there and would otherwise mint a phantom
        # cell. The twins keep their non-version candidates via the sentinel.
        if pure_version and not _strict_version(candidate):
            continue
        seen.add(candidate)
        ordered.append(candidate)
        if len(ordered) > max_cells:
            msg = f"value candidate set exceeds max_cells={max_cells}"
            raise ComplexityLimitExceeded(msg)
    return ordered


def _value_candidates(
    variable: str, atoms: Sequence[Atom], max_cells: int
) -> list[str]:
    literals = [atom.literal for atom in atoms]
    candidates: list[str] = []
    raw_kind = DOMAIN_REGISTRY[variable]
    # The OTHER cell (a string equal to no literal and not a version) exists only
    # where the domain admits arbitrary strings: String fields and the twins.
    if raw_kind in (DOMAIN_STRING, DOMAIN_TWIN):
        candidates.append(_SENTINEL)
    candidates.extend(literals)
    for atom in atoms:
        if atom.op in _MEMBERSHIP:
            candidates.extend(_membership_candidates(atom, max_cells))
    if _wants_versions(variable, literals):
        candidates.extend(_version_pool(literals))
    return _dedupe_candidates(
        candidates, pure_version=raw_kind == DOMAIN_VERSION, max_cells=max_cells
    )


def _reduce_cells(points: Iterable[object], atoms: Sequence[Atom]) -> list[Cell]:
    representatives: dict[tuple, object] = {}
    for point in points:
        vector = tuple(atom.holds(point) for atom in atoms)
        representatives.setdefault(vector, point)
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
    return _reduce_cells(_value_candidates(variable, atoms, max_cells), atoms)


def partition_set_axis(atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
    """Cells of a set axis: the powerset over the mentioned names (guarded)."""
    names = _mentioned_names(atoms)
    count = len(names)
    if (1 << count) > max_cells:
        msg = f"set powerset over {count} names exceeds max_cells={max_cells}"
        raise ComplexityLimitExceeded(msg)
    subsets = [
        frozenset(names[i] for i in range(count) if mask & (1 << i))
        for mask in range(1 << count)
    ]
    return _reduce_cells(subsets, atoms)


def partition_boolean_axis(atoms: Sequence[Atom]) -> list[Cell]:
    """Cells of an opaque boolean (contains) axis: the two truth values."""
    return _reduce_cells((False, True), atoms)


def partition_axis(axis: tuple, atoms: Sequence[Atom], max_cells: int) -> list[Cell]:
    """Partition one axis's domain into cells on which every atom is constant."""
    kind = axis[0]
    if kind == AXIS_VALUE:
        return partition_value_axis(axis[1], atoms, max_cells)
    if kind == AXIS_SET:
        return partition_set_axis(atoms, max_cells)
    return partition_boolean_axis(atoms)


def guarded_product_size(sizes: Iterable[int], max_cells: int) -> int:
    """Multiply per-axis cell counts, raising past the guard."""
    total = 1
    for size in sizes:
        total *= size
        if total > max_cells:
            msg = f"cell product exceeds max_cells={max_cells}"
            raise ComplexityLimitExceeded(msg)
    return total


# ------------------------------------------------------------------- evaluation


def evaluate_atom(atom: Atom, env: Mapping[str, object]) -> bool:
    """Evaluate one atom against a full environment (extras are sets)."""
    if atom.kind == AXIS_VALUE:
        return atom.holds(env[atom.variable])
    if atom.kind == AXIS_SET:
        raw = env.get(atom.origin, frozenset())
        selected = frozenset(canonicalize_name(name) for name in raw)  # type: ignore[union-attr]
        return atom.holds(selected)
    return atom.holds(atom.literal in env[atom.variable])  # type: ignore[operator]
