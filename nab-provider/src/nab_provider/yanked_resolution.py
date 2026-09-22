"""Resolve original exact-pin declarations with branch-scoped yank admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import BaseProvider, Resolver

from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.utils import canonicalize_name
from ._vendor.packaging.version import Version
from .extra_keys import split_extra
from .yank_candidates import (
    Candidate,
    UnavailableMetadataError,
    YankCandidates,
    add_range,
    is_pin,
    merge_stats,
)
from .yank_preference import (
    IncompletePreferenceError,
    PreferenceScope,
    PreparationStatus,
    YankPreference,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from nab_resolver.resolver import ResolverStats
    from nab_resolver.types import RangeProtocol, RootRequirement

    from ._vendor.packaging.requirements import Requirement
    from .provider import Provider


__all__ = ["YankResolver"]
_MAX_STEPS = 100_000


class ContextFailure(Enum):
    """Whether an exhausted branch proves failure or has unavailable metadata."""

    PROVEN = auto()
    INCOMPLETE = auto()


@dataclass(slots=True)
class SearchFrame:
    """A selected combination and the alternatives still to try."""

    selected: dict[str, Candidate]
    state: frozenset[Candidate]
    ranges: dict[str, VersionRange]
    pins: tuple[Requirement, ...]
    alternatives: Iterator[Candidate]
    generation: int
    failure: ContextFailure = ContextFailure.PROVEN
    pending_candidate: Candidate | None = None
    incomplete_live: set[str] = field(default_factory=set)

    def note_incomplete(self, candidate: Candidate) -> None:
        """Prevent a withdrawn fallback from relying on this unknown live branch."""
        self.failure = ContextFailure.INCOMPLETE
        if not candidate.withdrawn:
            self.incomplete_live.add(candidate.package)


class YankResolver:
    """Search candidate combinations using pins from inputs and selected parents."""

    def __init__(
        self,
        provider: Provider,
        factory: Callable[[], Provider],
        roots: Sequence[RootRequirement[str, Version]],
        requirements: Sequence[Requirement],
        constraint_ranges: Mapping[str, VersionRange],
        *,
        preferences: Mapping[str, Version],
    ) -> None:
        """Start a target search from original declarations and a provider factory."""
        self.roots: dict[str, VersionRange] = {}
        for root in roots:
            assert isinstance(root.constraint, VersionRange)
            add_range(self.roots, root.package, root.constraint)
        self.constraints = constraint_ranges
        self.input_pins = tuple(req for req in requirements if is_pin(req))
        self.failed: set[frozenset[Candidate]] = set()
        self.unresolved: set[frozenset[Candidate]] = set()
        self.contexts = 0
        self.next_probe = 32
        self.last_probe = 0
        self.catalogue = YankCandidates(provider, factory, preferences=preferences)
        self.scope = PreferenceScope()
        self.preference = YankPreference(
            self.catalogue,
            self.roots,
            self.input_pins,
            self.constraints,
            max_steps=_MAX_STEPS,
        )
        self.stats: ResolverStats[Any] = self.preference.stats
        self.query_count = 0

    def choices(self, package: str) -> tuple[Candidate, ...]:
        """Filter the shared file catalogue for this query's immutable scope."""
        candidates = self.catalogue.choices(package)
        fixed = next(
            (item for item in self.scope.fixed if item.package == package), None
        )
        return tuple(
            candidate
            for candidate in candidates
            if (fixed is None or candidate == fixed)
            and (not candidate.withdrawn or candidate.base not in self.scope.live)
        )

    def declarations(
        self, selected: Mapping[str, Candidate]
    ) -> tuple[Requirement, ...]:
        """Return pins from inputs and the selected parents' active dependencies."""
        pins = list(self.input_pins)
        for candidate in selected.values():
            data = self.catalogue.facts[candidate]
            assert data is not None
            pins.extend(data.pins)
        return tuple(pins)

    def admitted(self, candidate: Candidate, pins: Sequence[Requirement]) -> bool:
        """Require a matching original exact pin before using withdrawn files."""
        return not candidate.withdrawn or any(
            canonicalize_name(req.name) == candidate.base
            and req.specifier.contains(candidate.version, prereleases=True)
            for req in pins
        )

    def ranges(self, selected: Mapping[str, Candidate]) -> dict[str, VersionRange]:
        """Combine the actual roots, selected dependencies and active constraints."""
        result = dict(self.roots)
        for candidate in selected.values():
            data = self.catalogue.facts[candidate]
            assert data is not None
            for name, value in data.dependencies.items():
                add_range(result, name, value)
        for name, value in result.items():
            constraint = self.constraints.get(name)
            if constraint is not None:
                result[name] = value & constraint
        return result

    def admitted_order(
        self, package: str, version_range: VersionRange, pins: Sequence[Requirement]
    ) -> list[Candidate]:
        """Order only the choices admitted by the current declarations."""
        choices = tuple(c for c in self.choices(package) if self.admitted(c, pins))
        return self.catalogue.ordered(package, version_range, choices=choices)

    def possible_candidates(
        self,
        candidates: Sequence[Candidate],
        allowed: VersionRange,
        pins: Sequence[Requirement],
    ) -> Iterator[Candidate]:
        """Keep permitted candidates whose metadata has not already been rejected."""
        for candidate in candidates:
            if candidate.version not in allowed or not self.admitted(candidate, pins):
                continue
            if (
                candidate in self.catalogue.facts
                and self.catalogue.facts[candidate] is None
            ):
                continue
            yield candidate

    def possible_admission(
        self,
        selected: Mapping[str, Candidate],
        ranges: Mapping[str, VersionRange],
        pins: Sequence[Requirement],
    ) -> bool:
        """Check for remaining candidates or possible sources of exact pins."""
        reachable = set(ranges)
        possible: set[str] = set()
        permissions = {str(req): req for req in pins}
        inspected: set[Candidate] = set()
        unknown = False
        while True:
            changed = False
            for package in tuple(reachable):
                candidates = (
                    (selected[package],)
                    if package in selected
                    else self.choices(package)
                )
                if package in self.catalogue.missing_listings:
                    unknown = True
                allowed = ranges.get(
                    package, self.constraints.get(package, VersionRange.full())
                )
                for candidate in self.possible_candidates(
                    candidates, allowed, tuple(permissions.values())
                ):
                    possible.add(package)
                    if candidate in inspected:
                        continue
                    inspected.add(candidate)
                    data = self.catalogue.facts.get(candidate)
                    if data is None:
                        unknown = True
                        continue
                    new_names = data.dependencies.keys() - reachable
                    reachable.update(new_names)
                    changed |= bool(new_names)
                    for req in data.pins:
                        if str(req) not in permissions:
                            permissions[str(req)] = req
                            changed = True
            if not changed:
                return unknown or set(ranges) <= possible

    def check_known_dependencies(self) -> None:
        """Check known dependency conflicts without reading unknown metadata."""
        if self.preference.remaining <= 0:
            msg = "live preference analysis is incomplete"
            raise IncompletePreferenceError(msg)
        provider = OptimisticProvider(self)
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            max_iterations=self.preference.remaining,
        )
        try:
            resolver.resolve(self.roots, constraints=self.constraints)
        except UnavailableMetadataError:
            return
        except ResolutionError as exc:
            if exc.incompatibility is None or str(exc).startswith(
                "Conflict resolution made no progress"
            ):
                msg = (
                    "yanked dependency feasibility check stopped before proving failure"
                )
                raise IncompletePreferenceError(msg) from exc
            raise
        finally:
            self.preference.record_solver_stats(resolver.stats)
            self.preference.spend(resolver.stats.rounds)

    def consistent(
        self, selected: Mapping[str, Candidate], ranges: Mapping[str, VersionRange]
    ) -> bool:
        """Match version text and yank status across a base package and its extras."""
        identities: dict[str, tuple[str, bool]] = {}
        for package, candidate in selected.items():
            identity = (candidate.label, candidate.withdrawn)
            if (
                identities.setdefault(candidate.base, identity) != identity
                or candidate.version not in ranges[package]
            ):
                return False
        return not any(value.is_empty for value in ranges.values())

    def alternatives(
        self,
        pending: Sequence[str],
        ranges: Mapping[str, VersionRange],
        pins: Sequence[Requirement],
    ) -> Iterator[Candidate]:
        """Explore other preparation orders when the next package lacks permission."""
        for package in pending:
            version_range = ranges[package]
            base, extra = split_extra(package)
            if extra is not None and base in ranges:
                version_range &= ranges[base]
            yield from self.admitted_order(package, version_range, pins)

    def inspect_context(
        self, selected: dict[str, Candidate]
    ) -> SearchFrame | dict[str, Candidate] | ContextFailure:
        """Finish, reject, or extend a selection under its active declarations."""
        self.preference.spend(1)
        self.contexts += 1
        state = frozenset(selected.values())
        if state in self.failed:
            return ContextFailure.PROVEN
        if state in self.unresolved:
            return ContextFailure.INCOMPLETE
        ranges = self.ranges(selected)
        if not self.consistent(selected, ranges):
            self.failed.add(state)
            return ContextFailure.PROVEN
        pending = [name for name in ranges if name not in selected]
        if not pending:
            return selected
        if len(self.failed) >= self.next_probe:
            self.next_probe = max(self.next_probe * 2, len(self.failed) + 32)
            if len(self.catalogue.facts) > self.last_probe:
                self.last_probe = len(self.catalogue.facts)
                self.check_known_dependencies()
        pins = self.declarations(selected)
        if not self.possible_admission(selected, ranges, pins):
            self.failed.add(state)
            return ContextFailure.PROVEN
        return SearchFrame(
            selected,
            state,
            ranges,
            pins,
            self.alternatives(pending, ranges, pins),
            len(self.catalogue.facts),
            failure=(
                ContextFailure.INCOMPLETE
                if any(name in self.catalogue.missing_listings for name in pending)
                else ContextFailure.PROVEN
            ),
        )

    def finish_context(self, stack: list[SearchFrame]) -> ContextFailure:
        """Propagate incomplete alternatives and remember an exhausted branch."""
        frame = stack.pop()
        if frame.failure is ContextFailure.INCOMPLETE:
            self.unresolved.add(frame.state)
            if stack:
                parent = stack[-1]
                assert parent.pending_candidate is not None
                parent.note_incomplete(parent.pending_candidate)
        else:
            self.failed.add(frame.state)
        return frame.failure

    def inspect_choice(
        self, frame: SearchFrame, candidate: Candidate
    ) -> SearchFrame | dict[str, Candidate] | ContextFailure:
        """Prepare one permitted choice, preserving unknown failures separately."""
        if candidate.withdrawn and candidate.package in frame.incomplete_live:
            return ContextFailure.PROVEN
        selected = {**frame.selected, candidate.package: candidate}
        if not self.consistent(selected, frame.ranges):
            return ContextFailure.PROVEN
        if candidate.withdrawn:
            status = self.preference.check(candidate, selected, self.scope)
            if status is PreparationStatus.IMPOSSIBLE:
                return ContextFailure.PROVEN
            if status is PreparationStatus.INCOMPLETE:
                return ContextFailure.INCOMPLETE
        if self.catalogue.prepare(candidate) is None:
            return (
                ContextFailure.INCOMPLETE
                if candidate in self.catalogue.unavailable
                else ContextFailure.PROVEN
            )
        return self.inspect_context(selected)

    def visit(
        self, selected: dict[str, Candidate]
    ) -> dict[str, Candidate] | ContextFailure:
        """Keep dependency depth off Python's stack with explicit search frames."""
        first = self.inspect_context(selected)
        if not isinstance(first, SearchFrame):
            return first
        stack = [first]
        failure = ContextFailure.PROVEN
        while stack:
            frame = stack[-1]
            if frame.generation != len(self.catalogue.facts):
                frame.generation = len(self.catalogue.facts)
                if not self.possible_admission(
                    frame.selected, frame.ranges, frame.pins
                ):
                    frame.failure = ContextFailure.PROVEN
                    failure = self.finish_context(stack)
                    continue
            candidate = next(frame.alternatives, None)
            if candidate is None:
                failure = self.finish_context(stack)
                continue
            child = self.inspect_choice(frame, candidate)
            if isinstance(child, dict):
                return child
            if child is ContextFailure.INCOMPLETE:
                frame.note_incomplete(candidate)
            elif isinstance(child, SearchFrame):
                frame.pending_candidate = candidate
                stack.append(child)
        return failure

    def trial(self, scope: PreferenceScope) -> dict[str, Candidate]:
        """Search one static domain, keeping failure caches local to that query."""
        self.scope = scope
        if self.query_count:
            self.failed.clear()
            self.unresolved.clear()
            self.next_probe = 32
            self.last_probe = 0
        self.query_count += 1
        selected = self.visit({})
        if selected is ContextFailure.INCOMPLETE:
            msg = f"resolution is incomplete: {self.catalogue.unavailable_reason}"
            raise IncompletePreferenceError(msg)
        if selected is ContextFailure.PROVEN:
            detail = (
                f"\nLast metadata rejection: {self.catalogue.rejections[-1]}"
                if self.catalogue.rejections
                else ""
            )
            raise ResolutionError(
                "no solution satisfies the requirements "
                "with grounded yanked-file admission; "
                "a withdrawn file requires an active exact pin" + detail
            )
        return selected

    def resolve(self) -> tuple[dict[str, Version], Provider]:
        """Run preference checks iteratively and return the selected packages."""
        try:
            selected = self.preference.run(self.trial)
        finally:
            merge_stats(self.catalogue.provider.stats, self.catalogue.child_stats)
        return self.preference.adopt(selected)


