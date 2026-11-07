"""Resolve prepared catalogues and return their selected dependency graph."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from typing import TYPE_CHECKING, Generic, TypeVar

from nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
    ResolutionTerminatedError,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.result import IncompleteSelectionError, walk_selected

from ._compat import override
from .preference_provider import MetadataProxy, Package, PreferenceProvider

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Sequence

    from nab_resolver.resolver import ResolverProvider, ResolverStats
    from nab_resolver.types import RangeProtocol

    from .preference import PreferenceCatalogue, PreferenceSearch

__all__ = ["PreparedResolution", "PreparedSolution"]

CandidateType = TypeVar("CandidateType", bound=Hashable)
RangeType = TypeVar("RangeType")
PinnedRequirementType = TypeVar("PinnedRequirementType")


class PreparedSolution(Generic[CandidateType]):
    """Selected candidate identities, dependency edges and admission reasons."""

    __slots__ = ("admission_sources", "candidates", "edges", "roots")

    def __init__(
        self,
        candidates: dict[str, CandidateType],
        edges: tuple[tuple[str, str], ...],
        roots: tuple[str, ...],
        admission_sources: dict[str, tuple[str, ...]],
    ) -> None:
        """Retain one exact selected model and its host-facing graph."""
        self.candidates = candidates
        self.edges = edges
        self.roots = roots
        self.admission_sources = admission_sources


class PreparedResolution(Generic[CandidateType, RangeType, PinnedRequirementType]):
    """Keep one prepared snapshot and its preference-query lifetime together."""

    def __init__(
        self,
        provider: PreferenceProvider[CandidateType, RangeType, PinnedRequirementType],
        *,
        metadata_admission: Callable[[CandidateType, Mapping[str, CandidateType]], bool]
        | None = None,
        preferred_candidate: Callable[[str], CandidateType | None] | None = None,
    ) -> None:
        """Own query wiring while the supplied provider retains its facts."""
        self.provider = provider
        self.metadata_admission = metadata_admission
        self.preferred_candidate = preferred_candidate
        if preferred_candidate is not None:
            provider.preferred_candidate = preferred_candidate

    @classmethod
    def from_catalogue(  # noqa: PLR0913 - preserve independent public host hooks
        cls,
        catalogue: PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType],
        roots: Mapping[str, RangeType],
        input_pins: Sequence[PinnedRequirementType],
        constraints: Mapping[str, RangeType],
        *,
        refresh_incomplete: bool = False,
        selection_policy: Callable[[Mapping[str, CandidateType]], bool] | None = None,
        preferred_candidate: Callable[[str], CandidateType | None] | None = None,
        on_candidate_considered: Callable[[CandidateType], None] | None = None,
        exact_dependencies: Callable[[CandidateType], Sequence[CandidateType]]
        | None = None,
        max_rounds: int | None = None,
    ) -> PreparedResolution[CandidateType, RangeType, PinnedRequirementType]:
        """Use the shared query engine with the host's preparation and policy."""
        admission = (
            None
            if selection_policy is None
            else _SelectedAdmission(catalogue, roots, selection_policy)
        )
        provider = PreferenceProvider.from_catalogue(
            catalogue,
            roots,
            input_pins,
            constraints,
            refresh_incomplete=refresh_incomplete,
            metadata_is_admitted=admission,
            on_candidate_considered=on_candidate_considered,
            exact_dependencies=exact_dependencies,
        )
        if max_rounds is not None:
            provider.preference.remaining = max_rounds
        session = cls(
            provider,
            metadata_admission=admission,
            preferred_candidate=preferred_candidate,
        )
        if refresh_incomplete:
            provider.resolver_factory = session._new_resolver
        return session

    @property
    def stats(self) -> ResolverStats[Hashable]:
        """Return cumulative work across preferred and ordinary queries."""
        return self.provider.stats

    @property
    def remaining_rounds(self) -> int:
        """Return the solver rounds remaining across this session's queries."""
        return self.provider.preference.remaining

    def _check_round_budget(self) -> None:
        """Stop before another host callback when the cumulative budget is spent."""
        if self.remaining_rounds <= 0:
            message = "live preference analysis is incomplete: search limit reached"
            raise ResolutionLimitError(message)

    def _new_resolver(
        self, provider: ResolverProvider[Package, int], limit: int
    ) -> Resolver[Package, int]:
        """Refresh deferred queries only when this snapshot gains a real fact."""
        return Resolver(
            provider,
            max_iterations=limit,
            availability_generation=lambda: len(self.provider.catalogue.facts),
        )

    def _try_preferred(self) -> dict[str, CandidateType] | None:
        """Validate a disposable incumbent-only view without proving global absence."""
        preferred = self.preferred_candidate
        if preferred is None or not self.provider.roots:
            return None

        trial = _PreferredProvider(
            self.provider.preference,
            preferred,
            metadata_is_admitted=self.metadata_admission,
            on_candidate_considered=self.provider.on_candidate_considered,
        )
        if any(trial.preferred(package) is None for package in self.provider.roots):
            return None

        resolver = Resolver(trial, max_iterations=self.provider.preference.remaining)
        roots: dict[Package, RangeProtocol[int]] = {
            package: trial.encode_range(package, allowed)
            for package, allowed in self.provider.roots.items()
        }
        try:
            selected = resolver.try_resolve_preferred(roots, _PreferredTokens(trial))
        except ResolutionTerminatedError:
            raise
        except ResolutionError:
            return None
        finally:
            self.provider.preference.record_solver_stats(resolver.stats)
            self.provider.preference.spend(resolver.stats.rounds)

        if selected is None:
            return None
        return {
            package: trial.items[token]
            for package, token in selected.items()
            if isinstance(package, str)
        }

    def solve(self) -> PreparedSolution[CandidateType]:
        """Resolve once, retaining exact choices for host adoption and diagnostics."""
        self._check_round_budget()
        try:
            selected = self._try_preferred()
            if selected is None:
                selected = self.provider.select()
        except ResolutionTerminatedError:
            raise
        except Exception:
            self._check_round_budget()
            raise
        edges: list[tuple[str, str]] = []
        for package, candidate in selected.items():
            data = self.provider.catalogue.facts.get(candidate)
            if data is None:
                message = f"Selected candidate {candidate!r} has no prepared metadata"
                raise ResolutionInvariantError(message)
            edges.extend((package, child) for child in data.dependencies)

        return PreparedSolution(
            selected,
            tuple(edges),
            tuple(self.provider.roots),
            self.provider.preference.explain(selected),
        )


