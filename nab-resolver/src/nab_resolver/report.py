"""Error reporting and term combinators.

Mirrors pubgrub-rs's ``report.rs`` / dart pub's ``failure.dart``: the
message-building walk and the prior-cause / term-union combinators sit
alongside the public ``ResolutionError``.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#error-reporting
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

from .root import ROOT
from .types import IncompatibilityCause, PackageType, Term, VersionType

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .types import Incompatibility

    _NarrowFn: TypeAlias = Callable[[Any, Any], Any]  # (package, constraint) -> shown
    _FormatFn: TypeAlias = Callable[[Any], str]  # constraint -> display string

__all__ = [
    "explain_incompatibility",
    "format_error",
    "format_term",
    "prior_cause",
    "union_terms",
]


def format_error(
    root_incompatibility: Incompatibility[Any, Any],
    narrow: _NarrowFn | None = None,
    format_range: _FormatFn = str,
) -> str:
    """Format a human-readable error from an incompatibility derivation tree.

    Where narrowing leaves a line ruling out versions its causes no longer
    account for, the range it dropped is stated once for that package, as the
    resolver states it when it looks for a version in a range and finds none.

    ``narrow`` maps ``(package, constraint)`` to a display constraint and is
    applied to originally-positive terms only; a negative dependency-side
    term renders as requested even when displayed negated.  On a
    ``NO_VERSIONS`` line a narrowing to the full range is ignored, since the
    range is what keeps the sentence true.  Narrowing happens at render time
    only, never mutating the derivation tree.

    ``format_range`` renders a constraint for display and defaults to ``str``,
    which reads well for the resolver's own ``Range``.  A range type whose
    ``str`` is a debug repr passes its own.  Rendering a constraint as the
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
    incompatibility: Incompatibility[Any, Any],
    lines: list[str],
    visited_ids: set[int],
    narrow: _NarrowFn | None = None,
    format_range: _FormatFn = str,
) -> None:
    """Walk the cause tree appending one explanatory line per node.

    The walk is iterative: the tree gains a level per conflict, so a deeply
    backtracked resolve overflows the recursion limit.
    """
    # The flag marks a node whose children are already pushed: it renders after them.
    stack: list[tuple[Incompatibility[Any, Any], bool]] = [(incompatibility, False)]

    needed, unstated = (
        ({}, {}) if narrow is None else _unstated_ranges(incompatibility, narrow)
    )

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


def _narrow_positive(term: Term[Any, Any], narrow: _NarrowFn) -> Term[Any, Any]:
    """Return ``term`` with a narrowed constraint when originally positive."""
    if not term.is_positive():
        return term
    return Term(term.package, narrow(term.package, term.constraint), positive=True)


def _shown_terms(
    incompatibility: Incompatibility[Any, Any], narrow: _NarrowFn
) -> dict[Any, Term[Any, Any]]:
    """Return the terms of ``incompatibility`` as its line renders them.

    The comparison runs on exactly what the reader sees, so a requirement is
    read as printed, un-narrowed, the way the line states it.
    """
    if incompatibility.cause is IncompatibilityCause.NO_VERSIONS:
        return {term.package: term for term in incompatibility.terms}
    return {
        term.package: _narrow_positive(term, narrow) for term in incompatibility.terms
    }


def _satisfying(term: Term[Any, Any] | None) -> Any:
    """Return the versions that satisfy ``term``, or None for no restriction."""
    if term is None:
        return None
    return term.constraint if term.is_positive() else ~term.constraint


def _shortfall(
    conclusion: Term[Any, Any] | None,
    left: Term[Any, Any] | None,
    right: Term[Any, Any] | None,
) -> Any:
    """Return what a step rules out for one package that its causes do not.

    A step rules out the union of what its causes rule out.  Absent from one
    cause the package carries the other's term; absent from the step it was
    resolved away, which needs the causes to leave nothing.
    """
    joint = (
        left if right is None else right if left is None else union_terms(left, right)
    )
    covered = _satisfying(joint)
    stated = _satisfying(conclusion)
    if covered is None:
        return None
    return ~covered if stated is None else stated - covered


def _unstated_ranges(
    root: Incompatibility[Any, Any], narrow: _NarrowFn
) -> tuple[dict[int, list[Any]], dict[Any, Any]]:
    """Return which line needs a package's listing stated, and what to state.

    The first mapping is node id to packages, the second is package to the whole
    range to state for it.  A derivation can reach past its causes at more than
    one line over gaps of one listing, so the ranges are unioned per package and
    the caller states each once, at the first line that needs it.
    """
    needed: dict[int, list[Any]] = {}
    unstated: dict[Any, Any] = {}
    seen_ids: set[int] = set()
    stack: list[Incompatibility[Any, Any]] = [root]

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
    incompatibility: Incompatibility[Any, Any], narrow: _NarrowFn
) -> list[tuple[Any, Any]]:
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

    found: list[tuple[Any, Any]] = []
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


def _dependency_pair(
    incompatibility: Incompatibility[Any, Any], terms: Sequence[Term[Any, Any]]
) -> tuple[Term[Any, Any], Term[Any, Any]] | None:
    """Return the parent and dependency terms of a DEPENDENCY clause, else None.

    ``terms`` are the clause's terms as the line renders them, which is not
    ``incompatibility.terms`` once narrowing has been applied.  A package
    depending on itself merges the two terms into one, so the dependency side
    is rebuilt from the range the clause carries.
    """
    if incompatibility.cause is not IncompatibilityCause.DEPENDENCY:
        return None

    if len(terms) == _ATTRIBUTION_CLAUSE_TERMS:
        parent, dependency = terms
        return parent, dependency

    dependency_range = incompatibility.dependency_range
    if len(terms) != 1 or dependency_range is None:
        return None

    (parent,) = terms
    return parent, Term(parent.package, dependency_range, positive=False)


