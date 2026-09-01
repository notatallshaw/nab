"""PEP 751 lockfile (``pylock.toml``) emission for nab-project.

Public surface for producing lockfiles from a resolve. The three
emitters are :func:`write_lock` (PEP 751 ``pylock.toml``),
:func:`write_requirements_with_hashes`, and
:func:`write_requirements_without_hashes`. A resolve contributes one
:class:`TargetLock` per environment it ran against; the writer
collapses them into one ``Package`` per distinct ``(name, version,
source)`` with a marker disjoining the targets that chose it, and drops
the marker when every target agrees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import timezone
from typing import TYPE_CHECKING, Any

from nab_provider._vendor.packaging.pylock import is_valid_pylock_path
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version

from ._lockfile.builder import (
    MissingHashError,
    MissingSdistError,
    MissingVcsCommitError,
    build_target_lock,
    read_lockfile_anchor,
    read_lockfile_packages,
    strip_userinfo,
)
from ._lockfile.disjointness import DisjointnessError
from ._lockfile.groups import BASE_MEMBER
from ._lockfile.pylock import (
    DivergentBaseDependencyError,
    LockValidationError,
    build_pylock,
    render_lock,
    write_lock,
)
from ._lockfile.requirements import (
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from ._lockfile.validate import (
    InvalidLockfileError,
    LockDisqualification,
    LockfileSyntaxError,
    RootRequirement,
    check_locked,
)
from .value import ValueType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from nab_provider._vendor.packaging.markers import Marker
    from nab_provider.overrides import PackageOverride
    from nab_provider.target import ResolveTarget

    from .conflicts import ConflictSet


__all__ = [
    "ACCEPTED_HASH_ALGORITHMS",
    "BASE_MEMBER",
    "LOCK_VERSION",
    "ArchivePin",
    "DisjointnessError",
    "DivergentBaseDependencyError",
    "IndexPin",
    "InvalidLockfileError",
    "LocalPin",
    "LockDisqualification",
    "LockInput",
    "LockValidationError",
    "LockfileSyntaxError",
    "MissingHashError",
    "MissingSdistError",
    "MissingVcsCommitError",
    "PinShape",
    "Provenance",
    "RootRequirement",
    "SdistArtifact",
    "TargetLock",
    "VcsPin",
    "WheelArtifact",
    "build_pylock",
    "build_target_lock",
    "check_locked",
    "drop_workspace_pins",
    "is_valid_pylock_path",
    "package_metadata_override_records",
    "read_lockfile_anchor",
    "read_lockfile_packages",
    "render_lock",
    "strip_userinfo",
    "summarize_lock",
    "write_lock",
    "write_requirements_with_hashes",
    "write_requirements_without_hashes",
]


logger = logging.getLogger(__name__)

LOCK_VERSION = "1.0"

# Verification prefers sha256 (pip's hash-checking baseline), not the strongest;
# any one published hash verifies the same bytes.
ACCEPTED_HASH_ALGORITHMS: tuple[str, ...] = ("sha256", "sha384", "sha512")


def _select_primary_digest(
    hashes: tuple[tuple[str, str], ...],
) -> tuple[str, str] | None:
    by_algo = dict(hashes)
    for algo in ACCEPTED_HASH_ALGORITHMS:
        if algo in by_algo:
            return algo, by_algo[algo]
    return None


class WheelArtifact(ValueType):
    """A single wheel file to record in the lockfile.

    ``hashes`` is the set of (algorithm, digest) pairs the index
    published.  PEP 751 mandates at least one hash per artefact;
    nab requires at least one of ``sha256``, ``sha384``, ``sha512``
    so the lockfile is consumable by pip's hash-checking mode.

    ``upload_time`` is the index's upload timestamp when available;
    informational provenance per PEP 751 ``packages.wheels.upload-time``.

    ``local_path`` is the on-disk path of a wheel from a local
    find-links directory; the lockfile writer emits it as a relative
    ``path`` instead of ``url`` so the lockfile is portable.  ``None``
    for a wheel fetched from a remote index.
    """

    __slots__ = __match_args__ = (
        "filename",
        "url",
        "hashes",
        "size",
        "upload_time",
        "local_path",
    )

    filename: str
    url: str
    hashes: tuple[tuple[str, str], ...]
    size: int | None
    upload_time: datetime | None
    local_path: Path | None

    def __init__(
        self,
        filename: str,
        url: str,
        hashes: tuple[tuple[str, str], ...],
        size: int | None = None,
        upload_time: datetime | None = None,
        local_path: Path | None = None,
    ) -> None:
        """Record the wheel ``filename`` and where its bytes come from."""
        self.filename = filename
        self.url = url
        self.hashes = hashes
        self.size = size
        self.upload_time = upload_time
        self.local_path = local_path

    @property
    def primary_digest(self) -> tuple[str, str]:
        """Return ``(algo, digest)`` for the first acceptable algorithm present."""
        chosen = _select_primary_digest(self.hashes)
        if chosen is None:
            msg = f"{self.filename} has no acceptable hash"
            raise ValueError(msg)
        return chosen


class SdistArtifact(ValueType):
    """An sdist tarball to record in the lockfile.

    See :class:`WheelArtifact` for the meaning of ``hashes``,
    ``upload_time`` and ``local_path``.
    """

    __slots__ = __match_args__ = (
        "filename",
        "url",
        "hashes",
        "size",
        "upload_time",
        "local_path",
    )

    filename: str
    url: str
    hashes: tuple[tuple[str, str], ...]
    size: int | None
    upload_time: datetime | None
    local_path: Path | None

    def __init__(
        self,
        filename: str,
        url: str,
        hashes: tuple[tuple[str, str], ...],
        size: int | None = None,
        upload_time: datetime | None = None,
        local_path: Path | None = None,
    ) -> None:
        """Record the sdist ``filename`` and where its bytes come from."""
        self.filename = filename
        self.url = url
        self.hashes = hashes
        self.size = size
        self.upload_time = upload_time
        self.local_path = local_path

    @property
    def primary_digest(self) -> tuple[str, str]:
        """Return ``(algo, digest)`` for the first acceptable algorithm present."""
        chosen = _select_primary_digest(self.hashes)
        if chosen is None:
            msg = f"{self.filename} has no acceptable hash"
            raise ValueError(msg)
        return chosen


class IndexPin(ValueType):
    """A package resolved from a Simple-API index.

    ``index`` is the URL of the Simple-API root that served the
    package, matching what PEP 751 expects for ``packages.index``.
    """

    __slots__ = __match_args__ = (
        "name",
        "version",
        "index",
        "sdist",
        "wheels",
        "requires_python",
    )

    name: str
    version: str
    index: str
    sdist: SdistArtifact | None
    wheels: tuple[WheelArtifact, ...]
    requires_python: str | None

    def __init__(
        self,
        name: str,
        version: str,
        index: str,
        sdist: SdistArtifact | None = None,
        wheels: tuple[WheelArtifact, ...] = (),
        requires_python: str | None = None,
    ) -> None:
        """Record the ``version`` of ``name`` that ``index`` served."""
        self.name = name
        self.version = version
        self.index = index
        self.sdist = sdist
        self.wheels = wheels
        self.requires_python = requires_python

    def replace(self, **changes: object) -> IndexPin:
        """Return a copy with ``changes`` applied, as ``dataclasses.replace`` would."""
        kept = {name: getattr(self, name) for name in self.__match_args__}
        return IndexPin(**{**kept, **changes})


class LocalPin(ValueType):
    """A package resolved from a local checkout.

    ``path`` is the absolute filesystem path the resolver was pointed
    at.  Lockfile consumers walk the same tree to install.

    ``editable`` records a PEP 660 editable install request;
    ``subdirectory`` is a path under ``path`` for monorepo layouts.
    Both come from the ``[[tool.nab.local-sources]]`` entry.
    """

    __slots__ = __match_args__ = ("name", "version", "path", "editable", "subdirectory")

    name: str
    version: str
    path: str
    editable: bool
    subdirectory: str | None

    def __init__(
        self,
        name: str,
        version: str,
        path: str,
        *,
        editable: bool = False,
        subdirectory: str | None = None,
    ) -> None:
        """Record the tree at ``path`` as the source of ``name``."""
        self.name = name
        self.version = version
        self.path = path
        self.editable = editable
        self.subdirectory = subdirectory


class VcsPin(ValueType):
    """A package resolved from a VCS clone.

    ``repo_url`` is the reproducible pip-style installable URL: the
    ``git+`` prefix, the bare repository URL, ``@<commit-id>``, and any
    ``#subdirectory=`` fragment.  The requirements.txt emitter writes it
    verbatim, so a branch or tag pin installs the locked commit rather
    than a moving ref.  ``bare_repo_url`` is the plain repository URL
    with none of those parts, captured when the source URL is parsed and
    written to PEP 751 ``packages.vcs.url``.

    ``requested_revision`` is the human-readable ref (tag or branch)
    the user pinned, recorded only when it differs from ``commit_id``;
    informational per PEP 751 ``packages.vcs.requested-revision``.

    ``vcs_type`` is the PEP 751 ``packages.vcs.type`` backend
    (``git``/``hg``/``svn``/``bzr``), taken from the URL's ``<vcs>+``
    scheme.
    """

    __slots__ = __match_args__ = (
        "name",
        "version",
        "repo_url",
        "bare_repo_url",
        "commit_id",
        "subdirectory",
        "requested_revision",
        "vcs_type",
    )

    name: str
    version: str
    repo_url: str
    bare_repo_url: str
    commit_id: str
    subdirectory: str | None
    requested_revision: str | None
    vcs_type: str

    def __init__(
        self,
        name: str,
        version: str,
        repo_url: str,
        bare_repo_url: str,
        commit_id: str,
        subdirectory: str | None = None,
        requested_revision: str | None = None,
        vcs_type: str = "git",
    ) -> None:
        """Record ``name`` as the clone of ``repo_url`` at ``commit_id``."""
        self.name = name
        self.version = version
        self.repo_url = repo_url
        self.bare_repo_url = bare_repo_url
        self.commit_id = commit_id
        self.subdirectory = subdirectory
        self.requested_revision = requested_revision
        self.vcs_type = vcs_type


class ArchivePin(ValueType):
    """A package resolved from a direct-URL archive.

    ``url`` is the archive URL with the hash fragment stripped, written
    to PEP 751 ``packages.archive.url``.  ``hashes`` are the verified
    ``(algorithm, digest)`` pairs; PEP 751 requires at least one, and
    nab verifies the download against them before the archive is used.

    Unlike a VCS or directory source, an archive is content-pinned by
    its hash, so the pin carries a real ``version`` and the emitter
    records it (see :func:`_pin_to_package`).
    """

    __slots__ = __match_args__ = ("name", "version", "url", "hashes", "subdirectory")

    name: str
    version: str
    url: str
    hashes: tuple[tuple[str, str], ...]
    subdirectory: str | None

    def __init__(
        self,
        name: str,
        version: str,
        url: str,
        hashes: tuple[tuple[str, str], ...],
        subdirectory: str | None = None,
    ) -> None:
        """Record the archive at ``url`` as the source of ``name``."""
        self.name = name
        self.version = version
        self.url = url
        self.hashes = hashes
        self.subdirectory = subdirectory

    @property
    def primary_digest(self) -> tuple[str, str]:
        """Return ``(algo, digest)`` for the first acceptable algorithm present."""
        chosen = _select_primary_digest(self.hashes)
        if chosen is None:
            msg = f"{self.name} archive has no acceptable hash"
            raise ValueError(msg)
        return chosen


PinShape = IndexPin | LocalPin | VcsPin | ArchivePin


class Provenance(ValueType):
    """Optional ``[tool.nab]`` provenance block written into the lock.

    PEP 751 lets tools record any additional metadata under
    ``[tool.<name>]`` so long as it does not affect installation.
    nab uses the slot to record the inputs that produced the lock:
    a reader can audit a committed lockfile without re-running.

    Every field is informational.  The lockfile reader MUST NOT
    feed any of it into the install path.
    """

    __slots__ = __match_args__ = (
        "nab_version",
        "created_at",
        "command_line",
        "input_path",
        "mode",
        "python_specifier",
        "platforms",
        "cli_project_overrides",
        "package_metadata_overrides",
    )

    nab_version: str
    created_at: datetime
    command_line: tuple[str, ...]
    input_path: str
    mode: str
    python_specifier: str | None
    platforms: tuple[str, ...]

    cli_project_overrides: tuple[tuple[str, str], ...]
    """The ``--project-*`` CLI overrides that shaped this lock, as
    ``(flag, rendered value)`` pairs.  Recorded so a reader can see the
    lock did not derive from the committed files alone."""

    package_metadata_overrides: tuple[tuple[str, tuple[str, ...]], ...]
    """The configured ``[tool.nab]`` per-package metadata overrides, as
    ``(requirement, (field, ...))`` pairs."""

    def __init__(  # noqa: PLR0913 - one keyword per input the lock records
        self,
        nab_version: str,
        created_at: datetime,
        command_line: tuple[str, ...],
        input_path: str,
        mode: str,
        *,
        python_specifier: str | None = None,
        platforms: tuple[str, ...] = (),
        cli_project_overrides: tuple[tuple[str, str], ...] = (),
        package_metadata_overrides: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> None:
        """Record the inputs that produced the lock."""
        self.nab_version = nab_version
        self.created_at = created_at
        self.command_line = command_line
        self.input_path = input_path
        self.mode = mode
        self.python_specifier = python_specifier
        self.platforms = platforms
        self.cli_project_overrides = cli_project_overrides
        self.package_metadata_overrides = package_metadata_overrides

    def to_block(self) -> dict[str, Any]:
        """Render to the dict the TOML writer drops under ``[tool.nab]``.

        ``created-at`` is emitted in UTC because TOML offset date-times are
        RFC 3339, whose offset has no seconds field, so an offset such as
        ``+00:19:32`` cannot be written.  A naive value is taken as UTC, the
        way ``read_lockfile_anchor`` reads one back.
        """
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        block: dict[str, Any] = {
            "nab-version": self.nab_version,
            "created-at": created_at.astimezone(timezone.utc),
            "command-line": list(self.command_line),
            "input-path": self.input_path,
            "mode": self.mode,
        }
        if self.python_specifier is not None:
            block["python-specifier"] = self.python_specifier
        if self.platforms:
            block["platforms"] = list(self.platforms)
        if self.cli_project_overrides:
            block["cli-project-overrides"] = [
                f"{flag}={value}" for flag, value in self.cli_project_overrides
            ]
        if self.package_metadata_overrides:
            block["package-metadata-overrides"] = [
                f"{requirement}: {', '.join(fields)}"
                for requirement, fields in self.package_metadata_overrides
            ]
        return block


def package_metadata_override_records(
    overrides: Sequence[PackageOverride],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Summarise the configured per-package metadata overrides for provenance.

    Each returned pair is a requirement string and the metadata fields the
    entry set (``dependencies``, ``requires-python``, ``provides-extra``).
    Entries that set no metadata field are skipped.  This records every
    configured override as input provenance, including any scoped to a
    version no candidate has (a documented no-op), so an entry here is not a
    claim that it shaped the lock.  Strictly informational: the reader must
    not feed it into the install path.
    """
    records: list[tuple[str, tuple[str, ...]]] = []
    for override in overrides:
        fields: list[str] = []
        if override.dependencies is not None:
            fields.append("dependencies")
        if override.requires_python is not None:
            fields.append("requires-python")
        if override.provides_extra is not None:
            fields.append("provides-extra")

        if fields:
            records.append((str(override.requirement), tuple(fields)))
    return tuple(records)