class _SelectedAdmission(Generic[CandidateType, RangeType, PinnedRequirementType]):
    """Apply host admission to a complete selection of cached metadata."""

    def __init__(
        self,
        catalogue: PreferenceCatalogue[CandidateType, RangeType, PinnedRequirementType],
        roots: Mapping[str, RangeType],
        selection_is_admitted: Callable[[Mapping[str, CandidateType]], bool],
    ) -> None:
        self.catalogue = catalogue
        self.roots = roots
        self.selection_is_admitted = selection_is_admitted

    def _dependency_names(
        self, _package: str, candidate: CandidateType
    ) -> Collection[str] | None:
        data = self.catalogue.facts.get(candidate)
        return None if data is None else data.dependencies.keys()

    def __call__(
        self, candidate: CandidateType, selected: Mapping[str, CandidateType]
    ) -> bool:
        try:
            reachable = {
                name: choice
                for name, choice, _ in walk_selected(
                    self.roots, selected, self._dependency_names, depth_first=True
                )
            }
        except IncompleteSelectionError:
            return True

        return candidate not in reachable.values() or self.selection_is_admitted(
            reachable
        )


class _PreferredProvider(
    PreferenceProvider[CandidateType, RangeType, PinnedRequirementType]
):
    """Offer one eligible incumbent per package only for a disposable SAT trial."""

    def __init__(
        self,
        preference: PreferenceSearch[CandidateType, RangeType, PinnedRequirementType],
        preferred: Callable[[str], CandidateType | None],
        *,
        metadata_is_admitted: Callable[
            [CandidateType, Mapping[str, CandidateType]], bool
        ]
        | None,
        on_candidate_considered: Callable[[CandidateType], None] | None = None,
    ) -> None:
        super().__init__(
            preference,
            metadata_is_admitted=metadata_is_admitted,
            on_candidate_considered=on_candidate_considered,
        )
        self.read_preferred = preferred
        self.preferred_choices: dict[str, CandidateType | None] = {}

    def preferred(self, package: str) -> CandidateType | None:
        if package not in self.preferred_choices:
            candidate = self.read_preferred(package)
            if candidate is not None and self.catalogue.candidate_withdrawn(candidate):
                candidate = None
            self.preferred_choices[package] = candidate
        return self.preferred_choices[package]

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[Package, RangeProtocol[int]],
        decisions: Mapping[Package, int],
    ) -> None:
        """Preferred validation queries directly instead of running a package scan."""
        self.receive_decision_scan_hint(positive_ranges, decisions)

    @override
    def encode_range(self, package: str, allowed: RangeType) -> Range[int]:
        candidate = self.preferred(package)
        allowed = self.catalogue.intersect_ranges(
            allowed, self.constraints.get(package, self.catalogue.full_range())
        )
        if candidate is None or not self.catalogue.range_contains(candidate, allowed):
            return Range[int].empty()
        return Range[int].singleton(self.identify(candidate))

    @override
    def choose_version(
        self, package: Package, version_range: RangeProtocol[int]
    ) -> int | None:
        if not isinstance(package, str):
            return super().choose_version(package, version_range)
        candidate = self.preferred(package)
        if candidate is None:
            return None
        token = self.identify(candidate)
        if token not in version_range:
            return None

        # A refused trial still considered its real candidate choices.
        if self.on_candidate_considered is not None:
            self.on_candidate_considered(candidate)
        return token


class _PreferredTokens(
    Mapping[Package, int], Generic[CandidateType, RangeType, PinnedRequirementType]
):
    """Discover expected tokens lazily as the incumbent model grows."""

    def __init__(
        self,
        provider: _PreferredProvider[CandidateType, RangeType, PinnedRequirementType],
    ) -> None:
        self.provider = provider

    @override
    def __getitem__(self, package: Package) -> int:
        if (
            isinstance(package, MetadataProxy)
            and package.candidate in self.provider.items
        ):
            return 0
        if isinstance(package, str):
            candidate = self.provider.preferred(package)
            if candidate is not None:
                return self.provider.identify(candidate)
        raise KeyError(package)

    @override
    def __iter__(self) -> Iterator[Package]:
        for package, candidate in self.provider.preferred_choices.items():
            if candidate is not None:
                yield package
                yield MetadataProxy(self.provider.identify(candidate))

    @override
    def __len__(self) -> int:
        return 2 * sum(
            candidate is not None
            for candidate in self.provider.preferred_choices.values()
        )
