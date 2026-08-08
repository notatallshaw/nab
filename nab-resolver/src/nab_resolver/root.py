"""Root package sentinel for the virtual root.

Lives in its own module so the resolver phase modules can share
the sentinel without importing from ``resolver.py`` at module load
time (which would create a cycle).
"""

from __future__ import annotations

from ._compat import override

__all__ = [
    "ROOT",
]


class _RootPackage:
    """Singleton sentinel for the virtual root package.

    Uses object identity so it can never collide with user package names.
    """

    @override
    def __repr__(self) -> str:
        return "<root>"

    @override
    def __hash__(self) -> int:
        return hash("__nab_root__")


ROOT = _RootPackage()
