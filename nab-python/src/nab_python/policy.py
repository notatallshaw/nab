"""The resolution policies a project declares and the provider applies.

Every name here is a declaration the config layer parses and the
provider reads.  Both need them, so neither may own them: with these in
``provider.py`` the config loader imported the whole provider (and, with
it, ``nab_index``) to name an enum member, and the provider reached back
into the config module for the errors it raises.  The leaf breaks that.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = [
    "ArchiveSource",
    "BuildPolicy",
    "DecisionOrder",
    "DistPolicy",
    "ExtrasMode",
    "LocalSource",
    "ResolutionStrategy",
    "ResolveMode",
    "VcsSource",
]


class ExtrasMode(enum.Enum):
    """How to handle missing extras (not in Provides-Extra)."""

    WARN = "warn"
    """Log warning, drop the extra, resolution continues (pip's behavior)."""

    ERROR_USER = "error_user"
    """Error for user-provided extras, warn for transitive."""

    BACKTRACK = "backtrack"
    """Error for user-provided, backtrack for transitive."""


class ResolveMode(enum.Enum):
    """How the resolver interprets the project.

    ``SPECIFIC`` resolves one target, the host or an impersonated
    marker environment.  ``UNIVERSAL`` resolves one target per tuple
    declared in ``[tool.nab.matrix]``.  Both run the same engine over a
    list of targets.  ``UNIVERSAL``'s multi-target lockfile format is
    *experimental* and may change; the resolver itself is the same one
    ``SPECIFIC`` runs.  Users opt in by setting
    ``[tool.nab].mode = "universal"`` and declaring ``[tool.nab.matrix]``.
    """

    SPECIFIC = "specific"
    UNIVERSAL = "universal"


class DistPolicy(enum.Enum):
    """How to admit wheels and sdists during resolution."""

    WHEEL_ONLY = "wheel-only"
    """Ignore sdists entirely. Use wheels, reading PEP 658 metadata when
    published or an HTTP range read of the wheel otherwise."""

    PREFER_WHEEL = "prefer-wheel"
    """Try wheels first, fall back to sdists for versions without wheels."""

    WHEEL_OR_SDIST = "wheel-or-sdist"
    """Admit both. Newest version wins regardless of artifact kind."""

    SDIST_ONLY = "sdist-only"
    """Reject wheels; sdists only.  Mirrors pip's ``--no-binary <pkg>``."""

    SDIST_INSTALL = "sdist-install"
    """Lock the sdist; resolve from whichever artifact is cheapest.

    Same lockfile shape as :attr:`SDIST_ONLY` (only the sdist is pinned, so
    installers download and build that archive), but the resolver is free to
    consult either the wheel's METADATA (via PEP 658 or a range fetch) or
    the sdist's PKG-INFO when extracting dependency facts.  In practice it
    reads the wheel when one exists at the chosen version because that is
    the cheapest source; when only the sdist is published it falls back to
    PKG-INFO with the usual :pep:`643` and pyproject.toml fallbacks.
    Mirrors a pip install with ``--no-binary <pkg>`` while keeping the
    resolver-time fast paths intact.
    """


class BuildPolicy(enum.Enum):
    """How permissive the resolver is about invoking PEP 517 backends.

    Three levels, strictest to most permissive.  Each level reads static
    metadata from every source it admits; the difference is what is
    permitted to fall through to a backend invocation when the static
    read returns nothing usable.
    """

    NEVER = "never"
    """Static metadata only, from any source.

    Wheels, PEP 643 sdists, sdists with a static ``pyproject.toml`` fallback,
    local checkouts via ``[[tool.nab.local-sources]]``, VCS clones via
    ``[[tool.nab.vcs-sources]]``, and archive sources via
    ``[[tool.nab.archive-sources]]`` are all read statically.  A source whose
    metadata cannot be read statically raises :class:`UnsupportedSdistError`,
    which skips a PyPI sdist version but ends the resolve for a declared
    source.
    """

    BUILD_LOCAL = "build-local"
    """Static metadata everywhere, plus PEP 517 builds on local checkouts.

    Adds backend invocation for ``[[tool.nab.local-sources]]`` and
    workspace members when their ``pyproject.toml`` cannot be read
    statically.  VCS clones, archive sources, and remote PyPI sdists
    remain static-only.
    """

    BUILD_REMOTE = "build-remote"
    """Builds extend to VCS clones, archive sources, and remote PyPI sdists.

    On top of :attr:`BUILD_LOCAL`, invokes the backend on VCS-cloned
    trees, extracted archive trees, and fetched sdists when their
    metadata is dynamic and has no static fallback.
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
    """Wait for each listing, so the sort key cannot see which had arrived."""


@dataclass(frozen=True, slots=True)
class LocalSource:
    """A source tree on disk used as the only candidate for a package.

    ``name`` is the package name; the resolver pins the package to a
    single synthetic version, read from the directory's
    ``[project].version`` field or computed by the build backend when
    that field is declared dynamic.  ``path`` is the absolute filesystem
    path to the source tree.

    ``editable`` requests a PEP 660 editable install in the lockfile;
    ``subdirectory`` is a path under ``path`` for monorepo layouts.
    """

    name: str
    path: str
    editable: bool = False
    subdirectory: str | None = None


@dataclass(frozen=True, slots=True)
class VcsSource:
    """A VCS reference used as the only candidate for a package.

    ``name`` is the package name; ``url`` is the pip-style VCS URL
    (e.g. ``git+https://github.com/x/y.git@<sha>#subdirectory=pkg``).
    The provider clones the repo to its cache and treats the
    checked-out source as a :class:`LocalSource` for metadata
    extraction.
    """

    name: str
    url: str


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    """A direct-URL archive used as the only candidate for a package.

    ``name`` is the package name; ``url`` is the archive URL carrying its
    hash (and optional subdirectory) in the fragment, e.g.
    ``https://example.com/x-1.0.tar.gz#sha256=<hex>``.  The provider
    downloads and hash-verifies the archive, then extracts and treats it
    as a :class:`LocalSource` for metadata extraction.
    """

    name: str
    url: str