@dataclass(frozen=True, slots=True)
class TargetLock:
    """What one target contributed to the lock.

    ``pins`` is keyed by canonical package name; each value is the pin
    that target resolved to.  ``dependencies`` is the forward edge set
    among those pins, keyed the same way, which the writer emits as
    PEP 751 ``packages.dependencies``.  ``base_dependencies`` is its
    unconditional subset: the edges from each package's own metadata,
    before any activated extra folds its deps in.  The writer closes a
    conflict environment's no-member base-name set over these edges only.

    ``target`` is the environment the pins hold for.  The writer reads
    its markers and its
    :attr:`~nab_provider.target.ResolveTarget.selection` rather than being
    handed a projection of them, so a lock entry and the environment it
    was resolved for cannot drift apart.

    ``package_gates`` maps a package this target locked to every install
    context that reaches it, as ``(kind, name)`` members: each selected
    extra and group, including the conflict fork's own selection, and
    :data:`BASE_MEMBER` for the project's own dependencies, which the
    writer renames to ``[tool.nab].base-group``.  The writer disjoins
    them into ``'name' in extras`` / ``'name' in dependency_groups``
    clauses on that package's marker.  With no ``base-group`` set there
    is no name to give the project's own dependencies, so the writer
    drops the gate of every package they reach and it stays
    unconditional.
    """

    target: ResolveTarget
    pins: Mapping[str, PinShape]
    dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    base_dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    package_gates: Mapping[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict
    )


