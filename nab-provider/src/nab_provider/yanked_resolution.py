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
from nab_resolver.types import Incompatibility, IncompatibilityCause

from ._provider.listing_diagnosis import MetadataBlock, metadata_diagnostic
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.requirements import Requirement
from .diagnostics import ResolutionMessage
from .extra_keys import split_extra
from .preference_provider import (
    MetadataProxy,
    Package,
    PermissionProxy,
    PreferenceProvider,
)
from .yank_candidates import Candidate, YankCandidates, add_range, is_pin, merge_stats
from .yank_preference import (
    IncompletePreferenceError,
    YankPreference,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

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

    def _empty_requirements(
        self, node: Incompatibility[Package, int]
    ) -> Iterator[tuple[str, VersionRange]]:
        """Recover version constraints from empty encoded dependency domains."""
        if node.cause not in (
            IncompatibilityCause.ROOT,
            IncompatibilityCause.DEPENDENCY,
        ):
            return
        parent = node.terms[0].package
        for term in node.terms[1:]:
            if not isinstance(term.package, str) or not term.constraint.is_empty:
                continue
            if isinstance(parent, MetadataProxy):
                data = self._native_catalogue.facts[self.items[parent.candidate]]
                assert data is not None
                allowed = data.dependencies[term.package]
            else:
                allowed = self.roots[term.package]
            yield (
                term.package,
                allowed & self.constraints.get(term.package, VersionRange.full()),
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
            for package, allowed in self._empty_requirements(node):
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
            selected = self.select()
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
        return self._native_preference.adopt(selected)
