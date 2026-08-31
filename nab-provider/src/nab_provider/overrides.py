"""The per-package and per-index override records.

The config layer parses them and the provider applies them, so neither may own
them; they live here.
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
    table, which sets ``name_keyed``) or a ``[[tool.nab.package-rules]]``
    entry (one body across the requirements in its ``match`` selector).

    The selector is a single PEP 508 ``requirement`` (name plus an optional
    version specifier; no extras, marker, or URL); ``name`` is its canonical
    package name and ``version_range`` its range, so a policy field applies
    only to candidate versions inside it.  An entry that sets ``index`` must
    use a bare-name requirement (full range), because routing decides where
    to fetch a listing before any version is known.

    The body sets any combination of the fields below.
    ``dist_trust_unverified_deps`` folds in the sdist-trust flag, and
    ``uploaded_prior_to_disabled`` is the ``false`` form of the cutoff.

    The metadata-override fields ``dependencies``, ``requires_python``, and
    ``provides_extra`` each replace, independently, what nab would parse from
    the distribution over the matched version range (uv ``dependency-metadata``
    parity).  ``None`` means the entry does not set the field; an empty value
    (``()`` for the two tuples) means "replace with nothing".
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
    # Whether the entry is a table keyed by this selector, which a second
    # entry under the same key cannot be declared beside.
    name_keyed: bool = False
    # The config surface this entry was declared on (e.g. "packages.'numpy'"
    # or "package-rules[0]").  Only used to name the source in an error that
    # is raised after the two project files merge, so it is excluded from
    # equality.
    source_label: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class IndexOverride:
    """One ``[tool.nab.index.<name>]`` entry: policy for an index.

    Keyed by a declared index name and applied to every package served from
    it, so it carries no routing and no version scope.
    ``assume_fresh_seconds`` is a read-time freshness floor on the index's
    Simple listing.
    """

    dist_policy: DistPolicy | None = None
    dist_trust_unverified_deps: bool | None = None
    build_policy: BuildPolicy | None = None
    uploaded_prior_to: datetime | None = None
    uploaded_prior_to_disabled: bool = False
    assume_fresh_seconds: int | None = None
