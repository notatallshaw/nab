"""Enforce grounded candidate admission using shared PubGrub proxy packages."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from nab_resolver.priority import compute_tier
from nab_resolver.proofs import external_clauses
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.root import ROOT
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from ._compat import override
from .preference import (
    CataloguePreference,
    IncompletePreferenceError,
    PreferenceCatalogue,
    PreferenceScope,
    PreferenceSearch,
    PreparationStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from nab_resolver.resolver import ResolverProvider
    from nab_resolver.types import RangeProtocol

__all__ = ["MetadataProxy", "Package", "PermissionProxy", "PreferenceProvider"]

CandidateType = TypeVar("CandidateType", bound=Hashable)
RangeType = TypeVar("RangeType")
PinnedRequirementType = TypeVar("PinnedRequirementType")
ProjectedVersionType = TypeVar("ProjectedVersionType", bound=Hashable)
ClausePackageType = TypeVar("ClausePackageType", bound=Hashable)


@dataclass(frozen=True, slots=True)
class PermissionProxy:
    """Choose selected parents that permit a yanked candidate."""

    candidate: int


@dataclass(frozen=True, slots=True)
class MetadataProxy:
    """Expose a candidate's dependencies after establishing permission."""

    candidate: int


Package = str | PermissionProxy | MetadataProxy
Support = frozenset[int]


