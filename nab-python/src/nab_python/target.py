"""The environments one resolve runs against.

A :class:`ResolveTarget` is a complete PEP 508 marker environment plus
the wheel tags that environment accepts.  A resolve runs against a list
of them: the host interpreter alone, the one target
``[tool.nab.environment]`` declares, or one per (python, platform,
implementation) point a :class:`Matrix` expands to.  They all feed the
same provider, so the resolver has a single notion of "the environment
we are resolving for".

A declared target synthesizes its markers from the platform and
implementation it names, never from the interpreter running nab: a
matrix that models linux/3.11 must answer the same way on a macOS host.
A host target takes them from ``packaging.markers.default_environment``
untouched, and says so through :attr:`ResolveTarget.host_faithful`,
which is what tells a caller whether running a build backend here would
report metadata for the target or for someone else.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ._conflict_kind import (
    EMPTY_MEMBERSHIP_SETS,
    MARKER_VARIABLE_FOR_KIND,
    UnevaluableMarkerError,
)
from ._vendor.packaging import tags as ptags
from ._vendor.packaging.markers import Marker, default_environment
from ._vendor.packaging.markersets import variable_names
from ._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from ._vendor.packaging.version import InvalidVersion, Version
from .tags import (
    FREE_THREADED_MIN_PYTHON,
    PlatformSpec,
    TagSet,
    supports_free_threading,
)

if TYPE_CHECKING:
    from ._vendor.packaging.ranges import VersionRange
    from .tags import TagsSource


__all__ = [
    "IMPLEMENTATION_MARKERS",
    "KNOWN_PYTHON_MINORS",
    "PEP508_MARKER_VARIABLES",
    "PLATFORM_MARKERS",
    "UNBOUNDABLE_MARKER_VARIABLES",
    "EnvironmentSource",
    "Matrix",
    "NonIntervalMarkerError",
    "ResolveTarget",
    "UnevaluableMarkerError",
    "apply_python_axis_overlay",
    "check_free_threaded",
    "declared_environment",
    "declared_range_marker",
    "environment_declaration",
    "host_environment",
    "marker_variables",
    "micro_boundary_points",
    "python_axis_environment",
    "slices_from_points",
    "unboundable_variables",
]


# Where a host marker environment comes from.  Injected so a caller
# (and every test) can name the interpreter it means instead of the one
# running.
EnvironmentSource = Callable[[], Mapping[str, object]]


# The OS/arch PEP 508 marker values per matrix platform id.  These are
# the most common values for the named machine; they drive marker
# evaluation only, never the resolver's own constraints.
PLATFORM_MARKERS: dict[str, dict[str, str]] = {
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
    "windows_arm64": {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "platform_machine": "ARM64",
        "os_name": "nt",
    },
    "linux_i686": {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "i686",
        "os_name": "posix",
    },
    "linux_armv7l": {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "armv7l",
        "os_name": "posix",
    },
}


# The interpreter-identity PEP 508 marker values per implementation.
IMPLEMENTATION_MARKERS: dict[str, dict[str, str]] = {
    "cpython": {
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
    "pypy": {
        "platform_python_implementation": "PyPy",
        "implementation_name": "pypy",
    },
}


# The PEP 508 markers a wheel-tag set encodes: the python version, the
# interpreter, and the machine.  A marker overlay that moves one of these
# leaves the tags describing a different target than the markers do.
_TAG_AXIS_MARKERS: tuple[str, ...] = (
    "implementation_name",
    "os_name",
    "platform_machine",
    "platform_python_implementation",
    "platform_system",
    "python_version",
    "sys_platform",
)


# PEP 425 interpreter short tag per implementation, used in the label so
# targets differing only by implementation stay distinct.
_IMPLEMENTATION_PREFIX: dict[str, str] = {"cpython": "py", "pypy": "pp"}
_DEFAULT_IMPLEMENTATION_PREFIX = "py"

# The platform half of a label naming the machine nab itself runs on.
_HOST_PLATFORM_LABEL = "host"

# PEP 508 ``python_version`` is the ``major.minor`` pair;
# ``python_full_version`` is the full ``major.minor.micro`` release.
_PYTHON_VERSION_PARTS = 2
_PYTHON_FULL_VERSION_PARTS = 3

# The Python minors a :class:`Matrix` can expand to.  A minor outside this
# set cannot be modelled (nab has no tag knobs for it), so a declared range
# that names one raises rather than silently skipping it.
KNOWN_PYTHON_MINORS: tuple[str, ...] = (
    "3.8",
    "3.9",
    "3.10",
    "3.11",
    "3.12",
    "3.13",
    "3.14",
    "3.15",
)


# Every environment variable PEP 508 defines.  A lock declares the target's
# value for each one the resolve consulted, so the set has to be the spec's.
PEP508_MARKER_VARIABLES: frozenset[str] = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "python_version",
        "sys_platform",
    }
)

# ``platform_release`` and ``platform_version`` name one machine's kernel
# build (``6.18.33-microsoft-standard-WSL2``), so a lock cannot bound them:
# declaring the resolving machine's value would refuse every other machine,
# and omitting it leaves the axis open.  A marker that consults one is
# reported to the user rather than declared.
UNBOUNDABLE_MARKER_VARIABLES: frozenset[str] = frozenset(
    {"platform_release", "platform_version"}
)


def unboundable_variables(target: ResolveTarget) -> frozenset[str]:
    """Return the variables the lock cannot bound for ``target``.

    Always the kernel axes.  On a non-CPython target ``implementation_version``
    joins them: :func:`declared_environment` sets it to the target's Python
    level, but a released PyPy reports its own release (7.3.x) there, so a
    clause bounded on the synthetic value would refuse the very interpreter
    the lock was resolved for.  CPython's ``implementation_version`` is its
    Python micro, so it stays declarable by constraint.
    """
    if target.implementation == "cpython":
        return UNBOUNDABLE_MARKER_VARIABLES
    return UNBOUNDABLE_MARKER_VARIABLES | {"implementation_version"}


# Declared whether or not a marker consults them: these are the axes the
# package set was chosen for, so a lock that leaves them open is one any
# environment would accept.
_ALWAYS_DECLARED: tuple[str, ...] = (
    "python_version",
    "sys_platform",
    "platform_machine",
)

# The variables a lock never declares by value: their bounds come only from a
# slice's ``python_full_version`` clauses (see :func:`slices_from_points`).
# Both carry a micro release, and on CPython they carry the same one
# (``implementation_version`` comes from ``sys.implementation.version``), so
# declaring either by value would pin the lock to a single patch release.  A
# consulted marker on one splits the minor at its boundary instead, and each
# slice emits the bounds that fence it off; an unsplit minor emits neither.
_BY_CONSTRAINT = ("python_full_version", "implementation_version")

# One ``lhs op rhs`` comparison of a marker, matched against the string form
# :func:`Marker.__str__` normalises to: an operand is either a quoted literal
# or a bare variable token, which is what tells the two apart.  Both quote
# styles are matched, since packaging emits a value holding a double quote
# single-quoted.  Ordered so the two-character operators win over their
# prefixes.
_MARKER_OPERAND = r'"[^"]*"|\'[^\']*\'|[A-Za-z_][A-Za-z0-9_]*'
_MARKER_CLAUSE_RE = re.compile(
    rf"(?P<lhs>{_MARKER_OPERAND})\s*"
    r"(?P<op>===|==|!=|<=|>=|~=|<|>|not\s+in|in)\s*"
    rf"(?P<rhs>{_MARKER_OPERAND})"
)


def marker_variables(marker_text: str) -> frozenset[str]:
    """Return the PEP 508 environment variables ``marker_text`` names.

    Parses the marker and reads the variables its operands name, keeping
    only the PEP 508 environment variables: the set variables ``extra`` /
    ``extras`` / ``dependency_groups`` are not lock-environment axes (a lock
    cannot pin them), so they drop out.  A quoted name on the right of a
    variable comparison (``sys_platform == "python_version"``) is a value the
    marker compares against, not a variable it reads, and is not returned; but
    packaging reads the right operand of a literal-only comparison
    (``"3.9" == "python_version"``) as an environment key, so that name is
    returned.  The result is an
    over-approximation of semantic support: a tautology such as
    ``python_version >= "0"`` still reports ``python_version``.
    Over-declaring narrows the lock, which is the safe direction to be
    wrong in.

    Every call site passes a serialised :class:`Marker`; an input that is not
    a valid marker raises :class:`InvalidMarker`.
    """
    return variable_names(marker_text) & PEP508_MARKER_VARIABLES


def environment_declaration(target: ResolveTarget, consulted: Iterable[Marker]) -> str:
    """Render the PEP 751 ``environments`` marker for ``target``.

    Declares :data:`_ALWAYS_DECLARED`, ``implementation_name`` when the
    target names a non-default interpreter (see
    :attr:`ResolveTarget.declares_implementation`), and every variable the
    ``consulted`` markers named.  The resolve dropped every dependency whose
    marker was False here, so a lock that let an installer answer one of
    those variables differently would miss deps; the declaration refuses it.

    Most variables are declared by value (a ``platform_system`` marker pins
    the OS).  The :data:`_BY_CONSTRAINT` variables (``python_full_version``,
    and ``implementation_version`` on CPython) are not, since pinning the
    micro release would refuse the interpreters the resolve ran for.  A minor
    target is a micro interval: a consulted marker that splits it (see
    :func:`micro_boundary_points`) leaves each slice its
    :attr:`~ResolveTarget.micro_clauses` bounds and the row emits those,
    while an unsplit minor emits none and stays a plain ``python_version``
    row.  On CPython ``implementation_version`` tracks ``python_full_version``,
    so a marker consulting it gets the same bounds mirrored onto its name.

    The variables :func:`unboundable_variables` names are dropped: the kernel
    axes always, and ``implementation_version`` on a non-CPython target (its
    value there is the Python level, not the interpreter's release), so a dep
    gated on that axis may be missed at install.
    """
    texts = sorted({str(marker) for marker in consulted})
    variables: set[str] = set()
    for text in texts:
        variables |= marker_variables(text)

    always = list(_ALWAYS_DECLARED)
    if target.declares_implementation:
        always.append("implementation_name")
    by_value = variables - set(always) - unboundable_variables(target)
    names = [*always, *sorted(by_value - set(_BY_CONSTRAINT))]

    clauses = [f'{name} == "{target.marker_env[name]}"' for name in names]

    # A micro-slice target (see :func:`slices_from_points`) carries the
    # python_full_version bounds of its slice explicitly, so the environment
    # row covers exactly that slice.  On CPython implementation_version equals
    # python_full_version, so a marker that consulted it gets the same bounds
    # mirrored onto its own name.
    clauses.extend(target.micro_clauses)
    if "implementation_version" in variables and target.implementation == "cpython":
        clauses.extend(
            clause.replace("python_full_version", "implementation_version")
            for clause in target.micro_clauses
        )
    return " and ".join(clauses)


def declared_range_marker(target: ResolveTarget) -> str:
    """Render the environment a target resolved for, on its whole declared range.

    Pins every boundable environment axis to the target's value. A minor
    interval leaves the micro release open, so python_version pins the whole
    minor; a whole target (host, or a python-patches micro pin) adds the
    concrete python_full_version it resolved at.
    """
    skip = UNBOUNDABLE_MARKER_VARIABLES | set(_BY_CONSTRAINT)
    clauses = [
        f'{name} == "{value}"'
        for name, value in sorted(target.marker_env.items())
        if name not in skip
    ]
    if not target.is_minor_interval:
        clauses.append(f'python_full_version == "{target.python_full_version}"')
    return " and ".join(clauses)


def _clause_parts(atom: re.Match[str]) -> tuple[str, str, str]:
    """Return one clause's operands and its operator, whitespace normalized."""
    lhs, op, rhs = atom.group("lhs", "op", "rhs")
    return lhs, " ".join(op.split()), rhs


# The operators nab cannot tile into a micro interval: a membership or verbatim
# ``===`` test on python_full_version is not uniform across a slice.  A
# consulted marker using one on a minor interval is a loud crash.
_NON_INTERVAL_OPERATORS = frozenset({"===", "in", "not in"})

# The operators whose boundary lands at the literal.  A prerelease literal there
# carves a slice whose release-floor representative sits outside it, so a
# prerelease literal on one of these, strictly inside the minor, is not
# renderable and crashes.  ``<=``/``>`` land after the literal at a real
# release, so a prerelease there maps cleanly to that release.
_AT_LITERAL_OPERATORS = frozenset({"<", ">=", "==", "!=", "~="})

# ``<`` is dropped: a post-release sorts just above its release, so
# ``< "3.12.4.post1"`` lands its boundary on the next real micro (3.12.5) and
# tiles cleanly.  The other at-literal operators snap a post's boundary onto a
# micro whose truth does not flip there, so a post-release crashes on those.
_POST_AT_LITERAL_OPERATORS = _AT_LITERAL_OPERATORS - frozenset({"<"})

# PEP 508 lets either operand be the variable, and packaging preserves the
# written order, so ``python_full_version < "3.10.2"`` and its literal-first
# equivalent ``"3.10.2" > python_full_version`` both reach the scanner.  When
# the literal is on the left, an ordered or symmetric operator is mirrored back
# to the variable-on-left form; ``~=``/``===`` and the membership operators are
# absent because they cannot be mirrored, so a literal-first one of those is not
# tileable.
_MIRRORED_OPERATOR: dict[str, str] = {
    "<": ">",
    ">": "<",
    "<=": ">=",
    ">=": "<=",
    "==": "==",
    "!=": "!=",
}


class NonIntervalMarkerError(ValueError):
    """A consulted version marker cannot tile a minor interval.

    A minor target stands for every micro of its minor, and each consulted
    ``python_full_version`` (or, on CPython, ``implementation_version``)
    comparison has to partition that interval so every real interpreter reads
    it the same way its slice does.  A membership (``in``/``not in``), a
    verbatim ``===``, a non-version string comparison, a comparison against
    another variable, a pre- or post-release version literal that would carve
    a slice off a real micro, or a literal PEP 440 refuses under the operator
    it is written with (``< "3.12.*"``, ``~= "3"``) cannot be tiled, so the
    resolve stops loudly rather than pin the whole minor to one synthetic
    answer.
    """


def _non_interval(
    parts: tuple[str, str, str], target: ResolveTarget
) -> NonIntervalMarkerError:
    """Build the crash for a clause that cannot tile ``target``'s minor."""
    lhs, op, rhs = parts
    return NonIntervalMarkerError(
        f"consulted marker clause {lhs} {op} {rhs} cannot tile the"
        f" {target.label} minor interval: no micro boundary the lock can"
        " render comes from a python_full_version membership, a verbatim ===,"
        " a comparison against a variable, a pre- or post-release comparison,"
        " or a literal the operator cannot spell as a version bound"
    )


def slices_from_points(
    target: ResolveTarget, points: Sequence[Version]
) -> list[ResolveTarget]:
    """Partition ``target``'s minor into one slice per micro interval.

    ``points`` are the in-minor releases the micro line is cut at (see
    :func:`micro_boundary_points`), sorted ascending.  They partition
    ``[{minor}.0, inf)`` into intervals; each interval becomes ``target``
    moved onto its lower-bound release, carrying the python_full_version
    bounds that fence it off (see :meth:`ResolveTarget.with_micro_slice`).
    Every marker answers unambiguously at an interval's lower bound, so that
    release is where the slice resolves.  Each slice declares its own
    environment row and pins, so a marker that genuinely splits the micros is
    honoured per slice rather than lifted onto the whole minor.

    Returns ``[target]`` unchanged when ``points`` is empty: nothing cut the
    minor, so the whole minor resolves at once.
    """
    if not points:
        return [target]
    floor = Version(f"{target.python_version}.0")
    reps = [floor, *points]
    lowers: list[Version | None] = [None, *points]
    uppers: list[Version | None] = [*points, None]

    slices: list[ResolveTarget] = []
    for rep, low, high in zip(reps, lowers, uppers, strict=True):
        clauses: list[str] = []
        if low is not None:
            clauses.append(f'python_full_version >= "{_dev0(low)}"')
        if high is not None:
            clauses.append(f'python_full_version < "{high}"')
        slices.append(target.with_micro_slice(str(rep), tuple(clauses)))
    return slices


def _dev0(version: Version) -> str:
    """Return the ``.dev0`` boundary form of a release ``version``.

    The canonical disjoint and exhaustive split pair at a boundary ``X`` is
    ``< "X"`` on the lower side and ``>= "X.dev0"`` on the upper: a verbatim
    ``< "X"`` / ``>= "X"`` pair leaves a gap at prereleases of ``X`` (packaging
    carries a ``< X`` upper as ``X.dev0``), so the ``>=`` lower edge is snapped
    to ``X.dev0`` to meet the adjacent ``< "X"`` exactly.  The suffix is a
    syntactic form on a packaging-parsed version, validated by re-parsing.
    """
    return str(Version(f"{version}.dev0"))


def _scanned_version_variables(target: ResolveTarget) -> frozenset[str]:
    """Return the version variables whose comparisons split ``target``.

    Always ``python_full_version``.  On CPython ``implementation_version``
    tracks it, so a comparison on it mints the same boundary; on other
    implementations its value is synthetic (:func:`unboundable_variables`), so
    it is never split and its markers stay a known limitation.
    """
    if target.implementation == "cpython":
        return frozenset({"python_full_version", "implementation_version"})
    return frozenset({"python_full_version"})


def micro_boundary_points(
    target: ResolveTarget, consulted: Iterable[Marker]
) -> list[Version]:
    """Return the in-minor releases ``consulted`` cuts ``target``'s minor at.

    A minor target stands for every micro of its minor (its
    ``python_full_version`` is the synthesized ``{minor}.0`` floor).  A
    consulted marker comparing ``python_full_version`` against a literal inside
    the minor (``python_full_version < "3.10.2"``) needs different package sets
    on each side, so the release it flips at is a point the micro line is split
    on (see :func:`slices_from_points`).  On CPython ``implementation_version``
    tracks ``python_full_version``, so a comparison on it mints the same
    boundary.

    The per-operator boundary comes from the edges of the range's
    :meth:`~packaging.ranges.VersionRange.release_intervals`:
    ``<``/``>=`` at the literal, ``<=``/``>`` at the release just after it, and
    ``==``/``!=``/``~=``/``== V.*`` at each edge of the region they name.  A
    boundary outside the minor or at its floor cuts nothing.

    An operator that cannot tile the interval raises
    :class:`NonIntervalMarkerError` (see there); with the whole-minor pin gone,
    an untileable marker stops the resolve loudly.  A whole target (a host, or
    a python-patches pin) is not an interval and is never cut.

    Markers are scanned in text order, so the clause a crash names is the same
    every run even when ``consulted`` is unordered.
    """
    if not target.is_minor_interval:
        return []
    minor_release = Version(target.python_version).release[:_PYTHON_VERSION_PARTS]
    floor = Version(f"{target.python_version}.0")
    scanned = _scanned_version_variables(target)

    points: set[Version] = set()
    for marker in sorted(consulted, key=str):
        for atom in _MARKER_CLAUSE_RE.finditer(str(marker)):
            points.update(
                _clause_boundary_points(
                    _clause_parts(atom), scanned, minor_release, floor, target
                )
            )
    return sorted(points)


def _clause_boundary_points(
    parts: tuple[str, str, str],
    scanned: frozenset[str],
    minor_release: tuple[int, ...],
    floor: Version,
    target: ResolveTarget,
) -> set[Version]:
    """Return the in-minor release boundaries one clause cuts the minor at.

    Empty when the clause names no scanned version variable, or its boundaries
    fall outside the minor or at its floor.  Raises
    :class:`NonIntervalMarkerError` when the clause names a scanned variable
    but cannot tile the interval.
    """
    parsed = _clause_interval_literal(parts, scanned, target)
    if parsed is None:
        return set()
    op, specifier, version = parsed

    if (version.is_prerelease and op in _AT_LITERAL_OPERATORS) or (
        version.is_postrelease and op in _POST_AT_LITERAL_OPERATORS
    ):
        # An at-literal boundary lands on the literal, and a pre- or post-release
        # literal falls between two micros, so no micro reads it the way its slice
        # would.  Interior to the minor that cannot be tiled and crashes; below the
        # floor or in another minor it is uniform and splits nothing.  The literal
        # itself decides which, not its release form: a prerelease sorts just below
        # its release and a post just above, so ``3.12.0a1`` sits below the floor
        # while ``3.12.0.post1`` sits above it.
        if _in_minor(version, minor_release, floor):
            raise _non_interval(parts, target)
        return set()

    intervals = specifier.to_range().release_intervals(_PYTHON_FULL_VERSION_PARTS)
    return {
        edge
        for lower, upper in intervals
        for edge in (lower, upper)
        if edge is not None and _in_minor(edge, minor_release, floor)
    }


def _in_minor(point: Version, minor_release: tuple[int, ...], floor: Version) -> bool:
    """Whether ``point`` is an interior micro of the minor above its floor.

    An epoch sorts above every version of a lower one, so a boundary with a
    different epoch is outside the minor whatever its release says:
    ``1!3.12.4`` is not in ``[3.12.0, 3.13.0)``.
    """
    return (
        point.epoch == floor.epoch
        and point.release[:_PYTHON_VERSION_PARTS] == minor_release
        and point > floor
    )


def _clause_interval_literal(
    parts: tuple[str, str, str], scanned: frozenset[str], target: ResolveTarget
) -> tuple[str, SpecifierSet, Version] | None:
    """Return ``(op, specifier, version)`` for a version-boundary clause.

    ``None`` when the clause names no scanned version variable.  Raises
    :class:`NonIntervalMarkerError` when it names one but cannot tile an
    interval.  A literal-first clause has an ordered or symmetric operator
    mirrored back to variable-on-left form; ``~=``/``===`` and the membership
    operators cannot be mirrored, so a literal-first one of those is untileable.

    The specifier is built here rather than by the caller because PEP 508
    accepts literals PEP 440 refuses under the operator they are written with.
    A ``.*`` suffix is a specifier only under ``==``/``!=``, and ``~=`` needs
    two release components, so ``< "3.12.*"`` and ``~= "3"`` are valid markers
    with no specifier form; building it is what tells them from the clauses
    that have one.
    """
    lhs, op, rhs = parts
    if lhs in scanned:
        literal = rhs
    elif rhs in scanned:
        literal = lhs
        mirrored = _MIRRORED_OPERATOR.get(op)
        if mirrored is None:
            raise _non_interval(parts, target)
        op = mirrored
    else:
        return None

    if op in _NON_INTERVAL_OPERATORS or not literal.startswith('"'):
        raise _non_interval(parts, target)
    raw = literal.strip('"')
    base = raw.removesuffix(".*")
    try:
        version = Version(base)
        specifier = SpecifierSet(f"{op}{raw}")
    except (InvalidVersion, InvalidSpecifier):
        raise _non_interval(parts, target) from None
    return op, specifier, version


def host_environment(
    env_source: EnvironmentSource = default_environment,
) -> dict[str, str]:
    """Return the host's PEP 508 marker environment as a plain string dict.

    ``default_environment`` returns a TypedDict whose ``.items()`` view
    widens the values to ``object``, so rebuild it as ``dict[str, str]``
    the callers can overlay onto.
    """
    return {key: value for key, value in env_source().items() if isinstance(value, str)}


def python_axis_environment(python_version: str) -> dict[str, str]:
    """Map an explicit Python version to its PEP 508 marker keys.

    ``python_version`` is padded to two components and
    ``python_full_version`` to three so patch-precision markers evaluate
    the same here as in the universal matrix. Raises ``InvalidVersion``
    if the input is not a version.
    """
    try:
        parsed = Version(python_version)
    except InvalidVersion:
        msg = f"python_version {python_version!r} is not a valid version"
        raise InvalidVersion(msg) from None
    release = parsed.release
    minor = ".".join(str(part) for part in (*release, 0)[:_PYTHON_VERSION_PARTS])
    if len(release) >= _PYTHON_FULL_VERSION_PARTS:
        full = python_version
    else:
        # Pad the release to three components, keeping the epoch and any
        # prerelease/post/dev/local tag, which live outside ``release``.
        epoch = f"{parsed.epoch}!" if parsed.epoch else ""
        padded = ".".join(
            str(part) for part in (*release, 0, 0)[:_PYTHON_FULL_VERSION_PARTS]
        )
        suffix = str(parsed)[len(parsed.base_version) :]
        full = f"{epoch}{padded}{suffix}"
    return {"python_version": minor, "python_full_version": full}


def apply_python_axis_overlay(
    environment: dict[str, str], overlay: Mapping[str, str]
) -> None:
    """Merge ``overlay`` into ``environment``, keeping the python axis in sync.

    When the overlay moves only ``python_version`` (or only
    ``python_full_version``) the untouched key would keep the host patch
    level and the two would describe different interpreters. Re-derive both
    from whichever axis key the overlay supplies (``python_full_version``
    wins when both are present), so an overlay of ``python_version`` ``3.8``
    yields ``python_full_version`` ``3.8.0`` like the universal matrix. On
    CPython ``implementation_version`` equals ``python_full_version``, so move
    it with the axis; other implementations version separately and keep their
    host value unless the overlay sets it. Non-axis keys the overlay sets are
    kept verbatim; the ``python_version``/``python_full_version`` pair is always
    the derived one, so a patch-precision ``python_version`` (e.g. ``3.10.5``)
    normalizes to major.minor.
    """
    source = overlay.get("python_full_version") or overlay.get("python_version")
    if source is None:
        environment.update(overlay)
        return

    axis = python_axis_environment(source)
    if environment.get("implementation_name") == "cpython":
        environment["implementation_version"] = axis["python_full_version"]
    environment.update(overlay)
    environment.update(axis)


@dataclass(frozen=True)
class ResolveTarget:
    """One environment a resolve runs against: markers, wheel tags, a name.

    ``label`` names the target (``host``, ``py312-linux_x86_64``) and is
    the key a universal lock records its pins under, so it carries every
    axis that makes one target differ from another, including the
    conflict-fork ``selection``.

    ``selection`` is the conflict-fork this target belongs to: the
    ``(kind, name)`` members (``kind`` is ``"extra"`` or ``"group"``)
    active in this fork's resolve, empty for an unforked one.  When set,
    it adds a ``'name' in extras`` / ``'name' in dependency_groups``
    clause to :attr:`marker_string`; the lockfile emitter builds the
    per-package marker from the members themselves, keeping only the sets
    the package varies over and negating their co-members.

    ``platform_spec`` is set on a declared (matrix) target and names the
    tag knobs it was expanded from; a host target has none.
    ``multi_implementation`` says the matrix models more than one
    implementation, which pins ``implementation_name`` on
    :attr:`environment_marker_string` so the CPython and PyPy entries
    for one python/platform stay mutually exclusive.

    ``tags_faithful`` says :attr:`tags` still describes the machine
    :attr:`marker_env` does.  Only :meth:`with_marker_overrides` can
    break that, and a provider given such a target filters no wheel by
    tag: see there.
    """

    label: str
    marker_env: Mapping[str, str] = field(compare=False)
    tags: TagSet = field(compare=False)
    host_faithful: bool = field(compare=False)
    selection: tuple[tuple[str, str], ...] = ()
    platform_spec: PlatformSpec | None = field(default=None, compare=False)
    multi_implementation: bool = field(default=False, compare=False)
    tags_faithful: bool = field(default=True, compare=False)
    # python_full_version was pinned to a concrete micro (a matrix python-patches
    # value or a declared patch level), not synthesized as a bare minor's ``.0``
    # floor.  A pinned ``X.Y.0`` equals that floor by value, so only this flag
    # tells the two apart.
    concrete_micro: bool = field(default=False, compare=False)
    # The python_full_version clauses bounding this target's micro slice, set
    # by :func:`slices_from_points` when a consulted marker splits the minor.
    # Empty for an ordinary target.  Appended to
    # :attr:`environment_marker_string` so the per-package pins of one slice
    # do not leak onto another slice of the same python/platform.
    micro_clauses: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        """Reject a python the resolve cannot compare Requires-Python against.

        Every candidate's ``Requires-Python`` is tested against this target,
        so an unparseable version has to fail here, naming itself, rather than
        as an ``InvalidVersion`` raised per candidate deep in the listing.
        """
        try:
            Version(self.python_full_version)
        except InvalidVersion as exc:
            msg = (
                f"target {self.label!r} names python_full_version"
                f" {self.python_full_version!r}, which is not a PEP 440 version"
            )
            raise ValueError(msg) from exc

    @property
    def python_version(self) -> str:
        """The PEP 508 ``python_version``: the target's ``major.minor``."""
        return self.marker_env["python_version"]

    @property
    def python_full_version(self) -> str:
        """The PEP 508 ``python_full_version``: the target's full release."""
        return self.marker_env["python_full_version"]

    @property
    def python_release(self) -> Version:
        """The release a ``Requires-Python`` specifier is compared against.

        ``python_full_version`` is the PEP 508 marker value, so on a release
        candidate it carries the ``rc``.  A specifier admits no prerelease
        unless it names one, so comparing that value directly would exclude
        every distribution requiring the very release the interpreter is a
        candidate for.  pip compares ``sys.version_info``, and so does this.
        """
        return Version(Version(self.python_full_version).base_version)

    @property
    def is_minor_interval(self) -> bool:
        """Whether this target is a bare minor resolved as a micro interval.

        A host reports a real interpreter and a python-patches pin names a
        concrete micro; both are whole and resolve at one point.  A bare minor
        synthesizes ``{minor}.0`` and stands for every real micro of the minor,
        so the micro line can be split on it and Requires-Python is answered
        against the whole minor, not the synthetic floor.  A slice off that
        minor carries ``micro_clauses`` and still stands for every interpreter
        its bounds admit, so it too is an interval answering Requires-Python at
        the same whole minor.

        A pinned ``X.Y.0`` reads as the synthetic floor by string alone, so the
        concrete-micro flag, not the value, decides: a python-patches pin is
        whole even when its micro is ``.0``.
        """
        if self.host_faithful or self.concrete_micro:
            return False
        if self.micro_clauses:
            return True
        return self.python_full_version == f"{self.python_version}.0"

    @property
    def minor_range(self) -> VersionRange:
        """The ``[X.Y.0, X.(Y+1).0)`` range this target's minor covers."""
        release = Version(self.python_version).release
        major, minor = release[0], release[1]
        return SpecifierSet(f">={major}.{minor}.0,<{major}.{minor + 1}.0").to_range()

    def admits_requires_python(self, spec: SpecifierSet) -> bool:
        """Whether a candidate's ``Requires-Python`` admits this target.

        A whole target (host or a concrete micro) is admitted when its single
        release satisfies ``spec``.  A minor interval is admitted when ``spec``
        overlaps the whole minor, so a micro floor like ``>= "3.13.2"`` admits
        the 3.13 minor instead of excluding it at the synthetic ``.0`` floor.
        The test is range overlap, not a scalar ``in``: ``>= "3.13.2"`` would
        exclude both ``3.13`` and ``3.13.0``.
        """
        if self.is_minor_interval:
            return not spec.to_range().intersection(self.minor_range).is_empty
        return self.python_release in spec

    @property
    def implementation(self) -> str:
        """The target's interpreter implementation (``cpython``, ``pypy``)."""
        return self.marker_env["implementation_name"]

    @property
    def selection_slug(self) -> str:
        """The conflict fork this target belongs to, as a label slug.

        ``extra-cpu``, ``group-black22.group-isort5``, empty when
        unforked.  It is the tail of :attr:`label` without the leading
        separator.
        """
        return _selection_slug(self.selection)

    @property
    def platform_id(self) -> str:
        """The matrix platform this target names, or ``host`` for the host."""
        if self.platform_spec is None:
            return _HOST_PLATFORM_LABEL
        return self.platform_spec.platform_id

    def env_with_membership(self) -> dict[str, str | frozenset[str]]:
        """Return the marker env seeded with the empty membership sets.

        ``extras`` and ``dependency_groups`` are defined only when
        consuming a lockfile, so a dependency marker that tests one
        evaluates False here rather than raising (see
        :data:`~nab_python._conflict_kind.EMPTY_MEMBERSHIP_SETS`).
        """
        return {**self.marker_env, **EMPTY_MEMBERSHIP_SETS}

    @property
    def declares_implementation(self) -> bool:
        """Whether the interpreter is an axis this target has to name.

        CPython alone is the default, so a lone CPython target leaves the
        axis open.  A matrix modelling more than one implementation, or a
        target on any other interpreter, has to say which one it is, or its
        markers would also select the interpreter it is not.
        """
        return self.multi_implementation or self.implementation != "cpython"

    @property
    def environment_marker_string(self) -> str:
        """Return the PEP 508 marker for this target's environment only.

        Combines ``python_version``, ``sys_platform`` and
        ``platform_machine``, plus ``implementation_name`` when
        :attr:`declares_implementation`.  It carries no conflict-fork
        ``selection``, so it selects this target's platform/Python point,
        not which extras or groups are active.
        """
        env = self.marker_env
        marker = (
            f'python_version == "{self.python_version}"'
            f' and sys_platform == "{env["sys_platform"]}"'
            f' and platform_machine == "{env["platform_machine"]}"'
        )
        if self.declares_implementation:
            marker += f' and implementation_name == "{self.implementation}"'
        for clause in self.micro_clauses:
            marker += f" and {clause}"
        return marker

    @property
    def marker_string(self) -> str:
        """Return :attr:`environment_marker_string` plus a bare membership clause.

        Each ``(kind, name)`` member of :attr:`selection` adds
        ``'name' in extras`` / ``'name' in dependency_groups`` in sorted
        order.  The clause is bare: it names the members this fork
        selects, not the co-members it excludes.  The lockfile emitter
        derives the mutually-exclusive per-package marker, which also
        negates the co-members of the conflict sets that forbid
        co-selection, from the target's environment and selection.
        """
        marker = self.environment_marker_string
        for kind, name in sorted(self.selection):
            variable = MARKER_VARIABLE_FOR_KIND[kind]
            marker += f' and "{name}" in {variable}'
        return marker

    def with_marker_overrides(self, overrides: Mapping[str, str]) -> ResolveTarget:
        """Return this target with ``overrides`` merged into its marker env.

        The python axis is re-derived when the overlay moves it, so
        ``python_version`` and ``python_full_version`` never describe two
        different interpreters.  The tag set does not move with it: an
        overlay names no runs-on libc or macOS, so it cannot rebuild the
        wheel-tag axis.  The result is no longer
        host-faithful; a build backend run under it reports the host's
        metadata, not the impersonated target's.

        An overlay that moves a marker the tag set encodes (the python
        version, the implementation, or the machine) leaves the tags
        describing one machine and the markers another, so the result is
        not :attr:`tags_faithful` and a provider filters no wheel by tag
        under it: filtering by a tag set the markers disown would drop
        wheels the impersonated target installs and admit ones it cannot.
        Overlaying a value the target already has moves nothing and keeps
        the tags faithful.  ``[tool.nab.environment]`` and ``--python``
        do not come through here; they rebuild the tag axis (see
        :meth:`for_declared` and :meth:`for_host_python`).
        """
        if not overrides:
            return self
        env = dict(self.marker_env)
        apply_python_axis_overlay(env, overrides)
        moved = any(
            env.get(name) != self.marker_env.get(name) for name in _TAG_AXIS_MARKERS
        )
        return replace(
            self,
            marker_env=env,
            host_faithful=False,
            tags_faithful=self.tags_faithful and not moved,
        )

    def with_selection(self, selection: tuple[tuple[str, str], ...]) -> ResolveTarget:
        """Return this target under a conflict fork's active members.

        The label gains one ``kind-name`` clause per member, joined by
        ``.`` in sorted order, so the forks of one target stay distinct:
        ``py311-linux_x86_64-group-black22.group-isort5``.  The ``.``
        separator and the ``kind`` prefix keep it unambiguous, since
        canonical member names are ``[a-z0-9-]`` only: two selections
        that differ in how their names split on ``-`` (or an extra and a
        group of the same name) cannot collide onto one label and
        silently overwrite each other's pins.
        """
        base = self.label
        if self.selection:
            base = base[: -len(_selection_suffix(self.selection))]
        return replace(
            self,
            label=base + _selection_suffix(selection),
            selection=selection,
        )

    def with_micro_slice(
        self, python_full_version: str, clauses: tuple[str, ...]
    ) -> ResolveTarget:
        """Return this target moved onto one micro slice of its minor.

        ``python_full_version`` is the representative release the slice
        resolves at (its lower bound), and ``clauses`` are the
        python_full_version bounds that fence the slice off from the others,
        appended to :attr:`environment_marker_string`.  The label gains a
        ``-pfXYZ`` suffix so each slice pins under its own key.  The python
        axis is re-derived, so ``python_version`` stays the minor and only
        the micro (and, on CPython, ``implementation_version``) moves; the
        wheel tags do not move with the micro, so they stay faithful.
        """
        env = dict(self.marker_env)
        apply_python_axis_overlay(env, {"python_full_version": python_full_version})
        compact = python_full_version.replace(".", "")
        return replace(
            self,
            label=f"{self.label}-pf{compact}",
            marker_env=env,
            micro_clauses=clauses,
        )

    @classmethod
    def for_host(
        cls,
        *,
        env_source: EnvironmentSource = default_environment,
        tags_source: TagsSource = ptags.sys_tags,
    ) -> ResolveTarget:
        """Return the target the running interpreter is.

        Both sources are injected so a caller can model an interpreter
        other than the one running, and so tests do not have to resolve
        against whatever machine they happen to be on.
        """
        return cls(
            label=_HOST_PLATFORM_LABEL,
            marker_env=host_environment(env_source),
            tags=TagSet.for_host(tags_source=tags_source),
            host_faithful=True,
        )

    @classmethod
    def for_host_python(
        cls,
        python: str,
        *,
        env_source: EnvironmentSource = default_environment,
        tags_source: TagsSource = ptags.sys_tags,
    ) -> ResolveTarget:
        """Return the host with its interpreter moved to ``python``.

        The machine stays the host: its markers and platform tags carry
        over, and only the python axis (``python_version``,
        ``python_full_version``, and the tags' interpreter/abi) moves.
        This is what pip's ``--python-version`` targets.

        A bare minor ("3.10") resolves as a micro interval off its ``.0``
        floor; a named patch level ("3.10.5", "3.13.0") is one concrete
        micro resolved whole, so an ``X.Y.0`` pin is not the synthetic floor.
        """
        env = host_environment(env_source)
        apply_python_axis_overlay(env, python_axis_environment(python))
        return cls(
            label=_python_label(env["python_version"], env["implementation_name"])
            + f"-{_HOST_PLATFORM_LABEL}",
            marker_env=env,
            tags=TagSet.for_host_python(python, tags_source=tags_source),
            host_faithful=False,
            concrete_micro=len(Version(python).release) >= _PYTHON_FULL_VERSION_PARTS,
        )

    @classmethod
    def for_declared(
        cls,
        *,
        python_version: str,
        spec: PlatformSpec,
        implementation: str = "cpython",
        python_full_version: str | None = None,
        multi_implementation: bool = False,
    ) -> ResolveTarget:
        """Return a target declared as (python, platform, implementation).

        A bare minor resolves as a micro interval: its ``python_full_version``
        floor is ``{minor}.0`` and a consulted ``python_full_version`` marker
        splits it at the boundary it names.  Passing ``python_full_version``
        pins the target to one concrete deployment micro and resolves it whole,
        the manual single-point alternative to splitting.
        """
        return cls(
            label=_python_label(python_version, implementation) + f"-{spec.label}",
            marker_env=declared_environment(
                python_version, spec, implementation, python_full_version
            ),
            tags=TagSet.for_spec(
                python_version=python_version,
                spec=spec,
                implementation=implementation,
            ),
            host_faithful=False,
            platform_spec=spec,
            multi_implementation=multi_implementation,
            concrete_micro=python_full_version is not None,
        )


def declared_environment(
    python_version: str,
    spec: PlatformSpec,
    implementation: str,
    python_full_version: str | None = None,
) -> dict[str, str]:
    """Build a complete PEP 508 marker environment for a declared target.

    Combines the platform's OS/arch markers and the implementation's
    interpreter-identity markers with the python-axis values derived from
    ``python_version``.  ``platform_release`` and ``platform_version``
    come from the :class:`~nab_python.tags.PlatformSpec`; both default to
    ``""`` so kernel-conditioned markers evaluate False unless the user
    declares a target kernel or OS version.

    ``implementation_version`` is set to the Python version for every
    implementation; for non-CPython this is the interpreter's Python
    level, not its own release (PyPy 7.3.x), so the rare
    ``implementation_version`` marker on PyPy may misevaluate during the
    resolve.  The lock does not carry the synthetic value: see
    :func:`unboundable_variables`.
    """
    full = python_full_version or f"{python_version}.0"
    return {
        **PLATFORM_MARKERS[spec.platform_id],
        **IMPLEMENTATION_MARKERS[implementation],
        "python_version": python_version,
        "python_full_version": full,
        "implementation_version": full,
        "platform_release": spec.platform_release,
        "platform_version": spec.platform_version,
    }


@dataclass
class Matrix:
    """The declared set of targets a resolve covers.

    Expands a python range, a platform list, and an implementation list
    into a finite list of :class:`ResolveTarget`.  Every PEP 508 variable
    any marker on the dep graph names has a value in every target;
    ``Requires-Python`` filtering happens elsewhere.

    ``python_order``: ``"asc"`` (default, oldest first) or ``"desc"``.
    Combined with cross-target alignment in the resolver this selects
    between ``fork-strategy=fewest`` (asc: the oldest-Python pin
    propagates forward, so the lowest common version wins) and
    ``fork-strategy=requires-python`` (desc: the newest-Python pin
    propagates, so older Pythons diverge only when the new version is
    incompatible).

    ``python_patches``: optional ``{minor: full_version}`` mapping that pins a
    matrix minor to one concrete deployment micro and resolves it whole,
    instead of as a micro interval split at each consulted boundary.  Use it to
    resolve a minor as the single patch release you deploy.  Example:
    ``python_patches={"3.11": "3.11.4", "3.12": "3.12.1"}``.

    ``implementations``: the interpreter implementations to model
    (``"cpython"``, ``"pypy"``).  Each multiplies the target count;
    markers and wheel tags resolve per implementation.
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
        unknown_patches = [m for m in patches if m not in KNOWN_PYTHON_MINORS]
        if unknown_patches:
            msg = (
                f"Unknown python_patches minors: {unknown_patches!r};"
                " keys must be major.minor like '3.11'"
            )
            raise ValueError(msg)

        check_free_threaded(
            platforms=self.platforms,
            implementations=self.implementations,
            python_versions=tuple(_pythons_in_range(self.python)),
        )

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


def check_free_threaded(
    *,
    platforms: Sequence[PlatformSpec],
    implementations: Sequence[str],
    python_versions: Sequence[str],
) -> None:
    """Reject a free-threaded platform no interpreter build can satisfy.

    The ``cpXYt`` ABI ships only from CPython 3.13, and the rule needs all
    three axes: the platform carries the flag, and only the declaration
    around it knows the implementation and the python versions.  Both
    declaring surfaces (the matrix and the single environment) call this.
    """
    if not any(spec.free_threaded for spec in platforms):
        return

    foreign = [i for i in implementations if i != "cpython"]
    if foreign:
        msg = (
            f"a free-threaded platform needs CPython, not {foreign!r};"
            f" only CPython has a free-threaded build"
        )
        raise ValueError(msg)

    floor = ".".join(str(p) for p in FREE_THREADED_MIN_PYTHON)
    too_old = [py for py in python_versions if not supports_free_threading(py)]
    if too_old:
        msg = (
            f"a free-threaded platform needs CPython {floor} or newer, not {too_old!r}"
        )
        raise ValueError(msg)


def _pythons_in_range(spec: str) -> Iterable[str]:
    """Yield the known Python minors that satisfy ``spec``.

    ``spec`` is a PEP 440 specifier set, e.g. ``">=3.11, <3.14"``.
    """
    parsed = SpecifierSet(spec)
    for minor in KNOWN_PYTHON_MINORS:
        # Test membership on the .0 patch so a >=3.11 specifier admits
        # "3.11" through "3.11.0".
        if Version(f"{minor}.0") in parsed:
            yield minor


def _python_label(python_version: str, implementation: str) -> str:
    """Render the interpreter half of a label, e.g. ``py311`` or ``pp311``."""
    prefix = _IMPLEMENTATION_PREFIX.get(implementation, _DEFAULT_IMPLEMENTATION_PREFIX)
    return prefix + python_version.replace(".", "")


def _selection_slug(selection: tuple[tuple[str, str], ...]) -> str:
    """Render a conflict-fork selection as a slug, empty when unforked."""
    return ".".join(f"{kind}-{name}" for kind, name in sorted(selection))


def _selection_suffix(selection: tuple[tuple[str, str], ...]) -> str:
    """Render a conflict-fork selection as a label suffix, empty when unforked."""
    slug = _selection_slug(selection)
    return f"-{slug}" if slug else ""
