"""Matrix expansion for user-declared universal resolution.

Expands a python version range, a platform list, and an implementation
list into a finite list of :class:`~nab_python.target.ResolveTarget`,
each a complete PEP 508 marker environment the single-environment
resolver can run against. Every PEP 508 variable appearing in any marker
on the dep graph must have a value in every target. ``Requires-Python``
filtering happens elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.version import Version
from ..tags import FREE_THREADED_MIN_PYTHON
from ..target import IMPLEMENTATION_MARKERS, PLATFORM_MARKERS, ResolveTarget

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..tags import PlatformSpec

# Common Python minor releases. An unrecognized minor declared in the
# user range raises.
__all__ = [
    "Matrix",
]


_KNOWN_PYTHON_MINORS: tuple[str, ...] = (
    "3.8",
    "3.9",
    "3.10",
    "3.11",
    "3.12",
    "3.13",
    "3.14",
)


@dataclass
class Matrix:
    """User-declared universal resolution matrix.

    ``python_order``: ``"asc"`` (default, 3.9 first) or ``"desc"`` (3.13
    first).  Combined with cross-target alignment in the resolver this
    selects between ``fork-strategy=fewest`` (asc: oldest-Python pin
    propagates forward, the lowest common version wins) and
    ``fork-strategy=requires-python`` (desc: newest-Python pin
    propagates, older Pythons diverge only when the new version is
    incompatible).

    ``python_patches``: optional ``{minor: full_version}`` mapping that
    sets the per-target ``python_full_version`` marker.  Defaults to
    ``{minor}.0`` per target, which makes markers like
    ``python_full_version >= "3.11.4"`` evaluate to False on a 3.11
    target.  Users with deployments on later patch releases should
    declare them here so marker evaluation matches reality.  Example:
    ``python_patches={"3.11": "3.11.4", "3.12": "3.12.1"}``.

    ``implementations``: the interpreter implementations to model
    (``"cpython"``, ``"pypy"``).  Defaults to ``("cpython",)``.  Each
    multiplies the target count; markers and wheel tags resolve per
    implementation.
    """

    python: str
    platforms: tuple[PlatformSpec, ...]
    python_order: str = "asc"
    python_patches: dict[str, str] | None = None
    implementations: tuple[str, ...] = ("cpython",)

    def expand(self) -> list[ResolveTarget]:
        """Expand the matrix into concrete targets.

        Validates inputs eagerly: unknown platform ids, unknown
        implementations, ``python_patches`` keys that are not known
        minors, an empty python range, an invalid ``python_order``, or a
        free-threaded platform no interpreter build can satisfy each raise a
        ``ValueError`` before any work happens.
        """
        if self.python_order not in {"asc", "desc"}:
            msg = f"python_order must be 'asc' or 'desc'; got {self.python_order!r}"
            raise ValueError(msg)
        unknown = [
            s.platform_id
            for s in self.platforms
            if s.platform_id not in PLATFORM_MARKERS
        ]
        if unknown:
            msg = f"Unknown platform ids: {unknown!r}"
            raise ValueError(msg)
        unknown_impl = [
            i for i in self.implementations if i not in IMPLEMENTATION_MARKERS
        ]
        if unknown_impl:
            msg = f"Unknown implementations: {unknown_impl!r}"
            raise ValueError(msg)
        patches = self.python_patches or {}
        unknown_patches = [m for m in patches if m not in _KNOWN_PYTHON_MINORS]
        if unknown_patches:
            msg = (
                f"Unknown python_patches minors: {unknown_patches!r};"
                " keys must be major.minor like '3.11'"
            )
            raise ValueError(msg)
        self._check_free_threaded()
        py_versions = list(_pythons_in_range(self.python))
        if not py_versions:
            msg = f"No known Python versions match {self.python!r}"
            raise ValueError(msg)
        if self.python_order == "desc":
            py_versions.reverse()
        multi_impl = len(self.implementations) > 1
        return [
            ResolveTarget.for_declared(
                python_version=py,
                spec=spec,
                implementation=impl,
                python_full_version=patches.get(py),
                multi_implementation=multi_impl,
            )
            for py in py_versions
            for spec in self.platforms
            for impl in self.implementations
        ]

    def _check_free_threaded(self) -> None:
        """Reject a free-threaded platform no interpreter build can satisfy.

        The ``cpXYt`` ABI ships only from CPython 3.13, and only the matrix
        sees both axes the rule needs: the platform carries the flag, and the
        implementation and the python range live here.
        """
        if not any(spec.free_threaded for spec in self.platforms):
            return
        foreign = [i for i in self.implementations if i != "cpython"]
        if foreign:
            msg = (
                f"a free-threaded platform needs CPython, not {foreign!r};"
                f" only CPython has a free-threaded build"
            )
            raise ValueError(msg)
        floor = ".".join(str(p) for p in FREE_THREADED_MIN_PYTHON)
        too_old = [
            py
            for py in _pythons_in_range(self.python)
            if tuple(int(p) for p in py.split(".")) < FREE_THREADED_MIN_PYTHON
        ]
        if too_old:
            msg = (
                f"a free-threaded platform needs CPython {floor} or newer,"
                f" but matrix.python admits {too_old!r}"
            )
            raise ValueError(msg)


def _pythons_in_range(spec: str) -> Iterable[str]:
    """Yield known Python minors that satisfy ``spec``.

    ``spec`` is a PEP 440 specifier set, e.g. ``">=3.11, <3.14"``.
    """
    parsed = SpecifierSet(spec)
    for minor in _KNOWN_PYTHON_MINORS:
        # Use the .0 patch for membership testing so that a >=3.11
        # specifier admits "3.11" via "3.11.0".
        candidate = Version(f"{minor}.0")
        if candidate in parsed:
            yield minor
