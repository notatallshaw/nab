"""Errors shared between the build runner and its callers.

Lives in its own module so :mod:`nab_project.build_backend` can
import the exception without pulling in :mod:`build`,
:mod:`pyproject_hooks`, and the rest of the runner module at module
load time.  The runner re-exports the class for back-compat.
"""

from __future__ import annotations

__all__ = [
    "BuildBackendError",
]


class BuildBackendError(Exception):
    """A build-backend operation failed or returned unparseable metadata."""