@dataclass
class LockInput:
    """Everything the writer needs to produce a Pylock.

    ``targets`` maps a target's label to what that target contributed.
    A resolve always runs against at least one target, so there is
    always at least one entry; a declared matrix (and each conflict
    fork of it) adds more.  The writer collapses them into one
    ``[[packages]]`` entry per distinct ``(name, version, source)``,
    with a marker disjoining the targets that chose it, and omits the
    marker when the entry covers every target.

    ``env_base_names`` maps an environment signature
    (``tuple(sorted(env.items()))``) to the canonical names that the
    base (no-member) resolve produced for that environment.  A package
    present in every conflict fork only counts as a base dependency
    (and so drops its membership clause) when its name is listed here;
    a dependency required by every member but not by the base keeps the
    membership clause, so it does not install when no member is
    selected.

    The missing-key vs empty-frozenset distinction is load-bearing:
    a missing signature means no base pass ran for that env (with no
    forks: the no-conflict path; with forks: base status unknowable,
    so the membership OR is kept).  An empty frozenset means the base
    pass ran and produced zero pins, so every dep is member-only.
    Empty when no conflict fork ran.

    ``environments`` is the lockfile-level set of permitted
    environments (PEP 751 ``environments``): what each target declared
    of the environment it resolved for.

    ``provenance`` is optional metadata about the inputs that
    produced this lock.  When present, it lands in the ``[tool.nab]``
    block of the emitted ``pylock.toml``.
    """

    targets: Mapping[str, TargetLock] = field(default_factory=dict)
    env_base_names: Mapping[tuple[tuple[str, str], ...], frozenset[str]] = field(
        default_factory=dict
    )
    environments: list[Marker] = field(default_factory=list)
    requires_python: str | None = None
    created_by: str = "nab"
    extras: tuple[str, ...] = ()
    dependency_groups: tuple[str, ...] = ()
    default_groups: tuple[str, ...] = ()
    provenance: Provenance | None = None
    conflicts: tuple[ConflictSet, ...] = ()
    """Declared ``[tool.nab].conflicts``.  Prunes the disjointness
    validator's install-context universe so a per-fork lock (one entry
    per mutually-exclusive extra/group) validates."""
    base_group: str | None = None
    """``[tool.nab].base-group``: the group name the lock gives
    the project's own dependencies, or ``None`` to leave them unconditional."""
    build_group: str | None = None
    """``[tool.nab].build-group``: the group name the lock gives
    ``[build-system].requires``, or ``None`` to leave them out of the lock."""

    @property
    def active_groups(self) -> tuple[str, ...]:
        """Every group an install context can activate.

        PEP 751 keeps the ``default-groups`` names out of
        ``dependency-groups``, and an installer that selects nothing
        still activates the defaults, so the group axis of an install
        context is the union of the two arrays, plus the name the lock
        gives the project's own dependencies.
        """
        names = dict.fromkeys((*self.dependency_groups, *self.default_groups))
        for named in (self.base_group, self.build_group):
            if named is not None:
                names[named] = None
        return tuple(names)

    @property
    def marker_envs(self) -> dict[str, Mapping[str, str]]:
        """The PEP 508 marker environment each target resolved under."""
        return {label: lock.target.marker_env for label, lock in self.targets.items()}


