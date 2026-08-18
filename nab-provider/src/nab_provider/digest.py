"""Shape check for a published artefact digest.

:pep:`503` and :pep:`691` define an artefact's hash as the hex-encoded digest
of its bytes, so a value that is not hex is malformed input, not a digest.
"""

from __future__ import annotations

import re

__all__ = ["is_hex_digest"]

_HEX_DIGEST = re.compile(r"[0-9a-fA-F]+")


def is_hex_digest(value: str) -> bool:
    """Return True if ``value`` is a non-empty run of hexadecimal digits.

    ``bytes.fromhex`` is not a substitute: it ignores ASCII whitespace inside
    the string it decodes.
    """
    return _HEX_DIGEST.fullmatch(value) is not None
