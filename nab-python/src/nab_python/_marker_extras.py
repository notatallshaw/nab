"""Set-correct evaluation of ``extra`` markers against a full extras set.

The dependency-specifiers spec resolves ``extra`` comparisons against the whole
set of extras requested for a package: ``==`` is membership, ``!=`` is
non-membership, and every other operator is undefined (False).  Evaluating one
extra at a time gets ``extra != "x"`` and the undefined operators wrong -- the
vendored packaging evaluator even treats ``extra <= "x"`` / ``extra >= "x"`` as
``==`` (see ``markers._operators``), so ``extra <= "gpu"`` would wrongly gate a
dependency on the ``gpu`` extra.

packaging's :meth:`Marker.evaluate` only compares ``extra`` against a single
string, and this module uses **public packaging API only** (nab plans to stop
vendoring packaging), so it never touches ``Marker._markers`` or the parser.
Instead it rewrites each ``extra <op> "x"`` comparison in the canonical
``str(marker)`` into a membership test on the public ``extras`` set variable
(``"x" in extras`` / ``"x" not in extras``) and evaluates the rewritten marker
with ``extras`` bound to the requested set.  An empty set is the base (no-extra)
install.

``str(marker)`` is canonical (validated): values are double-quoted, the operand
order is preserved, and ``extra`` literals are already :pep:`685` canonicalized.

Reference: https://packaging.python.org/en/latest/specifications/dependency-specifiers/
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ._vendor.packaging.markers import Marker
from ._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

# A bare ``extra <op> "value"`` comparison, in either operand order, plus a
# passthrough alternative for a whole quoted value.  Ordering matters: the
# ``extra`` comparisons are tried first, then any remaining ``"..."`` value is
# consumed atomically so ``extra``/operator text that appears *inside* a value
# (``os_name == "junk extra =="``) is never mistaken for a comparison.  packaging
# serializes every value with unescaped double quotes, so a valid ``str(marker)``
# has all values in the ``"[^"]*"`` shape (a value with an embedded double quote
# already fails to reparse in packaging itself).  ``\bextra\b`` never matches
# inside the plural ``extras`` variable, and ``in`` / ``not in`` are excluded on
# purpose: ``"x" in extras`` is a set-variable test, not an ``extra`` comparison.
_OPS = r"===|==|!=|<=|>=|~=|<|>"
_EXTRA_COMPARISON = re.compile(
    rf'\bextra\b\s*(?P<op1>{_OPS})\s*"(?P<val1>[^"]*)"'
    rf'|"(?P<val2>[^"]*)"\s*(?P<op2>{_OPS})\s*\bextra\b'
    r'|"[^"]*"'
)

# ``extra`` operators other than ``==`` / ``!=`` are undefined by the spec; tools
# evaluate them False.  A parenthesised self-contradiction is False for every set.
_ALWAYS_FALSE = '("" in extras and "" not in extras)'


def _comparison_op(match: re.Match[str]) -> str | None:
    """Return the operator when ``match`` is an ``extra`` comparison, else None."""
    return match.group("op1") or match.group("op2")


def _rewrite_leaf(match: re.Match[str]) -> str:
    op = _comparison_op(match)
    if op is None:
        # A bare quoted value: pass it through untouched.
        return match.group(0)
    value = match.group("val1")
    if value is None:
        value = match.group("val2")
    name = canonicalize_name(value)
    if op == "==":
        return f'"{name}" in extras'
    if op == "!=":
        return f'"{name}" not in extras'
    return _ALWAYS_FALSE


def references_extra(marker: Marker) -> bool:
    """Return whether ``marker`` compares against the ``extra`` variable."""
    return any(_comparison_op(m) for m in _EXTRA_COMPARISON.finditer(str(marker)))


def rewrite_extra_markers(marker: Marker) -> Marker:
    """Rewrite ``extra`` comparisons into ``extras`` set-membership tests.

    The result evaluates set-correctly when ``extras`` is bound to the full
    requested set: ``extra == "x"`` becomes ``"x" in extras``, ``extra != "x"``
    becomes ``"x" not in extras``, and any other operator becomes a
    constant-False term.  Non-``extra`` leaves are left untouched.
    """
    return Marker(_EXTRA_COMPARISON.sub(_rewrite_leaf, str(marker)))


def evaluate_marker_with_extras(
    marker: Marker,
    extras: AbstractSet[str],
    environment: dict[str, str | AbstractSet[str]],
) -> bool:
    """Evaluate ``marker`` with ``extra`` resolved as membership in ``extras``.

    Convenience wrapper that rewrites on every call; hot paths cache the
    rewritten marker and reuse a mutated environment instead.  ``environment``
    supplies the values for the non-``extra`` marker leaves.
    """
    env = {**environment, "extras": frozenset(canonicalize_name(e) for e in extras)}
    return bool(rewrite_extra_markers(marker).evaluate(env))
