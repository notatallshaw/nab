"""Turn a project's PEP 508 requirements into the resolver's input shape.

One step, drawn as its own module because it is the only part of a
resolve that is pure: requirements plus a marker environment in, root
requirements and a ``{key: VersionRange}`` dict out, with no index, no
coordinator and no provider involved.  The engine calls it once per
target for the requirements and once for the constraints.

The whole of its config dependency is ``config.vcs``, read to decide
whether a direct-URL requirement is admitted at all.

Evaluating a root requirement's marker is the caller's, not this module's:
:data:`MarkerHolds` arrives as an argument.  The predicate needs a
set-valued ``extra`` and so needs :mod:`packaging.markersets`, which is the
one part of packaging a host embedding the engine would otherwise have to
carry.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, NamedTuple

from nab_resolver.errors import ResolutionError
from nab_resolver.types import RootRequirement

from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.utils import canonicalize_name
from .conflict_kind import membership_set_in_marker
from .errors import ConfigError
from .extra_keys import join_extra, split_extra
from .vcs_admission import admit_vcs_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from ._vendor.packaging.markers import Marker
    from ._vendor.packaging.requirements import Requirement
    from .config import NabProjectConfig

    # Whether a dependency marker holds for one environment.
    # :func:`nab_python.marker_holds.dependency_marker_holds` is nab's own;
    # a host embedding the engine supplies its own instead.
    MarkerHolds = Callable[[Marker, Mapping[str, str]], bool]


_logger = logging.getLogger(__name__)


def raise_for_unsatisfiable(
    ranges: Mapping[str, VersionRange],
    sources: Mapping[str, Sequence[str]],
    *,
    kind: str,
) -> None:
    """Raise :class:`ResolutionError` if any folded range is empty.

    ``ranges`` holds one intersected range per package and ``sources``
    the requirement strings folded into each.  An empty range means
    those requirements share no version; the error lists them.

    ``kind`` ("requirement" or "constraint") only shapes the wording.
    """
    unsatisfiable = [name for name, range_ in ranges.items() if range_.is_empty]
    if not unsatisfiable:
        return
    detail = "\n".join(
        f"  {name}: {', '.join(sources[name])}" for name in unsatisfiable
    )
    msg = f"conflicting {kind}s leave no satisfiable version:\n{detail}"
    raise ResolutionError(msg)


def _warn_dropped_root_marker(req: Requirement, warned: set[str]) -> None:
    """Warn when a dropped root requirement tests an extra/group membership.

    A root marker testing ``extra``, ``extras``, or ``dependency_groups``
    evaluates False at resolve time (root activates no extra or group), so the
    dep would otherwise be dropped silently.  ``warned`` carries the
    requirements already reported in this run, so one mistaken requirement is
    reported once rather than once per target per fork.
    """
    marker_text = str(req.marker)
    if "extra ==" not in marker_text and not membership_set_in_marker(marker_text):
        return
    text = str(req)
    if text in warned:
        return
    warned.add(text)
    _logger.warning(
        "Root requirement %r tests an extra or dependency-group membership "
        "marker; the dep is dropped because root activates no extra or group "
        "at resolve time. For an extra, use pkg[extra] (extras-of-package).",
        text,
    )


class _ResolverInputs(NamedTuple):
    """What one set of root requirements gives the resolver and the provider."""

    roots: list[RootRequirement[str, VersionRange]]
    ranges: dict[str, VersionRange]
    extras: set[tuple[str, str]]


def build_resolver_inputs(
    requirements: Sequence[Requirement],
    config: NabProjectConfig,
    *,
    environment: Mapping[str, str],
    marker_holds: MarkerHolds,
    kind: str = "requirement",
    warned: set[str] | None = None,
) -> _ResolverInputs:
    """Convert PEP 508 requirements to the resolver's input shape.

    Requirements whose PEP 508 marker ``marker_holds`` rejects under
    ``environment`` are skipped, matching pip/uv's root-requirement
    handling.  A direct-URL or VCS requirement is refused by
    :func:`admit_vcs_url`; resolving one is not implemented.

    Each surviving requirement becomes its own
    :class:`~nab_resolver.types.RootRequirement`, tagged with the string the
    user wrote, so a failure names the requirements rather than their
    intersection.  ``ranges`` folds the same requirements per package for the
    provider, which asks about one package at a time.

    ``kind`` is ``"requirement"`` or ``"constraint"``.  A constraint may not
    carry extras, and the returned extras set is empty for one.  Constraints
    do not become root clauses, so an empty constraint intersection is still
    caught here by :func:`raise_for_unsatisfiable` rather than by the solver.

    ``warned`` is the run's set of already-reported extra/group root
    markers (see :func:`_warn_dropped_root_marker`); a caller that does
    not share one gets a fresh set, so it warns per call.
    """
    roots: list[RootRequirement[str, VersionRange]] = []
    resolver_requirements: dict[str, VersionRange] = {}
    root_extras: set[tuple[str, str]] = set()
    already_warned = set() if warned is None else warned
    for req in requirements:
        if kind == "constraint" and req.extras:
            msg = f"Constraints cannot have extras: {req}"
            raise ConfigError(msg)
        if req.marker is not None and not marker_holds(req.marker, environment):
            _warn_dropped_root_marker(req, already_warned)
            continue
        if req.url is not None:
            admit_vcs_url(req.url, config.vcs)
            msg = (
                f"VCS {kind} admitted by policy but resolver path is not"
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = str(canonicalize_name(req.name))
        previous = resolver_requirements.get(name, VersionRange.full())
        term = (
            req.specifier.to_range()
            if req.specifier
            else VersionRange.full(admit_arbitrary=False)
        )
        resolver_requirements[name] = previous & term
        roots.append(RootRequirement(name, term, str(req)))
        for extra in sorted(req.extras):
            extra_key = join_extra(name, extra)
            # A proxy key carries no version, so a second mention of the same
            # extra would only repeat a line in the failure report.
            if extra_key not in resolver_requirements:
                proxy = VersionRange.full(admit_arbitrary=False)
                resolver_requirements[extra_key] = proxy
                roots.append(RootRequirement(extra_key, proxy, str(req)))
            _, normalized_extra = split_extra(extra_key)
            assert normalized_extra is not None  # join_extra always sets one
            root_extras.add((name, normalized_extra))
    if kind == "constraint":
        sources: defaultdict[str, list[str]] = defaultdict(list)
        for root in roots:
            sources[root.package].append(root.origin)
        raise_for_unsatisfiable(resolver_requirements, sources, kind=kind)
    return _ResolverInputs(roots, resolver_requirements, root_extras)


class ProxyConstraints(Mapping[str, VersionRange]):
    """The user's constraints, where an extras proxy's key reads its base's bound.

    The resolver keys a constraint by the package it is deciding, and an
    extras proxy decides under its own ``name[extra]`` key, so the base's
    bound would not otherwise reach it.  Answering under both keys also
    lets a failure blame the constraint that left the proxy nothing,
    rather than the proxy's listing.

    Iteration lists only the keys the user wrote, so a proxy key answers a
    lookup but is never enumerated.
    """

    def __init__(self, ranges: Mapping[str, VersionRange]) -> None:
        """Wrap the per-package ranges the user's constraints folded to."""
        self._ranges = ranges

    def __getitem__(self, package: str) -> VersionRange:
        """Answer with the base's bound; a proxy has no bound of its own."""
        return self._ranges[split_extra(package)[0]]

    def __iter__(self) -> Iterator[str]:
        """Enumerate only the keys the user wrote, never proxy keys."""
        return iter(self._ranges)

    def __len__(self) -> int:
        """Count the keys the user wrote."""
        return len(self._ranges)
