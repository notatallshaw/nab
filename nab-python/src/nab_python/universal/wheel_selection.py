"""Predict which wheel a ``(python_version, platform_id)`` tuple would install.

Universal resolution needs the install-time wheel selection answer
without a live interpreter, so the tag set is computed from
:class:`PlatformSpec` directly. CPython tags come from
``packaging.tags.cpython_tags``, interpreter-agnostic tags from
``compatible_tags``, macOS from ``mac_platforms``, and manylinux /
musllinux are expanded from a declared glibc / musl floor. A wheel
matches the tuple iff its parsed tags share a member with the
tuple's compatible-tag set.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache, lru_cache
from typing import TYPE_CHECKING

from .._vendor.packaging import tags as ptags

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nab_index.client import WheelFile

    from .._vendor.packaging.tags import Tag


# PEP 427: a wheel filename has at least 5 dash-separated segments
# (name-version-pythontag-abitag-platformtag.whl), or 6 with a build tag.
__all__ = [
    "PlatformSpec",
    "compatible_tags_for_tuple",
    "select_wheel_for_tuple",
    "wheel_compatible_with_tuple",
]


_MIN_WHEEL_FILENAME_PARTS = 5

# Default manylinux floor: glibc 2.17 (the manylinux2014 generation,
# adopted by every mainstream distro since CentOS 7 / Ubuntu 14.04).
# A tighter floor reduces accepted wheels; a looser floor accepts
# wheels that may not run on older glibc.
_DEFAULT_MANYLINUX_FLOOR = (2, 17)
# Default musllinux floor: musl 1.2 (adopted by Alpine 3.13+, 2021+).
_DEFAULT_MUSLLINUX_FLOOR = (1, 2)
# Default macOS minimum: 11 (Big Sur, 2020+).  arm64 was introduced
# at 11.0; using 10.x for arm64 has no compatible wheels.
_DEFAULT_MACOS_MIN = (11, 0)
# Default macOS minimum for x86_64 builds.  10.13 was the last with
# wide wheel coverage; newer macOS x86_64 builds rarely declare
# below 10.13.
_DEFAULT_MACOS_X86_64_MIN = (10, 13)


@dataclass(frozen=True)
class PlatformSpec:
    """Concrete tag floors for one matrix platform_id.

    Users can override the per-platform floors when their
    deployment target requires it.  The defaults are deliberately
    permissive (manylinux 2.17, musl 1.2, macOS 11) so most real
    deployments work out of the box.

    ``platform_release`` and ``platform_version`` set the
    corresponding PEP 508 marker values on this platform's tuples
    (hole 1.3 plug).  When unset, both default to the empty string,
    which makes any kernel-version-conditioned marker
    (``platform_release >= "5.10"``) evaluate False (the safe
    direction: drop the gated dep) but a silent failure if the
    target machine actually has that kernel.  Users who declare a
    minimum target kernel get the gated deps included.
    """

    platform_id: str
    manylinux_floor: tuple[int, int] = _DEFAULT_MANYLINUX_FLOOR
    musllinux_floor: tuple[int, int] = _DEFAULT_MUSLLINUX_FLOOR
    macos_min: tuple[int, int] | None = None  # arch-dependent default
    platform_release: str = ""
    platform_version: str = ""

    @property
    def arch(self) -> str:
        """The architecture suffix used in platform tags."""
        return _PLATFORM_ARCH[self.platform_id]


# Map our matrix platform_ids to (kind, arch).  Kind is one of
# "linux", "macos", "windows".  Used for tag generation.
_PLATFORM_ARCH: dict[str, str] = {
    "linux_x86_64": "x86_64",
    "linux_aarch64": "aarch64",
    "macos_arm64": "arm64",
    "macos_x86_64": "x86_64",
    "windows_amd64": "amd64",
}

_PLATFORM_KIND: dict[str, str] = {
    "linux_x86_64": "linux",
    "linux_aarch64": "linux",
    "macos_arm64": "macos",
    "macos_x86_64": "macos",
    "windows_amd64": "windows",
}


def _linux_platform_tags(
    arch: str,
    *,
    manylinux_floor: tuple[int, int],
    musllinux_floor: tuple[int, int],
) -> list[str]:
    """Generate manylinux + musllinux + plain linux tags for an arch.

    Returns the tag list in install-preference order: most-specific
    (highest glibc/musl version) first.  Accepts every minor version
    at or below the declared floor; this is the spec-compliant
    interpretation of "manylinux_X_Y means glibc X.Y or older".

    Note: PEP 600 says installers prefer wheels with the *highest*
    glibc among compatible ones.  Our use case is "decide which
    wheel a tuple would install"; the tag list ordering matches
    that preference.
    """
    # manylinux_X_Y: PEP 600 form.  We accept any minor at or below
    # the floor (a wheel built for glibc 2.5 runs on a system with
    # glibc 2.17; a wheel built for glibc 2.34 does not).  Iterate
    # high-to-low for preference order.
    major, minor = manylinux_floor
    out = [f"manylinux_{major}_{m}_{arch}" for m in range(minor, -1, -1)]
    # Legacy aliases (PEPs 513/571/599).  These map to specific
    # glibc versions: manylinux1=2.5, manylinux2010=2.12,
    # manylinux2014=2.17.  We include them when they're <= floor.
    legacy_aliases = [
        ("manylinux1", (2, 5)),
        ("manylinux2010", (2, 12)),
        ("manylinux2014", (2, 17)),
    ]
    out.extend(
        f"{name}_{arch}" for name, lver in legacy_aliases if lver <= manylinux_floor
    )
    # musllinux_X_Y: PEP 656 form.  Same accept-at-or-below rule.
    mu_major, mu_minor = musllinux_floor
    out.extend(f"musllinux_{mu_major}_{m}_{arch}" for m in range(mu_minor, -1, -1))
    # Plain linux_<arch>: the most generic Linux tag.  Most installers
    # accept this only when no manylinux/musllinux wheel is present.
    out.append(f"linux_{arch}")
    return out


def _platform_tags_for_spec(spec: PlatformSpec) -> list[str]:
    """Build the platform-tag list for ``spec`` in preference order."""
    kind = _PLATFORM_KIND[spec.platform_id]
    arch = spec.arch

    if kind == "linux":
        return _linux_platform_tags(
            arch,
            manylinux_floor=spec.manylinux_floor,
            musllinux_floor=spec.musllinux_floor,
        )

    if kind == "macos":
        macos_min = spec.macos_min
        if macos_min is None:
            macos_min = (
                _DEFAULT_MACOS_MIN if arch == "arm64" else _DEFAULT_MACOS_X86_64_MIN
            )
        # mac_platforms treats the declared OS as a max and yields older too.
        return list(ptags.mac_platforms(version=macos_min, arch=arch))

    if kind == "windows":
        return [f"win_{arch}"]

    # Unreachable; PlatformSpec construction validates.
    msg = f"Unknown platform kind: {kind}"  # pragma: no cover
    raise ValueError(msg)  # pragma: no cover


@cache
def compatible_tags_for_tuple(
    *,
    python_version: str,
    spec: PlatformSpec,
) -> frozenset[Tag]:
    """Return the full set of tags ``(python_version, spec)`` accepts.

    Combines:

    1. CPython-specific tags via ``packaging.tags.cpython_tags``
       (cpXY-cpXY, cpXY-abi3 forward-compat, cpXY-none).
    2. Interpreter-agnostic tags via ``packaging.tags.compatible_tags``
       (pyXY-none-any, py3-none-any, etc.).

    The platform list is computed by :func:`_platform_tags_for_spec`.
    Cached on ``(python_version, spec)``: both inputs are immutable
    (str, frozen dataclass) and the resulting set is identical across
    every wheel-compatibility check for the same tuple, so the cache
    skips rebuilding the same :class:`Tag` set per call.
    """
    major, minor = (int(p) for p in python_version.split("."))
    py_version = (major, minor)
    abi = f"cp{major}{minor}"
    platforms = _platform_tags_for_spec(spec)
    out: set[Tag] = set()
    out.update(
        ptags.cpython_tags(python_version=py_version, abis=[abi], platforms=platforms)
    )
    out.update(
        ptags.compatible_tags(
            python_version=py_version, interpreter=abi, platforms=platforms
        )
    )
    return frozenset(out)


@lru_cache(maxsize=4096)
def _intern_tag(tag: Tag) -> Tag:
    """Return a shared :class:`Tag` for ``tag``.

    ``packaging.tags.parse_tag`` constructs fresh :class:`Tag` instances
    on every call.  The set of distinct (interpreter, abi, platform)
    triples in a single PyPI scan is small compared with the wheels
    visited, so sharing the canonical instance collapses the duplicates.
    ``Tag`` is immutable (``__slots__``) so the shared instance is safe.
    """
    return tag


@lru_cache(maxsize=8192)
def _parse_tag_str(tag_str: str) -> frozenset[Tag] | None:
    """Cache ``parse_tag`` keyed on the wheel's ``python-abi-platform``.

    Many distinct wheel filenames share the same tag suffix
    (e.g. ``cp310-cp310-manylinux2014_x86_64``), so caching by tag
    string deduplicates more aggressively than caching by filename.
    Returns ``None`` for unparseable input.
    """
    try:
        raw = ptags.parse_tag(tag_str)
    except Exception:  # noqa: BLE001 - never trust upstream parser
        return None
    return frozenset(_intern_tag(t) for t in raw)


def wheel_tag_set(filename: str) -> frozenset[Tag] | None:
    """Parse a wheel filename into the set of tags it advertises.

    Per PEP 427 the filename's last three dash-separated segments
    are ``python-abi-platform``; per PEP 425 each can be a
    dot-separated compressed set.  Returns ``None`` for a non-wheel
    filename or one with too few segments.  The expensive work
    (``parse_tag`` + Tag interning) lives in :func:`_parse_tag_str`,
    which is cached on the suffix so wheels that share it
    short-circuit.
    """
    if not filename.endswith(".whl"):
        return None
    stem = filename[:-4]
    parts = stem.split("-")
    # PEP 427: filename has 5 segments (no build tag) or 6 (with build).
    if len(parts) < _MIN_WHEEL_FILENAME_PARTS:
        return None
    # The last three dash-separated segments are python-abi-platform.
    return _parse_tag_str("-".join(parts[-3:]))


def wheel_compatible_with_tuple(
    wheel: WheelFile,
    *,
    python_version: str,
    spec: PlatformSpec,
) -> bool:
    """Return True iff ``wheel`` is a candidate for the given tuple."""
    wheel_tags = wheel_tag_set(wheel.filename)
    if wheel_tags is None:
        return False
    compat = compatible_tags_for_tuple(python_version=python_version, spec=spec)
    # ``frozenset.isdisjoint`` is a C-level builtin that beats the
    # Python ``any(t in compat for t in wheel_tags)`` generator on
    # the per-wheel hot loop.
    return not wheel_tags.isdisjoint(compat)


def select_wheel_for_tuple(
    wheels: Iterable[WheelFile],
    *,
    python_version: str,
    spec: PlatformSpec,
) -> WheelFile | None:
    """Pick the most-specific compatible wheel for the tuple, or None.

    Implements PEP 425 preference: wheels matching earlier
    (more-specific) tags in ``compatible_tags_for_tuple`` win over
    those matching later (more-generic) tags.  Within the same tag
    rank, the first wheel in input order wins.
    """
    compat_list = list(_compatible_tags_in_order(python_version, spec))
    rank: dict[Tag, int] = {tag: i for i, tag in enumerate(compat_list)}

    best: tuple[int, WheelFile] | None = None
    for wheel in wheels:
        wheel_tags = wheel_tag_set(wheel.filename)
        if not wheel_tags:
            continue
        # Lowest rank index wins (most-specific tag).
        wheel_rank = min((rank[t] for t in wheel_tags if t in rank), default=None)
        if wheel_rank is None:
            continue
        if best is None or wheel_rank < best[0]:
            best = (wheel_rank, wheel)
    return best[1] if best is not None else None


def _compatible_tags_in_order(python_version: str, spec: PlatformSpec) -> Iterable[Tag]:
    """Yield compatible tags in install preference order."""
    major, minor = (int(p) for p in python_version.split("."))
    py_version = (major, minor)
    abi = f"cp{major}{minor}"
    platforms = _platform_tags_for_spec(spec)
    yield from ptags.cpython_tags(
        python_version=py_version, abis=[abi], platforms=platforms
    )
    yield from ptags.compatible_tags(
        python_version=py_version, interpreter=abi, platforms=platforms
    )
