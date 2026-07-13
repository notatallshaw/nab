"""Predict which wheel a target environment would install.

Resolution needs the install-time wheel selection answer without a
live interpreter, so the tag set is computed from :class:`PlatformSpec`
and the implementation name directly. CPython tags come from
``packaging.tags.cpython_tags``; PyPy tags are emitted directly
(interpreter ``ppXY``, abi ``pypyXY_pp73``). Both add
interpreter-agnostic tags from ``packaging.tags.compatible_tags``.
Platform tags use ``mac_platforms`` for macOS and expand manylinux /
musllinux from the declared glibc / musl floor. A wheel matches the
target iff its parsed tags share a member with the target's accepted
tag set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache, lru_cache
from typing import TYPE_CHECKING

from ._vendor.packaging import tags as ptags

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nab_index.client import WheelFile

    from ._vendor.packaging.tags import Tag


__all__ = [
    "PlatformSpec",
    "select_wheel",
    "tags_for_target",
    "wheel_tag_set",
]


# PEP 427: a wheel filename has at least 5 dash-separated segments
# (name-version-pythontag-abitag-platformtag.whl), or 6 with a build tag.
_MIN_WHEEL_FILENAME_PARTS = 5

# PEP 427: a build tag adds a sixth dash-separated segment at index 2,
# and build tags start with a digit (captured here for ordering).
_WHEEL_PARTS_WITH_BUILD = 6
_BUILD_TAG_RE = re.compile(r"(\d+)(.*)", re.ASCII)

# Default manylinux floor: glibc 2.17 (the manylinux2014 generation,
# adopted by every mainstream distro since CentOS 7 / Ubuntu 14.04).
# A tighter floor reduces accepted wheels; a looser floor accepts
# wheels that may not run on older glibc.
_DEFAULT_MANYLINUX_FLOOR = (2, 17)
# Default musllinux floor: musl 1.2 (adopted by Alpine 3.13+, 2021+).
_DEFAULT_MUSLLINUX_FLOOR = (1, 2)
# The macOS defaults model the deployment-target (system) macOS version.
# ``mac_platforms`` treats this as a ceiling: a system at macOS V installs
# wheels built for V and older, never newer, so a higher value accepts more
# (newer) wheels. Default arm64 target: 12.0 (Monterey). Apple Silicon was
# introduced at 11.0, but most arm64 wheels published since 2023 target
# macosx_12_0 or newer, so a value below 12.0 would reject them.
_DEFAULT_MACOS_MIN = (12, 0)
# Default macOS minimum for x86_64 builds.  10.13 was the last with
# wide wheel coverage; newer macOS x86_64 builds rarely declare
# below 10.13.
_DEFAULT_MACOS_X86_64_MIN = (10, 13)

# Legacy manylinux aliases (PEPs 513/571/599) keyed by their glibc floor.
_LEGACY_MANYLINUX: dict[tuple[int, int], str] = {
    (2, 17): "manylinux2014",
    (2, 12): "manylinux2010",
    (2, 5): "manylinux1",
}

# Lowest glibc 2.x minor a manylinux wheel may target, by arch.  manylinux1
# (PEP 513) and manylinux2010 (PEP 571) cover only x86_64/i686; manylinux2014
# (PEP 599, glibc 2.17) was the first to add other arches, so every other arch
# stops at glibc 2.17.
_LEGACY_GLIBC_MAJOR = 2
_X86_MANYLINUX_ARCHS = frozenset({"x86_64", "i686"})
_X86_MIN_GLIBC2_MINOR = 5
_OTHER_MIN_GLIBC2_MINOR = 17


@dataclass(frozen=True)
class PlatformSpec:
    """Concrete tag floors for one matrix platform_id.

    Users can override the per-platform floors when their
    deployment target requires it.  The defaults are deliberately
    permissive (manylinux 2.17, musl 1.2, macOS 12 arm64 / 10.13
    x86_64) so most real deployments work out of the box.

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

    def label_suffix(self) -> str:
        """Return a label discriminator, empty for the platform default.

        Two specs sharing a ``platform_id`` collapse to one matrix-tuple
        label unless their floors set them apart, which silently merges
        their per-tuple pins.  The suffix encodes the floors so distinct
        specs stay distinct; a spec left at the platform default emits no
        suffix and keeps the plain ``pyXY-platform`` label.
        """
        if self == PlatformSpec(self.platform_id):
            return ""
        fields = (
            ("glibc", _floor_tag(self.manylinux_floor)),
            ("musl", _floor_tag(self.musllinux_floor)),
            ("macos", _floor_tag(self.macos_min)),
            ("rel", _escape_label_value(self.platform_release)),
            ("ver", _escape_label_value(self.platform_version)),
        )
        return "".join(f"-{tag}{value}" for tag, value in fields if value)


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


def _floor_tag(floor: tuple[int, int] | None) -> str:
    """Render a ``(major, minor)`` floor as ``major.minor``, or ``""`` if unset."""
    return f"{floor[0]}.{floor[1]}" if floor is not None else ""


def _escape_label_value(value: str) -> str:
    """Escape a free-form marker value for a label suffix field.

    Alphanumerics and ``.`` pass through, ``_`` doubles itself, and any
    other character becomes ``_<hex codepoint>_``.  This keeps the
    encoding injective and the output free of ``-``, so a value can
    never forge a field boundary and collapse two distinct specs onto
    one label.
    """
    out: list[str] = []
    for ch in value:
        if ch == "_":
            out.append("__")
        elif ch.isalnum() or ch == ".":
            out.append(ch)
        else:
            out.append(f"_{ord(ch):x}_")
    return "".join(out)


