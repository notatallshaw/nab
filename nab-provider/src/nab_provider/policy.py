"""The resolution policies a project declares and the provider applies."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from ._value import SlottedValue

if TYPE_CHECKING:
    from pathlib import Path

    from .metadata import WheelMetadata

__all__ = [
    "ArchiveSource",
    "BuildPolicy",
    "DecisionOrder",
    "DistPolicy",
    "ExtrasMode",
    "LocalSource",
    "ResolutionStrategy",
    "ResolveMode",
    "SourceMaterialization",
    "SourceRequest",
    "VcsSource",
]


class ExtrasMode(enum.Enum):
    """How to handle missing extras (not in Provides-Extra)."""

    WARN = "warn"
    """Log a warning and drop the extra, as pip does."""

    ERROR_USER = "error_user"
    """Error for user-provided extras, warn for transitive."""

    BACKTRACK = "backtrack"
    """Error for user-provided extras; reject the version for a transitive one."""


class ResolveMode(enum.Enum):
    """How many targets one resolve covers."""

    SPECIFIC = "specific"
    """One target: the host, or an impersonated marker environment (default)."""

    UNIVERSAL = "universal"
    """One target per tuple declared in ``[tool.nab.matrix]``.

    The multi-target lockfile format it produces is *experimental*.
    """


class DistPolicy(enum.Enum):
    """How to admit wheels and sdists during resolution."""

    WHEEL_ONLY = "wheel-only"
    """Reject sdists; wheels only.  Mirrors pip's ``--only-binary <pkg>``."""

    PREFER_WHEEL = "prefer-wheel"
    """Try wheels first, fall back to sdists for versions without wheels."""

    WHEEL_OR_SDIST = "wheel-or-sdist"
    """Admit both; newest version wins regardless of artifact kind (default)."""

    SDIST_ONLY = "sdist-only"
    """Reject wheels; sdists only.  Mirrors pip's ``--no-binary <pkg>``."""

    SDIST_INSTALL = "sdist-install"
    """Lock the sdist; resolve from whichever artifact is cheapest.

    Pins only the sdist, as :attr:`SDIST_ONLY` does, but reads the deps from
    the wheel's METADATA when the chosen version has one, and from the sdist's
    PKG-INFO with the usual :pep:`643` and pyproject.toml fallbacks when it
    does not.
    """


class BuildPolicy(enum.Enum):
    """How permissive the resolver is about invoking PEP 517 backends.

    Three levels, strictest to most permissive.  Every level reads static
    metadata from every source it admits; they differ in what may fall through
    to a backend when that read returns nothing usable.
    """

    NEVER = "never"
    """Static metadata only.

    Wheels, sdists, local checkouts, VCS clones and archive sources are all
    read statically.  One that cannot be read that way raises
    :class:`UnsupportedSdistError`, which skips a PyPI sdist version but ends
    the resolve for a declared source.
    """

    BUILD_LOCAL = "build-local"
    """Static metadata everywhere, plus PEP 517 builds on local checkouts (default).

    Adds backend invocation for ``[[tool.nab.local-sources]]`` entries and
    workspace members when their ``pyproject.toml`` cannot be read
    statically.  VCS clones, archive sources, and remote PyPI sdists remain
    static-only.
    """

    BUILD_REMOTE = "build-remote"
    """Builds extend to VCS clones, archive sources and remote PyPI sdists.

    The backend runs on those trees when their metadata is dynamic and has no
    static fallback.
    """


class ResolutionStrategy(enum.Enum):
    """Which version the resolver picks within an allowed range.

    Mirrors uv's ``--resolution`` flag.  ``LOWEST_DIRECT`` catches missing
    ``>=`` bounds without dragging the whole transitive graph to its floor.
    """

    HIGHEST = "highest"
    """Newest compatible version (default)."""

    LOWEST = "lowest"
    """Oldest compatible version, transitively."""

    LOWEST_DIRECT = "lowest-direct"
    """Oldest for direct deps; newest for transitive deps."""


class DecisionOrder(enum.Enum):
    """Whether arrived listings may steer which package is decided next."""

    ARRIVAL = "arrival"
    """Rank on what has already landed, so the search keeps moving (default)."""

    STABLE = "stable"
    """Wait for each listing, so the sort key cannot see arrival order."""


