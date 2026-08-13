"""The per-package and per-index override records.

Two declarations the config layer parses and the provider applies, for the
same reason :mod:`nab_provider.policy` exists: both need them, so neither may own
them.  With these in ``config.py`` the provider had to name the config module
to type its own constructor arguments, which put the whole config ladder on the
provider's import graph for two dataclasses.

``config.py`` re-exports both, so ``from nab_python.config import
PackageOverride`` keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider._vendor.packaging.requirements import Requirement

    from .policy import BuildPolicy, DistPolicy

__all__ = ["IndexOverride", "PackageOverride"]


@dataclass(frozen=True, slots=True)
class PackageOverride:
    """One per-package override: a requirement plus a body.

    Built from either ``[tool.nab.packages.<name>]`` (the name-keyed sugar
    table) or a ``[[tool.nab.package-rules]]`` entry (one body across the
    requirements in its ``match`` selector).  The selector is a single PEP
    508 ``requirement`` (name plus an optional version specifier; no
    extras, marker, or URL); ``name`` is its canonical package name and
    ``version_range`` its range, so a policy field applies only to
    candidate versions inside it.  The *body* sets any combination of
    ``dist_policy`` (with ``dist_trust_unverified_deps`` folding in the
    sdist-trust flag), ``build_policy``, the ``uploaded_prior_to`` cutoff
    (or ``uploaded_prior_to_disabled`` for the ``false`` form), the
    routing ``index``, and the metadata-override fields.  An entry that
    sets ``index`` must use a bare-name requirement (full range), because
    routing decides where to fetch a listing before any version is known.

    The metadata-override fields ``dependencies``, ``requires_python``, and
    ``provides_extra`` substitute for what nab would parse from the
    distribution, keyed to the matched version range (uv
    ``dependency-metadata`` parity).  Each replaces its field independently:
    ``dependencies`` becomes the whole runtime ``Requires-Dist`` list,
    ``requires_python`` the Python specifier, and ``provides_extra`` the
    declared extras.  For every one, ``None`` means the entry does not set
    it; a present-but-empty value (``()`` for the two tuples) is a distinct,
    first-class value meaning "replace with nothing".
    """

    requirement: Requirement
    name: str
    version_range: VersionRange
    dist_policy: DistPolicy | None = None
    dist_trust_unverified_deps: bool | None = None
    build_policy: BuildPolicy | None = None
    uploaded_prior_to: datetime | None = None
    uploaded_prior_to_disabled: bool = False
    index: str | None = None
    dependencies: tuple[Requirement, ...] | None = None
    requires_python: str | None = None
    provides_extra: tuple[str, ...] | None = None
    # The config surface this entry was declared on (e.g. "packages.'numpy'"
    # or "package-rules[0]").  Only used to name the source in an error that
    # is raised after the two project files merge, so it is excluded from
    # equality.
    source_label: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class IndexOverride:
    """One ``[tool.nab.index.<name>]`` entry: policy for an index.

    Keyed by a declared index name.  The body sets any combination of
    ``dist_policy`` (with ``dist_trust_unverified_deps``),
    ``build_policy``, the ``uploaded_prior_to`` cutoff (or
    ``uploaded_prior_to_disabled`` for the ``false`` form), and
    ``assume_fresh_seconds``, a read-time freshness floor on the index's
    Simple listing.  It applies to every package served from that index;
    it carries no routing and no version scope.
    """

    dist_policy: DistPolicy | None = None
    dist_trust_unverified_deps: bool | None = None
    build_policy: BuildPolicy | None = None
    uploaded_prior_to: datetime | None = None
    uploaded_prior_to_disabled: bool = False
    assume_fresh_seconds: int | None = None
