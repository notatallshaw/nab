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

import itertools
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ._conflict_kind import EMPTY_MEMBERSHIP_SETS, MARKER_VARIABLE_FOR_KIND
from ._vendor.packaging import tags as ptags
from ._vendor.packaging.markers import Marker, default_environment
from ._vendor.packaging.specifiers import SpecifierSet
from ._vendor.packaging.version import InvalidVersion, Version
from .tags import (
    FREE_THREADED_MIN_PYTHON,
    PlatformSpec,
    TagSet,
    supports_free_threading,
)

if TYPE_CHECKING:
    from .tags import TagsSource


__all__ = [
    "IMPLEMENTATION_MARKERS",
    "KNOWN_PYTHON_MINORS",
    "PEP508_MARKER_VARIABLES",
    "PLATFORM_MARKERS",
    "UNBOUNDABLE_MARKER_VARIABLES",
    "EnvironmentSource",
    "Matrix",
    "ResolveTarget",
    "apply_python_axis_overlay",
    "check_free_threaded",
    "declared_environment",
    "environment_declaration",
    "host_environment",
    "marker_variables",
    "python_axis_environment",
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

_MARKER_VARIABLE_RE = re.compile(
    r"\b(" + "|".join(sorted(PEP508_MARKER_VARIABLES)) + r")\b"
)

# The variables a lock declares by constraint rather than by value: see
# :func:`_version_clauses`.  Both carry a micro release, and on CPython they
# carry the same one (``implementation_version`` comes from
# ``sys.implementation.version``), so pinning either collapses the lock to a
# single patch release.
_BY_CONSTRAINT = ("python_full_version", "implementation_version")

# The operator that states the complement of each comparison.  PEP 508 has no
# ``not``, so a clause the resolve found False is declared by flipping its
# operator.  ``~=``, ``===`` and the membership operators have no
# single-clause complement and are deliberately absent; a clause using one is
# declared by value instead.
_COMPLEMENT_OPERATOR: dict[str, str] = {
    "<": ">=",
    "<=": ">",
    ">": "<=",
    ">=": "<",
    "==": "!=",
    "!=": "==",
}

# One ``lhs op rhs`` comparison of a marker, matched against the string form
# :func:`Marker.__str__` normalises to: an operand is either a quoted literal
# or a bare variable token, which is what tells the two apart (the same
# property the membership scan in :mod:`nab_python._lockfile.disjointness`
# rests on).  Ordered so the two-character operators win over their prefixes.
_MARKER_OPERAND = r'"[^"]*"|[A-Za-z_][A-Za-z0-9_]*'
_MARKER_CLAUSE_RE = re.compile(
    rf"(?P<lhs>{_MARKER_OPERAND})\s*"
    r"(?P<op>===|==|!=|<=|>=|~=|<|>|not\s+in|in)\s*"
    rf"(?P<rhs>{_MARKER_OPERAND})"
)

# How many clauses of one marker the lock can leave open before
# :func:`_deciding_clauses` stops asking which of its clauses the answer
# turned on: the question is settled by reading every combination of them, so
# the cost doubles per clause.  Past this, every clause on the variable is
# declared.
_MAX_FREE_CLAUSES = 8


def marker_variables(marker_text: str) -> frozenset[str]:
    """Return the PEP 508 environment variables ``marker_text`` names.

    Matches the spec's names as whole words against the marker's string
    form.  A name inside a string literal (``sys_platform ==
    "python_version"``) counts, so the result over-approximates; a
    declaration built from too many variables is narrower than one built
    from too few, which is the safe direction to be wrong in.
    """
    return frozenset(_MARKER_VARIABLE_RE.findall(marker_text))


def environment_declaration(target: ResolveTarget, consulted: Iterable[Marker]) -> str:
    """Render the PEP 751 ``environments`` marker for ``target``.

    Declares every variable the ``consulted`` markers named, plus
    :data:`_ALWAYS_DECLARED` and, when the target names an interpreter
    other than the sole default (see
    :attr:`ResolveTarget.declares_implementation`),
    ``implementation_name``.  A resolve drops every dependency whose
    marker is False under the target, so an installer that consults the
    same variable and gets a different answer would be missing the deps
    that environment needs: the declaration refuses it instead.

    Most variables are declared by value: a marker on ``platform_system``
    pins the OS.  The variables in :data:`_BY_CONSTRAINT` are declared by
    constraint (see :func:`_version_clauses`), because a lock that pinned
    the micro release would refuse the very interpreters it resolved for.
    The variables :func:`unboundable_variables` names for the target are
    dropped: the kernel axes always, and ``implementation_version`` on a
    non-CPython target, whose value is the target's Python level rather than
    the interpreter's own release.  Dropping it leaves the lock open on that
    axis, so a dependency the resolve gated on ``implementation_version``
    under PyPy may still be missed at install; the resolve's own use of the
    synthetic value stays a known limitation (a real axis is needed).
    """
    texts = sorted({str(marker) for marker in consulted})
    variables: set[str] = set()
    for text in texts:
        variables |= marker_variables(text)

    always = list(_ALWAYS_DECLARED)
    if target.declares_implementation:
        always.append("implementation_name")
    names = [
        *always,
        *sorted(variables - set(always) - unboundable_variables(target)),
    ]
    declaring = _Declaring(
        marker_env=target.marker_env,
        environment=target.env_with_membership(),
        pinned=frozenset(name for name in names if name not in _BY_CONSTRAINT),
    )

    clauses: list[str] = []
    for name in names:
        if name in _BY_CONSTRAINT:
            clauses.extend(_version_clauses(declaring, texts, name))
        else:
            clauses.append(f'{name} == "{target.marker_env[name]}"')
    return " and ".join(clauses)


@dataclass(frozen=True, slots=True)
class _Declaring:
    """What deciding a clause of a consulted marker needs.

    ``environment`` is the target's marker env seeded with the empty
    membership sets the resolve evaluated its dependency markers under, so a
    marker reads here exactly as it read there.  ``pinned`` is the set of
    variables the declaration states by value: a clause on one of those
    answers the same in every environment the lock admits, so it can be held
    at the answer it gave.  Every other clause (``extra``, a kernel axis, the
    other by-constraint variable) is one the lock leaves open, so it has to
    be tried both ways.
    """

    marker_env: Mapping[str, str]
    environment: Mapping[str, str | frozenset[str]]
    pinned: frozenset[str]

    def constant(self, *, value: bool) -> str:
        """Render a clause that reads ``value`` under :attr:`environment`.

        ``python_version`` is always declared by value, so a clause on it
        reads the same in every environment the lock admits; here it is only
        a carrier for a truth value substituted into a marker.
        """
        operator = "==" if value else "!="
        return f'python_version {operator} "{self.marker_env["python_version"]}"'


def _version_clauses(
    declaring: _Declaring, texts: Sequence[str], variable: str
) -> list[str]:
    """Declare how the resolve read ``variable``, not its value.

    Pinning the target's own ``python_full_version`` would refuse every
    other micro release, including every real one when the target names a
    minor (``--python 3.13`` synthesizes ``3.13.0``, which no released
    interpreter reports).  The pins do not depend on the micro; they depend
    on how the markers reading it answered.  So that is what the lock
    declares: a clause the marker's answer turned on is declared as it
    stands when it held, and complemented when it did not
    (``python_full_version <= "3.11.0a6"`` read False becomes
    ``python_full_version > "3.11.0a6"``).  A clause the answer did not turn
    on (see :func:`_deciding_clauses`) is not declared at all, and a
    variable no marker's answer turned on leaves its axis open.  Every
    environment the result admits answers the resolve's markers the way the
    resolve did, and a marker that genuinely splits the micros (``>=
    "3.13.4"``) still partitions them.

    Each clause is decided by asking packaging: it is rebuilt as a marker of
    its own and evaluated against the target through the public
    ``Marker.evaluate``, so no marker semantics are re-derived here.

    A clause whose outcome nab cannot state as a clause falls back to
    declaring the target's exact value, which is sound if narrow: an unusual
    operator (``~=``, ``===``, a membership test), a comparison against
    another variable rather than a literal, or a PEP 440 boundary where the
    flipped operator is not the complement: ``< "3.10.2"`` excludes the
    prereleases of 3.10.2 and so does ``>= "3.10.2"``, so on a ``3.10.2rc1``
    target neither side holds.
    """
    exact = f'{variable} == "{declaring.marker_env[variable]}"'
    declared: set[str] = set()
    for text in texts:
        for lhs, op, rhs in _deciding_clauses(declaring, text, variable):
            declaration = _declared_clause(lhs, op, rhs, declaring, variable)
            if declaration is None:
                return [exact]
            declared.add(declaration)

    return sorted(declared)


def _deciding_clauses(
    declaring: _Declaring, text: str, variable: str
) -> list[tuple[str, str, str]]:
    """Return the clauses of ``text`` on ``variable`` that decide its answer.

    A marker is an ``and``/``or`` of clauses, so a clause on ``variable``
    can be dead: the other side of an ``or`` already held
    (``python_full_version >= "3.13.5" or sys_platform == "linux"`` on
    Linux), or the other side of an ``and`` already failed.  Declaring a
    dead clause would refuse a micro release that reads every consulted
    marker exactly as the resolve did, which is the environment the lock
    was resolved for.

    A clause is dropped only when the marker answers the same however that
    clause reads, whatever the clauses the lock leaves open read.  Both are
    settled by substituting truth values into the marker text and asking
    packaging for the answer: a marker has no ``not``, so its answer rises
    with its clauses, and testing the two extremes of a set of clauses
    settles every reading in between.

    Dropping is decided for the set as a whole, so clauses that only matter
    together cannot all go: ``>= "3.13.4" and >= "3.14"`` (both read False)
    drops the first, keeps the second, and the declaration still refuses
    3.14.

    The analysis is exponential in the number of clauses the lock leaves
    open, so past :data:`_MAX_FREE_CLAUSES` every clause on ``variable`` is
    declared, which is the narrow but sound answer.
    """
    atoms = list(_MARKER_CLAUSE_RE.finditer(text))
    candidates = [
        index for index, atom in enumerate(atoms) if variable in _clause_variables(atom)
    ]
    if not candidates:
        return []

    free = [
        index
        for index, atom in enumerate(atoms)
        if index not in candidates and not _clause_variables(atom) <= declaring.pinned
    ]
    if len(free) <= _MAX_FREE_CLAUSES:
        released: set[int] = set()
        for index in candidates:
            trial = released | {index}
            if _answer_ignores(declaring, text, atoms, free, trial):
                released = trial
        candidates = [index for index in candidates if index not in released]

    return [_clause_parts(atoms[index]) for index in candidates]


def _answer_ignores(
    declaring: _Declaring,
    text: str,
    atoms: Sequence[re.Match[str]],
    free: Sequence[int],
    released: set[int],
) -> bool:
    """Whether ``text`` answers the same however the ``released`` clauses read.

    Every combination of the ``free`` clauses is tried, since the lock does
    not pin them and the installer's environment (or its choice of extras)
    settles them.  Under each, the released clauses are read all False and
    all True; a marker's answer rises with its clauses, so agreeing at those
    two extremes means agreeing at every reading between them, the target's
    own included.  Clauses that are neither free nor released keep the text
    they had, and so keep the answer they gave the resolve.
    """
    for combination in itertools.product((False, True), repeat=len(free)):
        fixed = dict(zip(free, combination, strict=True))
        low = _substituted(
            declaring, text, atoms, {**fixed, **dict.fromkeys(released, False)}
        )
        high = _substituted(
            declaring, text, atoms, {**fixed, **dict.fromkeys(released, True)}
        )
        if Marker(low).evaluate(declaring.environment) != Marker(high).evaluate(
            declaring.environment
        ):
            return False
    return True


def _substituted(
    declaring: _Declaring,
    text: str,
    atoms: Sequence[re.Match[str]],
    readings: Mapping[int, bool],
) -> str:
    """Return ``text`` with each clause in ``readings`` forced to its reading."""
    pieces: list[str] = []
    end = 0
    for index, atom in enumerate(atoms):
        if index not in readings:
            continue
        pieces.append(text[end : atom.start()])
        pieces.append(declaring.constant(value=readings[index]))
        end = atom.end()
    pieces.append(text[end:])
    return "".join(pieces)


def _clause_variables(atom: re.Match[str]) -> frozenset[str]:
    """Return the environment variables one ``lhs op rhs`` clause reads."""
    return frozenset(
        operand for operand in atom.group("lhs", "rhs") if not operand.startswith('"')
    )


def _clause_parts(atom: re.Match[str]) -> tuple[str, str, str]:
    """Return one clause's operands and its operator, whitespace normalized."""
    lhs, op, rhs = atom.group("lhs", "op", "rhs")
    return lhs, " ".join(op.split()), rhs


def _declared_clause(
    lhs: str, op: str, rhs: str, declaring: _Declaring, variable: str
) -> str | None:
    """Return the clause declaring how ``lhs op rhs`` read, or None.

    The clause is one comparison of a marker the resolve evaluated, with
    ``variable`` on one side; packaging decides which way it read.
    None means nab cannot state that outcome as a clause of its own, and the
    caller declares the exact value instead.

    A clause that held is declared as it stands, whatever its operator: an
    environment satisfying it reads it the way the resolve did, and no
    complement is needed.  Only a clause that read False needs one, so only
    an operator PEP 508 cannot complement (``~=``, ``===``, a membership
    test) sends the caller to the exact value.
    """
    literal = rhs if lhs == variable else lhs
    if not literal.startswith('"'):
        # A comparison against another variable states nothing about this one.
        return None
    clause = f"{lhs} {op} {rhs}"
    if Marker(clause).evaluate(declaring.environment):
        return clause
    if op not in _COMPLEMENT_OPERATOR:
        return None
    complement = f"{lhs} {_COMPLEMENT_OPERATOR[op]} {rhs}"
    if not Marker(complement).evaluate(declaring.environment):
        return None
    return complement


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
    clause to :attr:`marker_string` so the lockfile entry fires only
    when the user selects that member.

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
        return marker

    @property
    def marker_string(self) -> str:
        """Return the per-package PEP 508 marker that selects this target.

        This is :attr:`environment_marker_string` plus a bare membership
        clause per active conflict-fork member.  The emit-time
        disjointness validator prunes the install contexts that activate
        two members of one declared conflict, so the bare clause needs no
        ``not in`` negation against the other members.
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
        """
        env = host_environment(env_source)
        apply_python_axis_overlay(env, python_axis_environment(python))
        return cls(
            label=_python_label(env["python_version"], env["implementation_name"])
            + f"-{_HOST_PLATFORM_LABEL}",
            marker_env=env,
            tags=TagSet.for_host_python(python, tags_source=tags_source),
            host_faithful=False,
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

        ``python_full_version`` overrides the default ``{minor}.0``
        patch release, which makes a marker like ``python_full_version >=
        "3.11.4"`` evaluate against the user's actual deployment.
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

    ``python_patches``: optional ``{minor: full_version}`` mapping that
    sets the per-target ``python_full_version`` marker.  Defaults to
    ``{minor}.0`` per target, which makes a marker like
    ``python_full_version >= "3.11.4"`` evaluate False on a 3.11 target.
    Users with deployments on later patch releases should declare them
    here so marker evaluation matches reality.  Example:
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
