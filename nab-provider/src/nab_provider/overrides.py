"""The per-package and per-index override records.

The config layer parses them and the provider applies them, so neither may own
them; they live here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._compat import override
from ._value import SlottedValue

if TYPE_CHECKING:
    from datetime import datetime

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider._vendor.packaging.requirements import Requirement

    from .policy import BuildPolicy, DistPolicy

__all__ = ["IndexOverride", "PackageOverride"]


class PackageOverride(SlottedValue):
    """One per-package override: a requirement plus a body.

    Built from either ``[tool.nab.packages.<name>]`` (the name-keyed sugar
    table, which sets ``name_keyed``) or a ``[[tool.nab.package-rules]]``
    entry (one body across the requirements in its ``match`` selector).
    TOML admits that table only once per name.

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

    ``source_label`` is the config surface the entry was declared on (e.g.
    ``packages.'numpy'`` or ``package-rules[0]``).
    """

    __slots__ = __match_args__ = (
        "requirement",
        "name",
        "version_range",
        "dist_policy",
        "dist_trust_unverified_deps",
        "build_policy",
        "uploaded_prior_to",
        "uploaded_prior_to_disabled",
        "index",
        "dependencies",
        "requires_python",
        "provides_extra",
        "name_keyed",
        "source_label",
    )

    # ``source_label`` records where an entry was written rather than what it
    # declares, so it sits last and stays out of comparison and hashing.
    _COMPARED = __match_args__[:-1]

    def __init__(  # noqa: PLR0913 - one keyword per key an entry's body may set
        self,
        *,
        requirement: Requirement,
        name: str,
        version_range: VersionRange,
        dist_policy: DistPolicy | None = None,
        dist_trust_unverified_deps: bool | None = None,
        build_policy: BuildPolicy | None = None,
        uploaded_prior_to: datetime | None = None,
        uploaded_prior_to_disabled: bool = False,
        index: str | None = None,
        dependencies: tuple[Requirement, ...] | None = None,
        requires_python: str | None = None,
        provides_extra: tuple[str, ...] | None = None,
        name_keyed: bool = False,
        source_label: str = "",
    ) -> None:
        """Record one entry's selector and the body it declares."""
        self.requirement = requirement
        self.name = name
        self.version_range = version_range

        self.dist_policy = dist_policy
        self.dist_trust_unverified_deps = dist_trust_unverified_deps
        self.build_policy = build_policy
        self.uploaded_prior_to = uploaded_prior_to
        self.uploaded_prior_to_disabled = uploaded_prior_to_disabled
        self.index = index

        self.dependencies = dependencies
        self.requires_python = requires_python
        self.provides_extra = provides_extra

        self.name_keyed = name_keyed
        self.source_label = source_label

    @override
    def __eq__(self, other: object) -> bool:
        """Compare every field but ``source_label``."""
        if other.__class__ is not self.__class__:
            return NotImplemented

        names = self._COMPARED
        return tuple(getattr(self, name) for name in names) == tuple(
            getattr(other, name) for name in names
        )

    @override
    def __hash__(self) -> int:
        """Hash every field but ``source_label``."""
        return hash(tuple(getattr(self, name) for name in self._COMPARED))

    def replace(self, **changes: object) -> PackageOverride:
        """Return a copy with ``changes`` applied, as ``dataclasses.replace`` would."""
        kept: dict[str, Any] = {
            name: getattr(self, name) for name in self.__match_args__
        }
        kept.update(changes)
        return PackageOverride(**kept)


class IndexOverride(SlottedValue):
    """One ``[tool.nab.index.<name>]`` entry: policy for an index.

    Keyed by a declared index name and applied to every package served from
    it, so it carries no routing and no version scope.
    ``assume_fresh_seconds`` is a read-time freshness floor on the index's
    Simple listing.
    """

    __slots__ = __match_args__ = (
        "dist_policy",
        "dist_trust_unverified_deps",
        "build_policy",
        "uploaded_prior_to",
        "uploaded_prior_to_disabled",
        "assume_fresh_seconds",
    )

    def __init__(
        self,
        *,
        dist_policy: DistPolicy | None = None,
        dist_trust_unverified_deps: bool | None = None,
        build_policy: BuildPolicy | None = None,
        uploaded_prior_to: datetime | None = None,
        uploaded_prior_to_disabled: bool = False,
        assume_fresh_seconds: int | None = None,
    ) -> None:
        """Record the policy declared for one index."""
        self.dist_policy = dist_policy
        self.dist_trust_unverified_deps = dist_trust_unverified_deps
        self.build_policy = build_policy
        self.uploaded_prior_to = uploaded_prior_to
        self.uploaded_prior_to_disabled = uploaded_prior_to_disabled
        self.assume_fresh_seconds = assume_fresh_seconds

    def replace(self, **changes: object) -> IndexOverride:
        """Return a copy with ``changes`` applied, as ``dataclasses.replace`` would."""
        kept: dict[str, Any] = {
            name: getattr(self, name) for name in self.__match_args__
        }
        kept.update(changes)
        return IndexOverride(**kept)