def drop_workspace_pins(lock_input: LockInput, exclude: frozenset[str]) -> LockInput:
    """Return a copy of ``lock_input`` with the ``exclude`` pins removed.

    ``exclude`` holds canonical workspace member names; pin keys are already
    canonical.  An empty set returns ``lock_input`` unchanged.  Each target's
    pins are filtered, and its forward dependency graph and membership gates
    with them, so no emitted edge or gate names a dropped member with no
    ``[[packages]]`` entry.  ``base_dependencies`` carries through untouched:
    it is never emitted, and cutting the member out of it would strip base
    status from everything reached only through that member.
    """
    if not exclude:
        return lock_input

    def keep(name: str) -> bool:
        return canonicalize_name(name) not in exclude

    targets = {
        label: replace(
            lock,
            pins={name: pin for name, pin in lock.pins.items() if keep(name)},
            dependencies={
                name: kept
                for name, deps in lock.dependencies.items()
                if keep(name) and (kept := tuple(dep for dep in deps if keep(dep)))
            },
            package_gates={
                name: gate for name, gate in lock.package_gates.items() if keep(name)
            },
        )
        for label, lock in lock_input.targets.items()
    }
    return replace(lock_input, targets=targets)


def summarize_lock(lock_input: LockInput, prior: Mapping[str, Version] | None) -> str:
    """Summarise what was written: a package diff, or the tuple count.

    A matrix pins a package once per tuple, and two tuples may disagree, so
    there is no one version to diff against the prior lock; it reports the
    tuples it covered instead.  ``prior`` comes from
    :func:`read_lockfile_packages`.
    """
    if len(lock_input.targets) > 1:
        return f"{len(lock_input.targets)} tuples"

    pins = {
        name: pin
        for lock in lock_input.targets.values()
        for name, pin in lock.pins.items()
    }

    # Index and archive pins record a version; local and VCS pins emit
    # version=None, so read_lockfile_packages never returns them.
    # Diff against the same set or they read as added every relock.
    versioned = {
        name: Version(pin.version)
        for name, pin in pins.items()
        if isinstance(pin, (IndexPin, ArchivePin))
    }
    return f"{len(pins)} packages{_diff_summary(prior, versioned)}"


def _diff_summary(
    prior: Mapping[str, Version] | None, current: Mapping[str, Version]
) -> str:
    """Return a ``: A added, B upgraded, ...`` suffix for a re-lock.

    ``prior`` is the previous pylock's pins or ``None`` (first lock or an
    unparseable prior file); both fall back to an empty suffix.  An unchanged
    pin set also yields an empty suffix.
    """
    if prior is None:
        return ""
    added = sum(name not in prior for name in current)
    removed = sum(name not in current for name in prior)
    upgraded = downgraded = 0
    for name, version in current.items():
        old = prior.get(name)
        if old is None or old == version:
            continue
        if version > old:
            upgraded += 1
        else:
            downgraded += 1
    parts = [
        f"{count} {label}"
        for count, label in (
            (added, "added"),
            (upgraded, "upgraded"),
            (downgraded, "downgraded"),
            (removed, "removed"),
        )
        if count
    ]
    return f": {', '.join(parts)}" if parts else ""
