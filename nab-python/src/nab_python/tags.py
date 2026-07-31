"""Predict which wheel a target environment would install.

Resolution needs the install-time wheel selection answer without a
live interpreter, so a declared target's tag set is computed from
:class:`PlatformSpec` and the implementation name directly, never from
the interpreter running nab. CPython tags come from
``packaging.tags.cpython_tags``; PyPy tags are emitted directly
(interpreter ``ppXY``, abi ``pypyXY_pp73``). Both add
interpreter-agnostic tags from ``packaging.tags.compatible_tags``.
Platform tags use ``mac_platforms`` for macOS and expand the declared
libc family's tags on Linux, in the order ``sys_tags`` uses, so the
declared path and the host path rank the same wheel first. A wheel
matches the target iff its parsed tags share a member with the target's
accepted tag set.

:class:`TagSet` is that accepted set, in install-preference order. The
host builds one from ``packaging.tags.sys_tags``; a target Python on
the host machine keeps the host's platform axis and moves only the
interpreter and abi, which is what pip's ``--python-version`` does.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache, cached_property, lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from ._vendor.packaging import tags as ptags
from ._vendor.packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ._vendor.packaging.tags import Tag


__all__ = [
    "DEFAULT_LIBC",
    "FREE_THREADED_MIN_PYTHON",
    "LIBC_MAJOR",
    "Libc",
    "PlatformSpec",
    "TagSet",
    "TagsSource",
    "platform_kind",
    "python_axis_accepts",
    "supports_free_threading",
    "wheel_tag_set",
]

Libc = Literal["glibc", "musl"]

# The free-threaded build ships from CPython 3.13 (PEP 703).
FREE_THREADED_MIN_PYTHON = (3, 13)

# Where a host tag set comes from.  Injected so a caller (and every
# test) can name the interpreter it means instead of the one running.
TagsSource = Callable[[], "Iterable[Tag]"]


# PEP 427: a wheel filename has at least 5 dash-separated segments
# (name-version-pythontag-abitag-platformtag.whl), or 6 with a build tag.
_MIN_WHEEL_FILENAME_PARTS = 5

# A PEP 425 compressed tag set is a cross product, so an index-supplied
# filename naming 40 interpreters, 40 abis and 40 platforms would parse into
# 64000 tags.  No real wheel comes close to this bound.
_MAX_WHEEL_TAGS = 4096

# PEP 427: a build tag adds a sixth dash-separated segment at index 2, and
# starts with a digit (captured here for ordering).
_WHEEL_PARTS_WITH_BUILD = 6
_BUILD_TAG_RE = re.compile(r"(\d+)(.*)", re.ASCII)

# runs-on-libc (or runs-on-macos) names a system the lock must run on.  A
# wheel is a member iff it runs there: the target accepts every
# manylinux/musllinux (or macosx) tag at or below the named level and drops a
# wheel that needs something newer.  Unset, the knob names no system and a
# wheel of any level is accepted, deferring compatibility to install time, the
# way uv resolves.
DEFAULT_LIBC: Libc = "glibc"
# The only major each family has shipped.  Tags expand within the declared
# major, so a foreign major names platform tags no wheel is built for.
LIBC_MAJOR: Mapping[Libc, int] = MappingProxyType({"glibc": 2, "musl": 1})

# The oldest macOS each arch can name a wheel tag for.  ``mac_platforms``
# yields no x86_64 binary format below 10.4, and Apple Silicon shipped at
# 11.0, so below these there is no machine to model.  An empty platform list
# also reads to packaging as "unset", which would silently hand the target
# the tags of whatever host nab is running on.
_MACOS_TAG_FLOOR: Mapping[str, tuple[int, int]] = MappingProxyType(
    {"x86_64": (10, 4), "arm64": (11, 0)}
)

# Legacy manylinux aliases (PEPs 513/571/599) keyed by the glibc they mean.
_LEGACY_MANYLINUX: dict[tuple[int, int], str] = {
    (2, 17): "manylinux2014",
    (2, 12): "manylinux2010",
    (2, 5): "manylinux1",
}

# A tag list runs one entry per version below the declared one, so a typo
# like "2.99999999" would build tags until it ran out of memory.  No libc or
# macOS release is anywhere near this max.
_MAX_VERSION_PART = 99

# The no-limit sentinel an unset knob stands in for.  A tag generator reads a
# version as a max and yields it and every older one, so the sentinel admits
# every real tag.  macOS enumerates on the major for 11+, so the sentinel major
# is 11 or newer; the minor is 0, which is all mac_platforms emits above 11.
_MACOS_NO_LIMIT = (_MAX_VERSION_PART, 0)


def _libc_no_limit(libc: Libc) -> tuple[int, int]:
    """Return a libc family's no-limit sentinel: its major at the max minor."""
    return (LIBC_MAJOR[libc], _MAX_VERSION_PART)