class LocalSource(SlottedValue):
    """A source tree on disk used as the only candidate for a package.

    The package is pinned to the version the tree declares in
    ``[project].version``, or to the one the build backend computes when that
    field is dynamic.

    ``path`` is absolute; ``subdirectory`` is a path under it for monorepo
    layouts, and ``editable`` records an editable install in the lockfile.
    """

    __slots__ = ("editable", "name", "path", "subdirectory")
    __match_args__ = ("name", "path", "editable", "subdirectory")

    def __init__(
        self,
        name: str,
        path: str,
        *,
        editable: bool = False,
        subdirectory: str | None = None,
    ) -> None:
        """Record the tree at ``path`` as the only candidate for ``name``."""
        self.name = name
        self.path = path
        self.editable = editable
        self.subdirectory = subdirectory

    @property
    def descriptor(self) -> str:
        """How this source is named in error messages."""
        return f"local source {self.name!r}"


class VcsSource(SlottedValue):
    """A VCS reference used as the only candidate for a package.

    ``url`` is a pip-style VCS URL, e.g.
    ``git+https://github.com/x/y.git@<sha>#subdirectory=pkg``.  The clone is
    read for metadata as a :class:`LocalSource` is.
    """

    __slots__ = ("name", "url")
    __match_args__ = ("name", "url")

    def __init__(self, name: str, url: str) -> None:
        """Record the repository at ``url`` as the only candidate for ``name``."""
        self.name = name
        self.url = url

    @property
    def descriptor(self) -> str:
        """How this source is named in error messages."""
        return f"vcs source {self.name!r}"


class ArchiveSource(SlottedValue):
    """A direct-URL archive used as the only candidate for a package.

    ``url`` carries the archive's hash, and an optional subdirectory, in its
    fragment: ``https://example.com/x-1.0.tar.gz#sha256=<hex>``.  The archive
    is hash-verified and extracted, then read for metadata as a
    :class:`LocalSource` is.
    """

    __slots__ = ("name", "url")
    __match_args__ = ("name", "url")

    def __init__(self, name: str, url: str) -> None:
        """Record the archive at ``url`` as the only candidate for ``name``."""
        self.name = name
        self.url = url

    @property
    def descriptor(self) -> str:
        """How this source is named in error messages."""
        return f"archive source {self.name!r}"


class SourceRequest(SlottedValue):
    """One declared source and everything a host needs to materialise it.

    ``build_policy`` is the source's effective policy, per-package overrides
    already applied.  ``require_pin`` applies only to a VCS clone, where it
    demands a full commit sha.
    """

    __slots__ = (
        "archive_cache_dir",
        "build_policy",
        "package",
        "require_pin",
        "source",
        "vcs_cache_dir",
    )
    __match_args__ = (
        "package",
        "source",
        "build_policy",
        "vcs_cache_dir",
        "archive_cache_dir",
        "require_pin",
    )

    def __init__(
        self,
        package: str,
        source: LocalSource | VcsSource | ArchiveSource,
        build_policy: BuildPolicy,
        vcs_cache_dir: Path | None,
        archive_cache_dir: Path | None,
        *,
        require_pin: bool,
    ) -> None:
        """Record what a host needs to materialise ``source`` for ``package``."""
        self.package = package
        self.source = source
        self.build_policy = build_policy
        self.vcs_cache_dir = vcs_cache_dir
        self.archive_cache_dir = archive_cache_dir
        self.require_pin = require_pin


class SourceMaterialization(SlottedValue):
    """What a host produced for one declared source.

    ``path`` is the directory the metadata was read from.  ``commit_sha`` is
    the resolved commit of a VCS clone, and ``None`` for a local directory or
    an archive.
    """

    __slots__ = ("commit_sha", "metadata", "path")
    __match_args__ = ("path", "metadata", "commit_sha")

    def __init__(
        self, path: Path, metadata: WheelMetadata, commit_sha: str | None
    ) -> None:
        """Record the tree at ``path`` and the metadata read out of it."""
        self.path = path
        self.metadata = metadata
        self.commit_sha = commit_sha
