"""Cache target-specific candidates and their prepared dependency metadata."""

from __future__ import annotations

__lazy_modules__ = (
    "nab_provider._provider.extras",
    "nab_provider._provider.listing_diagnosis",
    "nab_provider._provider.metadata_resolver",
    "nab_provider.errors",
    "nab_provider.extra_keys",
    "nab_provider.metadata",
    "nab_provider.policy",
    "nab_provider.records",
    "packaging.utils",
)

from types import MappingProxyType
from typing import TYPE_CHECKING

from packaging.ranges import VersionRange
from packaging.utils import canonicalize_name

from nab_candidates.preference import UnavailableMetadataError
from nab_candidates.versions import (
    PreparedVersionCatalogue,
    VersionCandidate,
    VersionCandidateData,
    is_pin,
)
from nab_resolver.errors import ResolutionInvariantError

from ._compat import override
from ._provider.extras import version_provides_extra
from ._provider.listing_diagnosis import MetadataBlock
from ._provider.metadata_resolver import classify_requirement, dists_at_version
from .errors import MetadataError, MissingExtraError
from .extra_keys import split_extra
from .metadata import intern_version
from .policy import DistPolicy, ExtrasMode, MetadataPolicy
from .records import SdistFile, WheelFile
from .value import field_names

__all__ = [
    "Candidate",
    "CandidateData",
    "YankCandidates",
    "add_range",
    "is_pin",
    "merge_stats",
]

_UNRESTRICTED_RANGE = VersionRange.full()

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from packaging.requirements import Requirement
    from packaging.version import Version

    from .provider import DistFile, Provider, ProviderStats


class Candidate(VersionCandidate):
    """A package version restricted to its yanked or non-yanked files."""

    __slots__ = ()

    @override
    def _comparison_values(self) -> tuple[object, ...]:
        """Return fixed identity fields, honoring subclass comparison declarations."""
        if type(self) is not Candidate:
            return super()._comparison_values()

        return self.package, self.version, self.withdrawn, self.label

    @property
    @override
    def base(self) -> str:
        """Return the package name without an extra proxy."""
        return split_extra(self.package)[0]


def _prefer_version(candidates: list[Candidate], preferred: Version) -> None:
    """Move the preferred version text, or the first equal candidate, to the front."""
    matching = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if candidate.version == preferred
        ),
        None,
    )
    if matching is None:
        return
    label = str(preferred)
    picked = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if candidate.label == label
        ),
        matching,
    )
    candidates.insert(0, candidates.pop(picked))


class _CandidateGroups:
    """One listing's candidates grouped by withdrawal and sorted newest first."""

    __slots__ = ("available", "groups")

    def __init__(
        self,
        available: Sequence[Candidate],
        groups: tuple[tuple[Candidate, ...], ...],
    ) -> None:
        self.available = available
        self.groups = groups


class _CandidateOrder:
    """A preference-free query over one candidate snapshot."""

    __slots__ = ("allowed", "available", "lowest", "result")

    def __init__(
        self,
        available: Sequence[Candidate],
        allowed: VersionRange,
        result: tuple[Candidate, ...],
        *,
        lowest: bool,
    ) -> None:
        self.available = available
        self.allowed = allowed
        self.lowest = lowest
        self.result = result


class CandidateData(VersionCandidateData):
    """Dependencies and the provider selecting the candidate's files."""

    __slots__ = ("provider",)
    __match_args__ = ("provider", "dependencies", "pins")

    provider: Provider

    def __init__(
        self,
        provider: Provider,
        dependencies: dict[str, VersionRange],
        pins: tuple[Requirement, ...],
    ) -> None:
        """Retain candidate dependencies and independent selected pins."""
        super().__init__(dependencies, pins)
        self.provider = provider


def add_range(ranges: dict[str, VersionRange], name: str, value: VersionRange) -> None:
    """Intersect a requirement with the constraints already collected for its name."""
    previous = ranges.get(name)
    ranges[name] = value if previous is None else previous & value