# Glibc 2.x minor floor a manylinux wheel may target, by arch.  manylinux1
# (PEP 513) and manylinux2010 (PEP 571) cover only x86_64/i686; manylinux2014
# (PEP 599, glibc 2.17) was the first to add other arches, so every other arch
# stops at glibc 2.17.
_X86_MANYLINUX_ARCHS = frozenset({"x86_64", "i686"})
_X86_GLIBC2_MINOR_FLOOR = 5
_OTHER_GLIBC2_MINOR_FLOOR = 17


@dataclass(frozen=True)
class PlatformSpec:
    """Concrete tag knobs for one matrix platform_id.

    ``libc`` names the C library the Linux target runs; a machine links one,
    so the target emits that family's wheel tags and never the other's.
    ``runs_on_libc`` names a glibc or musl the lock must run on: a wheel built
    against it or an older libc runs there and is accepted, and one built
    against a newer libc is dropped.  Unset, the knob names no system and a
    wheel of any level is accepted, deferring compatibility to install time.
    ``runs_on_macos`` reads the same way for macOS: a macOS the lock must run
    on.

    ``platform_release`` and ``platform_version`` set the corresponding PEP 508
    marker values.  Left empty, a kernel-conditioned marker
    (``platform_release >= "5.10"``) evaluates False and its dependency is
    dropped, so a target that does have that kernel has to declare it.

    ``free_threaded`` picks the ``cpXYt`` ABI (and ``abi3t`` in place of
    ``abi3``) of a CPython 3.13+ free-threaded build.
    """

    platform_id: str
    libc: Libc = DEFAULT_LIBC
    runs_on_libc: tuple[int, int] | None = None  # unset: accept any level
    runs_on_macos: tuple[int, int] | None = None  # unset: accept any level
    platform_release: str = ""
    platform_version: str = ""
    free_threaded: bool = False

    def __post_init__(self) -> None:
        """Reject a knob the declared platform cannot carry.

        The tag generator reads a libc only on Linux and a macOS version
        only on macOS, so a knob on the wrong platform changes no wheel it
        selects.  It does reach the target's label and the lock's
        provenance, which would then name a machine nothing modelled.

        An unrecognised ``platform_id`` passes: the matrix reports the
        whole unknown set at once, and that says more than a complaint
        about a knob on a platform that does not exist.
        """
        kind = platform_kind(self.platform_id)
        if kind is None:
            return

        if kind == "linux":
            self._check_runs_on_libc()
        elif self.libc != DEFAULT_LIBC or self.runs_on_libc is not None:
            msg = (
                "libc and runs-on-libc are Linux knobs, and"
                f" {self.platform_id} is not Linux"
            )
            raise ValueError(msg)

        if kind == "macos":
            self._check_runs_on_macos()
        elif self.runs_on_macos is not None:
            msg = f"runs-on-macos is a macOS knob, and {self.platform_id} is not macOS"
            raise ValueError(msg)

    def _check_runs_on_libc(self) -> None:
        """Reject a libc version outside its family's only major."""
        if self.runs_on_libc is None:
            return
        major = LIBC_MAJOR[self.libc]
        if self.runs_on_libc[0] != major:
            msg = (
                f"{self.libc} has only a {major}.x series, so runs-on-libc"
                f" {_version_tag(self.runs_on_libc)} names platform tags"
                f" nothing is built for"
            )
            raise ValueError(msg)
        _check_version_max("runs-on-libc", self.runs_on_libc)

    def _check_runs_on_macos(self) -> None:
        """Reject a macOS version below the oldest this arch can name a tag for."""
        if self.runs_on_macos is None:
            return
        floor = _MACOS_TAG_FLOOR[self.arch]
        if self.runs_on_macos < floor:
            msg = (
                f"runs-on-macos {_version_tag(self.runs_on_macos)} is below"
                f" {_version_tag(floor)}, the oldest macOS {self.arch} runs"
            )
            raise ValueError(msg)
        _check_version_max("runs-on-macos", self.runs_on_macos)

    @property
    def arch(self) -> str:
        """The architecture suffix used in platform tags."""
        return _PLATFORM_ARCH[self.platform_id]

    @property
    def label(self) -> str:
        """Render the spec as its label: the id, plus what sets it apart.

        The suffix encodes every knob the spec declares, so two distinct
        specs never render one label.  A spec that declares none is just
        its id.
        """
        parts: list[str] = [self.platform_id]
        if self.free_threaded:
            parts.append("-ft")

        # The family shows even without a version, so a musl target never
        # renders the label of a glibc one.
        if self.libc != DEFAULT_LIBC or self.runs_on_libc is not None:
            parts.append(f"-{self.libc}{_version_tag(self.runs_on_libc)}")

        fields = (
            ("macos", _version_tag(self.runs_on_macos)),
            ("rel", _escape_label_value(self.platform_release)),
            ("ver", _escape_label_value(self.platform_version)),
        )
        parts += [f"-{tag}{value}" for tag, value in fields if value]
        return "".join(parts)


