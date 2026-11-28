"""Render incompatibility derivations as human-readable resolution errors.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#error-reporting
"""

from __future__ import annotations

__lazy_modules__ = ("nab_resolver.root",)

from typing import TYPE_CHECKING, TypeAlias

from .proofs import dependency_terms
from .root import ROOT
from .types import IncompatibilityCause, Term

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .types import Incompatibility, PackageType, RangeProtocol, VersionType

    _NarrowRange: TypeAlias = Callable[
        [PackageType, RangeProtocol[VersionType]], RangeProtocol[VersionType]
    ]
    _FormatRange: TypeAlias = Callable[[RangeProtocol[VersionType]], str]

__all__ = [
    "explain_incompatibility",
    "format_error",
    "format_term",
]


def format_error(
    root_incompatibility: Incompatibility[PackageType, VersionType],
    narrow: _NarrowRange[PackageType, VersionType] | None = None,
    format_range: _FormatRange[VersionType] = str,
) -> str:
    """Format a human-readable error from an incompatibility derivation tree.

    ``narrow`` maps ``(package, constraint)`` to a display constraint and is
    applied to positive terms except on a ``NO_VERSIONS`` line, whose stored
    range keeps the sentence true. Negative terms are not narrowed. Rendering
    never mutates the derivation tree.

    When narrowing leaves a line ruling out versions its causes do not account
    for, the dropped range is stated once for that package.

    ``format_range`` renders a constraint for display and defaults to ``str``,
    which reads well for the resolver's own ``Range``. A range type whose
    ``str`` is a debug repr passes its own. Rendering a constraint as the
    empty string drops it from the line along with its separating space.
    """
    lines: list[str] = []
    explain_incompatibility(root_incompatibility, lines, set(), narrow, format_range)
    return "\n".join(lines) if lines else "Resolution impossible"


# DEPENDENCY/ROOT clauses have two terms (parent + dependency); a
# self-dependency merges them into one.
_ATTRIBUTION_CLAUSE_TERMS = 2

# Prefixes that name the package themselves ("no versions of a"), so their
# terms render as requirements.
_REQUIREMENT_PREFIX_CAUSES = frozenset(
    {IncompatibilityCause.ROOT, IncompatibilityCause.NO_VERSIONS}
)


def explain_incompatibility(
    incompatibility: Incompatibility[PackageType, VersionType],
    lines: list[str],
    visited_ids: set[int],
    narrow: _NarrowRange[PackageType, VersionType] | None = None,
    format_range: _FormatRange[VersionType] = str,
) -> None:
    """Walk the cause tree appending one explanatory line per node.

    The walk is iterative: the tree gains a level per conflict, so a deeply
    backtracked resolve overflows the recursion limit.
    """
    # The flag marks a node whose children are already pushed: it renders after them.
    stack: list[tuple[Incompatibility[PackageType, VersionType], bool]] = [
        (incompatibility, False)
    ]

    needed: dict[int, list[PackageType]] = {}
    unstated: dict[PackageType, RangeProtocol[VersionType]] = {}
    if narrow is not None:
        needed, unstated = _unstated_ranges(incompatibility, narrow)

    while stack:
        node, expanded = stack.pop()
        if expanded:
            for package in needed.get(id(node), ()):
                gap = unstated.pop(package, None)
                if gap is not None:
                    subject = _with_range(str(package), format_range(gap))
                    lines.append(f"because no versions of {subject} are available")
            lines.append(_render_line(node, narrow, format_range))
            continue

        if id(node) in visited_ids:
            continue
        visited_ids.add(id(node))

        stack.append((node, True))

        # Right before left, so the left cause pops first and lines keep their order.
        if node.cause == IncompatibilityCause.DERIVED:
            if node.cause_right:
                stack.append((node.cause_right, False))
            if node.cause_left:
                stack.append((node.cause_left, False))


def _narrow_positive(
    term: Term[PackageType, VersionType], narrow: _NarrowRange[PackageType, VersionType]
) -> Term[PackageType, VersionType]:
    """Return ``term`` with a narrowed constraint when originally positive."""
    if not term.is_positive():
        return term
    return Term(term.package, narrow(term.package, term.constraint), positive=True)


def _shown_terms(
    incompatibility: Incompatibility[PackageType, VersionType],
    narrow: _NarrowRange[PackageType, VersionType],
) -> dict[PackageType, Term[PackageType, VersionType]]:
    """Return the terms of ``incompatibility`` as its line renders them.

    The comparison runs on exactly what the reader sees, so a requirement is
    read as printed, un-narrowed, the way the line states it.
    """
    if incompatibility.cause in {
        IncompatibilityCause.NO_VERSIONS,
        IncompatibilityCause.CONTEXTUAL_NO_VERSIONS,
    }:
        return {term.package: term for term in incompatibility.terms}
    return {
        term.package: _narrow_positive(term, narrow) for term in incompatibility.terms
    }


