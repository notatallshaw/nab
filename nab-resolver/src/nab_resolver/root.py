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
    The resolver's package-keyed dicts hash it on every lookup, so it
    defines no ``__hash__`` of its own and hashes through ``object``
    without a Python frame.
    """

    @override
    def __repr__(self) -> str:
        return "<root>"


ROOT = _RootPackage()
