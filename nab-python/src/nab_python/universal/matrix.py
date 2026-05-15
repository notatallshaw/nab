"""Matrix expansion for user-declared universal resolution.

Expands a python version range plus a platform list into a finite
list of tuples, each a complete PEP 508 marker environment the
single-environment resolver can run against. Every PEP 508 variable
appearing in any marker on the dep graph must have a value in every
tuple. Wheel-tag and ``Requires-Python`` filtering happens elsewhere.
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
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
    "linux_aarch64": {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "aarch64",
        "os_name": "posix",
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
    "macos_arm64": {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "arm64",
        "os_name": "posix",
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
    "macos_x86_64": {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "x86_64",
        "os_name": "posix",
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
    "windows_amd64": {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "platform_machine": "AMD64",
        "os_name": "nt",
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
}


@dataclass(frozen=True)
class MatrixTuple:
    """A single point in the universal-resolution matrix."""

    python_version: str
    platform_id: str
    environment: dict[str, str] = field(hash=False, compare=False)
    platform_spec: PlatformSpec = field(
        hash=False,
        compare=False,
        default_factory=lambda: PlatformSpec("linux_x86_64"),
    )

    @property
    def label(self) -> str:
        """Return a short human-readable id like ``py311-linux_x86_64``."""
        return f"py{self.python_version.replace('.', '')}-{self.platform_id}"

    @property
    def marker_string(self) -> str:
        """Return a PEP 508 marker that selects this tuple.

        Combines ``python_version``, ``sys_platform``, and
        ``platform_machine`` into a conjunction.  Universal lockfiles
        attach this to each per-tuple ``Package`` entry so an installer
        on a matching environment picks the right pin.
        """
        env = self.environment
        return (
            f'python_version == "{self.python_version}"'
            f' and sys_platform == "{env["sys_platform"]}"'
            f' and platform_machine == "{env["platform_machine"]}"'
        )


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
    """

    python: str
    platforms: tuple[str | PlatformSpec, ...]
    python_order: str = "asc"
    python_patches: dict[str, str] | None = None

    def expand(self) -> list[MatrixTuple]:
        """Expand the matrix into concrete tuples.

        Validates inputs eagerly: unknown platform ids, an empty
        python range, or an invalid ``python_order`` each raise a
        ``ValueError`` before any work happens.

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
        py_versions = list(_pythons_in_range(self.python))
        if not py_versions:
            msg = f"No known Python versions match {self.python!r}"
            raise ValueError(msg)
        if self.python_order == "desc":
            py_versions.reverse()
        patches = self.python_patches or {}
        return [
            MatrixTuple(
                python_version=py,
                platform_id=spec.platform_id,
                environment=_build_environment(py, spec, patches.get(py)),
                platform_spec=spec,
            )
            for py in py_versions
            for spec in specs
        ]


def _build_environment(
    python_version: str,
    spec: PlatformSpec,
    python_full_version: str | None = None,
) -> dict[str, str]:
    """Build a complete PEP 508 marker environment for one tuple.

    Combines the platform's OS/arch defaults with python-axis values
    derived from ``python_version``.  ``platform_release`` and
    ``platform_version`` come from the :class:`PlatformSpec`; both
    default to ``""`` so kernel-conditioned markers evaluate False
    unless the user declares a target kernel/OS version.

    ``python_full_version`` overrides the default ``{minor}.0`` value.
    Used when the matrix declares ``python_patches`` to make
    patch-bound markers (``python_full_version >= "3.11.4"``)
    evaluate against the user's actual deployment patch release.
    """
    full = python_full_version or f"{python_version}.0"
    return {
        **_PLATFORM_DEFAULTS[spec.platform_id],
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