# The arch suffix each platform id names in its wheel tags.
_PLATFORM_ARCH: dict[str, str] = {
    "linux_x86_64": "x86_64",
    "linux_aarch64": "aarch64",
    "macos_arm64": "arm64",
    "macos_x86_64": "x86_64",
    "windows_amd64": "amd64",
    "windows_arm64": "arm64",
    "linux_i686": "i686",
    "linux_armv7l": "armv7l",
}

# The kind behind :func:`platform_kind`, which both tag generation and the
# knob checks read.
_PLATFORM_KIND: dict[str, str] = {
    "linux_x86_64": "linux",
    "linux_aarch64": "linux",
    "macos_arm64": "macos",
    "macos_x86_64": "macos",
    "windows_amd64": "windows",
    "windows_arm64": "windows",
    "linux_i686": "linux",
    "linux_armv7l": "linux",
}


def _check_version_max(key: str, version: tuple[int, int]) -> None:
    """Reject a version so high the tag list it names would exhaust memory."""
    if max(version) > _MAX_VERSION_PART:
        msg = (
            f"{key} {_version_tag(version)} is higher than any release,"
            f" and one tag is named per version below it"
        )
        raise ValueError(msg)


def platform_kind(platform_id: str) -> str | None:
    """Return a platform id's kind, or ``None`` if nab does not know the id."""
    return _PLATFORM_KIND.get(platform_id)


def _version_tag(version: tuple[int, int] | None) -> str:
    """Render a ``(major, minor)`` pair as ``major.minor``, or ``""`` if unset."""
    return f"{version[0]}.{version[1]}" if version is not None else ""


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


def _manylinux_platform_tags(arch: str, glibc_version: tuple[int, int]) -> list[str]:
    """Generate the manylinux tags a glibc target accepts, newest first.

    Emits each legacy alias right after its equivalent PEP 600 tag so a
    legacy-named wheel ranks at its own glibc, matching packaging.tags.
    The target stops at the arch's oldest tag: 2.5 for x86, 2.17 otherwise.
    """
    major, minor = glibc_version
    floor_minor = (
        _X86_GLIBC2_MINOR_FLOOR
        if arch in _X86_MANYLINUX_ARCHS
        else _OTHER_GLIBC2_MINOR_FLOOR
    )
    out: list[str] = []
    for m in range(minor, floor_minor - 1, -1):
        out.append(f"manylinux_{major}_{m}_{arch}")
        legacy = _LEGACY_MANYLINUX.get((major, m))
        if legacy is not None:
            out.append(f"{legacy}_{arch}")
    return out


