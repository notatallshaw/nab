"""Resolve opaque requirements using a host's native candidate callbacks."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from typing import Generic, Protocol, TypeVar

from nab_resolver.predicate_ranges import PredicateAlgebra, PredicateRange
from nab_resolver.priority import compute_tier
from nab_resolver.proofs import external_clauses, select_dependency_causes
from nab_resolver.resolver import BaseProvider
from nab_resolver.types import (
    DecisionRetreat,
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    RootRequirement,
    Term,
)

from ._compat import override

__all__ = ["CallbackHost", "CallbackProvider", "CandidateQuery", "RequirementCause"]

PackageType = TypeVar("PackageType", bound=Hashable)
RequirementType = TypeVar("RequirementType", bound=Hashable)
CandidateType = TypeVar("CandidateType", bound=Hashable)
PriorityType = TypeVar("PriorityType")
CovariantPriorityType_co = TypeVar("CovariantPriorityType_co", covariant=True)


class CandidateQuery(Generic[CandidateType]):
    """A single candidate stream and its declaration-dependent exhaustion policy."""

    __slots__ = ("candidates", "may_expand")

    def __init__(
        self, candidates: Iterable[CandidateType], *, may_expand: bool = False
    ) -> None:
        """Keep availability evidence with the query that establishes it."""
        self.candidates = candidates
        self.may_expand = may_expand

    def __iter__(self) -> Iterator[CandidateType]:
        """Visit candidates in the producer's order."""
        return iter(self.candidates)


class CallbackHost(
    Protocol[PackageType, RequirementType, CandidateType, CovariantPriorityType_co]
):
    """Supply stable native meanings and a natively ordered candidate stream."""

    def identify(
        self, requirement_or_candidate: RequirementType | CandidateType
    ) -> PackageType:
        """Return the package identifier of a native value."""
        ...

    def find_matches(
        self,
        package: PackageType,
        requirements: Mapping[PackageType, Sequence[RequirementType]],
        incompatibilities: Mapping[PackageType, Sequence[CandidateType]],
    ) -> CandidateQuery[CandidateType]:
        """Return a native stream and its exhaustion policy for live declarations."""
        ...

    def is_satisfied_by(
        self, requirement: RequirementType, candidate: CandidateType
    ) -> bool:
        """Evaluate a stable native requirement against a candidate."""
        ...

    def exact_candidate(self, requirement: RequirementType) -> CandidateType | None:
        """Return a currently eligible identity forced by this declaration, if any."""
        ...

    def get_dependencies(self, candidate: CandidateType) -> Iterable[RequirementType]:
        """Read original dependency declarations for a candidate."""
        ...

    def preference(
        self,
        package: PackageType,
        requirements: Mapping[PackageType, Sequence[RequirementType]],
    ) -> CovariantPriorityType_co:
        """Return mutually orderable package priorities without preparation."""
        ...


class RequirementCause(Generic[RequirementType, CandidateType]):
    """Retain one original declaration and its selected parent."""

    __slots__ = ("parent", "requirement")

    def __init__(
        self, requirement: RequirementType, parent: CandidateType | None
    ) -> None:
        """Retain the original declaration and its parent."""
        self.requirement = requirement
        self.parent = parent


class _RetreatChoice(Generic[PackageType, CandidateType]):
    """A native choice awaiting a selected child decision."""

    __slots__ = ("candidate", "child")

    def __init__(self, candidate: CandidateType, child: PackageType) -> None:
        self.candidate = candidate
        self.child = child