class OptimisticProvider(BaseProvider[str, Version]):
    """A relaxed provider that treats unknown metadata as having no dependencies."""

    def __init__(self, search: YankResolver) -> None:
        """Snapshot prepared metadata for one known-dependency conflict check."""
        self.search = search
        self.facts = dict(search.catalogue.facts)

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        """Choose any listed version not already rejected by metadata validation."""
        choices = self.search.choices(package)
        if package in self.search.catalogue.missing_listings:
            raise UnavailableMetadataError(self.search.catalogue.unavailable_reason)
        return next(
            (
                candidate.version
                for candidate in choices
                if candidate.version in version_range
                and (candidate not in self.facts or self.facts[candidate] is not None)
            ),
            None,
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        """Check the same relaxed domain used for choosing a version."""
        return self.choose_version(package, version_range) is not None

    def get_dependencies(
        self, package: str, version: Version
    ) -> dict[str, VersionRange]:
        """Expose dependencies only when exactly one known candidate remains."""
        alternatives = [
            candidate
            for candidate in self.search.choices(package)
            if candidate.version == version
        ]
        if any(candidate not in self.facts for candidate in alternatives):
            return {}
        facts = [
            self.facts[candidate]
            for candidate in alternatives
            if self.facts[candidate] is not None
        ]
        if len(facts) != 1:
            return {}
        data = facts[0]
        assert data is not None
        return dict(data.dependencies)

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Try the smallest listed domain first."""
        del version_range, conflict_counts, culprit_counts
        return len(self.search.choices(package))

    def widen_decision(self, package: str, version: Version) -> None:
        """Keep clauses tied to one version because neighboring facts may differ."""
        del package, version