def _linux_platform_tags(
    arch: str, *, libc: Libc, libc_version: tuple[int, int]
) -> list[str]:
    """Generate plain linux plus the declared libc family's tags, for an arch.

    The order is ``packaging.tags.sys_tags``': the plain ``linux_<arch>``
    tag first, then the family's tags from the highest libc version down
    to the oldest the family and arch allow.  A tag set predicts which
    wheel an installer picks on the target, and an installer reads its
    tags off ``sys_tags``, which yields the plain tag before any
    manylinux one (``_linux_platforms``).  A declared target that ranked
    manylinux first would predict a wheel the target machine does not
    install.  It only shows on an index that serves plain ``linux_*``
    wheels; PyPI rejects them.

    A target links one C library, so a glibc target emits no musllinux
    tags and a musl target emits no manylinux tags; the other family's
    wheels do not run there.
    """
    major, minor = libc_version
    out = [f"linux_{arch}"]
    if libc == "musl":
        # musllinux_X_Y: PEP 656.
        out += [f"musllinux_{major}_{m}_{arch}" for m in range(minor, -1, -1)]
    else:
        # manylinux_X_Y: PEP 600.
        out += _manylinux_platform_tags(arch, libc_version)
    return out


def _platform_tags_for_spec(spec: PlatformSpec) -> list[str]:
    """Build the platform-tag list for ``spec`` in preference order."""
    kind = _PLATFORM_KIND[spec.platform_id]
    arch = spec.arch

    if kind == "linux":
        # Unset runs-on-libc names no system: enumerate to the max so a wheel
        # of any manylinux/musllinux level is a set member.
        libc_version = (
            spec.runs_on_libc
            if spec.runs_on_libc is not None
            else _libc_no_limit(spec.libc)
        )
        return _linux_platform_tags(arch, libc=spec.libc, libc_version=libc_version)

    if kind == "macos":
        # Unset runs-on-macos names no system; the sentinel accepts every macosx
        # tag.  mac_platforms treats the version as a max and yields older.
        macos_version = (
            spec.runs_on_macos if spec.runs_on_macos is not None else _MACOS_NO_LIMIT
        )
        return list(ptags.mac_platforms(version=macos_version, arch=arch))

    if kind == "windows":
        return [f"win_{arch}"]

    # The id-to-kind table holds three kinds; a fourth added without a tag
    # rule of its own is a programming error.
    msg = f"Unknown platform kind: {kind}"
    raise ValueError(msg)


# PyPy 7.3.x soabi, stable across every Python minor PyPy 3 ships
# (pp36..pp311 all use ``_pp73``); the abi tag is ``pypyXY_pp73``.
_PYPY_SOABI = "73"

# CPython carried the pymalloc flag in its ABI tag through 3.7, so its wheels
# are tagged cp37m, not cp37.  3.8 dropped the flag.  Left to packaging the
# flag would come from the config vars of the host interpreter, which says
# nothing about a declared target.
_PYMALLOC_ABI_SUFFIX = "m"
_PYMALLOC_LAST_VERSION = (3, 7)

# PEP 425 interpreter short code for PyPy, and the abi suffix CPython's
# free-threaded build carries (``cp313t``).  Both are read back off a
# host tag set to name the interpreter a bare ``sys_tags()`` came from.
# The PEP 425 interpreter prefix each implementation names itself with.
_IMPLEMENTATION_FOR_PREFIX: Mapping[str, str] = MappingProxyType(
    {"cp": "cpython", "pp": "pypy"}
)
_INTERPRETER_PREFIX_LEN = 2
_FREE_THREADED_ABI_SUFFIX = "t"

# The implementations nab has tag rules for, read off the prefix map so the
# two never name different sets.
_TAG_RULE_IMPLEMENTATIONS = frozenset(_IMPLEMENTATION_FOR_PREFIX.values())

# The platform tag of an interpreter-agnostic wheel.  It is not a
# machine, so it never seeds the platform axis of another target.
_ANY_PLATFORM = "any"


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
        raw = ptags.parse_tag(tag_str, limit=_MAX_WHEEL_TAGS)
    except Exception:  # noqa: BLE001 - never trust upstream parser
        return None
    return frozenset(_intern_tag(t) for t in raw)


