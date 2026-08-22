"""Parse PEP 508 requirement strings.

The vendored parser recurses through each parenthesised marker group, so a
deeply nested marker raises ``RecursionError`` instead of
``InvalidRequirement``.
"""

from __future__ import annotations

from nab_provider._vendor.packaging.requirements import InvalidRequirement, Requirement

__all__ = ["NESTED_MARKER_MESSAGE", "parse_requirement"]

NESTED_MARKER_MESSAGE = "marker is nested too deeply to parse"


def parse_requirement(text: str) -> Requirement:
    """Return ``text`` parsed as a PEP 508 requirement.

    Raises :class:`InvalidRequirement` for a malformed string and for a marker
    whose nesting exhausts the stack.
    """
    try:
        return Requirement(text)
    except RecursionError as exc:
        raise InvalidRequirement(NESTED_MARKER_MESSAGE) from exc
