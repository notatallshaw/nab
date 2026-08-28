"""Read a committed pylock back for ``nab lock``.

Kept out of the builder, which every command imports, so the vendored
pylock model is loaded only when a lock is read.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import tomli

from nab_provider._vendor.packaging.pylock import Pylock, PylockValidationError
from nab_provider.iso8601 import parse_iso_datetime

from .. import toml_io
from .._toml import tool_nab_section
from ..paths import path_state

if TYPE_CHECKING:
    from pathlib import Path

    from nab_provider._vendor.packaging.version import Version


__all__ = [
    "read_lockfile_anchor",
    "read_lockfile_packages",
]


def read_lockfile_anchor(path: Path) -> datetime | None:
    """Return the ``[tool.nab].created-at`` timestamp from ``path`` if any.

    Used by ``nab lock`` to keep ``P<n>D`` durations stable across
    re-locks: the anchor used for the previous resolve is read back
    and reused unless the user passes ``--upgrade``.

    Returns ``None`` when ``path`` does not exist, cannot be read, is
    not valid TOML, is not a PEP 751-shaped pylock, or is missing the
    ``[tool.nab]`` block.  Naive timestamps (no offset) are coerced to UTC
    for symmetry with the writer; this is informational provenance, so
    a missing offset is recoverable rather than fatal.
    """
    if not path_state(path).should_read:
        return None

    try:
        with path.open("rb") as f:
            data = toml_io.load(f)
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError):
        return None

    nab = tool_nab_section(data)
    raw = nab.get("created-at") if isinstance(nab, dict) else None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = parse_iso_datetime(raw)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def read_lockfile_packages(path: Path) -> dict[str, Version] | None:
    """Return the ``name -> version`` map from a prior pylock at ``path``.

    Used by ``nab lock`` to diff a re-lock against the previous result.
    Packages without a recorded version (direct-reference entries that
    omit it) are skipped.

    Returns ``None`` when ``path`` does not exist, cannot be read, is
    not valid TOML, or is not a spec-compliant PEP 751 lockfile; the
    caller falls back to a no-diff summary line.
    """
    if not path_state(path).should_read:
        return None

    try:
        with path.open("rb") as f:
            data = toml_io.load(f)
        pylock = Pylock.from_dict(data)
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError, PylockValidationError):
        return None

    return {
        str(pkg.name): pkg.version for pkg in pylock.packages if pkg.version is not None
    }
