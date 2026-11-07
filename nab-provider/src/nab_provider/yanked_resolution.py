"""Resolve yanked candidates through permission and metadata proxies."""

from __future__ import annotations

__lazy_modules__ = (
    "nab_provider._vendor.packaging.ranges",
    "nab_provider._vendor.packaging.utils",
    "nab_provider.extra_keys",
    "nab_provider.diagnostics",
    "nab_provider._provider.listing_diagnosis",
    "nab_provider.yank_candidates",
    "nab_resolver.errors",
    "nab_resolver.ranges",
    "nab_resolver.types",
)

from typing import TYPE_CHECKING

from nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from nab_resolver.types import IncompatibilityCause

from ._provider.listing_diagnosis import MetadataBlock, metadata_diagnostic
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.requirements import Requirement
from .diagnostics import ResolutionMessage
from .extra_keys import split_extra
from .preference_provider import (
    MetadataProxy,
    PermissionProxy,
    PreferenceProvider,
)
from .prepared_resolution import PreparedResolution
from .yank_candidates import Candidate, YankCandidates, add_range, is_pin, merge_stats
from .yank_preference import (
    IncompletePreferenceError,
    YankPreference,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_resolver.types import RootRequirement

    from ._vendor.packaging.version import Version
    from .diagnostics import Diagnostic
    from .provider import Provider

__all__ = ["MetadataProxy", "PermissionProxy", "YankProxyProvider"]
_MAX_STEPS = 100_000


class YankProxyProvider(PreferenceProvider["Candidate", "VersionRange", Requirement]):
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
    ) -> None:
        """Retain original declarations and target policy for lazy admission."""
        catalogue = YankCandidates(provider, factory, preferences=preferences)
        root_ranges: dict[str, VersionRange] = {}
        for root in roots:
            assert isinstance(root.constraint, VersionRange)
            add_range(root_ranges, root.package, root.constraint)
        pins = tuple(req for req in requirements if is_pin(req))
        preference = YankPreference(
            catalogue, root_ranges, pins, constraint_ranges, max_steps=_MAX_STEPS
        )
        super().__init__(preference)
        self._native_preference = preference
        self._native_catalogue = catalogue
        self.session = PreparedResolution(
            self, preferred_candidate=self._preferred_candidate
        )

    def _preferred_candidate(self, package: str) -> Candidate | None:
        """Offer cached live preferences without reading another listing."""
        catalogue = self._native_catalogue
        base = catalogue.base_name(package)
        version = catalogue.preferences.get(base)
        versions = catalogue.provider.versions_cache.get(base)
        if version is None or versions is None or catalogue.provider.wants_lowest(base):
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

    def _failure_diagnostics(
        self, error: ResolutionError
    ) -> tuple[tuple[str, Diagnostic], ...]:
        """Translate final proof leaves into the original package requirements."""
        incompatibility = error.incompatibility
        assert incompatibility is not None
        failures: dict[str, VersionRange] = {}
        rejections: dict[str, dict[Candidate, MetadataBlock]] = {}
        for node in incompatibility.walk():
            for package, allowed in self.empty_requirements(node):
                failures[package] = (
                    failures.get(package, VersionRange.empty()) | allowed
                )
            if node.cause is IncompatibilityCause.NO_VERSIONS:
                for term in node.terms:
                    package = term.package
                    if isinstance(package, MetadataProxy):
                        candidate = self.items[package.candidate]
                        block = self._native_catalogue.rejections.get(candidate)
                        if block is not None:
                            rejections.setdefault(candidate.package, {})[candidate] = (
                                block
                            )

        entries: list[tuple[str, Diagnostic]] = []
        for package, allowed in failures.items():
            diagnostic = self._native_catalogue.provider.get_no_versions_reason(
                package, allowed
            )
            if diagnostic is not None:
                entries.append((package, diagnostic))
        entries.extend(
            (
                package,
                metadata_diagnostic(
                    self._native_catalogue.provider,
                    split_extra(package)[0],
                    tuple(blocks.values()),
                ),
            )
            for package, blocks in rejections.items()
        )
        return tuple(entries)

    def resolve(self) -> tuple[dict[str, Version], Provider]:
        """Check non-yanked alternatives before adopting the selected candidates."""
        try:
            result = self.session.solve()
        except (IncompletePreferenceError, ResolutionTerminatedError):
            raise
        except ResolutionError as exc:
            entries = self._failure_diagnostics(exc)
            summary = "no solution satisfies the requirements and yanked-file policy"
            message = ResolutionMessage(summary, entries)
            error = ResolutionError(message)
            error.verbose_message = message.render(verbose=True)
            raise error from exc
        finally:
            merge_stats(
                self._native_catalogue.provider.stats,
                self._native_catalogue.child_stats,
            )
        pins, provider = self._native_catalogue.adopt(result.candidates)
        provider.yank_admission_sources = result.admission_sources
        return pins, provider
