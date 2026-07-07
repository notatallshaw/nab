"""Containment check for a source-tree subdirectory.

The project root inside a materialised source (an extracted archive, a
VCS clone, or a local directory) is selected with ``root / subdirectory``.
An absolute path, a ``..`` component, or a Windows drive letter would make
that join land outside the tree.
"""

from __future__ import annotations

import ntpath

__all__ = ["subdirectory_escapes"]

# ntpath treats both slash kinds as separators, so a value that stays
# inside on POSIX but escapes on Windows (the real join is native) is
# caught on every platform.
_SUBDIR_ROOT = ntpath.normpath("/source-root")


def subdirectory_escapes(subdirectory: str) -> bool:
    """Return True if ``subdirectory`` would resolve outside the source tree."""
    if not subdirectory:
        return False

    resolved = ntpath.normpath(ntpath.join(_SUBDIR_ROOT, subdirectory))
    try:
        return ntpath.commonpath((_SUBDIR_ROOT, resolved)) != _SUBDIR_ROOT
    except ValueError:
        # Different drives (e.g. a ``C:\\`` subdirectory) have no common path.
        return True
