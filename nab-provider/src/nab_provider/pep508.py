"""Parse PEP 508 requirements and inspect formatted marker comparisons.

Translate stack exhaustion when a caller has less stack available than
packaging's marker parser needs.
"""

from __future__ import annotations

__lazy_modules__ = ("packaging.requirements",)

import re

from packaging.requirements import InvalidRequirement, Requirement

__all__ = ["MARKER_COMPARISON", "NESTED_MARKER_MESSAGE", "parse_requirement"]

NESTED_MARKER_MESSAGE = "marker is nested too deeply to parse"

# Packaging quotes literals when formatting markers. Keep the quotes so callers
# can distinguish a variable from a literal that has the same name.
_MARKER_OPERAND = r'"[^"]*"|\'[^\']*\'|[A-Za-z_][A-Za-z0-9_]*'
MARKER_COMPARISON = re.compile(
    rf"(?P<lhs>{_MARKER_OPERAND})\s*"
    r"(?P<op>===|==|!=|<=|>=|~=|<|>|not\s+in|in)\s*"
    rf"(?P<rhs>{_MARKER_OPERAND})"
)


def parse_requirement(text: str) -> Requirement:
    """Return ``text`` parsed as a PEP 508 requirement.

    Raises :class:`InvalidRequirement` for a malformed string and for a marker
    whose nesting exhausts the stack.
    """
    try:
        return Requirement(text)
    except RecursionError as exc:
        raise InvalidRequirement(NESTED_MARKER_MESSAGE) from exc
