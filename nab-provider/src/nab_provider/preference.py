"""Coordinate grounded withdrawn-candidate preference over a host catalogue."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Hashable
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Final, Generic, Protocol, TypeVar

from nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
    ResolutionTerminatedError,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver, ResolverStats

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from typing_extensions import override as _override

    from nab_resolver.types import RangeProtocol
else:
    MethodType = TypeVar("MethodType")

    def _override(method: MethodType) -> MethodType:
        """Keep override annotations free of runtime dependencies on Python 3.10."""
        return method


__all__ = [
    "CataloguePreference",
    "DependencyData",
    "IncompletePreferenceError",
    "PreferenceCatalogue",
    "PreferenceScope",
    "PreferenceSearch",
    "PreparationStatus",
    "UnavailableMetadataError",
]


CandidateType = TypeVar("CandidateType", bound=Hashable)
RangeType = TypeVar("RangeType")
PinnedRequirementType = TypeVar("PinnedRequirementType")
CovariantRangeType = TypeVar(  # noqa: PLC0105 - spell out covariance.
    "CovariantRangeType", covariant=True
)
CovariantPinnedRequirementType = TypeVar(  # noqa: PLC0105 - spell out covariance.
    "CovariantPinnedRequirementType", covariant=True
)
StatisticsPackageType = TypeVar("StatisticsPackageType", bound=Hashable)


class DependencyData(Protocol[CovariantRangeType, CovariantPinnedRequirementType]):
    """Prepared dependency ranges and original admission declarations."""

    @property
    def dependencies(self) -> Mapping[str, CovariantRangeType]:
        """Dependency ranges belonging to this exact candidate."""
        ...

    @property
    def pins(self) -> Sequence[CovariantPinnedRequirementType]:
        """Original exact-pin declarations from this candidate."""
        ...


class PreferenceCatalogue(Protocol[CandidateType, RangeType, PinnedRequirementType]):
    """Use stable candidate lists and append-only, immutable dependency facts.

    Each candidate's identity and existing dependency facts stay fixed.
    Incomplete-proof caches use the fact count as their revision.
    Record a listing failure before returning its query.
    """

    def candidate_package(self, candidate: CandidateType) -> str:
        """Return the identifier decided by the resolver."""
        ...

    def candidate_base(self, candidate: CandidateType) -> str:
        """Return the identifier without an extras proxy."""
        ...

    def candidate_label(self, candidate: CandidateType) -> str:
        """Return the original version text for diagnostic messages."""
        ...

    def candidate_withdrawn(self, candidate: CandidateType) -> bool:
        """Return whether preparation requires exact-pin permission."""
        ...

    @property
    def facts(
        self,
    ) -> Mapping[
        CandidateType, DependencyData[RangeType, PinnedRequirementType] | None
    ]:
        """Prepared candidate facts; None records rejected metadata."""
        ...

    @property
    def missing_listings(self) -> AbstractSet[str]:
        """Package listings whose absence has not been established."""
        ...

    @property
    def unavailable(self) -> AbstractSet[CandidateType]:
        """Candidate metadata whose absence has not been established."""
        ...

    @property
    def unavailable_reason(self) -> str:
        """Describe the latest listing or metadata uncertainty."""
        ...

    def prepare(
        self, candidate: CandidateType
    ) -> DependencyData[RangeType, PinnedRequirementType] | None:
        """Prepare metadata only after the provider has established admission."""
        ...

    def choices(self, package: str) -> Sequence[CandidateType]:
        """Return all fixed candidate identities for one package."""
        ...

    def ordered(self, package: str, allowed: RangeType, /) -> Sequence[CandidateType]:
        """Order candidates within the supplied version bounds."""
        ...

    def full_range(self) -> RangeType:
        """Return the identity for version-range intersection."""
        ...

    def intersect_ranges(self, left: RangeType, right: RangeType) -> RangeType:
        """Intersect two ranges using the native version algebra."""
        ...

    def range_contains(self, candidate: CandidateType, allowed: RangeType) -> bool:
        """Test a candidate version against native bounds."""
        ...

    def pin_allows(self, pin: PinnedRequirementType, candidate: CandidateType) -> bool:
        """Check whether an original pin permits this withdrawn candidate."""
        ...

    def pin_project(self, pin: PinnedRequirementType) -> str:
        """Return the normalized project named by an original pin."""
        ...

    def base_name(self, package: str) -> str:
        """Remove an extras proxy from a package identifier."""
        ...

    def base_candidate(self, candidate: CandidateType) -> CandidateType:
        """Return the matching base identity for an extra candidate."""
        ...


class UnavailableMetadataError(Exception):
    """Unread metadata cannot establish the failure of a preferred choice."""


class PreferenceScope(Generic[CandidateType]):
    """Keep selected ancestors fixed and selected non-yanked packages non-yanked."""

    __slots__ = ("_fixed", "_live", "_target")

    def __init__(
        self,
        fixed: frozenset[CandidateType] = frozenset(),
        live: frozenset[str] = frozenset(),
        target: str = "",
    ) -> None:
        """Fix selected ancestors and preserve the required non-yanked choices."""
        self._fixed: Final[frozenset[CandidateType]] = fixed
        self._live: Final[frozenset[str]] = live
        self._target: Final[str] = target

    @property
    def fixed(self) -> frozenset[CandidateType]:
        """Candidate identities fixed by independently grounded ancestors."""
        return self._fixed

    @property
    def live(self) -> frozenset[str]:
        """Package bases restricted to non-yanked choices."""
        return self._live

    @property
    def target(self) -> str:
        """Package whose non-yanked alternatives this query must retain."""
        return self._target

    @_override
    def __hash__(self) -> int:
        """Hash the immutable query restrictions."""
        return hash((self.fixed, self.live, self.target))

    @_override
    def __eq__(self, other: object) -> bool:
        """Compare each restriction without inspecting candidate metadata."""
        if not isinstance(other, PreferenceScope):
            return NotImplemented
        return (self.fixed, self.live, self.target) == (
            other.fixed,
            other.live,
            other.target,
        )


class PreparationStatus(Enum):
    """Whether non-yanked alternatives allow preparing a yanked candidate."""

    ALLOWED = auto()
    IMPOSSIBLE = auto()
    INCOMPLETE = auto()


class IncompletePreferenceError(ResolutionError):
    """An interrupted or unknown query cannot prove that live choices fail."""


class _NeedLiveProofError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Context(Generic[CandidateType]):
    """A selected assignment whose known metadata may already establish failure."""

    scope: PreferenceScope[CandidateType]
    selected: frozenset[CandidateType]


class _NeedContextProofError(Exception):
    pass


class PreferenceSearch(ABC, Generic[CandidateType, RangeType, PinnedRequirementType]):
    """Schedule scoped preference proofs against one catalogue snapshot."""

    def __init__(
        self,
        roots: Mapping[str, RangeType],
        input_pins: Sequence[PinnedRequirementType],
        constraints: Mapping[str, RangeType],
        *,
        max_steps: int = 100_000,
        refresh_incomplete: bool = False,
        exact_dependencies: Callable[[CandidateType], Sequence[CandidateType]]
        | None = None,
    ) -> None:
        """Retain shared facts and the original target declarations."""
        self.roots = roots
        self.input_pins = input_pins
        self.constraints = constraints
        self.remaining = max_steps
        self.refresh_incomplete = refresh_incomplete
        self.exact_dependencies = exact_dependencies
        self.stats: ResolverStats[Hashable] = ResolverStats()

        self.outcomes: dict[PreferenceScope[CandidateType], PreparationStatus] = {}
        self.incomplete_facts: dict[PreferenceScope[CandidateType], int] = {}
        self.refuted: set[_Context[CandidateType]] = set()
        self.possible: dict[_Context[CandidateType], int] = {}

        # These typed requests survive unwinding the active resolver callback.
        self.requested_scope: PreferenceScope[CandidateType] | None = None
        self.requested_context: _Context[CandidateType] | None = None

    def spend(self, steps: int) -> None:
        """Charge work from any resolver query against the cumulative limit."""
        self.remaining -= steps
        if self.remaining < 0:
            message = "live preference analysis is incomplete: search limit reached"
            raise ResolutionLimitError(message)

    @property
    @abstractmethod
    def catalogue(
        self,
    ) -> PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType]:
        """Return the host catalogue retained for this search."""
        ...

    def record_solver_stats(self, stats: ResolverStats[StatisticsPackageType]) -> None:
        """Accumulate scalar and per-package work across all scoped queries."""
        for name in ResolverStats.__slots__:
            value = getattr(stats, name)
            if isinstance(value, int):
                setattr(self.stats, name, getattr(self.stats, name) + value)
            else:
                target = getattr(self.stats, name)
                for package, count in value.items():
                    target[package] += count

    def parents(
        self, candidate: CandidateType, selected: Mapping[str, CandidateType]
    ) -> frozenset[CandidateType]:
        """Find ancestors reachable from roots without the candidate's metadata."""
        demand = set(self.roots)
        pins = list(self.input_pins)
        expanded: set[str] = set()

        # The target's metadata must not establish its own admission.
        while True:
            before = len(expanded)
            for name, parent in selected.items():
                if (
                    self.catalogue.candidate_base(parent)
                    == self.catalogue.candidate_base(candidate)
                    or name not in demand
                    or name in expanded
                ):
                    continue

                data = self.catalogue.facts.get(parent)
                if data is None:
                    continue

                if self.catalogue.candidate_withdrawn(parent) and not any(
                    self.catalogue.pin_allows(req, parent) for req in pins
                ):
                    continue

                expanded.add(name)
                demand.update(data.dependencies)
                pins.extend(data.pins)
            if len(expanded) == before:
                break

        ancestors: set[str] = set()
        pending = [
            name
            for name, value in selected.items()
            if self.catalogue.candidate_base(value)
            == self.catalogue.candidate_base(candidate)
        ]
        pending.extend(
            (
                self.catalogue.candidate_base(candidate),
                self.catalogue.candidate_package(candidate),
            )
        )
        while pending:
            child = pending.pop()
            for name in expanded - ancestors:
                data = self.catalogue.facts[selected[name]]
                assert data is not None
                if child in data.dependencies:
                    ancestors.add(name)
                    pending.append(name)
        return frozenset(selected[name] for name in ancestors)

    def fixed_range(
        self, candidate: CandidateType, parents: frozenset[CandidateType]
    ) -> RangeType:
        """Intersect target requirements from inputs and reachable selected parents."""
        required = {
            self.catalogue.candidate_base(candidate),
            self.catalogue.candidate_package(candidate),
        }
        allowed = self.catalogue.full_range()
        declarations = [self.roots]
        for parent in parents:
            data = self.catalogue.facts[parent]
            assert data is not None
            declarations.append(data.dependencies)
        for dependencies in declarations:
            for package, version_range in dependencies.items():
                if self.catalogue.base_name(package) == self.catalogue.candidate_base(
                    candidate
                ):
                    required.add(package)
                    allowed = self.catalogue.intersect_ranges(allowed, version_range)
        for package in required:
            allowed = self.catalogue.intersect_ranges(
                allowed, self.constraints.get(package, self.catalogue.full_range())
            )
        return allowed

    def _has_exact_parent(
        self, candidate: CandidateType, parents: frozenset[CandidateType]
    ) -> bool:
        """Check whether an ancestor fixes this identity or an extra's base."""
        exact_dependencies = self.exact_dependencies
        if exact_dependencies is None:
            return False

        for parent in parents:
            for child in exact_dependencies(parent):
                if child == candidate:
                    return True

                # An extra constrains only its own base artifact.
                if (
                    self.catalogue.candidate_package(child)
                    != self.catalogue.candidate_base(child)
                    and self.catalogue.base_candidate(child) == candidate
                ):
                    return True
        return False

    def check(
        self,
        candidate: CandidateType,
        selected: Mapping[str, CandidateType],
        scope: PreferenceScope[CandidateType],
    ) -> PreparationStatus:
        """Request non-yanked alternatives before preparing yanked metadata."""
        if not self.catalogue.candidate_withdrawn(candidate):
            return PreparationStatus.ALLOWED

        parents = self.parents(candidate, selected)
        allowed = self.fixed_range(candidate, parents)
        if not self.catalogue.range_contains(candidate, allowed):
            return PreparationStatus.IMPOSSIBLE

        exact_parent = self._has_exact_parent(candidate, parents)
        # A fixed exact edge needs the scoped proof, without an unrelated listing.
        if not exact_parent and not any(
            not self.catalogue.candidate_withdrawn(choice)
            for choice in self.catalogue.ordered(
                self.catalogue.candidate_base(candidate), allowed
            )
        ):
            return PreparationStatus.ALLOWED

        live = (
            scope.live
            | {self.catalogue.candidate_base(candidate)}
            | {
                self.catalogue.candidate_base(value)
                for value in selected.values()
                if not self.catalogue.candidate_withdrawn(value)
            }
        )
        query = PreferenceScope(
            parents,
            frozenset(live),
            self.catalogue.candidate_base(candidate),
        )

        # Append-only fact counts identify the revision behind each proof.
        outcome = self.outcomes.get(query)
        if outcome is PreparationStatus.INCOMPLETE:
            if not self.refresh_incomplete or self.incomplete_facts.get(query) == len(
                self.catalogue.facts
            ):
                return outcome
            outcome = None
        if outcome is None:
            if not scope.live < query.live:
                message = "live preference analysis is incomplete: cyclic query"
                raise ResolutionInvariantError(message)
            self.requested_scope = query
            raise _NeedLiveProofError

        context = _Context(scope, frozenset(selected.values()))
        if context in self.refuted:
            return PreparationStatus.IMPOSSIBLE
        if self.possible.get(context) != len(self.catalogue.facts):
            self.requested_context = context
            raise _NeedContextProofError
        return PreparationStatus.ALLOWED

    def run(
        self,
        callback: Callable[[PreferenceScope[CandidateType]], dict[str, CandidateType]],
    ) -> dict[str, CandidateType]:
        """Retry scopes after failed proofs and return a complete solution."""
        stack = [PreferenceScope[CandidateType]()]
        while True:
            self.spend(0)
            if not self.remaining:
                message = "live preference analysis is incomplete: search limit reached"
                raise ResolutionLimitError(message)

            scope = stack[-1]
            # Unwind the callback before a prerequisite consumes the shared budget.
            try:
                selected = callback(scope)
            except _NeedLiveProofError:
                assert self.requested_scope is not None
                stack.append(self.requested_scope)
            except _NeedContextProofError:
                assert self.requested_context is not None
                self.check_context(self.requested_context)
            except ResolutionTerminatedError:
                raise
            except ResolutionError as exc:
                if len(stack) == 1:
                    raise

                self.record_failure(scope, exc)
                stack.pop()
            else:
                if scope.target and not any(
                    self.catalogue.candidate_base(value) == scope.target
                    for value in selected.values()
                ):
                    message = (
                        "live preference analysis is incomplete: target disappeared"
                    )
                    raise ResolutionInvariantError(message)
                return selected

    def record_failure(
        self, scope: PreferenceScope[CandidateType], error: ResolutionError
    ) -> None:
        """Retain a failed scope and the metadata revision supporting its result."""
        incomplete = isinstance(error, IncompletePreferenceError)
        self.outcomes[scope] = (
            PreparationStatus.INCOMPLETE if incomplete else PreparationStatus.IMPOSSIBLE
        )
        if incomplete:
            self.incomplete_facts[scope] = len(self.catalogue.facts)

    def explain(
        self, selected: Mapping[str, CandidateType]
    ) -> dict[str, tuple[str, ...]]:
        """Find an allowing input or dependency pin for each selected yank."""
        declarations: dict[str, list[tuple[PinnedRequirementType, str]]] = {}
        for requirement in self.input_pins:
            declarations.setdefault(self.catalogue.pin_project(requirement), []).append(
                (requirement, f"input pin {requirement}")
            )

        pending = deque(self.roots)
        demanded = set(self.roots)
        expanded: set[str] = set()
        waiting: dict[str, set[str]] = {}
        sources: dict[str, tuple[str, ...]] = {}

        while pending:
            package = pending.popleft()
            candidate = selected.get(package)
            data = (
                self.catalogue.facts.get(candidate) if candidate is not None else None
            )
            if data is None:
                message = (
                    f"cannot explain yanked admission: missing metadata for {package}"
                )
                raise ResolutionInvariantError(message)

            assert candidate is not None
            # An unadmitted yanked parent cannot supply pins to its children.
            if self.catalogue.candidate_withdrawn(candidate):
                source = next(
                    (
                        text
                        for requirement, text in declarations.get(
                            self.catalogue.candidate_base(candidate), ()
                        )
                        if self.catalogue.pin_allows(requirement, candidate)
                    ),
                    None,
                )
                if source is None:
                    waiting.setdefault(
                        self.catalogue.candidate_base(candidate), set()
                    ).add(package)
                    continue
                sources.setdefault(self.catalogue.candidate_base(candidate), (source,))

            expanded.add(package)
            for child in data.dependencies:
                if child not in demanded:
                    demanded.add(child)
                    pending.append(child)

            for requirement in data.pins:
                base = self.catalogue.pin_project(requirement)
                name = self.catalogue.candidate_package(candidate)
                label = self.catalogue.candidate_label(candidate)
                text = f"{name}=={label} requires {requirement}"
                declarations.setdefault(base, []).append((requirement, text))
                pending.extend(sorted(waiting.pop(base, ())))

        if expanded != set(selected):
            message = "cannot explain yanked admission: selection is not rooted"
            raise ResolutionInvariantError(message)
        return sources

    def check_context(self, context: _Context[CandidateType]) -> None:
        """Check for known conflicts after the requesting solver unwinds."""
        provider = _KnownFacts(
            self.catalogue,
            self.constraints,
            context,
            exact_dependencies=self.exact_dependencies,
        )
        solver: Resolver[str, int] = Resolver(provider, max_iterations=self.remaining)
        refuted = False
        try:
            roots = {
                name: provider.encode(name, allowed)
                for name, allowed in self.roots.items()
            }
            solver.resolve(roots)
        except UnavailableMetadataError:
            # Missing listings cannot refute this selected context.
            pass
        except ResolutionTerminatedError:
            raise
        except ResolutionError as exc:
            if exc.incompatibility is None:
                message = (
                    "live preference analysis is incomplete: context check stopped"
                )
                raise IncompletePreferenceError(message) from exc
            refuted = True
        finally:
            self.record_solver_stats(solver.stats)
            self.spend(solver.stats.rounds)

        if refuted:
            self.refuted.add(context)
        else:
            self.possible[context] = len(self.catalogue.facts)


