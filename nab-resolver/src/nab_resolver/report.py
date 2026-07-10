"""Error reporting and term combinators.

Mirrors pubgrub-rs's ``report.rs`` / dart pub's ``failure.dart``: the
message-building walk and the prior-cause / term-union combinators sit
alongside the public ``ResolutionError``.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#error-reporting
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .types import IncompatibilityCause, PackageType, Term, VersionType

if TYPE_CHECKING:
    from .types import Incompatibility

__all__ = [
    "explain_incompatibility",
    "format_error",
    "format_term",
    "prior_cause",
    "union_terms",
]


def format_error(root_incompatibility: Incompatibility[Any, Any]) -> str:
    """Format a human-readable error from an incompatibility derivation tree."""
    lines: list[str] = []
    explain_incompatibility(root_incompatibility, lines, set())
    return "\n".join(lines) if lines else "Resolution impossible"


# DEPENDENCY/ROOT clauses always have two terms (parent + dependency);
# synthetic single-term test clauses fall through to the prefix renderer.
_ATTRIBUTION_CLAUSE_TERMS = 2


def explain_incompatibility(
    incompatibility: Incompatibility[Any, Any],
    lines: list[str],
    visited_ids: set[int],
) -> None:
    """Walk the cause tree appending one explanatory line per node."""
    if id(incompatibility) in visited_ids:
        return
    visited_ids.add(id(incompatibility))

    if incompatibility.cause == IncompatibilityCause.DERIVED:
        if incompatibility.cause_left:
            explain_incompatibility(incompatibility.cause_left, lines, visited_ids)
        if incompatibility.cause_right:
            explain_incompatibility(incompatibility.cause_right, lines, visited_ids)

    lines.append(_render_line(incompatibility))


def _render_line(incompatibility: Incompatibility[Any, Any]) -> str:
    """Render a single incompatibility as one explanation line."""
    cause = incompatibility.cause
    terms = incompatibility.terms

    # Attribution form for the two standard two-term clauses.
    if (
        cause is IncompatibilityCause.DEPENDENCY
        and len(terms) == _ATTRIBUTION_CLAUSE_TERMS
    ):
        parent, dep = terms
        positive_dep = dep if dep.is_positive() else dep.negate()
        return f"because {format_term(parent)} depends on {format_term(positive_dep)}"

    if cause is IncompatibilityCause.ROOT and len(terms) == _ATTRIBUTION_CLAUSE_TERMS:
        _, dep = terms
        positive_dep = dep if dep.is_positive() else dep.negate()
        return f"because your project depends on {format_term(positive_dep)}"

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
    body = " and ".join(format_term(term) for term in terms)

    if cause is IncompatibilityCause.NO_VERSIONS:
        return f"{prefix} {body} are available"
    return f"{prefix} {body}"


def format_term(term: Term[Any, Any]) -> str:
    """Render a single term as ``[not ]package range``."""
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