def _linux_platform_tags(
    arch: str,
    *,
    manylinux_floor: tuple[int, int],
    musllinux_floor: tuple[int, int],
) -> list[str]:
    """Generate manylinux + musllinux + plain linux tags for an arch.

    Returns the tag list in install-preference order: most-specific
    (highest glibc/musl version) first.  Accepts every minor version
    at or below the declared floor, down to the arch's oldest
    supported glibc; this is the spec-compliant interpretation of
    "manylinux_X_Y means glibc X.Y or older".

    Note: PEP 600 says installers prefer wheels with the *highest*
    glibc among compatible ones.  Our use case is "decide which
    wheel a tuple would install"; the tag list ordering matches
    that preference.
    """
    # manylinux_X_Y: PEP 600 form.  Iterate high-to-low for preference
    # order, emitting each legacy alias right after its equivalent
    # PEP 600 tag so a legacy-named wheel ranks at its own glibc, matching
    # packaging.tags.  A glibc 2.x floor stops at the arch's oldest tag
    # (2.5 for x86, 2.17 otherwise); any other major stops at minor 0.
    major, minor = manylinux_floor
    if major == _LEGACY_GLIBC_MAJOR:
        min_minor = (
            _X86_MIN_GLIBC2_MINOR
            if arch in _X86_MANYLINUX_ARCHS
            else _OTHER_MIN_GLIBC2_MINOR
        )
    else:
        min_minor = 0
    out: list[str] = []
    for m in range(minor, min_minor - 1, -1):
        out.append(f"manylinux_{major}_{m}_{arch}")
        legacy = _LEGACY_MANYLINUX.get((major, m))
        if legacy is not None:
            out.append(f"{legacy}_{arch}")
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


# PyPy 7.3.x soabi, stable across every Python minor PyPy 3 ships
# (pp36..pp311 all use ``_pp73``); the abi tag is ``pypyXY_pp73``.
_PYPY_SOABI = "73"


@cache
def tags_for_target(
    *,
    python_version: str,
    spec: PlatformSpec,
    implementation: str = "cpython",
) -> frozenset[Tag]:
    """Return the full set of tags ``(python_version, spec, impl)`` accepts.

    Builds the same ordered tags as :func:`_tags_in_order` and returns
    them as a frozenset.  Cached on the three immutable inputs (str,
    frozen dataclass, str): the resulting set is identical across every
    wheel-compatibility check for the same target, so the cache skips
    rebuilding the same :class:`Tag` set per call.
    """
    return frozenset(_tags_in_order(python_version, spec, implementation))


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


def select_wheel(
    wheels: Iterable[WheelFile],
    *,
    python_version: str,
    spec: PlatformSpec,
    implementation: str = "cpython",
) -> WheelFile | None:
    """Pick the most-specific compatible wheel for the target, or None.

    Implements PEP 425 preference: wheels matching earlier
    (more-specific) tags in :func:`tags_for_target` win over those
    matching later (more-generic) tags.  Within the same tag rank, the
    wheel with the highest PEP 427 build tag wins (an absent tag sorts
    lowest); exact ties keep input order.
    """
    compat_list = list(_tags_in_order(python_version, spec, implementation))
    rank: dict[Tag, int] = {tag: i for i, tag in enumerate(compat_list)}

    best: tuple[int, tuple[int, str], WheelFile] | None = None
    for wheel in wheels:
        wheel_tags = wheel_tag_set(wheel.filename)
        if not wheel_tags:
            continue
        # Lowest rank index wins (most-specific tag).
        wheel_rank = min((rank[t] for t in wheel_tags if t in rank), default=None)
        if wheel_rank is None:
            continue
        build_key = _build_tag_sort_key(wheel.filename)
        if (
            best is None
            or wheel_rank < best[0]
            or (wheel_rank == best[0] and build_key > best[1])
        ):
            best = (wheel_rank, build_key, wheel)
    return best[2] if best is not None else None


def _build_tag_sort_key(filename: str) -> tuple[int, str]:
    """Return a PEP 427 build-tag sort key; an absent tag sorts lowest.

    The build tag is the third dash-separated segment when present.
    A missing or malformed tag sorts below every real build number.
    """
    parts = filename[:-4].split("-")
    if len(parts) != _WHEEL_PARTS_WITH_BUILD:
        return (-1, "")
    match = _BUILD_TAG_RE.match(parts[2])
    if match is None:
        return (-1, "")
    return (int(match.group(1)), match.group(2))


def _tags_in_order(
    python_version: str, spec: PlatformSpec, implementation: str = "cpython"
) -> Iterable[Tag]:
    """Yield the tags a target accepts in install preference order.

    CPython targets use ``packaging.tags.cpython_tags`` (cpXY-cpXY,
    cpXY-abi3 forward-compat, cpXY-none).  PyPy targets cannot reuse
    that (it forces the ``cp`` interpreter and abi3, which PyPy lacks),
    so their interpreter/abi/none tags are emitted directly.  Both then
    add the interpreter-agnostic tags (pyXY-none-any, py3-none-any, ...).
    """
    major, minor = (int(p) for p in python_version.split("."))
    py_version = (major, minor)
    platforms = _platform_tags_for_spec(spec)
    if implementation == "pypy":
        interpreter = f"pp{major}{minor}"
        abi = f"pypy{major}{minor}_pp{_PYPY_SOABI}"
        for platform_ in platforms:
            yield ptags.Tag(interpreter, abi, platform_)
        for platform_ in platforms:
            yield ptags.Tag(interpreter, "none", platform_)
    else:
        interpreter = f"cp{major}{minor}"
        yield from ptags.cpython_tags(
            python_version=py_version, abis=[interpreter], platforms=platforms
        )
    yield from ptags.compatible_tags(
        python_version=py_version, interpreter=interpreter, platforms=platforms
    )