def _render_line(
    incompatibility: Incompatibility[Any, Any],
    narrow: _NarrowFn | None,
    format_range: _FormatFn = str,
) -> str:
    """Render a single incompatibility as one explanation line."""
    cause = incompatibility.cause
    terms = incompatibility.terms
    # An availability line renders its own range: narrowing it onto the listing
    # is what let it stop covering the requirement it closes against.
    if narrow is not None and cause is not IncompatibilityCause.NO_VERSIONS:
        terms = [_narrow_positive(term, narrow) for term in terms]

    attributed = _dependency_pair(incompatibility, terms)
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
        shown = format_range(incompatibility.constraint_range)
        subject = _with_range(str(term.package), shown)
        return f"because the user constrained {subject}"

    return _render_prefix_line(cause, terms, format_range)


def _render_prefix_line(
    cause: IncompatibilityCause,
    terms: Sequence[Term[Any, Any]],
    format_range: _FormatFn = str,
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


def _is_full(term: Term[Any, Any]) -> bool:
    """Return whether ``term`` is positive over a range with an empty complement."""
    return term.is_positive() and (~term.constraint).is_empty


def _with_range(subject: str, shown: str) -> str:
    """Join a subject to its rendered range, omitting an empty one.

    An unconstrained range renders as the empty string, and a bare space
    before nothing would trail the line.
    """
    return f"{subject} {shown}" if shown else subject


def _format_requirement(term: Term[Any, Any], format_range: _FormatFn = str) -> str:
    """Render a term in object position ("depends on b").

    A full term there is the package name alone.
    """
    if _is_full(term):
        return str(term.package)
    return format_term(term, format_range)


def format_term(term: Term[Any, Any], format_range: _FormatFn = str) -> str:
    """Render a single term as ``[not ]package range``.

    A full term reads as "all versions of package"; :func:`_format_requirement`
    renders the object form.
    """
    if _is_full(term):
        return f"all versions of {term.package}"
    sign = "" if term.is_positive() else "not "
    return _with_range(f"{sign}{term.package}", format_range(term.constraint))


def prior_cause(
    incompatibility: Incompatibility[PackageType, VersionType],
    satisfier_cause: Incompatibility[PackageType, VersionType],
    shared_package: PackageType,
) -> list[Term[PackageType, VersionType]]:
    """Compute the prior cause by resolving two incompatibilities.

    Follows pubgrub-rs's prior_cause: for the shared package, union
    the terms (and drop if the union is a tautology). For other shared
    packages, intersect the terms. For packages in only one side,
    keep as-is.

    Reference: https://github.com/pubgrub-rs/pubgrub
    """
    incompat_terms: dict[PackageType, Term[PackageType, VersionType]] = {
        term.package: term for term in incompatibility.terms
    }
    cause_terms: dict[PackageType, Term[PackageType, VersionType]] = {
        term.package: term for term in satisfier_cause.terms
    }

    result: list[Term[PackageType, VersionType]] = []

    # Shared package: union, dropping if the result is a tautology.
    incompat_shared = incompat_terms.pop(shared_package, None)
    cause_shared = cause_terms.pop(shared_package, None)
    if incompat_shared is not None and cause_shared is not None:
        unioned = union_terms(incompat_shared, cause_shared)
        if unioned is not None:
            result.append(unioned)
    elif incompat_shared is not None:
        result.append(incompat_shared)
    elif cause_shared is not None:
        result.append(cause_shared)

    # Remaining packages: intersect when in both sides, else keep as-is.
    # Dict merge keeps insertion order; a set union would iterate in hash
    # order, making learned-clause term order vary across processes.
    all_packages = {**incompat_terms, **cause_terms}
    for package in all_packages:
        incompat_term = incompat_terms.get(package)
        cause_term = cause_terms.get(package)
        if incompat_term is not None and cause_term is not None:
            intersected = incompat_term.intersect(cause_term)
            assert intersected is not None
            result.append(intersected)
        elif incompat_term is not None:
            result.append(incompat_term)
        else:
            assert cause_term is not None
            result.append(cause_term)

    return result


def union_terms(
    first: Term[PackageType, VersionType], second: Term[PackageType, VersionType]
) -> Term[PackageType, VersionType] | None:
    """Union two terms for the same package.

    Returns None when the union is a tautology (the term can be dropped
    from the resolvent).  Only a negative result can be one: a positive
    term, even over the full range, still requires the package to be
    selected, so solutions that omit the package don't satisfy it.
    """
    # Positive | Positive = Positive(R1 | R2); never a tautology.
    if first.is_positive() and second.is_positive():
        merged = first.constraint | second.constraint
        return Term(first.package, merged, positive=True)

    # Negative | Negative = Negative(R1 & R2) by De Morgan.
    if not first.is_positive() and not second.is_positive():
        merged = first.constraint & second.constraint
        if merged.is_empty:
            return None
        return Term(first.package, merged, positive=False)

    # Mixed: the negative's range minus the positive's.
    positive_term = first if first.is_positive() else second
    negative_term = second if first.is_positive() else first
    remainder = negative_term.constraint - positive_term.constraint
    if remainder.is_empty:
        return None
    return Term(first.package, remainder, positive=False)
