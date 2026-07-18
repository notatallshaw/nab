"""On-demand cell decomposition for the marker algebra.

A set is kept as a boolean op-tree over the typed atoms of :mod:`.atoms`,
including a :class:`~.atoms.NotNode` so complement is structural (a terminal
swap per cell, never an operator flip on an atom). No canonical form is stored:
every decision procedure re-decomposes the referenced variables' domains into
cells on which each atom is constant, enumerates the cell product under the
``max_cells`` guard, and evaluates the op-tree once per cell.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import product
from typing import TYPE_CHECKING

from .._vendor.packaging.specifiers import InvalidSpecifier, Specifier
from .atoms import (
    AXIS_CONTAINS,
    AXIS_SET,
    AXIS_VALUE,
    FALSE,
    TRUE,
    AndNode,
    Atom,
    AtomLeaf,
    BoolConst,
    Cell,
    Formula,
    NotNode,
    OrNode,
    as_name_set,
    evaluate_atom,
    guarded_product_size,
    is_pure_version,
    is_version_dispatch,
    make_and,
    make_not,
    make_or,
    partition_axis,
)
from .errors import UnserializableSetError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

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


def variables_of(node: Formula) -> frozenset[str]:
    """Return the variables a tree references, as written."""
    return frozenset(atom.origin for atom in collect_atoms(node))


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
    guarded_product_size(
        (*(len(part) for part in partitions), leaf_occurrences), max_cells
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
    The search over ``contains`` atoms is incomplete: ``None`` is returned for the
    empty set, and may also be returned for a non-empty set when a value
    constraint and a substring constraint on one variable have no jointly
    realisable cell representative.
    """
    for cell in _satisfying_cells(node, max_cells):
        env = _materialise(cell)
        if evaluate_tree(node, env):
            return env
    return None


def _materialise(
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
        env[variable] = "".join(sorted(lit for lit, present in items if present))
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
        return AtomLeaf(replace(atom, op="!=" if op == "==" else "=="))
    msg = f"cannot complement version atom on {var!r}"
    raise UnserializableSetError(msg)


def _complement_string(atom: Atom, op: str) -> Formula:
    if op in ("==", ">=", "<="):
        return AtomLeaf(replace(atom, op="!="))
    if op == "!=":
        return AtomLeaf(replace(atom, op="=="))
    if op == "in":
        return AtomLeaf(replace(atom, op="not in"))
    if op == "not in":
        return AtomLeaf(replace(atom, op="in"))
    # < and > are constant-false on a string variable, so the complement is all.
    return TRUE


def _complement_leaf(atom: Atom) -> Formula:
    if atom.kind in (AXIS_SET, AXIS_CONTAINS):
        return AtomLeaf(replace(atom, positive=not atom.positive))
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
    raise UnserializableSetError(msg)  # pragma: no cover


def _render_atom(atom: Atom) -> str:
    if atom.kind == AXIS_SET:
        if atom.origin == "extra":
            op = "==" if atom.positive else "!="
            return f"extra {op} {_quote(atom.literal)}"
        op = "in" if atom.positive else "not in"
        return f"{_quote(atom.literal)} {op} {atom.origin}"
    if atom.kind == AXIS_CONTAINS:
        op = "in" if atom.positive else "not in"
        return f"{_quote(atom.literal)} {op} {atom.variable}"
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