def _satisfying(
    term: Term[PackageType, VersionType] | None,
) -> RangeProtocol[VersionType] | None:
    """Return the versions that satisfy ``term``, or None for no restriction."""
    if term is None:
        return None
    return term.constraint if term.is_positive() else ~term.constraint


def _shortfall(
    conclusion: Term[PackageType, VersionType] | None,
    left: Term[PackageType, VersionType] | None,
    right: Term[PackageType, VersionType] | None,
) -> RangeProtocol[VersionType] | None:
    """Return what a step rules out for one package that its causes do not.

    A step rules out the union of what its causes rule out.  Absent from one
    cause the package carries the other's term; absent from the step it was
    resolved away, which needs the causes to leave nothing.
    """
    joint = left if right is None else right if left is None else left.union(right)
    covered = _satisfying(joint)
    stated = _satisfying(conclusion)
    if covered is None:
        return None
    return ~covered if stated is None else stated - covered


def _unstated_ranges(
    root: Incompatibility[PackageType, VersionType],
    narrow: _NarrowRange[PackageType, VersionType],
) -> tuple[dict[int, list[PackageType]], dict[PackageType, RangeProtocol[VersionType]]]:
    """Return which line needs a package's listing stated, and what to state.

    The first mapping is node id to packages, the second is package to the whole
    range to state for it.  A derivation can reach past its causes at more than
    one line over gaps of one listing, so the ranges are unioned per package and
    the caller states each once, at the first line that needs it.
    """
    needed: dict[int, list[PackageType]] = {}
    unstated: dict[PackageType, RangeProtocol[VersionType]] = {}
    seen_ids: set[int] = set()
    stack: list[Incompatibility[PackageType, VersionType]] = [root]

    while stack:
        node = stack.pop()
        if id(node) in seen_ids:
            continue
        seen_ids.add(id(node))

        for package, gap in _narrowed_away(node, narrow):
            needed.setdefault(id(node), []).append(package)
            previous = unstated.get(package)
            unstated[package] = gap if previous is None else previous | gap

        if node.cause_left is not None:
            stack.append(node.cause_left)
        if node.cause_right is not None:
            stack.append(node.cause_right)

    return needed, unstated


def _narrowed_away(
    incompatibility: Incompatibility[PackageType, VersionType],
    narrow: _NarrowRange[PackageType, VersionType],
) -> list[tuple[PackageType, RangeProtocol[VersionType]]]:
    """Return the packages and ranges narrowing drops from a line's support.

    Narrowing a widened term back onto the listed versions drops the versions the
    widening added, so a line can rule out, or resolve a requirement over, a range
    its causes no longer account for.  Only a shortfall the un-narrowed terms do
    not have is reported, so what is stated is what narrowing dropped: a range
    holding no listed version, since narrowing keeps which listed versions a
    constraint contains.
    """
    left = incompatibility.cause_left
    right = incompatibility.cause_right
    if (
        incompatibility.cause is not IncompatibilityCause.DERIVED
        or left is None
        or right is None
    ):
        return []

    raw_left = {term.package: term for term in left.terms}
    raw_right = {term.package: term for term in right.terms}
    raw_node = {term.package: term for term in incompatibility.terms}
    shown_left = _shown_terms(left, narrow)
    shown_right = _shown_terms(right, narrow)
    shown_node = _shown_terms(incompatibility, narrow)

    found: list[tuple[PackageType, RangeProtocol[VersionType]]] = []
    for package in {**raw_left, **raw_right}:
        raw_gap = _shortfall(
            raw_node.get(package), raw_left.get(package), raw_right.get(package)
        )
        if raw_gap is not None and not raw_gap.is_empty:
            continue
        gap = _shortfall(
            shown_node.get(package), shown_left.get(package), shown_right.get(package)
        )
        if gap is not None and not gap.is_empty:
            found.append((package, gap))
    return found


