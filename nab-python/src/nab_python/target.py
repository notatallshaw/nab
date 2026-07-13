"""The environment one resolve runs against.

A :class:`ResolveTarget` is a complete PEP 508 marker environment plus
the wheel tags that environment accepts.  Specific mode resolves
against one, built from the host interpreter; universal mode builds one
per (python, platform, implementation) point of the declared matrix.
Both feed the same provider, so the resolver has a single notion of
"the environment we are resolving for".

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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ._conflict_kind import EMPTY_MEMBERSHIP_SETS, MARKER_VARIABLE_FOR_KIND
from ._vendor.packaging import tags as ptags
from ._vendor.packaging.markers import default_environment
from ._vendor.packaging.version import InvalidVersion, Version
from .tags import TagSet

if TYPE_CHECKING:
    from .tags import PlatformSpec, TagsSource


__all__ = [
    "IMPLEMENTATION_MARKERS",
    "PEP508_MARKER_VARIABLES",
    "PLATFORM_MARKERS",
    "UNBOUNDABLE_MARKER_VARIABLES",
    "EnvironmentSource",
    "ResolveTarget",
    "apply_python_axis_overlay",
    "declared_environment",
    "environment_declaration",
    "host_environment",
    "marker_variables",
    "python_axis_environment",
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


def marker_variables(marker_text: str) -> frozenset[str]:
    """Return the PEP 508 environment variables ``marker_text`` names.

    Matches the spec's names as whole words against the marker's string
    form.  A name inside a string literal (``sys_platform ==
    "python_version"``) counts, so the result over-approximates; a
    declaration built from too many variables is narrower than one built
    from too few, which is the safe direction to be wrong in.
    """
    return frozenset(_MARKER_VARIABLE_RE.findall(marker_text))


def environment_declaration(target: ResolveTarget, consulted: Iterable[str]) -> str:
    """Render the PEP 751 ``environments`` marker for ``target``.

    Pins every variable in ``consulted`` to the value ``target`` gives it,
    plus :data:`_ALWAYS_DECLARED`.  A resolve drops every dependency whose
    marker is False under the target, so an installer that consults the
    same variable and gets a different answer would be missing the deps
    that environment needs: the declaration refuses it instead.

    ``consulted`` is the union of the variables the resolve's markers
    named, not a fixed projection of the target: a marker on
    ``python_full_version`` pins the micro release, one on
    ``platform_system`` pins the OS.  Variables in
    :data:`UNBOUNDABLE_MARKER_VARIABLES` are dropped (see there).
    """
    names = [
        *_ALWAYS_DECLARED,
        *sorted(
            (set(consulted) & PEP508_MARKER_VARIABLES)
            - set(_ALWAYS_DECLARED)
            - UNBOUNDABLE_MARKER_VARIABLES
        ),
    ]
    return " and ".join(f'{name} == "{target.marker_env[name]}"' for name in names)


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
    """

    label: str
    marker_env: Mapping[str, str] = field(compare=False)
    tags: TagSet = field(compare=False)
    host_faithful: bool = field(compare=False)
    selection: tuple[tuple[str, str], ...] = ()
    platform_spec: PlatformSpec | None = field(default=None, compare=False)
    multi_implementation: bool = field(default=False, compare=False)

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
    def environment_marker_string(self) -> str:
        """Return the PEP 508 marker for this target's environment only.

        Combines ``python_version``, ``sys_platform`` and
        ``platform_machine``, plus ``implementation_name`` when the
        matrix models more than one implementation.  It carries no
        conflict-fork ``selection``, so it is what the lockfile's
        top-level ``environments`` list declares: the platform/Python
        universe, not which extras or groups are active.
        """
        env = self.marker_env
        marker = (
            f'python_version == "{self.python_version}"'
            f' and sys_platform == "{env["sys_platform"]}"'
            f' and platform_machine == "{env["platform_machine"]}"'
        )
        if self.multi_implementation or self.implementation != "cpython":
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
        overlay names no libc floor or macOS deployment target, so it
        cannot rebuild the wheel-tag axis.  The result is no longer
        host-faithful; a build backend run under it reports the host's
        metadata, not the impersonated target's.
        """
        if not overrides:
            return self
        env = dict(self.marker_env)
        apply_python_axis_overlay(env, overrides)
        return replace(self, marker_env=env, host_faithful=False)

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
    ``implementation_version`` marker on PyPy may misevaluate.
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


def _python_label(python_version: str, implementation: str) -> str:
    """Render the interpreter half of a label, e.g. ``py311`` or ``pp311``."""
    prefix = _IMPLEMENTATION_PREFIX.get(implementation, _DEFAULT_IMPLEMENTATION_PREFIX)
    return prefix + python_version.replace(".", "")


def _selection_suffix(selection: tuple[tuple[str, str], ...]) -> str:
    """Render a conflict-fork selection as a label suffix, empty when unforked."""
    if not selection:
        return ""
    members = ".".join(f"{kind}-{name}" for kind, name in sorted(selection))
    return f"-{members}"