@lru_cache(maxsize=65536)
def wheel_tag_set(filename: str) -> frozenset[Tag] | None:
    """Parse a wheel filename into the set of tags it advertises.

    Per PEP 427 the filename's last three dash-separated segments
    are ``python-abi-platform``; per PEP 425 each can be a
    dot-separated compressed set.  Returns ``None`` for a non-wheel
    filename or one with too few segments.

    The tag set is a pure function of the filename, so the whole
    result is memoized on it, and a repeated filename skips the string
    splitting.  The parse and Tag interning are shared further across
    distinct filenames by :func:`_parse_tag_str`, keyed on the tag
    suffix.
    """
    if not filename.endswith(".whl"):
        return None

    stem = filename[:-4]
    parts = stem.split("-")
    if len(parts) < _MIN_WHEEL_FILENAME_PARTS:
        return None

    return _parse_tag_str("-".join(parts[-3:]))


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

    # int() refuses a digit run past CPython's conversion limit.
    try:
        build_number = int(match.group(1))
    except ValueError:
        return (-1, "")

    return (build_number, match.group(2))


def _cpython_abi(py_version: tuple[int, int], *, free_threaded: bool) -> str:
    """Name the ABI a CPython target loads."""
    abi = f"cp{py_version[0]}{py_version[1]}"
    if free_threaded:
        return abi + _FREE_THREADED_ABI_SUFFIX
    if py_version <= _PYMALLOC_LAST_VERSION:
        return abi + _PYMALLOC_ABI_SUFFIX
    return abi


class _NamedWheel(Protocol):
    """A record carrying a wheel's filename.

    Both an index listing's wheel and a lockfile's wheel artefact
    satisfy it, so :meth:`TagSet.pick` orders either.
    """

    @property
    def filename(self) -> str:
        """The wheel's PEP 427 filename."""
        ...


_WheelT = TypeVar("_WheelT", bound=_NamedWheel)