def _render_line(
    incompatibility: Incompatibility[PackageType, VersionType],
    narrow: _NarrowRange[PackageType, VersionType] | None,
    format_range: _FormatRange[VersionType] = str,
) -> str:
    """Render a single incompatibility as one explanation line."""
    cause = incompatibility.cause
    terms = incompatibility.terms
    # An availability line renders its own range: narrowing it onto the listing
    # is what let it stop covering the requirement it closes against.
    if narrow is not None and cause not in {
        IncompatibilityCause.NO_VERSIONS,
        IncompatibilityCause.CONTEXTUAL_NO_VERSIONS,
    }:
        terms = [_narrow_positive(term, narrow) for term in terms]

    if cause is IncompatibilityCause.CONTEXTUAL_NO_VERSIONS:
        return _render_contextual_no_versions(incompatibility, format_range)

    attributed = dependency_terms(incompatibility, terms)
    if attributed is not None:
        parent, dep = attributed
        plural = _is_full(parent)
        # A negative dep term holds the parent's required range (negate to
        # show it); a positive dep term holds a version the parent forbids.
        if dep.is_positive():
            verb = "are" if plural else "is"
            return (
                f"because {format_term(parent, format_range)} {verb} "
                f"incompatible with {format_term(dep, format_range)}"
            )
        verb = "depend on" if plural else "depends on"
        requirement = _format_requirement(dep.negate(), format_range)
        return f"because {format_term(parent, format_range)} {verb} {requirement}"

    if cause is IncompatibilityCause.ROOT and len(terms) == _ATTRIBUTION_CLAUSE_TERMS:
        _, dep = terms
        positive_dep = dep if dep.is_positive() else dep.negate()
        requirement = _format_requirement(positive_dep, format_range)
        return f"because your project depends on {requirement}"

    if cause is IncompatibilityCause.CONSTRAINT:
        (term,) = terms
        constraint = incompatibility.constraint_range
        if constraint is None:
            msg = (
                f"cannot explain the constraint for {term.package!r} "
                "without its declared range"
            )
            raise ValueError(msg)
        shown = format_range(constraint)
        subject = _with_range(str(term.package), shown)
        return f"because the user constrained {subject}"

    return _render_prefix_line(cause, terms, format_range)


def _render_contextual_no_versions(
    incompatibility: Incompatibility[PackageType, VersionType],
    format_range: _FormatRange[VersionType],
) -> str:
    """Render an absence with its supporting decisions."""
    unavailable, *guards = incompatibility.terms
    context = " and ".join(format_term(term, format_range) for term in guards)
    if incompatibility.constraint_range is not None:
        requirement = _with_range(
            str(unavailable.package), format_range(incompatibility.constraint_range)
        )
        return f"because the user constrained {requirement} with {context} selected"
    requirement = _format_requirement(unavailable, format_range)
    return f"because no versions of {requirement} are available with {context} selected"


def _render_prefix_line(
    cause: IncompatibilityCause,
    terms: Sequence[Term[PackageType, VersionType]],
    format_range: _FormatRange[VersionType] = str,
) -> str:
    """Render a clause that has no attribution form as a prefix and a body."""
    # The virtual root is always selected, so naming it says nothing; a line
    # holding nothing else is the conclusion.
    stated = [term for term in terms if term.package is not ROOT]
    if not stated:
        return "so your project's requirements cannot be satisfied"

    prefix = {
        IncompatibilityCause.ROOT: "because root requires",
        IncompatibilityCause.DEPENDENCY: "because",
        IncompatibilityCause.NO_VERSIONS: "because no versions of",
        IncompatibilityCause.DERIVED: "so",
    }.get(cause, "")
    render = _format_requirement if cause in _REQUIREMENT_PREFIX_CAUSES else format_term
    body = " and ".join(render(term, format_range) for term in stated)

    if cause is IncompatibilityCause.NO_VERSIONS:
        return f"{prefix} {body} are available"
    return f"{prefix} {body}"


def _is_full(term: Term[PackageType, VersionType]) -> bool:
    """Return whether ``term`` is positive over a range with an empty complement."""
    return term.is_positive() and (~term.constraint).is_empty


def _with_range(subject: str, shown: str) -> str:
    """Join a subject to its rendered range, omitting an empty one.

    An unconstrained range renders as the empty string, and a bare space
    before nothing would trail the line.
    """
    return f"{subject} {shown}" if shown else subject


def _format_requirement(
    term: Term[PackageType, VersionType], format_range: _FormatRange[VersionType] = str
) -> str:
    """Render a term in object position ("depends on b").

    A full term there is the package name alone.
    """
    if _is_full(term):
        return str(term.package)
    return format_term(term, format_range)


def format_term(
    term: Term[PackageType, VersionType], format_range: _FormatRange[VersionType] = str
) -> str:
    """Render a single term as ``[not ]package range``.

    A full term reads as "all versions of package"; :func:`_format_requirement`
    renders the object form.
    """
    if _is_full(term):
        return f"all versions of {term.package}"
    sign = "" if term.is_positive() else "not "
    return _with_range(f"{sign}{term.package}", format_range(term.constraint))
