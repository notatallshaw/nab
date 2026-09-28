"""Check non-yanked alternatives while allowing unrelated package versions to change."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver, ResolverStats

from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.utils import canonicalize_name
from .extra_keys import split_extra
from .yank_candidates import Candidate, UnavailableMetadataError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_resolver.types import RangeProtocol

    from ._vendor.packaging.requirements import Requirement
    from ._vendor.packaging.version import Version
    from .provider import Provider
    from .yank_candidates import YankCandidates


@dataclass(frozen=True, slots=True)
class PreferenceScope:
    """Keep selected ancestors fixed and selected non-yanked packages non-yanked."""

    fixed: frozenset[Candidate] = frozenset()
    live: frozenset[str] = frozenset()
    target: str = ""


class PreparationStatus(Enum):
    """Whether non-yanked alternatives allow preparing a yanked candidate."""

    ALLOWED = auto()
    IMPOSSIBLE = auto()
    INCOMPLETE = auto()


class IncompletePreferenceError(ResolutionError):
    """An interrupted or unknown query cannot prove that live choices fail."""


class _NeedLiveProofError(Exception):
    def __init__(self, scope: PreferenceScope) -> None:
        self.scope = scope


@dataclass(frozen=True, slots=True)
class _Context:
    """A selected assignment whose known metadata may already establish failure."""

    scope: PreferenceScope
    selected: frozenset[Candidate]


class _NeedContextProofError(Exception):
    def __init__(self, context: _Context) -> None:
        self.context = context


class YankPreference:
    """Schedule scoped resolver queries on an explicit stack and share their budget."""

    def __init__(
        self,
        catalogue: YankCandidates,
        roots: Mapping[str, VersionRange],
        input_pins: Sequence[Requirement],
        constraints: Mapping[str, VersionRange],
        *,
        max_steps: int = 100_000,
    ) -> None:
        """Retain shared facts and the original target declarations."""
        self.catalogue = catalogue
        self.roots = roots
        self.input_pins = input_pins
        self.constraints = constraints
        self.remaining = max_steps
        self.stats: ResolverStats[Any] = ResolverStats()
        self.outcomes: dict[PreferenceScope, PreparationStatus] = {}
        self.refuted: set[_Context] = set()
        self.possible: dict[_Context, int] = {}

    def spend(self, steps: int) -> None:
        """Charge work from any resolver query against the cumulative limit."""
        self.remaining -= steps
        if self.remaining < 0:
            message = "live preference analysis is incomplete: search limit reached"
            raise IncompletePreferenceError(message)

    def record_solver_stats(self, stats: ResolverStats[Any]) -> None:
        """Accumulate every scalar and package counter before a query is discarded."""
        for name in ResolverStats.__slots__:
            value = getattr(stats, name)
            if isinstance(value, int):
                setattr(self.stats, name, getattr(self.stats, name) + value)
            else:
                target = getattr(self.stats, name)
                for package, count in value.items():
                    target[package] += count

    def parents(
        self, candidate: Candidate, selected: Mapping[str, Candidate]
    ) -> frozenset[Candidate]:
        """Find ancestors reachable from roots without the candidate's metadata."""
        demand = set(self.roots)
        pins = list(self.input_pins)
        expanded: set[str] = set()
        while True:
            before = len(expanded)
            for name, parent in selected.items():
                if (
                    parent.base == candidate.base
                    or name not in demand
                    or name in expanded
                ):
                    continue
                data = self.catalogue.facts.get(parent)
                if data is None:
                    continue
                if parent.withdrawn and not any(
                    canonicalize_name(req.name) == parent.base
                    and req.specifier.contains(parent.version, prereleases=True)
                    for req in pins
                ):
                    continue
                expanded.add(name)
                demand.update(data.dependencies)
                pins.extend(data.pins)
            if len(expanded) == before:
                break

        ancestors: set[str] = set()
        pending = [
            name for name, value in selected.items() if value.base == candidate.base
        ]
        pending.extend((candidate.base, candidate.package))
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
        self, candidate: Candidate, parents: frozenset[Candidate]
    ) -> VersionRange:
        """Intersect target requirements from inputs and reachable selected parents."""
        required = {candidate.base, candidate.package}
        allowed = VersionRange.full()
        declarations = [self.roots]
        for parent in parents:
            data = self.catalogue.facts[parent]
            assert data is not None
            declarations.append(data.dependencies)
        for dependencies in declarations:
            for package, version_range in dependencies.items():
                if split_extra(package)[0] == candidate.base:
                    required.add(package)
                    allowed &= version_range
        for package in required:
            allowed &= self.constraints.get(package, VersionRange.full())
        return allowed

    def check(
        self,
        candidate: Candidate,
        selected: Mapping[str, Candidate],
        scope: PreferenceScope,
    ) -> PreparationStatus:
        """Request non-yanked alternatives before preparing yanked metadata."""
        if not candidate.withdrawn:
            return PreparationStatus.ALLOWED
        parents = self.parents(candidate, selected)
        allowed = self.fixed_range(candidate, parents)
        if candidate.version not in allowed:
            return PreparationStatus.IMPOSSIBLE
        if not any(
            not choice.withdrawn
            for choice in self.catalogue.ordered(candidate.base, allowed)
        ):
            return PreparationStatus.ALLOWED
        live = (
            scope.live
            | {candidate.base}
            | {value.base for value in selected.values() if not value.withdrawn}
        )
        query = PreferenceScope(
            parents,
            frozenset(live),
            candidate.base,
        )
        outcome = self.outcomes.get(query)
        if outcome is None:
            if not scope.live < query.live:
                message = "live preference analysis is incomplete: cyclic query"
                raise IncompletePreferenceError(message)
            raise _NeedLiveProofError(query)
        if outcome is PreparationStatus.INCOMPLETE:
            return outcome

        context = _Context(scope, frozenset(selected.values()))
        if context in self.refuted:
            return PreparationStatus.IMPOSSIBLE
        if self.possible.get(context) != len(self.catalogue.facts):
            raise _NeedContextProofError(context)
        return PreparationStatus.ALLOWED

    def run(
        self, callback: Callable[[PreferenceScope], dict[str, Candidate]]
    ) -> dict[str, Candidate]:
        """Retry scopes after failed proofs and return a complete solution."""
        stack = [PreferenceScope()]
        while True:
            self.spend(0)
            if not self.remaining:
                message = "live preference analysis is incomplete: search limit reached"
                raise IncompletePreferenceError(message)
            scope = stack[-1]
            try:
                selected = callback(scope)
            except _NeedLiveProofError as request:
                stack.append(request.scope)
            except _NeedContextProofError as request:
                self.check_context(request.context)
            except ResolutionError as exc:
                if len(stack) == 1:
                    raise
                self.outcomes[scope] = (
                    PreparationStatus.INCOMPLETE
                    if isinstance(exc, IncompletePreferenceError)
                    else PreparationStatus.IMPOSSIBLE
                )
                stack.pop()
            else:
                if scope.target and not any(
                    value.base == scope.target for value in selected.values()
                ):
                    message = (
                        "live preference analysis is incomplete: target disappeared"
                    )
                    raise IncompletePreferenceError(message)
                return selected

    def explain(self, selected: Mapping[str, Candidate]) -> dict[str, tuple[str, ...]]:
        """Find an allowing input or dependency pin for each selected yank."""
        declarations: dict[str, list[tuple[Requirement, str]]] = {}
        for requirement in self.input_pins:
            declarations.setdefault(canonicalize_name(requirement.name), []).append(
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
                raise IncompletePreferenceError(message)
            assert candidate is not None
            if candidate.withdrawn:
                source = next(
                    (
                        text
                        for requirement, text in declarations.get(candidate.base, ())
                        if requirement.specifier.contains(
                            candidate.version, prereleases=True
                        )
                    ),
                    None,
                )
                if source is None:
                    waiting.setdefault(candidate.base, set()).add(package)
                    continue
                sources.setdefault(candidate.base, (source,))
            expanded.add(package)
            for child in data.dependencies:
                if child not in demanded:
                    demanded.add(child)
                    pending.append(child)
            for requirement in data.pins:
                base = canonicalize_name(requirement.name)
                text = f"{candidate.package}=={candidate.label} requires {requirement}"
                declarations.setdefault(base, []).append((requirement, text))
                pending.extend(waiting.pop(base, ()))
        if expanded != set(selected):
            message = "cannot explain yanked admission: selection is not rooted"
            raise IncompletePreferenceError(message)
        return sources

    def adopt(
        self, selected: Mapping[str, Candidate]
    ) -> tuple[dict[str, Version], Provider]:
        """Attach the allowing input or dependency pins to the selected provider."""
        sources = self.explain(selected)
        pins, provider = self.catalogue.adopt(selected)
        provider.yank_admission_sources = sources
        return pins, provider

    def check_context(self, context: _Context) -> None:
        """Check for known conflicts after the requesting solver unwinds."""
        provider = _KnownFacts(self.catalogue, self.constraints, context)
        solver: Resolver[str, int] = Resolver(provider, max_iterations=self.remaining)
        refuted = False
        try:
            roots = {
                name: provider.encode(name, allowed)
                for name, allowed in self.roots.items()
            }
            solver.resolve(roots)
        except UnavailableMetadataError:
            pass
        except ResolutionError as exc:
            if exc.incompatibility is None or str(exc).startswith(
                "Conflict resolution made no progress"
            ):
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


class _KnownFacts(BaseProvider[str, int]):
    """Keep selected candidates fixed and leave unread dependencies unconstrained."""

    def __init__(
        self,
        catalogue: YankCandidates,
        constraints: Mapping[str, VersionRange],
        context: _Context,
    ) -> None:
        self.catalogue = catalogue
        self.constraints = constraints
        self.fixed = {value.package: value for value in context.scope.fixed}
        self.fixed.update((value.package, value) for value in context.selected)
        self.live = context.scope.live
        self.facts = dict(catalogue.facts)
        self.items: dict[int, Candidate] = {}
        self.identities: dict[Candidate, int] = {}

    def identify(self, candidate: Candidate) -> int:
        token = self.identities.get(candidate)
        if token is None:
            token = len(self.items) + 1
            self.items[token] = candidate
            self.identities[candidate] = token
        return token

    def choices(self, package: str, allowed: VersionRange) -> list[Candidate]:
        candidates = self.catalogue.ordered(
            package, allowed & self.constraints.get(package, VersionRange.full())
        )
        if package in self.catalogue.missing_listings:
            raise UnavailableMetadataError(package)
        return [
            candidate
            for candidate in candidates
            if self.fixed.get(package, candidate) == candidate
            and not (candidate.withdrawn and candidate.base in self.live)
            and (candidate not in self.facts or self.facts[candidate] is not None)
        ]

    def encode(self, package: str, allowed: VersionRange) -> Range[int]:
        result: Range[int] = Range.empty()
        for candidate in self.choices(package, allowed):
            result |= Range.singleton(self.identify(candidate))
        return result

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        del version_range, conflict_counts, culprit_counts
        return len(self.catalogue.choices(package))

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return next(
            (
                self.identify(value)
                for value in self.choices(package, VersionRange.full())
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
            else {
                name: self.encode(name, allowed)
                for name, allowed in data.dependencies.items()
            }
        )
        base, extra = split_extra(package)
        if extra is not None:
            underlying = Candidate(base, candidate.version, candidate.withdrawn)
            result[base] = Range.singleton(self.identify(underlying))
        return result