class PreferenceProvider(
    BaseProvider[Package, int],
    Generic[CandidateType, RangeType, PinnedRequirementType],
):
    """Resolve a host catalogue with grounded admission and scoped preference proofs."""

    def __init__(
        self,
        preference: PreferenceSearch[CandidateType, RangeType, PinnedRequirementType],
        *,
        scope: PreferenceScope[CandidateType] | None = None,
        metadata_is_admitted: Callable[
            [CandidateType, Mapping[str, CandidateType]], bool
        ]
        | None = None,
        query_is_complete: Callable[[PreferenceScope[CandidateType]], bool]
        | None = None,
        resolver_factory: Callable[
            [ResolverProvider[Package, int], int], Resolver[Package, int]
        ]
        | None = None,
    ) -> None:
        """Retain one search and the host's metadata and completeness policies."""
        self.preference = preference
        self.catalogue = preference.catalogue
        self.roots = preference.roots
        self.constraints = preference.constraints
        self.input_pins = preference.input_pins

        self.scope: PreferenceScope[CandidateType] = (
            PreferenceScope() if scope is None else scope
        )
        self.fixed: dict[str, CandidateType] = {
            self.catalogue.candidate_package(candidate): candidate
            for candidate in self.scope.fixed
        }

        self.items: dict[int, CandidateType] = {}
        self.identities: dict[CandidateType, int] = {}
        self.proofs: dict[PermissionProxy, dict[Support, int]] = {}
        self.supports: dict[int, Support] = {}
        self.decisions: Mapping[Package, int] = {}
        self.positive: Mapping[Package, RangeProtocol[int]] = {}
        self.pending: list[Incompatibility[Package, int]] = []
        self.incomplete = False
        self.stats = preference.stats

        self._metadata_is_admitted = metadata_is_admitted
        self._query_is_complete = query_is_complete
        self.resolver_factory = resolver_factory

    @staticmethod
    def from_catalogue(
        catalogue: PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType],
        roots: Mapping[str, RangeType],
        input_pins: Sequence[PinnedRequirementType],
        constraints: Mapping[str, RangeType],
        *,
        refresh_incomplete: bool = False,
        metadata_is_admitted: Callable[
            [CandidateType, Mapping[str, CandidateType]], bool
        ]
        | None = None,
        query_is_complete: Callable[[PreferenceScope[CandidateType]], bool]
        | None = None,
        resolver_factory: Callable[
            [ResolverProvider[Package, int], int], Resolver[Package, int]
        ]
        | None = None,
    ) -> PreferenceProvider[CandidateType, RangeType, PinnedRequirementType]:
        """Own the default preference search and its cumulative query lifetime."""
        preference = CataloguePreference(
            catalogue,
            roots,
            input_pins,
            constraints,
            refresh_incomplete=refresh_incomplete,
        )

        return PreferenceProvider(
            preference,
            metadata_is_admitted=metadata_is_admitted,
            query_is_complete=query_is_complete,
            resolver_factory=resolver_factory,
        )

    def empty_requirements(
        self, clause: Incompatibility[Package, int]
    ) -> Iterator[tuple[str, RangeType]]:
        """Recover original requirements whose encoded candidate domains are empty."""
        if clause.cause not in {
            IncompatibilityCause.ROOT,
            IncompatibilityCause.DEPENDENCY,
        }:
            return

        parent = clause.terms[0].package
        for term in clause.terms[1:]:
            if not isinstance(term.package, str) or not term.constraint.is_empty:
                continue

            if isinstance(parent, MetadataProxy):
                data = self.catalogue.facts[self.items[parent.candidate]]
                assert data is not None
                allowed = data.dependencies[term.package]
            else:
                allowed = self.roots[term.package]
            yield (
                term.package,
                self.catalogue.intersect_ranges(
                    allowed,
                    self.constraints.get(term.package, self.catalogue.full_range()),
                ),
            )

    def project_requirements(
        self,
        derivation: Incompatibility[ClausePackageType, int] | None,
        *,
        version_of: Callable[[CandidateType], ProjectedVersionType],
        range_type: type[RangeProtocol[ProjectedVersionType]],
        left_first: bool = True,
    ) -> list[Incompatibility[object, ProjectedVersionType]]:
        """Decode original request clauses into the host's candidate version domain."""
        projected: list[Incompatibility[object, ProjectedVersionType]] = []
        for clause in external_clauses(derivation, left_first=left_first):
            if clause.cause not in {
                IncompatibilityCause.ROOT,
                IncompatibilityCause.DEPENDENCY,
            }:
                continue

            terms: list[Term[object, ProjectedVersionType]] = []
            for term in clause.terms:
                # Solver-root terms share the proof with encoded catalogue packages.
                package: object = term.package
                if isinstance(package, MetadataProxy):
                    candidate = self.items[package.candidate]
                    named: object = self.catalogue.candidate_package(candidate)
                    allowed = range_type.singleton(version_of(candidate))
                elif isinstance(package, str):
                    named = package
                    allowed = range_type.empty()
                    for token, candidate in self.items.items():
                        if (
                            self.catalogue.candidate_package(candidate) == package
                            and token in term.constraint
                        ):
                            allowed |= range_type.singleton(version_of(candidate))
                elif package is ROOT:
                    named = ROOT
                    allowed = range_type.full()
                else:
                    break
                terms.append(Term(named, allowed, positive=term.is_positive()))
            else:
                if terms:
                    projected.append(Incompatibility(terms, cause=clause.cause))
        return projected

    def identify(self, candidate: CandidateType) -> int:
        """Assign a stable identity without relying on the size of the catalogue."""
        token = self.identities.get(candidate)
        if token is None:
            token = len(self.items) + 1
            self.identities[candidate] = token
            self.items[token] = candidate
        return token

    def encode_range(self, package: str, allowed: RangeType) -> Range[int]:
        """Translate a version constraint into distinct candidate choices."""
        self.catalogue.choices(package)
        if package in self.catalogue.missing_listings:
            self.incomplete = True
        allowed = self.catalogue.intersect_ranges(
            allowed, self.constraints.get(package, self.catalogue.full_range())
        )
        result = Range[int].empty()
        for candidate in self.catalogue.ordered(package, allowed):
            if self.eligible(candidate):
                result |= Range.singleton(self.identify(candidate))
        return result

    def receive_decision_scan_hint(
        self,
        positive_ranges: Mapping[Package, RangeProtocol[int]],
        decisions: Mapping[Package, int],
    ) -> None:
        """Refresh permission and readiness after propagation or backtracking."""
        self.decisions = decisions
        self.positive = positive_ranges
        self.supports = self.ground()
        for token, support in self.supports.items():
            if self.catalogue.candidate_withdrawn(self.items[token]):
                proofs = self.proofs.setdefault(PermissionProxy(token), {})
                if support not in proofs:
                    proofs[support] = len(proofs) + 1

    def ground(self) -> dict[int, Support]:
        """Derive permission from input pins and already-permitted parents."""
        demand: dict[str, Support] = {name: frozenset() for name in self.roots}
        pins: list[tuple[PinnedRequirementType, Support]] = [
            (req, frozenset()) for req in self.input_pins
        ]
        admitted: dict[int, Support] = {}
        expanded: set[int] = set()
        while True:
            changed = False
            for package, demand_support in tuple(demand.items()):
                token = self.decisions.get(package)
                if token is None:
                    continue
                candidate = self.items[token]
                if token not in admitted:
                    support = self.admission_support(candidate, demand_support, pins)
                    if support is None:
                        continue
                    admitted[token] = support
                    changed = True

                data = self.catalogue.facts.get(candidate)
                if token in expanded or data is None:
                    continue
                expanded.add(token)
                support = admitted[token] | {token}

                for child in data.dependencies:
                    if child not in demand:
                        demand[child] = support
                        changed = True
                pins.extend((req, support) for req in data.pins)
                changed |= bool(data.pins)

            if not changed:
                return admitted

    def admission_support(
        self,
        candidate: CandidateType,
        demand: Support,
        pins: Sequence[tuple[PinnedRequirementType, Support]],
    ) -> Support | None:
        """Combine the parents requiring a candidate with those supplying its pin."""
        if not self.catalogue.candidate_withdrawn(candidate):
            return demand
        for requirement, support in pins:
            if self.catalogue.pin_allows(requirement, candidate):
                return demand | support
        return None

    @override
    def begin_decision_scan(self) -> Callable[[Package], bool]:
        """Permission readiness can change when another package is decided."""
        return lambda _package: True

    @override
    def is_ready(self, package: Package) -> bool:
        """Wait for an exact-pin reason before preparing yanked metadata."""
        if isinstance(package, MetadataProxy):
            return package.candidate in self.supports
        if isinstance(package, PermissionProxy):
            allowed: RangeProtocol[int] = self.positive.get(package, Range[int].full())
            return any(v in allowed for v in self.proofs.get(package, {}).values())
        return True

    def prioritize(
        self,
        package: Package,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[Package, int],
        culprit_counts: Mapping[Package, int] | None = None,
    ) -> tuple[int, int]:
        """Choose real candidates using conflict evidence, then permitted metadata."""
        del version_range
        if isinstance(package, str):
            # Restarts must revise real-package order while permission stays lazy.
            tier = compute_tier(
                package,
                conflict_counts.get(package, 0),
                0 if culprit_counts is None else culprit_counts.get(package, 0),
                culprit_counts,
            )
            return (0, tier)
        return (1 if isinstance(package, MetadataProxy) else 2, 0)

    def block_context(self, package: Package) -> None:
        """Reject selected choices without excluding a candidate globally."""
        terms: list[Term[Package, int]] = [
            Term(package, Range[int].full(), positive=True)
        ]
        terms.extend(
            Term(name, Range.singleton(value), positive=True)
            for name, value in self.decisions.items()
            if isinstance(name, str)
        )
        self.pending.append(
            Incompatibility(terms, cause=IncompatibilityCause.NO_VERSIONS)
        )

    @override
    def consume_pending_clauses(self) -> list[Incompatibility[Package, int]]:
        """Return incompatibilities that depend on the selected candidates."""
        pending = self.pending
        self.pending = []
        return pending

    def eligible(self, candidate: CandidateType) -> bool:
        """Apply this check's parent versions and non-yanked restrictions."""
        if self.catalogue.candidate_base(
            candidate
        ) in self.scope.live and self.catalogue.candidate_withdrawn(candidate):
            return False

        return (
            self.fixed.get(self.catalogue.candidate_package(candidate), candidate)
            == candidate
        )

    def choose_version(
        self, package: Package, version_range: RangeProtocol[int]
    ) -> int | None:
        """Choose a candidate or explain why its current context cannot proceed."""
        if not self.is_ready(package):
            self.block_context(package)
            return None
        if isinstance(package, str):
            # Cached dependencies need incompatibilities before excluding candidates.
            allowed = self.constraints.get(package, self.catalogue.full_range())
            return next(
                (
                    self.identify(c)
                    for c in self.catalogue.ordered(package, allowed)
                    if self.eligible(c) and self.identify(c) in version_range
                ),
                None,
            )
        if isinstance(package, PermissionProxy):
            return next(
                (
                    v
                    for v in self.proofs.get(package, {}).values()
                    if v in version_range
                ),
                None,
            )
        return self.prepare_candidate(package, version_range)

    def prepare_candidate(
        self, package: MetadataProxy, version_range: RangeProtocol[int]
    ) -> int | None:
        """Prepare admitted metadata and apply the host's selected-plan policy."""
        token = package.candidate
        candidate = self.items[token]
        if self.catalogue.candidate_withdrawn(candidate):
            selected = {
                name: self.items[value]
                for name, value in self.decisions.items()
                if isinstance(name, str)
            }
            status = self.preference.check(candidate, selected, self.scope)
            if status is not PreparationStatus.ALLOWED:
                self.incomplete |= status is PreparationStatus.INCOMPLETE
                if (
                    status is not PreparationStatus.INCOMPLETE
                    or not self.preference.refresh_incomplete
                ):
                    self.block_context(package)
                return None

        data = self.catalogue.prepare(candidate)
        if data is None:
            if candidate in self.catalogue.unavailable:
                self.incomplete = True
            return None

        if self._metadata_is_admitted is not None:
            selected = {
                name: self.items[value]
                for name, value in self.decisions.items()
                if isinstance(name, str)
            }
            if not self._metadata_is_admitted(candidate, selected):
                self.block_context(package)
                return None

        return 0 if 0 in version_range else None

    def has_satisfying_version(
        self, package: Package, version_range: RangeProtocol[int]
    ) -> bool:
        """Probe the same domain used by candidate selection."""
        return self.choose_version(package, version_range) is not None

    def get_dependencies(
        self, package: Package, version: int
    ) -> dict[Package, Range[int]]:
        """Return dependencies for a candidate or selected permission reason."""
        if isinstance(package, str):
            candidate = self.items[version]
            result: dict[Package, Range[int]] = {
                MetadataProxy(version): Range.singleton(0)
            }
            if self.catalogue.candidate_withdrawn(candidate):
                result[PermissionProxy(version)] = Range.full()
            base = self.catalogue.candidate_base(candidate)
            if base != package:
                # The extra must bind to the same version text and withdrawn files.
                underlying = self.catalogue.base_candidate(candidate)
                result[base] = Range.singleton(self.identify(underlying))
            return result
        if isinstance(package, PermissionProxy):
            support = next(s for s, v in self.proofs[package].items() if v == version)
            return {
                self.catalogue.candidate_package(self.items[i]): Range.singleton(i)
                for i in support
            }
        token = package.candidate
        data = self.catalogue.facts[self.items[token]]
        assert data is not None
        return {
            name: self.encode_range(name, allowed)
            for name, allowed in data.dependencies.items()
        }

    def widen_decision(self, package: Package, version: int) -> None:
        """Keep incompatibilities specific to a candidate or permission reason."""
        del package, version

    def select(self) -> dict[str, CandidateType]:
        """Resolve the catalogue with all admission proofs and one shared budget."""
        return self.preference.run(self.run_query)

    def new_query(
        self, scope: PreferenceScope[CandidateType]
    ) -> PreferenceProvider[CandidateType, RangeType, PinnedRequirementType]:
        """Share candidate identities and host policies with a scoped query."""
        query = PreferenceProvider(
            self.preference,
            scope=scope,
            metadata_is_admitted=self._metadata_is_admitted,
            query_is_complete=self._query_is_complete,
            resolver_factory=self.resolver_factory,
        )

        # Scopes use one candidate snapshot and retain each choice's token identity.
        query.items = self.items
        query.identities = self.identities

        return query

    def run_query(
        self, scope: PreferenceScope[CandidateType]
    ) -> dict[str, CandidateType]:
        """Resolve under one set of restrictions with the shared work budget."""
        query = self.new_query(scope)
        solver = (
            Resolver(query, max_iterations=self.preference.remaining)
            if self.resolver_factory is None
            else self.resolver_factory(query, self.preference.remaining)
        )
        roots: dict[Package, Range[int]] = {
            name: query.encode_range(name, allowed)
            for name, allowed in query.roots.items()
        }
        try:
            selected = solver.resolve(roots)
        except ResolutionTerminatedError:
            raise
        except ResolutionError as exc:
            if (
                query.incomplete
                or exc.incompatibility is None
                or (
                    self._query_is_complete is not None
                    and not self._query_is_complete(scope)
                )
            ):
                message = "yanked dependency resolution is incomplete"
                if query.incomplete:
                    message += ": " + self.catalogue.unavailable_reason
                raise IncompletePreferenceError(message) from exc
            raise
        finally:
            self.preference.record_solver_stats(solver.stats)
            self.preference.spend(solver.stats.rounds)
        return {
            name: self.items[token]
            for name, token in selected.items()
            if isinstance(name, str)
        }