@dataclass(frozen=True)
class TagSet:
    """The wheel tags one target accepts, in install-preference order.

    ``ordered`` is PEP 425 preference order, most specific first, so a
    tag's index in it is the target's preference for the wheels
    carrying it.
    """

    ordered: tuple[Tag, ...]

    @cached_property
    def members(self) -> frozenset[Tag]:
        """The accepted tags as a set, for compatibility tests."""
        return frozenset(self.ordered)

    @cached_property
    def rank(self) -> Mapping[Tag, int]:
        """Preference index per accepted tag; the lowest index wins."""
        return {tag: i for i, tag in enumerate(self.ordered)}

    def accepts(self, wheel_filename: str) -> bool:
        """Return True when the target can install ``wheel_filename``.

        A filename that is not a parseable wheel is never accepted.
        """
        wheel_tags = wheel_tag_set(wheel_filename)
        return wheel_tags is not None and not wheel_tags.isdisjoint(self.members)

    def wheel_rank(self, wheel_filename: str) -> tuple[int, tuple[int, str]] | None:
        """Return the target's install-preference key for a wheel, or None.

        The key is ``(min_rank_index, build_key)``: the index of the most
        specific tag the target accepts (lowest wins), then the PEP 427
        build key (an absent tag sorts lowest).  None means the target
        accepts no tag the wheel carries, or the filename is not a wheel.

        Two wheels the target's own rules cannot order return an equal,
        non-None key, so an equal, non-None pair is exactly a tie.
        """
        wheel_tags = wheel_tag_set(wheel_filename)
        if not wheel_tags:
            return None
        rank = self.rank
        rank_index = min((rank[t] for t in wheel_tags if t in rank), default=None)
        if rank_index is None:
            return None
        return (rank_index, _build_tag_sort_key(wheel_filename))

    def pick(self, wheels: Iterable[_WheelT]) -> _WheelT | None:
        """Pick the most-specific compatible wheel for the target, or None.

        Implements PEP 425 preference: wheels matching earlier
        (more-specific) tags in :attr:`ordered` win over those matching
        later (more-generic) tags.  Within the same tag rank, the wheel
        with the highest PEP 427 build tag wins (an absent tag sorts
        lowest); exact ties keep input order.

        ``wheels`` is anything carrying a ``filename``: an index
        listing's wheel or a lockfile's wheel artefact.
        """
        best_key: tuple[int, tuple[int, str]] | None = None
        best_wheel: _WheelT | None = None
        for wheel in wheels:
            key = self.wheel_rank(wheel.filename)
            if key is None:
                continue
            rank_index, build_key = key
            if (
                best_key is None
                or rank_index < best_key[0]
                or (rank_index == best_key[0] and build_key > best_key[1])
            ):
                best_key = key
                best_wheel = wheel
        return best_wheel

    @classmethod
    def for_spec(
        cls,
        *,
        python_version: str,
        spec: PlatformSpec,
        implementation: str = "cpython",
    ) -> TagSet:
        """Return the tags a declared (python, platform, impl) target accepts."""
        return cls(_ordered_tags_for_spec(python_version, spec, implementation))

    @classmethod
    def for_host(cls, *, tags_source: TagsSource = ptags.sys_tags) -> TagSet:
        """Return the tags the running interpreter accepts.

        ``packaging.tags.sys_tags`` already answers this for the live
        machine, libc probing and all, so nothing is re-derived here.
        """
        return cls(tuple(tags_source()))

    @classmethod
    def for_host_python(
        cls, python: str, *, tags_source: TagsSource = ptags.sys_tags
    ) -> TagSet:
        """Return the host's tags with the interpreter moved to ``python``.

        The machine is still the host, so its platform tags carry over
        unchanged (the ``any`` platform is not a machine and is
        re-derived with the rest of the interpreter-agnostic tags); only
        the interpreter and abi axes move.  This is what pip's
        ``--python-version`` targets.  The host's implementation and
        free-threaded build are read back off its own tags.
        """
        host = tuple(tags_source())
        if not host:
            msg = "tags_source yielded no tags, so the host platform is unknown"
            raise ValueError(msg)

        platforms = [
            platform
            for platform in dict.fromkeys(tag.platform for tag in host)
            if platform != _ANY_PLATFORM
        ]

        # sys_tags yields the most specific tag first, so the running
        # interpreter names itself in the first entry.
        implementation = _host_implementation(host[0].interpreter)

        # The host's free-threaded ABI only carries to a Python that has one:
        # ``cp310t`` has never existed, and a target advertising it matches no
        # wheel at all.  The declared-target path refuses the same combination.
        free_threaded = host[0].abi.endswith(
            _FREE_THREADED_ABI_SUFFIX
        ) and supports_free_threading(python)

        return cls(
            tuple(
                _tags_in_order(
                    python,
                    platforms,
                    implementation,
                    free_threaded=free_threaded,
                )
            )
        )


@cache
def _ordered_tags_for_spec(
    python_version: str, spec: PlatformSpec, implementation: str
) -> tuple[Tag, ...]:
    """Build a declared target's ordered tags.

    Cached on the three immutable inputs (str, frozen dataclass, str):
    the matrix rebuilds a target's tags per resolve pass, and the tag
    list is identical every time.
    """
    return tuple(
        _tags_in_order(
            python_version,
            _platform_tags_for_spec(spec),
            implementation,
            free_threaded=spec.free_threaded,
        )
    )


def _python_pair(python_version: str) -> tuple[int, int]:
    """Return ``(major, minor)`` for a ``3.12`` or ``3.12.5`` version."""
    release = Version(python_version).release
    major, minor = (*release, 0)[:2]
    return major, minor


def _host_implementation(interpreter: str) -> str:
    """Name the implementation a PEP 425 interpreter tag belongs to.

    An interpreter nab has no tag rules for cannot be resolved for: guessing
    CPython would hand it a ``cpXY`` tag set it can load none of.
    """
    prefix = interpreter[:_INTERPRETER_PREFIX_LEN]
    implementation = _IMPLEMENTATION_FOR_PREFIX.get(prefix)
    if implementation is None:
        msg = (
            f"unsupported interpreter {interpreter!r}:"
            f" nab resolves for CPython and PyPy"
        )
        raise ValueError(msg)
    return implementation


