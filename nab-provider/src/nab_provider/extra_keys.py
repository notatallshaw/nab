"""Key shapes for extras: the ``name[extra]`` proxy the resolver decides under.

An extra resolves as its own package, so the provider and the lock writer both
have to spell its key the same way.
"""

from __future__ import annotations

import re

from nab_provider._vendor.packaging.utils import canonicalize_name

__all__ = [
    "join_extra",
    "split_extra",
]


_EXTRA_RE = re.compile(r"^(?P<base>[^\[]+)\[(?P<extra>[^\]]+)\]$")


def split_extra(package: str) -> tuple[str, str | None]:
    """Split 'name[extra]' into ('name', 'extra'), or ('name', None).

    The extra name is normalized per PEP 685.
    """
    m = _EXTRA_RE.match(package)
    if m is None:
        return (package, None)
    return (m.group("base"), canonicalize_name(m.group("extra")))


def join_extra(base: str, extra: str) -> str:
    """Join a base name and extra into 'name[extra]'.

    The extra name is normalized per PEP 685.
    """
    return f"{base}[{canonicalize_name(extra)}]"
