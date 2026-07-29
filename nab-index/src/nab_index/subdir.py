"""Containment check for a source-tree subdirectory.

The project root inside a materialised source (an extracted archive, a
VCS clone, or a local directory) is selected with ``root / subdirectory``.
An absolute path, a ``..`` component, or a Windows drive letter would make
that join land outside the tree.
"""

from __future__ import annotations

import ntpath
import posixpath

__all__ = ["subdirectory_escapes"]


def subdirectory_escapes(subdirectory: str) -> bool:
    r"""Return True if ``subdirectory`` would resolve outside the source tree.

    The materialisation join is native, so the value must stay contained
    under both separator conventions. ntpath catches Windows escapes such
    as drive letters and backslash separators. posixpath catches an escape
    a literal backslash hides from ntpath: ``c\d`` is one segment on POSIX
    but two under ntpath, which would then absorb a trailing ``..``.
    """
    if not subdirectory:
        return False

    for normpath, join, commonpath in (
        (ntpath.normpath, ntpath.join, ntpath.commonpath),
        (posixpath.normpath, posixpath.join, posixpath.commonpath),
    ):
        root = normpath("/source-root")
        resolved = normpath(join(root, subdirectory))
        try:
            escapes = commonpath((root, resolved)) != root
        except ValueError:
            # Different drives (e.g. a ``C:\\`` subdirectory) have no common path.
            return True
        if escapes:
            return True
    return False