class CallbackProvider(
    BaseProvider[PackageType, CandidateType],
    Generic[PackageType, RequirementType, CandidateType, PriorityType],
):
    """Own predicate ranges, dependency facts and live declaration scope."""

    def __init__(
        self,
        host: CallbackHost[PackageType, RequirementType, CandidateType, PriorityType],
        roots: Sequence[RequirementType],
    ) -> None:
        """Start fresh predicate, declaration and proof state."""
        self.host = host
        self.roots = tuple(roots)
        self.root_packages = frozenset(
            host.identify(requirement) for requirement in roots
        )
        self.support_order: dict[PackageType, int] | None = None
        self.algebra = PredicateAlgebra[CandidateType]()
        self.requirement_ranges: dict[
            RequirementType, PredicateRange[CandidateType]
        ] = {}
        self.dependencies: dict[CandidateType, tuple[RequirementType, ...]] = {}
        self.dependency_ranges: dict[
            CandidateType, dict[PackageType, PredicateRange[CandidateType]]
        ] = {}
        self.decisions: Mapping[PackageType, CandidateType] = {}
        self.positive_ranges: Mapping[PackageType, RangeProtocol[CandidateType]] = {}
        self.active: dict[PackageType, tuple[RequirementType, ...]] | None = None
        self.pending: list[Incompatibility[PackageType, CandidateType]] = []
        self.failed_queries: set[PackageType] = set()
        self.force_targets: list[DecisionRetreat[PackageType]] = []
        self.considered: set[CandidateType] = set()
        self.matches: dict[tuple[RequirementType, CandidateType], bool] = {}

    def restriction(
        self, requirement: RequirementType
    ) -> PredicateRange[CandidateType]:
        """Memoize the exact native meaning of a declaration."""
        if requirement not in self.requirement_ranges:
            exact = self.host.exact_candidate(requirement)
            self.requirement_ranges[requirement] = (
                PredicateRange.singleton(exact)
                if exact is not None
                else self.algebra.restriction(
                    lambda candidate: self.is_satisfied_by(requirement, candidate)
                )
            )
        return self.requirement_ranges[requirement]

    def is_satisfied_by(
        self, requirement: RequirementType, candidate: CandidateType
    ) -> bool:
        """Cache immutable host requirement meanings within this operation."""
        key = requirement, candidate
        if key not in self.matches:
            self.matches[key] = self.host.is_satisfied_by(requirement, candidate)
        return self.matches[key]

    def incompatibilities(
        self, package: PackageType, allowed: RangeProtocol[CandidateType]
    ) -> dict[PackageType, tuple[CandidateType, ...]]:
        """Let native streams skip candidates already excluded by learned bounds."""
        return {
            package: tuple(
                candidate
                for candidate in self.considered
                if self.host.identify(candidate) == package and candidate not in allowed
            )
        }

    def root_requirements(self) -> list[RootRequirement[PackageType, CandidateType]]:
        """Bind original roots to native predicate restrictions."""
        return [
            RootRequirement(
                self.host.identify(requirement),
                self.restriction(requirement),
                requirement,
            )
            for requirement in self.roots
        ]

    def active_requirements(self) -> Mapping[PackageType, tuple[RequirementType, ...]]:
        """Rebuild live declarations after a decision or dependency read."""
        if self.active is None:
            grouped: dict[PackageType, list[RequirementType]] = defaultdict(list)
            for requirement in self.roots:
                grouped[self.host.identify(requirement)].append(requirement)
            for package, candidate in self.decisions.items():
                if self._root_order(package) == len(self.roots):
                    continue
                for requirement in self.dependencies.get(candidate, ()):
                    grouped[self.host.identify(requirement)].append(requirement)
            self.active = {
                package: tuple(requirements)
                for package, requirements in grouped.items()
            }
        return self.active

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[PackageType, RangeProtocol[CandidateType]],
        decisions: Mapping[PackageType, CandidateType],
    ) -> None:
        """Replace the live selection and discard its declaration view."""
        self.positive_ranges = positive_ranges
        self.decisions = decisions
        self.active = None
        self.support_order = None

    def choose_version(
        self, package: PackageType, version_range: RangeProtocol[CandidateType]
    ) -> CandidateType | None:
        """Choose a native match or exclude only this live context."""
        requirements = self.active_requirements()
        query = (
            self.host.find_matches(
                package, requirements, self.incompatibilities(package, version_range)
            )
            if package in requirements
            else CandidateQuery[CandidateType](())
        )
        chosen = self._choose_candidate(package, version_range, query)
        if chosen is not None:
            return chosen

        self.failed_queries.add(package)
        if self.pending:
            return None
        if not query.may_expand and not self.unread_exact_dependencies(requirements):
            terms = [Term(package, version_range, positive=True)]
            terms.extend(
                Term(
                    parent,
                    PredicateRange[CandidateType].singleton(candidate),
                    positive=True,
                )
                for parent, candidate in self.decisions.items()
            )
            self.pending.append(
                Incompatibility(
                    terms, cause=IncompatibilityCause.CONTEXTUAL_NO_VERSIONS
                )
            )
        return None

    def _choose_candidate(
        self,
        package: PackageType,
        version_range: RangeProtocol[CandidateType],
        query: CandidateQuery[CandidateType],
    ) -> CandidateType | None:
        """Try native alternatives before reconsidering an earlier input."""
        fallback: _RetreatChoice[PackageType, CandidateType] | None = None
        for candidate in query:
            self.considered.add(candidate)
            if candidate not in version_range:
                continue
            accepted = self.dependencies_allow(package, candidate)
            if isinstance(accepted, _RetreatChoice):
                if accepted.child in self.root_packages and self._root_order(
                    package
                ) > self._root_order(accepted.child):
                    if fallback is None:
                        fallback = accepted
                    continue
                self.force_targets.append(DecisionRetreat(accepted.child))
                self.failed_queries.add(package)
                return candidate
            if accepted:
                self.failed_queries.discard(package)
                return candidate
        if fallback is not None:
            self.force_targets.append(DecisionRetreat(fallback.child))
            self.failed_queries.add(package)
            return fallback.candidate
        return None

    def unread_exact_dependencies(
        self, declarations: Mapping[PackageType, Sequence[RequirementType]]
    ) -> bool:
        """Wait for forced, eligible identities whose dependency facts remain unread."""
        for package, requirements in declarations.items():
            for requirement in requirements:
                choice = self.host.exact_candidate(requirement)
                if choice is None or choice in self.dependencies:
                    continue
                allowed = self.positive_ranges.get(package)
                if allowed is not None and not allowed.__contains__(choice):
                    continue
                if all(self.is_satisfied_by(item, choice) for item in requirements):
                    return True
        return False

    def _root_order(self, package: PackageType) -> int:
        """Find the earliest input supporting this package's current demand."""
        if self.support_order is None:
            order: dict[PackageType, int] = {}
            pending: deque[PackageType] = deque()
            for position, requirement in enumerate(self.roots):
                root = self.host.identify(requirement)
                if root not in order:
                    order[root] = position
                    pending.append(root)
            while pending:
                parent = pending.popleft()
                candidate = self.decisions.get(parent)
                if candidate is None:
                    continue
                for requirement in self.dependencies.get(candidate, ()):
                    child = self.host.identify(requirement)
                    if child not in order or order[child] > order[parent]:
                        order[child] = order[parent]
                        pending.append(child)
            self.support_order = order
        return self.support_order.get(package, len(self.roots))

    def _reject_candidate(
        self,
        package: PackageType,
        candidate: CandidateType,
        child: PackageType,
        required: RangeProtocol[CandidateType],
        allowed: RangeProtocol[CandidateType],
    ) -> None:
        """Keep the original dependency and scoped absence behind a parent rejection."""
        parent_term = Term(
            package, PredicateRange[CandidateType].singleton(candidate), positive=True
        )
        dependency = Incompatibility(
            [parent_term, Term(child, required, positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        guards = [
            Term(name, PredicateRange[CandidateType].singleton(value), positive=True)
            for name, value in self.decisions.items()
        ]
        absence = Incompatibility(
            [Term(child, allowed, positive=True), parent_term, *guards],
            cause=IncompatibilityCause.CONTEXTUAL_NO_VERSIONS,
        )
        self.pending.append(
            Incompatibility(
                [parent_term, *guards],
                cause=IncompatibilityCause.DERIVED,
                cause_left=dependency,
                cause_right=absence,
            )
        )

    def dependencies_allow(
        self, package: PackageType, candidate: CandidateType
    ) -> bool | _RetreatChoice[PackageType, CandidateType]:
        """Check child declarations without discarding an earlier input's choices."""
        dependencies = self.get_dependencies(package, candidate)
        declarations = {
            name: list(values) for name, values in self.active_requirements().items()
        }
        for requirement in self.dependencies[candidate]:
            declarations.setdefault(self.host.identify(requirement), []).append(
                requirement
            )

        retreat: _RetreatChoice[PackageType, CandidateType] | None = None
        for child, required in dependencies.items():
            existing = self.positive_ranges.get(child)
            allowed = required if existing is None else required & existing
            exclusions = required if child in self.decisions else allowed
            query = self.host.find_matches(
                child, declarations, self.incompatibilities(child, exclusions)
            )
            alternative = False
            for choice in query:
                self.considered.add(choice)
                if choice in allowed:
                    break
                if child in self.decisions and choice in required:
                    alternative = True
            else:
                if alternative:
                    if (
                        child not in self.root_packages
                        or package in self.root_packages
                        or self._root_order(package) <= self._root_order(child)
                    ):
                        if retreat is None:
                            retreat = _RetreatChoice(candidate, child)
                        self.pending.append(
                            Incompatibility(
                                [
                                    Term(
                                        package,
                                        PredicateRange[CandidateType].singleton(
                                            candidate
                                        ),
                                        positive=True,
                                    ),
                                    Term(child, required, positive=False),
                                ],
                                cause=IncompatibilityCause.DEPENDENCY,
                            )
                        )
                        continue
                    self._reject_candidate(package, candidate, child, required, allowed)
                    return False
                self.failed_queries.add(child)
                if query.may_expand or self.unread_exact_dependencies(declarations):
                    return retreat if retreat is not None else True
                self._reject_candidate(package, candidate, child, required, allowed)
                return False
        return retreat if retreat is not None else True

    @override
    def consume_pending_clauses(
        self,
    ) -> list[Incompatibility[PackageType, CandidateType]]:
        """Drain host-final absence clauses without waiting for unrelated metadata."""
        pending = self.pending
        self.pending = []
        return pending

    @override
    def consume_force_backtrack_targets(self) -> list[DecisionRetreat[PackageType]]:
        """Revisit a selected child when a new parent supplies a native match."""
        targets = self.force_targets
        self.force_targets = []
        return targets

    def has_satisfying_version(
        self, package: PackageType, version_range: RangeProtocol[CandidateType]
    ) -> bool:
        """Probe native matches without recording a decision."""
        requirements = self.active_requirements()
        return package in requirements and any(
            candidate in version_range
            for candidate in self.host.find_matches(
                package, requirements, self.incompatibilities(package, version_range)
            )
        )

    def get_dependencies(
        self, package: PackageType, version: CandidateType
    ) -> Mapping[PackageType, RangeProtocol[CandidateType]]:
        """Cache original declarations and their conjunctive restrictions."""
        del package
        candidate = version
        if candidate not in self.dependency_ranges:
            requirements = tuple(self.host.get_dependencies(candidate))
            dependencies: dict[PackageType, PredicateRange[CandidateType]] = {}
            for requirement in requirements:
                child = self.host.identify(requirement)
                allowed = self.restriction(requirement)
                dependencies[child] = (
                    dependencies.get(child, PredicateRange.full()) & allowed
                )
            self.dependencies[candidate] = requirements
            self.dependency_ranges[candidate] = dependencies
            self.active = None
            self.support_order = None
        return self.dependency_ranges[candidate]

    def prioritize(
        self,
        package: PackageType,
        version_range: RangeProtocol[CandidateType],
        conflict_counts: Mapping[PackageType, int],
        culprit_counts: Mapping[PackageType, int] | None = None,
    ) -> tuple[int, bool, PriorityType]:
        """Apply shared conflict feedback before native package preference."""
        del version_range
        tier = compute_tier(
            package,
            conflict_counts.get(package, 0),
            culprit_counts.get(package, 0) if culprit_counts is not None else 0,
            culprit_counts,
        )
        return (
            tier,
            package not in self.failed_queries,
            self.host.preference(package, self.active_requirements()),
        )

    @override
    def is_ready(self, package: PackageType) -> bool:
        """Refresh priorities while active native declarations can still change."""
        del package
        return False

    def widen_decision(self, package: PackageType, version: CandidateType) -> None:
        """Keep dependency clauses tied to exact candidate identities."""
        del package, version

    def availability_generation(self) -> int:
        """Let the resolver defer empty queries until pending declarations are read."""
        return len(self.dependencies)

    def causes(self) -> Iterable[RequirementCause[RequirementType, CandidateType]]:
        """Expose original declarations retained for host error rendering."""
        yield from (
            RequirementCause[RequirementType, CandidateType](requirement, None)
            for requirement in self.roots
        )
        for candidate, requirements in self.dependencies.items():
            yield from (
                RequirementCause[RequirementType, CandidateType](requirement, candidate)
                for requirement in requirements
            )

    def failure_causes(
        self, derivation: Incompatibility[PackageType, CandidateType]
    ) -> list[RequirementCause[RequirementType, CandidateType]]:
        """Recover original declarations represented by the final failure proof."""
        selected = select_dependency_causes(
            external_clauses(derivation, left_first=False)
        )
        result: list[RequirementCause[RequirementType, CandidateType]] = []
        seen: set[tuple[RequirementType, CandidateType | None]] = set()
        for edge in selected:
            for cause in self.causes():
                if self.host.identify(cause.requirement) != edge.package:
                    continue
                if edge.parent is None:
                    if cause.parent is not None:
                        continue
                elif (
                    cause.parent is None
                    or self.host.identify(cause.parent) != edge.parent
                    or edge.parent_range is None
                    or not edge.parent_range.__contains__(cause.parent)
                ):
                    continue

                key = cause.requirement, cause.parent
                if key not in seen:
                    seen.add(key)
                    result.append(cause)
        packages = {self.host.identify(cause.requirement) for cause in result}
        for requirement in self.roots:
            key = requirement, None
            if self.host.identify(requirement) in packages and key not in seen:
                seen.add(key)
                result.append(
                    RequirementCause[RequirementType, CandidateType](requirement, None)
                )
        return result
