"""Turn a project's PEP 508 requirements into the resolver's input shape.

Pure: no index, coordinator or provider involved.  It asks the config only
whether a direct-URL requirement is admitted, so it takes a
:class:`~nab_provider.vcs_admission.VcsConfig` rather than the whole thing,
and takes the marker predicate as an argument.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, NamedTuple

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_resolver.errors import ResolutionError
from nab_resolver.types import RootRequirement

from .conflict_kind import (
    KIND_EXTRA,
    MARKER_VARIABLE_FOR_KIND,
    membership_set_in_marker,
)
from .errors import ConfigError
from .extra_keys import join_extra, split_extra
from .vcs_admission import admit_vcs_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from nab_provider._vendor.packaging.markers import Marker
    from nab_provider._vendor.packaging.requirements import Requirement

    from .vcs_admission import VcsConfig

    # Whether a dependency marker holds for one environment.
    MarkerHolds = Callable[[Marker, Mapping[str, str]], bool]


_logger = logging.getLogger(__name__)

# What a dropped entry is called in its warning.  A kind not listed here is
# named after itself, since ``kind`` is a free string on the public entry point.
_DROPPED_MARKER_SUBJECT = {
    "requirement": "Root requirement",
    "constraint": "Constraint",
}

_EXTRAS_VARIABLE = MARKER_VARIABLE_FOR_KIND[KIND_EXTRA]


def raise_for_unsatisfiable(
    ranges: Mapping[str, VersionRange],
    sources: Mapping[str, Sequence[str]],
    *,
    kind: str,
) -> None:
    """Raise :class:`ResolutionError` if any folded range is empty.

    ``ranges`` holds one intersected range per package and ``sources`` the
    requirement strings folded into each, which the error lists.  ``kind``
    ("requirement" or "constraint") only shapes the wording.
    """
    unsatisfiable = [name for name, range_ in ranges.items() if range_.is_empty]
    if not unsatisfiable:
        return

    detail = "\n".join(
        f"  {name}: {', '.join(sources[name])}" for name in unsatisfiable
    )
    msg = f"conflicting {kind}s leave no satisfiable version:\n{detail}"
    raise ResolutionError(msg)


def _repair_advice(*, kind: str, tests_extra: bool) -> str:
    """Return the fix a dropped entry's warning ends with.

    A constraint cannot be pointed at ``pkg[extra]``, since the constraint
    parser rejects a constraint carrying extras, so it is asked to edit the
    marker instead.  Every other kind keeps the extras-of-package advice.
    """
    if kind == "constraint":
        if tests_extra:
            return (
                "A constraint cannot carry extras, so drop the membership test "
                "from the marker and keep the rest."
            )
        return "Drop the membership test from the marker and keep the rest."
    return "For an extra, use pkg[extra] (extras-of-package)."


def _warn_dropped_membership_marker(
    req: Requirement, warned: set[tuple[str, str]], *, kind: str
) -> None:
    """Warn when a dropped top-level entry tests an extra or group membership.

    A selected extra or group is folded into the requirements rather than the
    environment, so ``extra``, ``extras`` and ``dependency_groups`` are empty
    whatever the run selects and a membership test never holds.  ``kind`` names
    the entry in the message and picks the repair.  ``warned`` holds the
    ``(kind, text)`` pairs already reported, so one mistake warns once per kind.
    """
    marker_text = str(req.marker)
    membership_set = membership_set_in_marker(marker_text)
    if "extra ==" not in marker_text and membership_set is None:
        return

    text = str(req)
    if (kind, text) in warned:
        return
    warned.add((kind, text))

    tests_extra = "extra ==" in marker_text or membership_set == _EXTRAS_VARIABLE
    _logger.warning(
        "%s %r tests an extra or dependency-group membership marker; it is "
        "dropped because a selected extra or group is folded into the "
        "requirements rather than the environment, so extra, extras and "
        "dependency_groups are empty at resolve time. %s",
        _DROPPED_MARKER_SUBJECT.get(kind, kind.capitalize()),
        text,
        _repair_advice(kind=kind, tests_extra=tests_extra),
    )


class _ResolverInputs(NamedTuple):
    """What one requirement set gives the resolver and the provider."""

    roots: list[RootRequirement[str, VersionRange]]
    ranges: dict[str, VersionRange]
    extras: set[tuple[str, str]]


def build_resolver_inputs(
    requirements: Sequence[Requirement],
    vcs: VcsConfig,
    *,
    environment: Mapping[str, str],
    marker_holds: MarkerHolds,
    kind: str = "requirement",
    warned: set[tuple[str, str]] | None = None,
) -> _ResolverInputs:
    """Convert PEP 508 requirements to the resolver's input shape.

    A requirement whose marker ``marker_holds`` rejects under ``environment``
    is skipped.  A direct-URL or VCS one is checked against ``vcs`` by
    :func:`admit_vcs_url` and then raises ``NotImplementedError``.

    Each surviving requirement becomes its own
    :class:`~nab_resolver.types.RootRequirement`, tagged with the string the
    user wrote, so a failure names the requirements rather than their
    intersection.  ``ranges`` folds the same requirements per package.

    A ``"constraint"`` ``kind`` may not carry extras, and returns an empty
    extras set.  Constraints do not become root clauses, so an empty
    constraint intersection is caught here by :func:`raise_for_unsatisfiable`
    rather than by the solver.

    ``warned`` collects the entries already reported, keyed by ``kind``:
    callers sharing one set warn once between them, a caller that omits it
    warns per call.
    """
    roots: list[RootRequirement[str, VersionRange]] = []
    resolver_requirements: dict[str, VersionRange] = {}
    root_extras: set[tuple[str, str]] = set()
    already_warned: set[tuple[str, str]] = set() if warned is None else warned

    for req in requirements:
        if kind == "constraint" and req.extras:
            msg = f"Constraints cannot have extras: {req}"
            raise ConfigError(msg)

        if req.marker is not None and not marker_holds(req.marker, environment):
            _warn_dropped_membership_marker(req, already_warned, kind=kind)
            continue

        if req.url is not None:
            admit_vcs_url(req.url, vcs)
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

            # A proxy key carries no version, so a second mention would only
            # repeat a line in the failure report.
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

    The resolver keys a constraint by the package it is deciding, and an extras
    proxy decides under its own ``name[extra]`` key, which the user never wrote
    a constraint for.
    """

    def __init__(self, ranges: Mapping[str, VersionRange]) -> None:
        """Wrap the per-package ranges the user's constraints folded to."""
        self._ranges = ranges

    def __getitem__(self, package: str) -> VersionRange:
        """Answer with the base's bound; a proxy has no bound of its own."""
        return self._ranges[split_extra(package)[0]]

    def __iter__(self) -> Iterator[str]:
        """Enumerate only the keys the user wrote."""
        return iter(self._ranges)

    def __len__(self) -> int:
        """Count the keys the user wrote."""
        return len(self._ranges)
