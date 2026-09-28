"""Resolve yanked candidates through permission and metadata proxies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver, ResolverStats
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from ._compat import override
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.utils import canonicalize_name
from .extra_keys import split_extra
from .yank_candidates import Candidate, YankCandidates, add_range, is_pin, merge_stats
from .yank_preference import (
    IncompletePreferenceError,
    PreferenceScope,
    PreparationStatus,
    YankPreference,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_resolver.types import RangeProtocol, RootRequirement

    from ._vendor.packaging.requirements import Requirement
    from ._vendor.packaging.version import Version
    from .provider import Provider

__all__ = ["YankProxyProvider"]
_MAX_STEPS = 100_000
_ROOT_SCOPE = PreferenceScope()


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


class YankProxyProvider(BaseProvider[Package, int]):
    """Use internal packages to enforce yank permission in PubGrub."""

    def __init__(
        self,
        provider: Provider,
        factory: Callable[[], Provider],
        roots: Sequence[RootRequirement[str, Version]],
        requirements: Sequence[Requirement],
        constraint_ranges: Mapping[str, VersionRange],
        *,
        preferences: Mapping[str, Version],
        preference: YankPreference | None = None,
        scope: PreferenceScope = _ROOT_SCOPE,
    ) -> None:
        """Retain original declarations and target policy for lazy admission."""
        self.root_requirements = tuple(roots)
        if preference is None:
            catalogue = YankCandidates(provider, factory, preferences=preferences)
            root_ranges: dict[str, VersionRange] = {}
            for root in roots:
                assert isinstance(root.constraint, VersionRange)
                add_range(root_ranges, root.package, root.constraint)
            pins = tuple(req for req in requirements if is_pin(req))
            preference = YankPreference(
                catalogue, root_ranges, pins, constraint_ranges, max_steps=_MAX_STEPS
            )
        self.preference = preference
        self.catalogue = preference.catalogue
        self.roots = preference.roots
        self.constraints = preference.constraints
        self.input_pins = preference.input_pins
        self.scope = scope
        self.fixed = {candidate.package: candidate for candidate in scope.fixed}
        self.items: dict[int, Candidate] = {}
        self.identities: dict[Candidate, int] = {}
        self.proofs: dict[PermissionProxy, dict[Support, int]] = {}
        self.supports: dict[int, Support] = {}
        self.decisions: Mapping[Package, int] = {}
        self.positive: Mapping[Package, RangeProtocol[int]] = {}
        self.pending: list[Incompatibility[Package, int]] = []
        self.incomplete = False
        self.stats: ResolverStats[Package] = preference.stats

    def identify(self, candidate: Candidate) -> int:
        """Assign a stable identity without relying on the size of the catalogue."""
        token = self.identities.get(candidate)
        if token is None:
            token = len(self.items) + 1
            self.identities[candidate] = token
            self.items[token] = candidate
        return token

    def encode_range(self, package: str, allowed: VersionRange) -> Range[int]:
        """Translate a version constraint into distinct candidate choices."""
        self.catalogue.choices(package)
        if package in self.catalogue.missing_listings:
            self.incomplete = True
        allowed &= self.constraints.get(package, VersionRange.full())
        result: Range[int] = Range.empty()
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
            if self.items[token].withdrawn:
                proofs = self.proofs.setdefault(PermissionProxy(token), {})
                if support not in proofs:
                    proofs[support] = len(proofs) + 1

    def ground(self) -> dict[int, Support]:
        """Derive permission from input pins and already-permitted parents."""
        demand: dict[str, Support] = {name: frozenset() for name in self.roots}
        pins: list[tuple[Requirement, Support]] = [
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

    @staticmethod
    def admission_support(
        candidate: Candidate,
        demand: Support,
        pins: Sequence[tuple[Requirement, Support]],
    ) -> Support | None:
        """Combine the parents requiring a candidate with those supplying its pin."""
        if not candidate.withdrawn:
            return demand
        for requirement, support in pins:
            if canonicalize_name(
                requirement.name
            ) == candidate.base and requirement.specifier.contains(
                candidate.version, prereleases=True
            ):
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
    ) -> int:
        """Choose real candidates, then read their permitted metadata."""
        del version_range, conflict_counts, culprit_counts
        if isinstance(package, str):
            return 0
        return 1 if isinstance(package, MetadataProxy) else 2

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
        pending, self.pending = self.pending, []
        return pending

    def eligible(self, candidate: Candidate) -> bool:
        """Apply this check's parent versions and non-yanked restrictions."""
        return (
            candidate.base not in self.scope.live or not candidate.withdrawn
        ) and self.fixed.get(candidate.package, candidate) == candidate

    def choose_version(
        self, package: Package, version_range: RangeProtocol[int]
    ) -> int | None:
        """Choose a candidate or explain why its current context cannot proceed."""
        if not self.is_ready(package):
            self.block_context(package)
            return None
        if isinstance(package, str):
            # Cached dependencies need incompatibilities before excluding candidates.
            allowed = self.constraints.get(package, VersionRange.full())
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
        token = package.candidate
        candidate = self.items[token]
        if candidate.withdrawn:
            selected = {
                name: self.items[value]
                for name, value in self.decisions.items()
                if isinstance(name, str)
            }
            status = self.preference.check(candidate, selected, self.scope)
            if status is not PreparationStatus.ALLOWED:
                self.incomplete |= status is PreparationStatus.INCOMPLETE
                self.block_context(package)
                return None
        data = self.catalogue.prepare(candidate)
        if data is None:
            if candidate in self.catalogue.unavailable:
                self.incomplete = True
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
            if candidate.withdrawn:
                result[PermissionProxy(version)] = Range.full()
            base, extra = split_extra(package)
            if extra is not None:
                underlying = Candidate(base, candidate.version, candidate.withdrawn)
                result[base] = Range.singleton(self.identify(underlying))
            return result
        if isinstance(package, PermissionProxy):
            support = next(s for s, v in self.proofs[package].items() if v == version)
            return {self.items[i].package: Range.singleton(i) for i in support}
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

    def new_query(self, scope: PreferenceScope) -> YankProxyProvider:
        """Share cached metadata and candidate identities with a new provider."""
        query = YankProxyProvider(
            self.catalogue.provider,
            self.catalogue.factory,
            self.root_requirements,
            self.input_pins,
            self.constraints,
            preferences=self.catalogue.preferences,
            preference=self.preference,
            scope=scope,
        )
        query.items = self.items
        query.identities = self.identities
        return query

    def run_query(self, scope: PreferenceScope) -> dict[str, Candidate]:
        """Resolve under one set of restrictions with the shared work budget."""
        query = self.new_query(scope)
        solver: Resolver[Package, int] = Resolver(
            query, max_iterations=self.preference.remaining
        )
        roots: dict[Package, Range[int]] = {
            name: query.encode_range(name, allowed)
            for name, allowed in query.roots.items()
        }
        try:
            selected = solver.resolve(roots)
        except ResolutionError as exc:
            if (
                query.incomplete
                or exc.incompatibility is None
                or str(exc).startswith("Conflict resolution made no progress")
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

    def resolve(self) -> tuple[dict[str, Version], Provider]:
        """Check non-yanked alternatives before adopting the selected candidates."""
        try:
            selected = self.preference.run(self.run_query)
        except IncompletePreferenceError:
            raise
        except ResolutionError as exc:
            message = (
                "no solution satisfies the requirements "
                "with grounded yanked-file admission; "
                "a withdrawn file requires an active exact pin"
            )
            if self.catalogue.rejections:
                message += f"\nLast metadata rejection: {self.catalogue.rejections[-1]}"
            raise ResolutionError(message) from exc
        finally:
            merge_stats(self.catalogue.provider.stats, self.catalogue.child_stats)
        return self.preference.adopt(selected)
