"""Resolve yanked candidates through permission and metadata proxies."""

from __future__ import annotations

__lazy_modules__ = (
    "nab_provider._provider.listing_diagnosis",
    "nab_provider.diagnostics",
    "nab_provider.extra_keys",
    "nab_provider.ranges",
    "nab_provider.yank_candidates",
    "nab_resolver.errors",
    "nab_resolver.ranges",
    "nab_resolver.types",
)

from typing import TYPE_CHECKING

from nab_candidates.provider import (
    MetadataProxy,
    Package,
    PermissionProxy,
    PreferenceProvider,
)
from nab_candidates.resolution import PreparedResolution
from nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from nab_resolver.priority import compute_tier
from nab_resolver.ranges import IntegerRangeMembership, Range
from nab_resolver.resolver import DEFAULT_MAX_ITERATIONS
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from ._compat import override
from ._provider.listing_diagnosis import MetadataBlock, metadata_diagnostic
from .diagnostics import ResolutionMessage
from .extra_keys import split_extra
from .ranges import VersionRange, require_version_range
from .yank_candidates import Candidate, YankCandidates, add_range, is_pin, merge_stats
from .yank_preference import (
    IncompletePreferenceError,
    PreferenceScope,
    PreparationStatus,
    YankPreference,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from packaging.requirements import Requirement
    from packaging.version import Version

    from nab_resolver.types import RangeProtocol, RootRequirement

    from .diagnostics import Diagnostic
    from .provider import Provider

__all__ = ["MetadataProxy", "PermissionProxy", "YankProxyProvider"]
_MAX_SOLVER_ROUNDS = DEFAULT_MAX_ITERATIONS
_ROOT_SCOPE = PreferenceScope()


class _AdmissionRequiredError(Exception):
    """An encountered exact pin requires the admission search."""


class YankProxyProvider(PreferenceProvider["Candidate", "VersionRange", "Requirement"]):
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
                constraint = require_version_range(root.package, root.constraint)
                add_range(root_ranges, root.package, constraint)
            pins = tuple(req for req in requirements if is_pin(req))
            preference = YankPreference(
                catalogue,
                root_ranges,
                pins,
                constraint_ranges,
                max_steps=_MAX_SOLVER_ROUNDS,
            )
        self._native_preference = preference
        super().__init__(preference, scope=scope)
        self._mixed_release_packages: set[str] = set()
        self._dependency_parents: dict[str, set[str]] = {}
        self.live_trial = False
        self._trial_pins = self.input_pins
        self._full_version_range = VersionRange.full()
        self._full_integer_range: Range[int] = Range.full()
        self._integer_membership = IntegerRangeMembership()

    @property
    @override
    def preference(self) -> YankPreference:
        """The scoped search whose selected files the native provider can adopt."""
        return self._native_preference

    @property
    @override
    def catalogue(self) -> YankCandidates:
        """The native catalogue retaining fetched candidate metadata."""
        return self.preference.catalogue

    @override
    def encode_range(self, package: str, allowed: VersionRange) -> Range[int]:
        """Translate a version constraint into distinct candidate choices."""
        self.catalogue.choices(package)
        if package in self.catalogue.missing_listings:
            self.incomplete = True
        allowed &= self.constraints.get(package, self._full_version_range)
        choices = self.catalogue.ordered(package, allowed)
        if choices and package not in self._mixed_release_packages:
            first_is_prerelease = choices[0].version.is_prerelease
            if any(
                choice.version.is_prerelease != first_is_prerelease
                for choice in choices
            ):
                self._mixed_release_packages.add(package)
        return Range[int].from_versions(
            self.identify(candidate)
            for candidate in choices
            if self.eligible(candidate)
        )

    @override
    def receive_decision_scan_hint(
        self,
        positive_ranges: Mapping[Package, RangeProtocol[int]],
        decisions: Mapping[Package, int],
    ) -> None:
        """Refresh permission and readiness after propagation or backtracking."""
        self.decisions = decisions
        self.positive = positive_ranges
        if self.live_trial:
            return
        self.supports = self.ground()
        for token, support in self.supports.items():
            if self.items[token].withdrawn:
                proofs = self.proofs.setdefault(PermissionProxy(token), {})
                if support not in proofs:
                    proofs[support] = len(proofs) + 1

    @override
    def is_ready(self, package: Package) -> bool:
        """Wait for an exact-pin reason before preparing yanked metadata."""
        if self.live_trial:
            return True
        if isinstance(package, MetadataProxy):
            return package.candidate in self.supports
        if isinstance(package, PermissionProxy):
            allowed: RangeProtocol[int] = self.positive.get(
                package, self._full_integer_range
            )
            return any(v in allowed for v in self.proofs.get(package, {}).values())
        return True

    @override
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
            # Read parent requirements before ranking final and prerelease candidates.
            return (2 if package in self._mixed_release_packages else 0, tier)
        return (1 if isinstance(package, MetadataProxy) else 3, 0)

    @override
    def block_context(self, package: Package) -> None:
        """Reject selected choices without excluding a candidate globally."""
        terms: list[Term[Package, int]] = [
            Term(package, self._full_integer_range, positive=True)
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
    def eligible(self, candidate: Candidate) -> bool:
        """Apply this check's parent versions and non-yanked restrictions."""
        if candidate.withdrawn:
            if self.live_trial:
                if any(
                    self.catalogue.pin_allows(pin, candidate)
                    for pin in self._trial_pins
                ):
                    raise _AdmissionRequiredError
                return False
            if candidate.base in self.scope.live:
                return False

        fixed = self.fixed.get(candidate.package)
        return fixed is None or fixed == candidate

    @override
    def choose_version(
        self, package: Package, version_range: RangeProtocol[int]
    ) -> int | None:
        """Choose a candidate or explain why its current context cannot proceed."""
        if not self.is_ready(package):
            self.block_context(package)
            return None
        if isinstance(package, str):
            return self._choose_candidate(package, version_range)
        if isinstance(package, PermissionProxy):
            return next(
                (
                    v
                    for v in self.proofs.get(package, {}).values()
                    if self._integer_membership.contains(version_range, v)
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
        return 0 if self._integer_membership.contains(version_range, 0) else None

    def _choose_candidate(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Choose an eligible package candidate within the encoded range."""
        for order in self._ordering_ranges(package):
            for candidate in self.catalogue.ordered(package, order):
                if not self.eligible(candidate):
                    continue
                token = self.identify(candidate)
                if self._integer_membership.contains(version_range, token):
                    return token
        return None

    def _ordering_ranges(self, package: str) -> Iterator[VersionRange]:
        """Rank prereleases using the requirements of the currently selected parents."""
        constraint = self.constraints.get(package, self._full_version_range)
        if package in self._mixed_release_packages:
            required = self.roots.get(package, self._full_version_range) & constraint
            for parent in self._dependency_parents.get(package, ()):
                token = self.decisions.get(parent)
                if token is not None:
                    data = self.catalogue.facts.get(self.items[token])
                    if data is not None and package in data.dependencies:
                        required &= data.dependencies[package]
            if required != constraint:
                yield required

        # Only the solver's encoded range can rule out a candidate. A previous
        # metadata read can tell us how to rank it before its clause is active.
        yield constraint

    @override
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
                result[PermissionProxy(version)] = self._full_integer_range
            base, extra = split_extra(package)
            if extra is not None:
                underlying = Candidate(
                    base, candidate.version, withdrawn=candidate.withdrawn
                )
                result[base] = Range.singleton(self.identify(underlying))
            return result
        if isinstance(package, PermissionProxy):
            support = next(s for s, v in self.proofs[package].items() if v == version)
            return {self.items[i].package: Range.singleton(i) for i in support}
        token = package.candidate
        parent = self.items[token]
        data = self.catalogue.prepared_data(parent)
        self._trial_pins = (*self.input_pins, *data.pins)
        try:
            encoded: dict[Package, Range[int]] = {}
            for child, allowed in data.dependencies.items():
                encoded[child] = self.encode_range(child, allowed)
                if child in self._mixed_release_packages:
                    self._dependency_parents.setdefault(child, set()).add(
                        parent.package
                    )
            return encoded
        finally:
            self._trial_pins = self.input_pins

    @override
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
        query.package_tokens = self.package_tokens
        query._integer_membership = self._integer_membership
        return query

    @override
    def run_query(
        self, scope: PreferenceScope, *, live_trial: bool = False
    ) -> dict[str, Candidate]:
        """Resolve under one set of restrictions with the shared work budget."""
        query = self.new_query(scope)
        query.live_trial = live_trial
        return query.solve_query()

    def _failure_diagnostics(
        self, incompatibility: Incompatibility[Package, int]
    ) -> tuple[tuple[str, Diagnostic], ...]:
        """Translate final proof leaves into the original package requirements."""
        failures: dict[str, VersionRange] = {}
        rejections: dict[str, dict[Candidate, MetadataBlock]] = {}
        for node in incompatibility.walk():
            for package, allowed in self.empty_requirements(node):
                failures[package] = (
                    failures.get(package, VersionRange.empty()) | allowed
                )
            if node.cause is IncompatibilityCause.NO_VERSIONS:
                for term in node.terms:
                    proxy = term.package
                    if isinstance(proxy, MetadataProxy):
                        candidate = self.items[proxy.candidate]
                        block = self.catalogue.rejections.get(candidate)
                        if block is not None:
                            rejections.setdefault(candidate.package, {})[candidate] = (
                                block
                            )

        entries: list[tuple[str, Diagnostic]] = []
        for package, allowed in failures.items():
            diagnostic = self.catalogue.provider.get_no_versions_reason(
                package, allowed
            )
            if diagnostic is not None:
                entries.append((package, diagnostic))
        entries.extend(
            (
                package,
                metadata_diagnostic(
                    self.catalogue.provider,
                    split_extra(package)[0],
                    tuple(blocks.values()),
                ),
            )
            for package, blocks in rejections.items()
        )
        return tuple(entries)

    def _preferred_candidate(self, package: str) -> Candidate | None:
        """Offer cached live preferences without reading another listing."""
        catalogue = self.catalogue
        base = catalogue.base_name(package)
        version = catalogue.preferences.get(base)
        versions = catalogue.provider.versions_cache.get(base)
        if (
            version is None
            or version.is_prerelease
            or versions is None
            or catalogue.provider.wants_lowest(base)
        ):
            return None

        candidate = Candidate(package, version, withdrawn=False)
        if not any(
            value == version and str(value) == candidate.label and not file.yanked
            for value, file in versions
        ):
            return None

        return (
            candidate
            if catalogue.installable(candidate, versions, assume_sorted=True)
            else None
        )

    def resolve(self) -> tuple[dict[str, Version], Provider]:
        """Check non-yanked alternatives before adopting the selected candidates."""
        session = _NativeResolution(
            self,
            preferred_candidate=self._preferred_candidate
            if self.catalogue.preferences
            else None,
        )
        try:
            selected = session.select()
        except ResolutionError as exc:
            proof = exc.incompatibility
            if proof is None or isinstance(
                exc, (IncompletePreferenceError, ResolutionTerminatedError)
            ):
                raise
            entries = self._failure_diagnostics(proof)
            summary = "no solution satisfies the requirements and yanked-file policy"
            message = ResolutionMessage(summary, entries)
            error = ResolutionError(message)
            error.verbose_message = message.render(verbose=True)
            raise error from exc
        finally:
            self._integer_membership.clear()

            merge_stats(self.catalogue.provider.stats, self.catalogue.child_stats)
        return self.preference.adopt(selected)


class _NativeResolution(PreparedResolution["Candidate", "VersionRange", "Requirement"]):
    """Use target-specific prerelease order during the shared live-file trial."""

    def __init__(
        self,
        query: YankProxyProvider,
        *,
        preferred_candidate: Callable[[str], Candidate | None] | None,
    ) -> None:
        super().__init__(query, preferred_candidate=preferred_candidate, try_live=True)
        self._query = query

    @override
    def try_live_candidates(self) -> dict[str, Candidate] | None:
        try:
            return self._query.run_query(_ROOT_SCOPE, live_trial=True)
        except _AdmissionRequiredError:
            # A matching pin makes the full admission proof relevant.
            return None