class YankCandidates(PreparedVersionCatalogue[Candidate, CandidateData]):
    """Cache candidates and metadata without granting permission to use yanked files."""

    range_type = VersionRange

    @staticmethod
    @override
    def pin_project(pin: Requirement) -> str:
        """Return the normalized project named by an original pin."""
        return canonicalize_name(pin.name)

    @staticmethod
    @override
    def base_name(package: str) -> str:
        """Remove an extras proxy from a package identifier."""
        return split_extra(package)[0]

    @staticmethod
    @override
    def base_candidate(candidate: Candidate) -> Candidate:
        """Return the matching base identity for an extra candidate."""
        return Candidate(
            candidate.base, candidate.version, withdrawn=candidate.withdrawn
        )

    def __init__(
        self,
        provider: Provider,
        factory: Callable[[], Provider],
        *,
        preferences: Mapping[str, Version],
    ) -> None:
        """Start one target snapshot with the caller's preparation policy."""
        super().__init__(unavailable_reason="required metadata is unavailable")
        self.provider = provider
        self.factory = factory
        self.preferences = preferences
        self._default_orders: dict[tuple[str, bool], _CandidateOrder] = {}
        self._candidate_groups: dict[str, _CandidateGroups] = {}
        self.rejections: dict[Candidate, MetadataBlock] = {}
        self.child_stats: ProviderStats = type(provider.stats)()

    @override
    def read_choices(self, package: str) -> tuple[Candidate, ...]:
        """List compatible candidates without preparing their metadata."""
        versions = self.provider.fetch_versions(package)
        if self.provider.coordinator.index.is_offline_listing_miss(
            split_extra(package)[0]
        ):
            msg = (
                "cannot establish yanked-file admission: "
                f"the listing for {package} is missing offline"
            )
            self.unavailable_reason = msg
            self.missing_listings.add(package)
            return ()
        result = dict.fromkeys(
            Candidate(
                package, intern_version(file.version), withdrawn=bool(file.yanked)
            )
            for _version, file in versions
        )
        return tuple(
            candidate
            for candidate in result
            if self.installable(candidate, versions, assume_sorted=True)
        )

    def installable(
        self,
        candidate: Candidate,
        versions: Sequence[tuple[Version, DistFile]],
        *,
        assume_sorted: bool = False,
    ) -> bool:
        """Require an sdist for sdist-only; sorted inputs must be newest first."""
        if (
            self.provider.effective_dist_policy(
                candidate.base,
                candidate.version,
                self.provider.serving_index(candidate.base),
            )
            is not DistPolicy.SDIST_ONLY
        ):
            return True
        files = (
            dists_at_version(versions, candidate.version)
            if assume_sorted
            else (file for version, file in versions if version == candidate.version)
        )
        return any(
            str(intern_version(file.version)) == candidate.label
            and isinstance(file, SdistFile)
            and bool(file.yanked) == candidate.withdrawn
            for file in files
        )

    def preparation_provider(self, candidate: Candidate) -> Provider:
        """Isolate preparation when a candidate excludes other files at its version."""
        versions = self.provider.fetch_versions(candidate.base)
        files = dists_at_version(versions, candidate.version)
        if not candidate.withdrawn and all(
            not file.yanked and str(intern_version(file.version)) == candidate.label
            for file in files
        ):
            return self.provider

        # A yanked source may use metadata from a live wheel of the same version.
        allow_wheel_metadata = (
            self.provider.effective_dist_policy(
                candidate.base,
                candidate.version,
                self.provider.serving_index(candidate.base),
            )
            is DistPolicy.SDIST_ONLY
            and self.provider.effective_metadata_policy(
                candidate.base,
                candidate.version,
                self.provider.serving_index(candidate.base),
            )
            is MetadataPolicy.PREFER_WHEEL
        )
        provider = self.factory()
        provider.versions_cache[candidate.base] = [
            (candidate.version, file)
            for file in files
            if (
                bool(file.yanked) == candidate.withdrawn
                and str(intern_version(file.version)) == candidate.label
            )
            or (
                allow_wheel_metadata
                and isinstance(file, WheelFile)
                and (candidate.withdrawn or not file.yanked)
            )
        ]
        if candidate.withdrawn:
            provider.yank_admissions = MappingProxyType(
                {(candidate.base, candidate.version): ()}
            )
        return provider

    @override
    def read_candidate(self, candidate: Candidate) -> CandidateData | None:
        """Prepare a candidate after the caller has checked permission to use it."""
        provider = self.preparation_provider(candidate)
        try:
            return self.read_metadata(provider, candidate)
        except UnavailableMetadataError as exc:
            self.unavailable.add(candidate)
            self.unavailable_reason = str(exc)
            return None
        finally:
            if provider is not self.provider:
                merge_stats(self.child_stats, provider.stats)
                self.provider.consulted_markers.update(provider.consulted_markers)

    def read_metadata(
        self, provider: Provider, candidate: Candidate
    ) -> CandidateData | None:
        """Read dependencies and their original pin declarations for one candidate."""
        base = candidate.base
        try:
            dependencies = _candidate_dependencies(provider, candidate)
        except (MetadataError, MissingExtraError) as exc:
            index = provider.coordinator.index
            for version, file in provider.versions_cache[base]:
                if version != candidate.version:
                    continue
                metadata_url = (
                    file.metadata_url if isinstance(file, WheelFile) else None
                )
                if index.is_offline_metadata_miss(
                    base, str(version), metadata_url or file.url
                ):
                    msg = (
                        "resolution is incomplete: "
                        f"metadata for {base}=={version} is missing offline"
                    )
                    raise UnavailableMetadataError(msg) from exc
            self.rejections[candidate] = MetadataBlock.from_error(exc)
            return None
        metadata = provider.metadata_cache[(base, candidate.version)]
        extra = split_extra(candidate.package)[1]
        provided: set[str] = {
            canonicalize_name(value) for value in metadata.provides_extra
        }
        pins = []
        for req in metadata.requires_dist:
            selected_by = classify_requirement(provider, req, provided)
            if (
                selected_by is not None
                and (not selected_by or extra in selected_by)
                and is_pin(req)
            ):
                pins.append(req)
        return CandidateData(provider, dict(dependencies), tuple(pins))

    def _sorted_groups(
        self,
        package: str,
        available: Sequence[Candidate],
        *,
        cacheable: bool,
    ) -> tuple[tuple[Candidate, ...], ...]:
        """Sort live and withdrawn groups; cache default listing snapshots."""
        cached = self._candidate_groups.get(package) if cacheable else None
        if cached is not None and cached.available is available:
            return cached.groups

        groups = []
        for withdrawn in (False, True):
            by_label = {
                candidate.label: candidate
                for candidate in available
                if candidate.withdrawn == withdrawn
            }
            ordered = sorted(
                by_label.values(), key=lambda candidate: candidate.version, reverse=True
            )
            groups.append(tuple(ordered))
        result = tuple(groups)
        if cacheable:
            self._candidate_groups[package] = _CandidateGroups(available, result)
        return result

    def ordered(
        self,
        package: str,
        version_range: VersionRange,
        *,
        choices: Sequence[Candidate] | None = None,
    ) -> list[Candidate]:
        """Order non-yanked final releases and pre-releases before yanked candidates."""
        available = self.choices(package) if choices is None else choices
        result: list[Candidate] = []
        base = split_extra(package)[0]
        lowest = self.provider.wants_lowest(base)
        cache_key = None
        if choices is None and self.preferences.get(base) is None:
            # Requirement filters should not evict the unrestricted selection order.
            cache_key = (package, version_range == _UNRESTRICTED_RANGE)
            cached = self._default_orders.get(cache_key)
            if (
                cached is not None
                and cached.available is available
                and cached.allowed == version_range
                and cached.lowest == lowest
            ):
                return list(cached.result)

        for group in self._sorted_groups(package, available, cacheable=choices is None):
            remaining = list(group)
            while remaining:
                accepted = list(
                    version_range.filter(
                        remaining, key=lambda candidate: candidate.version
                    )
                )
                if not accepted:
                    break
                accepted.sort(
                    key=lambda candidate: (
                        len(candidate.version.release),
                        candidate.label,
                    )
                )
                accepted.sort(
                    key=lambda candidate: candidate.version, reverse=not lowest
                )
                preferred = self.preferences.get(base)
                if preferred is not None and not lowest:
                    _prefer_version(accepted, preferred)
                result.extend(accepted)
                exhausted = {candidate.label for candidate in accepted}
                remaining = [
                    candidate
                    for candidate in remaining
                    if candidate.label not in exhausted
                ]

        if cache_key is not None:
            self._default_orders[cache_key] = _CandidateOrder(
                available, version_range, tuple(result), lowest=lowest
            )
        return result

    def prepared_data(self, candidate: Candidate) -> CandidateData:
        """Read the facts required to use a candidate selected by the solver."""
        data = self.facts.get(candidate)
        if data is None:
            msg = (
                f"cannot use {candidate.package}=={candidate.version}"
                " without prepared metadata"
            )
            raise ResolutionInvariantError(msg)
        return data

    def adopt(
        self, selected: Mapping[str, Candidate], sources: Mapping[str, tuple[str, ...]]
    ) -> tuple[dict[str, Version], Provider]:
        """Install prepared metadata and the declarations allowing each yank."""
        # Check the whole selection before changing the provider used for the lock.
        prepared = []
        admissions = {}
        for package, candidate in selected.items():
            data = self.prepared_data(candidate)
            prepared.append((package, candidate, data))

            if candidate.withdrawn:
                declarations = sources.get(candidate.base)
                if not declarations:
                    msg = (
                        f"cannot adopt yanked {candidate.base}=={candidate.version}"
                        " without an admitting declaration"
                    )
                    raise ResolutionInvariantError(msg)
                admissions[candidate.base, candidate.version] = declarations

        final = self.provider
        for package, candidate, data in prepared:
            source = data.provider
            base = candidate.base
            final.version_dists_cache.pop(base, None)
            final.versions_cache[base] = source.versions_cache[base]
            final.metadata_cache[(base, candidate.version)] = source.metadata_cache[
                (base, candidate.version)
            ]
            final.deps_cache[(package, candidate.version)] = data.dependencies
            final.deferred_url_extras[(base, candidate.version)] = (
                source.deferred_url_extras.get((base, candidate.version), {})
            )
            final.consulted_markers.update(source.consulted_markers)
        for candidate in selected.values():
            final.defer_extra_deps((candidate.base, candidate.version))
        final.yank_admissions = MappingProxyType(admissions)
        return {name: candidate.version for name, candidate in selected.items()}, final


def _candidate_dependencies(
    provider: Provider, candidate: Candidate
) -> dict[str, VersionRange]:
    """Read dependencies after applying the provider's missing-extra policy."""
    base, extra = split_extra(candidate.package)
    if (
        extra is not None
        and provider.extras_mode is ExtrasMode.BACKTRACK
        and not version_provides_extra(provider, base, extra, candidate.version)
    ):
        msg = f"{base}=={candidate.version} does not provide extra '{extra}'"
        raise MissingExtraError(msg)
    return provider.get_dependencies(candidate.package, candidate.version)


def merge_stats(target: ProviderStats, source: ProviderStats) -> None:
    """Accumulate counters from a candidate's preparation provider."""
    for name in field_names(source):
        setattr(target, name, getattr(target, name) + getattr(source, name))
