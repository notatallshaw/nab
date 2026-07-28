"""Error reporting and term combinators.

Mirrors pubgrub-rs's ``report.rs`` / dart pub's ``failure.dart``: the
message-building walk and the prior-cause / term-union combinators sit
alongside the public ``ResolutionError``.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#error-reporting
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

from .types import IncompatibilityCause, PackageType, Term, VersionType

if TYPE_CHECKING:
    from collections.abc import Callable

    from .types import Incompatibility

    _NarrowFn: TypeAlias = Callable[[Any, Any], Any]  # (package, constraint) -> shown

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
) -> str:
    """Format a human-readable error from an incompatibility derivation tree.

    ``narrow`` maps ``(package, constraint)`` to a display constraint and is
    applied to originally-positive terms only; a negative dependency-side
    term renders as requested even when displayed negated.  On a
    ``NO_VERSIONS`` line a narrowing to the full range is ignored, since the
    range is what keeps the sentence true.  Narrowing happens at render time
    only, never mutating the derivation tree.
    """
    lines: list[str] = []
    explain_incompatibility(root_incompatibility, lines, set(), narrow)
    return "\n".join(lines) if lines else "Resolution impossible"


# DEPENDENCY/ROOT clauses always have two terms (parent + dependency);
# synthetic single-term test clauses fall through to the prefix renderer.
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
) -> None:
    """Walk the cause tree appending one explanatory line per node.

    The walk is iterative: the tree gains a level per conflict, so a deeply
    backtracked resolve overflows the recursion limit.
    """
    # The flag marks a node whose children are already pushed: it renders after them.
    stack: list[tuple[Incompatibility[Any, Any], bool]] = [(incompatibility, False)]

    while stack:
        node, expanded = stack.pop()
        if expanded:
            lines.append(_render_line(node, narrow))
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
    term: Term[Any, Any], narrow: _NarrowFn, *, allow_full: bool
) -> Term[Any, Any]:
    """Return ``term`` with a narrowed constraint when originally positive.

    Unless ``allow_full``, a narrowing to the full range is refused and the
    term renders as requested.
    """
    if not term.is_positive():
        return term
    shown = Term(term.package, narrow(term.package, term.constraint), positive=True)
    if allow_full or not _is_full(shown):
        return shown
    return term


def _render_line(
    incompatibility: Incompatibility[Any, Any],
    narrow: _NarrowFn | None,
) -> str:
    """Render a single incompatibility as one explanation line."""
    cause = incompatibility.cause
    terms = incompatibility.terms
    if narrow is not None:
        # Without its range the line would claim the package has no versions
        # at all, not that the ones it has were rejected here.
        allow_full = cause is not IncompatibilityCause.NO_VERSIONS
        terms = [
            _narrow_positive(term, narrow, allow_full=allow_full) for term in terms
        ]

    # Attribution form for the two standard two-term clauses.
    if (
        cause is IncompatibilityCause.DEPENDENCY
        and len(terms) == _ATTRIBUTION_CLAUSE_TERMS
    ):
        parent, dep = terms
        plural = _is_full(parent)
        # A negative dep term holds the parent's required range (negate to
        # show it); a positive dep term holds a version the parent forbids.
        if dep.is_positive():
            verb = "are" if plural else "is"
            return (
                f"because {format_term(parent)} {verb} incompatible with "
                f"{format_term(dep)}"
            )
        verb = "depend on" if plural else "depends on"
        requirement = _format_requirement(dep.negate())
        return f"because {format_term(parent)} {verb} {requirement}"

    if cause is IncompatibilityCause.ROOT and len(terms) == _ATTRIBUTION_CLAUSE_TERMS:
        _, dep = terms
        positive_dep = dep if dep.is_positive() else dep.negate()
        return f"because your project depends on {_format_requirement(positive_dep)}"

    if cause is IncompatibilityCause.CONSTRAINT:
        (term,) = terms
        return (
            f"because the user constrained "
            f"{term.package} {incompatibility.constraint_range}"
        )

    prefix = {
        IncompatibilityCause.ROOT: "because root requires",
        IncompatibilityCause.DEPENDENCY: "because",
        IncompatibilityCause.NO_VERSIONS: "because no versions of",
        IncompatibilityCause.DERIVED: "so",
    }.get(cause, "")
    render = _format_requirement if cause in _REQUIREMENT_PREFIX_CAUSES else format_term
    body = " and ".join(render(term) for term in terms)

    if cause is IncompatibilityCause.NO_VERSIONS:
        return f"{prefix} {body} are available"
    return f"{prefix} {body}"


def _is_full(term: Term[Any, Any]) -> bool:
    """Return whether ``term`` is positive over a range with an empty complement."""
    return term.is_positive() and (~term.constraint).is_empty


def _format_requirement(term: Term[Any, Any]) -> str:
    """Render a term in object position ("depends on b").

    A full term there is the package name alone.
    """
    if _is_full(term):
        return str(term.package)
    return format_term(term)


def format_term(term: Term[Any, Any]) -> str:
    """Render a single term as ``[not ]package range``.

    A full term reads as "all versions of package"; :func:`_format_requirement`
    renders the object form.
    """
    if _is_full(term):
        return f"all versions of {term.package}"
    sign = "" if term.is_positive() else "not "
    return f"{sign}{term.package} {term.constraint}"


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
