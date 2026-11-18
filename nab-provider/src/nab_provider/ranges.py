"""The PEP 440 range backend used by the Python provider."""

from __future__ import annotations

__lazy_modules__ = ("nab_provider._vendor.packaging.specifiers",)

from typing import TYPE_CHECKING

from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_resolver.types import RangeProtocol

    from ._vendor.packaging.version import Version

__all__ = ["VersionRange"]

# More exclusions make the requirement harder to read than the original range.
_MAX_EXCLUSIONS = 3


def require_version_range(
    package: str, constraint: RangeProtocol[Version]
) -> VersionRange:
    """Require the native PEP 440 backend before making a candidate decision."""
    if isinstance(constraint, VersionRange):
        return constraint
    received = type(constraint)
    msg = (
        f"{package!r} uses {received.__module__}.{received.__qualname__}. "
        "Use nab_provider.ranges.VersionRange for Python requirements and "
        'construct Resolver with range_type=VersionRange and root_version="0".'
    )
    raise TypeError(msg)


def narrow_over_listing(
    constraint: VersionRange, universe: Sequence[Version]
) -> VersionRange:
    """Render the same selection over a known ascending release listing."""
    selected = [version for version in universe if version in constraint]
    if not selected:
        return constraint
    if len(selected) == len(universe):
        return VersionRange.full(admit_arbitrary=False)

    spelled = _requirement_over_listing(constraint, universe, selected)
    if spelled is not None:
        return spelled
    has_specifier = constraint.to_specifier_set() is not None
    return constraint if has_specifier else constraint.snap_bounds(universe)


def _requirement_over_listing(
    constraint: VersionRange,
    universe: Sequence[Version],
    selected: Sequence[Version],
) -> VersionRange | None:
    """Return the requirement admitting exactly ``selected`` out of ``universe``.

    Bounds ``selected`` on each side ``constraint`` is bounded, then excludes
    by name every other listed version those bounds admit.  Built out of
    specifiers, so it has a text form to render.

    ``None`` when a bound carries a local segment, which an ordering specifier
    does not accept, when the span holds more than ``_MAX_EXCLUSIONS`` versions
    to exclude, or when excluding one by name would take a selected version
    with it.
    """
    clauses: list[str] = []
    if not VersionRange.from_bounds(None, selected[0]).is_subset(constraint):
        clauses.append(f">={selected[0]}")
    if not VersionRange.from_bounds(selected[-1], None).is_subset(constraint):
        clauses.append(f"<={selected[-1]}")

    try:
        bounded = SpecifierSet(",".join(clauses)).to_range()
    except InvalidSpecifier:
        return None

    chosen = set(selected)
    holes: list[Version] = []
    for version in universe:
        if version in bounded and version not in chosen:
            holes.append(version)
            if len(holes) > _MAX_EXCLUSIONS:
                return None

    if not holes:
        return bounded

    clauses.extend(f"!={hole}" for hole in holes)
    stated = SpecifierSet(",".join(clauses)).to_range()

    # ``!=1.0`` also excludes ``1.0+cu118``: PEP 440 ignores a candidate's
    # local label when the specifier carries none.
    if any(version not in stated for version in selected):
        return None
    return stated