def supports_free_threading(python_version: str) -> bool:
    """Whether ``python_version`` has a free-threaded build at all (:pep:`703`)."""
    return _python_pair(python_version) >= FREE_THREADED_MIN_PYTHON


def _tags_in_order(
    python_version: str,
    platforms: Sequence[str],
    implementation: str,
    *,
    free_threaded: bool,
) -> Iterable[Tag]:
    """Yield the tags a target accepts in install preference order.

    CPython targets use ``packaging.tags.cpython_tags`` (cpXY-cpXY,
    cpXY-abi3 forward-compat, cpXY-none), with the abi named from the
    declared target (``cpXYt`` when free-threaded); left to packaging it
    would come from the config vars of the host interpreter.  PyPy targets
    cannot reuse ``cpython_tags`` (it forces the ``cp`` interpreter and abi3,
    which PyPy lacks), so their interpreter/abi/none tags are emitted
    directly.  Both then add the interpreter-agnostic tags (pyXY-none-any,
    py3-none-any, ...).
    """
    py_version = _python_pair(python_version)
    major, minor = py_version

    if implementation == "pypy":
        interpreter = f"pp{major}{minor}"
        abi = f"pypy{major}{minor}_pp{_PYPY_SOABI}"
        for platform_ in platforms:
            yield ptags.Tag(interpreter, abi, platform_)
        for platform_ in platforms:
            yield ptags.Tag(interpreter, "none", platform_)
        # ``sys_tags`` hands ``compatible_tags`` the major-only ``pp3``, so a
        # real PyPy advertises ``pp3-none-any`` and never ``ppXY-none-any``.
        compat_interpreter = f"pp{major}"
    else:
        interpreter = f"cp{major}{minor}"
        abi = _cpython_abi(py_version, free_threaded=free_threaded)
        yield from ptags.cpython_tags(
            python_version=py_version, abis=[abi], platforms=platforms
        )
        compat_interpreter = interpreter

    yield from ptags.compatible_tags(
        python_version=py_version,
        interpreter=compat_interpreter,
        platforms=platforms,
    )


@cache
def _python_axis_tags(
    python_version: str, implementation: str
) -> frozenset[tuple[str, str]]:
    """Return the ``(interpreter, abi)`` pairs one Python axis accepts.

    :func:`_tags_in_order` pairs the same interpreters and abis with every
    platform it is given, so projecting the platform away leaves exactly the
    axes the Python version and the implementation decide.  Both builds are
    admitted where :pep:`703` offers one, since free-threading is a property
    of the interpreter binary that a marker environment does not name.
    """
    builds = (False, True) if supports_free_threading(python_version) else (False,)
    return frozenset(
        (tag.interpreter, tag.abi)
        for free_threaded in builds
        for tag in _tags_in_order(
            python_version,
            (_ANY_PLATFORM,),
            implementation,
            free_threaded=free_threaded,
        )
    )


def python_axis_accepts(
    python_version: str, implementation: str, wheel_filename: str
) -> bool:
    """Whether a Python axis accepts a wheel filename's tags, on any platform.

    The platform axis is ignored.  That is what a target whose tags a marker
    overlay disowned can still be asked: an overlay cannot rebuild the
    platform tags, but ``python_version`` and ``implementation_name`` survive
    it (see :meth:`~nab_python.target.ResolveTarget.with_marker_overrides`).
    A filename that does not parse as a wheel is admitted; it carries no tags
    to reject it by.

    An implementation nab has no tag rules for gets no opinion either, for the
    same reason :func:`_host_implementation` refuses to guess one: CPython's
    tag rules would admit the wheels such a target cannot load and reject the
    ones it can.  Only a marker overlay reaches here with one; every other
    route builds the Python axis from an interpreter nab has rules for.
    """
    if implementation not in _TAG_RULE_IMPLEMENTATIONS:
        return True
    tags = wheel_tag_set(wheel_filename)
    if not tags:
        return True
    accepted = _python_axis_tags(python_version, implementation)
    return any((tag.interpreter, tag.abi) in accepted for tag in tags)
