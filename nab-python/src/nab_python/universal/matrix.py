"""Matrix expansion for user-declared universal resolution.

Expands a python version range, a platform list, and an implementation
list into a finite list of tuples, each a complete PEP 508 marker
environment the single-environment resolver can run against. Every PEP
508 variable appearing in any marker on the dep graph must have a value
in every tuple. Wheel-tag and ``Requires-Python`` filtering happens
elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.version import Version
from .wheel_selection import PlatformSpec

if TYPE_CHECKING:
    from collections.abc import Iterable

# Common Python minor releases. An unrecognized minor declared in the
# user range raises.
__all__ = [
    "Matrix",
    "MatrixTuple",
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


# Defaults filled in for marker keys the user did not specify.  These
# come from the most common PEP 508 environment values for the named
# OS/arch.  They are used only for marker *evaluation*; the resolver
# does not consume them as constraints on its own.
_PLATFORM_DEFAULTS: dict[str, dict[str, str]] = {
    "linux_x86_64": {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "x86_64",
        "os_name": "posix",
    },
    "linux_aarch64": {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "aarch64",
        "os_name": "posix",
    },
    "macos_arm64": {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "arm64",
        "os_name": "posix",
    },
    "macos_x86_64": {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "x86_64",
        "os_name": "posix",
    },
    "windows_amd64": {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "platform_machine": "AMD64",
        "os_name": "nt",
    },
}


# The implementation-axis PEP 508 marker values per known
# implementation.
_IMPLEMENTATION_DEFAULTS: dict[str, dict[str, str]] = {
    "cpython": {
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
    "pypy": {
        "platform_python_implementation": "PyPy",
        "implementation_name": "pypy",
    },
}

# PEP 425 interpreter short tag per implementation, used in the tuple
# label so tuples differing only by implementation stay distinct.
_IMPLEMENTATION_PREFIX: dict[str, str] = {"cpython": "py", "pypy": "pp"}


@dataclass(frozen=True)
class MatrixTuple:
    """A single point in the universal-resolution matrix.

    ``selection`` records the conflict-fork this tuple belongs to: a
    tuple of ``(kind, name)`` members (``kind`` is ``"extra"`` or
    ``"group"``) that are active in this fork's resolve.  It is empty
    for an unforked resolve.  When set, it both disambiguates the
    label and adds an ``'name' in extras`` / ``'name' in
    dependency_groups`` clause to the marker so the lockfile entry
    fires only when the user selects that member.
    """

    python_version: str
    platform_id: str
    environment: dict[str, str] = field(hash=False, compare=False)
    platform_spec: PlatformSpec = field(
        hash=False,
        compare=False,
        default_factory=lambda: PlatformSpec("linux_x86_64"),
    )
    implementation: str = "cpython"
    multi_implementation: bool = field(default=False, hash=False, compare=False)
    selection: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        """Return a short human-readable id like ``py311-linux_x86_64``.

        Uses the interpreter prefix (``py`` for CPython, ``pp`` for
        PyPy) so tuples that differ only by implementation get distinct
        labels, and appends the platform spec's floor discriminator so
        two specs sharing a ``platform_id`` do not collapse.  A
        conflict-fork ``selection`` then appends each active member as
        ``kind-name``, joined by ``.``, in sorted order so the forks of
        one python/platform stay distinct, e.g.
        ``py311-linux_x86_64-group-black22.group-isort5``.  The ``.``
        separator and the ``kind`` prefix keep the label unambiguous:
        canonical member names are ``[a-z0-9-]`` only, so a name can
        never introduce a ``.``, and two selections that differ only in
        how their names split on ``-`` (or an extra versus a group of
        the same name) cannot collide into one label and silently
        overwrite each other's pins when the label is used as a dict
        key.
        """
        prefix = _IMPLEMENTATION_PREFIX[self.implementation]
        base = (
            f"{prefix}{self.python_version.replace('.', '')}-{self.platform_id}"
            + self.platform_spec.label_suffix()
        )
        if not self.selection:
            return base
        suffix = ".".join(f"{kind}-{name}" for kind, name in sorted(self.selection))
        return f"{base}-{suffix}"

    @property
    def environment_marker_string(self) -> str:
        """Return the PEP 508 marker for this tuple's environment only.

        Combines ``python_version``, ``sys_platform``, and
        ``platform_machine``.  In a multi-implementation matrix every
        tuple also constrains ``implementation_name`` so the CPython and
        PyPy entries for the same python/platform stay mutually
        exclusive; a sole-CPython matrix omits the clause.

        This carries no conflict-fork ``selection``, so it is what the
        lockfile's top-level ``environments`` list declares: the
        platform/Python universe, not which extras or groups are active.
        """
        env = self.environment
        marker = (
            f'python_version == "{self.python_version}"'
            f' and sys_platform == "{env["sys_platform"]}"'
            f' and platform_machine == "{env["platform_machine"]}"'
        )
        if self.multi_implementation or self.implementation != "cpython":
            marker += f' and implementation_name == "{env["implementation_name"]}"'
        return marker

    @property
    def marker_string(self) -> str:
        """Return the per-package PEP 508 marker that selects this tuple.

        This is :attr:`environment_marker_string` plus a bare membership
        clause per active conflict-fork member (``'name' in extras`` for
        an extra, ``'name' in dependency_groups`` for a group).  The
        emit-time disjointness validator prunes the install contexts
        that activate two members of one declared conflict, so the bare
        clause needs no ``not in`` negation against the other members.
        """
        marker = self.environment_marker_string
        for kind, name in sorted(self.selection):
            variable = "extras" if kind == "extra" else "dependency_groups"
            marker += f' and "{name}" in {variable}'
        return marker


@dataclass
class Matrix:
    """User-declared universal resolution matrix.

    ``python_order``: ``"asc"`` (default, 3.9 first) or ``"desc"`` (3.13
    first).  Combined with cross-tuple alignment in the resolver this
    selects between ``fork-strategy=fewest`` (asc: oldest-Python pin
    propagates forward, the lowest common version wins) and
    ``fork-strategy=requires-python`` (desc: newest-Python pin
    propagates, older Pythons diverge only when the new version is
    incompatible).

    ``python_patches``: optional ``{minor: full_version}`` mapping that
    sets the per-tuple ``python_full_version`` marker.  Defaults to
    ``{minor}.0`` per tuple, which makes markers like
    ``python_full_version >= "3.11.4"`` evaluate to False on a 3.11
    tuple.  Users with deployments on later patch releases should
    declare them here so marker evaluation matches reality.  Example:
    ``python_patches={"3.11": "3.11.4", "3.12": "3.12.1"}``.
    See ``universal_open_questions.md`` section 1.1 for the design
    discussion.

    ``implementations``: the interpreter implementations to model
    (``"cpython"``, ``"pypy"``).  Defaults to ``("cpython",)``.  Each
    multiplies the tuple count; markers and wheel tags resolve per
    implementation.
    """

    python: str
    platforms: tuple[str | PlatformSpec, ...]
    python_order: str = "asc"
    python_patches: dict[str, str] | None = None
    implementations: tuple[str, ...] = ("cpython",)

    def expand(self) -> list[MatrixTuple]:
        """Expand the matrix into concrete tuples.

        Validates inputs eagerly: unknown platform ids, unknown
        implementations, an empty python range, or an invalid
        ``python_order`` each raise a ``ValueError`` before any work
        happens.

        ``platforms`` accepts either bare platform-id strings (use
        default tag floors) or :class:`PlatformSpec` instances for
        per-platform glibc/musl/macOS overrides.
        """
        if self.python_order not in {"asc", "desc"}:
            msg = f"python_order must be 'asc' or 'desc'; got {self.python_order!r}"
            raise ValueError(msg)
        specs = [
            p if isinstance(p, PlatformSpec) else PlatformSpec(p)
            for p in self.platforms
        ]
        unknown = [
            s.platform_id for s in specs if s.platform_id not in _PLATFORM_DEFAULTS
        ]
        if unknown:
            msg = f"Unknown platform ids: {unknown!r}"
            raise ValueError(msg)
        unknown_impl = [
            i for i in self.implementations if i not in _IMPLEMENTATION_DEFAULTS
        ]
        if unknown_impl:
            msg = f"Unknown implementations: {unknown_impl!r}"
            raise ValueError(msg)
        py_versions = list(_pythons_in_range(self.python))
        if not py_versions:
            msg = f"No known Python versions match {self.python!r}"
            raise ValueError(msg)
        if self.python_order == "desc":
            py_versions.reverse()
        patches = self.python_patches or {}
        multi_impl = len(self.implementations) > 1
        return [
            MatrixTuple(
                python_version=py,
                platform_id=spec.platform_id,
                environment=_build_environment(py, spec, impl, patches.get(py)),
                platform_spec=spec,
                implementation=impl,
                multi_implementation=multi_impl,
            )
            for py in py_versions
            for spec in specs
            for impl in self.implementations
        ]


def _build_environment(
    python_version: str,
    spec: PlatformSpec,
    implementation: str,
    python_full_version: str | None = None,
) -> dict[str, str]:
    """Build a complete PEP 508 marker environment for one tuple.

    Combines the platform's OS/arch defaults and the implementation's
    interpreter-identity defaults with python-axis values derived from
    ``python_version``.  ``platform_release`` and ``platform_version``
    come from the :class:`PlatformSpec`; both default to ``""`` so
    kernel-conditioned markers evaluate False unless the user declares a
    target kernel/OS version.

    ``python_full_version`` overrides the default ``{minor}.0`` value.
    Used when the matrix declares ``python_patches`` to make
    patch-bound markers (``python_full_version >= "3.11.4"``) evaluate
    against the user's actual deployment patch release.

    ``implementation_version`` is set to the Python version for every
    implementation; for non-CPython this is the interpreter's Python
    level, not its own release (PyPy 7.3.x), so the rare
    ``implementation_version`` marker on PyPy may misevaluate.
    """
    full = python_full_version or f"{python_version}.0"
    return {
        **_PLATFORM_DEFAULTS[spec.platform_id],
        **_IMPLEMENTATION_DEFAULTS[implementation],
        "python_version": python_version,
        "python_full_version": full,
        "implementation_version": full,
        "platform_release": spec.platform_release,
        "platform_version": spec.platform_version,
    }


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
