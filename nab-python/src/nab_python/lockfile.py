"""PEP 751 lockfile (``pylock.toml``) emission for nab-python.

Public surface for producing lockfiles from a resolve. The three
emitters are :func:`write_lock` (PEP 751 ``pylock.toml``),
:func:`write_requirements_with_hashes`, and
:func:`write_requirements_without_hashes`. Per-tuple pins from a
universal resolve are collapsed into one ``Package`` per distinct
``(name, version, source)`` with a marker disjoining the matching
tuples; same-version tuples emit a single entry with no marker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._lockfile.builder import (
    MissingHashError,
    MissingSdistError,
    MissingVcsCommitError,
    build_lock_input_from_provider,
    read_lockfile_anchor,
    read_lockfile_packages,
)
from ._lockfile.disjointness import DisjointnessError
from ._lockfile.pylock import build_pylock, write_lock
from ._lockfile.requirements import (
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from ._vendor.packaging.pylock import is_valid_pylock_path

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from ._vendor.packaging.markers import Marker
    from .config import ConflictSet


__all__ = [
    "ACCEPTED_HASH_ALGORITHMS",
    "LOCK_VERSION",
    "DisjointnessError",
    "IndexPin",
    "LocalPin",
    "LockInput",
    "MissingHashError",
    "MissingSdistError",
    "MissingVcsCommitError",
    "PinShape",
    "Provenance",
    "SdistArtifact",
    "VcsPin",
    "WheelArtifact",
    "build_lock_input_from_provider",
    "build_pylock",
    "is_valid_pylock_path",
    "read_lockfile_anchor",
    "read_lockfile_packages",
    "write_lock",
    "write_requirements_with_hashes",
    "write_requirements_without_hashes",
]


logger = logging.getLogger(__name__)

LOCK_VERSION = "1.0"
ACCEPTED_HASH_ALGORITHMS: tuple[str, ...] = ("sha256", "sha384", "sha512")


def _select_primary_digest(
    hashes: tuple[tuple[str, str], ...],
) -> tuple[str, str] | None:
    by_algo = dict(hashes)
    for algo in ACCEPTED_HASH_ALGORITHMS:
        if algo in by_algo:
            return algo, by_algo[algo]
    return None


@dataclass(frozen=True, slots=True)
class WheelArtifact:
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

    filename: str
    url: str
    hashes: tuple[tuple[str, str], ...]
    size: int | None = None
    upload_time: datetime | None = None
    local_path: Path | None = None

    @property
    def primary_digest(self) -> tuple[str, str]:
        """Return ``(algo, digest)`` of the strongest acceptable hash."""
        chosen = _select_primary_digest(self.hashes)
        if chosen is None:
            msg = f"{self.filename} has no acceptable hash"
            raise ValueError(msg)
        return chosen


@dataclass(frozen=True, slots=True)
class SdistArtifact:
    """An sdist tarball to record in the lockfile.

    See :class:`WheelArtifact` for the meaning of ``hashes``,
    ``upload_time`` and ``local_path``.
    """

    filename: str
    url: str
    hashes: tuple[tuple[str, str], ...]
    size: int | None = None
    upload_time: datetime | None = None
    local_path: Path | None = None

    @property
    def primary_digest(self) -> tuple[str, str]:
        """Return ``(algo, digest)`` of the strongest acceptable hash."""
        chosen = _select_primary_digest(self.hashes)
        if chosen is None:
            msg = f"{self.filename} has no acceptable hash"
            raise ValueError(msg)
        return chosen


@dataclass(frozen=True, slots=True)
class IndexPin:
    """A package resolved from a Simple-API index.

    ``index`` is the URL of the Simple-API root that served the
    package, matching what PEP 751 expects for ``packages.index``.
    """

    name: str
    version: str
    index: str
    sdist: SdistArtifact | None = None
    wheels: tuple[WheelArtifact, ...] = ()
    requires_python: str | None = None


@dataclass(frozen=True, slots=True)
class LocalPin:
    """A package resolved from a local checkout.

    ``path`` is the absolute filesystem path the resolver was pointed
    at.  Lockfile consumers walk the same tree to install.

    ``editable`` records a PEP 660 editable install request;
    ``subdirectory`` is a path under ``path`` for monorepo layouts.
    Both come from the ``[[tool.nab.local-sources]]`` entry.
    """

    name: str
    version: str
    path: str
    editable: bool = False
    subdirectory: str | None = None


@dataclass(frozen=True, slots=True)
class VcsPin:
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

    name: str
    version: str
    repo_url: str
    bare_repo_url: str
    commit_id: str
    subdirectory: str | None = None
    requested_revision: str | None = None
    vcs_type: str = "git"


PinShape = IndexPin | LocalPin | VcsPin


@dataclass(frozen=True, slots=True)
class Provenance:
    """Optional ``[tool.nab]`` provenance block written into the lock.

    PEP 751 lets tools record any additional metadata under
    ``[tool.<name>]`` so long as it does not affect installation.
    nab uses the slot to record the inputs that produced the lock:
    a reader can audit a committed lockfile without re-running.

    Every field is informational.  The lockfile reader MUST NOT
    feed any of it into the install path.
    """

    nab_version: str
    created_at: datetime
    command_line: tuple[str, ...]
    input_path: str
    mode: str
    python_specifier: str | None = None
    platforms: tuple[str, ...] = ()

    def to_block(self) -> dict[str, Any]:
        """Render to the dict the TOML writer drops under ``[tool.nab]``."""
        block: dict[str, Any] = {
            "nab-version": self.nab_version,
            "created-at": self.created_at,
            "command-line": list(self.command_line),
            "input-path": self.input_path,
            "mode": self.mode,
        }
        if self.python_specifier is not None:
            block["python-specifier"] = self.python_specifier
        if self.platforms:
            block["platforms"] = list(self.platforms)
        return block


@dataclass
class LockInput:
    """Everything the writer needs to produce a Pylock.

    ``pins`` is keyed by canonical package name; each value is the
    single pin chosen for that package across the resolve.

    ``per_tuple_pins`` is the per-tuple expansion when a universal
    resolve produced different pins for different environments.  The
    writer collapses these into one or more ``Package`` entries with
    markers attached.  When ``per_tuple_pins`` is empty, ``pins`` is
    used directly with no markers.

    ``tuple_markers`` maps each tuple label (the keys of
    ``per_tuple_pins``) to the PEP 508 marker that selects that
    tuple.  The writer uses these to build per-package markers.

    ``tuple_env_markers`` maps each tuple label to its
    environment-only marker (no conflict-fork membership clause).
    A base dependency present in every fork of an environment emits
    this marker so it installs even when no conflicting member is
    selected; with no conflict forks it equals ``tuple_markers``.

    ``environments`` is the lockfile-level set of permitted
    environments (PEP 751 ``environments``).  Independent from
    ``tuple_markers``; intended for declaring the universe.

    ``provenance`` is optional metadata about the inputs that
    produced this lock.  When present, it lands in the ``[tool.nab]``
    block of the emitted ``pylock.toml``.
    """

    pins: Mapping[str, PinShape] = field(default_factory=dict)
    per_tuple_pins: Mapping[str, Mapping[str, PinShape]] = field(default_factory=dict)
    tuple_markers: Mapping[str, Marker] = field(default_factory=dict)
    tuple_env_markers: Mapping[str, Marker] = field(default_factory=dict)
    tuple_environments: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
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