def encode_dependencies(
    catalogue: PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType],
    candidate: CandidateType,
    dependencies: Mapping[str, RangeType],
    exact_dependencies: Callable[[CandidateType], Sequence[CandidateType]] | None,
    encode_range: Callable[[str, RangeType], Range[int]],
    encode_candidate: Callable[[CandidateType, RangeType], Range[int]],
) -> dict[str, Range[int]]:
    """Refine prepared dependency edges with conjunctive candidate identities."""
    exact: dict[str, Range[int]] = {}
    if exact_dependencies is not None:
        for child in exact_dependencies(candidate):
            package = catalogue.candidate_package(child)
            if package not in dependencies:
                message = f"Exact candidate {child!r} has no prepared dependency edge"
                raise ResolutionInvariantError(message)

            # Different identities are simultaneous requirements, even at one version.
            encoded = encode_candidate(child, dependencies[package])
            exact[package] = exact.get(package, Range[int].full()) & encoded

    return {
        package: exact[package] if package in exact else encode_range(package, allowed)
        for package, allowed in dependencies.items()
    }


class _KnownFacts(
    BaseProvider[str, int], Generic[CandidateType, RangeType, PinnedRequirementType]
):
    """Keep selected candidates fixed and leave unread dependencies unconstrained."""

    def __init__(
        self,
        catalogue: PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType],
        constraints: Mapping[str, RangeType],
        context: _Context[CandidateType],
        *,
        exact_dependencies: Callable[[CandidateType], Sequence[CandidateType]]
        | None = None,
    ) -> None:
        self.catalogue = catalogue
        self.constraints = constraints
        self.exact_dependencies = exact_dependencies

        self.fixed = {
            self.catalogue.candidate_package(value): value
            for value in context.scope.fixed
        }
        self.fixed.update(
            (self.catalogue.candidate_package(value), value)
            for value in context.selected
        )
        self.live = context.scope.live

        # Unread metadata stays unconstrained throughout this proof snapshot.
        self.facts = dict(catalogue.facts)

        self.items: dict[int, CandidateType] = {}
        self.identities: dict[CandidateType, int] = {}

    def identify(self, candidate: CandidateType) -> int:
        token = self.identities.get(candidate)
        if token is None:
            token = len(self.items) + 1
            self.items[token] = candidate
            self.identities[candidate] = token
        return token

    def choices(self, package: str, allowed: RangeType) -> list[CandidateType]:
        candidates = self.catalogue.ordered(
            package,
            self.catalogue.intersect_ranges(
                allowed, self.constraints.get(package, self.catalogue.full_range())
            ),
        )

        if package in self.catalogue.missing_listings:
            raise UnavailableMetadataError(package)

        return [candidate for candidate in candidates if self.eligible(candidate)]

    def eligible(self, candidate: CandidateType) -> bool:
        """Keep frozen choices, live restrictions and known metadata rejections."""
        package = self.catalogue.candidate_package(candidate)
        return (
            self.fixed.get(package, candidate) == candidate
            and not (
                self.catalogue.candidate_withdrawn(candidate)
                and self.catalogue.candidate_base(candidate) in self.live
            )
            and (candidate not in self.facts or self.facts[candidate] is not None)
        )

    def encode_candidate(
        self, candidate: CandidateType, allowed: RangeType
    ) -> Range[int]:
        """Encode one known identity while retaining frozen proof restrictions."""
        package = self.catalogue.candidate_package(candidate)
        allowed = self.catalogue.intersect_ranges(
            allowed, self.constraints.get(package, self.catalogue.full_range())
        )
        if not self.eligible(candidate) or not self.catalogue.range_contains(
            candidate, allowed
        ):
            return Range[int].empty()

        return Range[int].singleton(self.identify(candidate))

    def singleton_token(self, package: str, allowed: RangeProtocol[int]) -> int | None:
        """Recognize a registered exact domain without consulting the catalogue."""
        return next(
            (
                token
                for token, candidate in self.items.items()
                if self.catalogue.candidate_package(candidate) == package
                and allowed == Range.singleton(token)
            ),
            None,
        )

    def encode(self, package: str, allowed: RangeType) -> Range[int]:
        return Range[int].from_versions(
            self.identify(candidate) for candidate in self.choices(package, allowed)
        )

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        del conflict_counts, culprit_counts
        if (
            self.exact_dependencies is not None
            and self.singleton_token(package, version_range) is not None
        ):
            return 1

        return len(self.catalogue.choices(package))

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        if self.exact_dependencies is not None:
            token = self.singleton_token(package, version_range)
            if token is not None:
                # Registration already applied this frozen proof's restrictions.
                return token

        return next(
            (
                self.identify(value)
                for value in self.choices(package, self.catalogue.full_range())
                if self.identify(value) in version_range
            ),
            None,
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return self.choose_version(package, version_range) is not None

    def widen_decision(self, package: str, version: int) -> None:
        del package, version

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        candidate = self.items[version]
        data = self.facts.get(candidate)
        result = (
            {}
            if data is None
            else encode_dependencies(
                self.catalogue,
                candidate,
                data.dependencies,
                self.exact_dependencies,
                self.encode,
                self.encode_candidate,
            )
        )

        # Extra metadata belongs to exactly the same base artifact.
        base = self.catalogue.base_name(package)
        if base != package:
            underlying = self.catalogue.base_candidate(candidate)
            if self.exact_dependencies is None:
                result[base] = Range.singleton(self.identify(underlying))
            else:
                result[base] = result.get(
                    base, Range[int].full()
                ) & self.encode_candidate(underlying, self.catalogue.full_range())
        return result


class CataloguePreference(
    PreferenceSearch[CandidateType, RangeType, PinnedRequirementType]
):
    """Run scoped preference proofs against a supplied candidate catalogue."""

    def __init__(
        self,
        catalogue: PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType],
        roots: Mapping[str, RangeType],
        input_pins: Sequence[PinnedRequirementType],
        constraints: Mapping[str, RangeType],
        *,
        max_steps: int = 100_000,
        refresh_incomplete: bool = False,
        exact_dependencies: Callable[[CandidateType], Sequence[CandidateType]]
        | None = None,
    ) -> None:
        """Keep one host snapshot and a shared budget for its preference proofs."""
        super().__init__(
            roots,
            input_pins,
            constraints,
            max_steps=max_steps,
            refresh_incomplete=refresh_incomplete,
            exact_dependencies=exact_dependencies,
        )
        self._catalogue = catalogue

    @property
    @_override
    def catalogue(
        self,
    ) -> PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType]:
        """Return the catalogue whose immutable facts the search shares."""
        return self._catalogue
