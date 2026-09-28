"""Cache target-specific candidates and their prepared dependency metadata."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

from ._provider.extras import version_provides_extra
from ._provider.metadata_resolver import classify_requirement, dists_at_version
from ._vendor.packaging.utils import canonicalize_name
from .errors import MetadataError, MissingExtraError
from .extra_keys import split_extra
from .metadata import intern_version
from .policy import DistPolicy, ExtrasMode
from .records import SdistFile, WheelFile

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ._vendor.packaging.ranges import VersionRange
    from ._vendor.packaging.requirements import Requirement
    from ._vendor.packaging.version import Version
    from .provider import DistFile, Provider, ProviderStats


@dataclass(frozen=True, slots=True)
class Candidate:
    """A package version restricted to its yanked or non-yanked files."""

    package: str
    version: Version
    withdrawn: bool
    label: str = field(init=False)

    def __post_init__(self) -> None:
        """Keep the version text for arbitrary equality comparisons."""
        object.__setattr__(self, "label", str(self.version))

    @property
    def base(self) -> str:
        """Return the package name without an extra proxy."""
        return split_extra(self.package)[0]


@dataclass(slots=True)
class CandidateData:
    """Dependencies and the provider selecting the candidate's files."""

    provider: Provider
    dependencies: dict[str, VersionRange]
    pins: tuple[Requirement, ...]


class UnavailableMetadataError(Exception):
    """An offline miss cannot establish that a live choice is impossible."""


def is_pin(requirement: Requirement) -> bool:
    """Recognize original exact equality syntax, including redundant conjuncts."""
    return any(
        spec.operator == "==="
        or (spec.operator == "==" and not spec.version.endswith(".*"))
        for spec in requirement.specifier
    )


def add_range(ranges: dict[str, VersionRange], name: str, value: VersionRange) -> None:
    """Intersect a requirement with the constraints already collected for its name."""
    previous = ranges.get(name)
    ranges[name] = value if previous is None else previous & value


class YankCandidates:
    """Cache candidates and metadata without granting permission to use yanked files."""

    def __init__(
        self,
        provider: Provider,
        factory: Callable[[], Provider],
        *,
        preferences: Mapping[str, Version],
    ) -> None:
        """Start one target snapshot with the caller's preparation policy."""
        self.provider = provider
        self.factory = factory
        self.preferences = preferences
        self.candidates: dict[str, tuple[Candidate, ...]] = {}
        self.facts: dict[Candidate, CandidateData | None] = {}
        self.unavailable: set[Candidate] = set()
        self.missing_listings: set[str] = set()
        self.unavailable_reason = "required metadata is unavailable"
        self.rejections: list[str] = []
        self.child_stats: ProviderStats = type(provider.stats)()

    def choices(self, package: str) -> tuple[Candidate, ...]:
        """List compatible candidates without preparing their metadata."""
        cached = self.candidates.get(package)
        if cached is not None:
            return cached
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
            self.candidates[package] = ()
            return ()
        result = dict.fromkeys(
            Candidate(package, intern_version(file.version), bool(file.yanked))
            for _version, file in versions
        )
        cached = tuple(
            candidate for candidate in result if self.installable(candidate, versions)
        )
        self.candidates[package] = cached
        return cached

    def installable(
        self, candidate: Candidate, versions: Sequence[tuple[Version, DistFile]]
    ) -> bool:
        """Require an sdist among this candidate's files for sdist-install."""
        if (
            self.provider.effective_dist_policy(
                candidate.base,
                candidate.version,
                self.provider.serving_index(candidate.base),
            )
            is not DistPolicy.SDIST_INSTALL
        ):
            return True
        return any(
            version == candidate.version
            and str(intern_version(file.version)) == candidate.label
            and isinstance(file, SdistFile)
            and bool(file.yanked) == candidate.withdrawn
            for version, file in versions
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
        provider = self.factory()
        provider.versions_cache[candidate.base] = [
            (candidate.version, file)
            for file in files
            if bool(file.yanked) == candidate.withdrawn
            and str(intern_version(file.version)) == candidate.label
        ]
        if candidate.withdrawn:
            provider.yank_admissions = frozenset({(candidate.base, candidate.version)})
        return provider

    def prepare(self, candidate: Candidate) -> CandidateData | None:
        """Prepare a candidate after the caller has checked permission to use it."""
        if candidate in self.facts:
            return self.facts[candidate]
        if candidate in self.unavailable:
            return None
        provider = self.preparation_provider(candidate)
        try:
            data = self.read_metadata(provider, candidate)
        except UnavailableMetadataError as exc:
            self.unavailable.add(candidate)
            self.unavailable_reason = str(exc)
            return None
        finally:
            if provider is not self.provider:
                merge_stats(self.child_stats, provider.stats)
                self.provider.consulted_markers.update(provider.consulted_markers)
        self.facts[candidate] = data
        return data

    def read_metadata(
        self, provider: Provider, candidate: Candidate
    ) -> CandidateData | None:
        """Read dependencies and their original pin declarations for one candidate."""
        base = candidate.base
        try:
            dependencies = candidate_dependencies(provider, candidate)
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
            self.rejections.append(str(exc))
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
        for withdrawn in (False, True):
            candidates = {
                candidate.label: candidate
                for candidate in available
                if candidate.withdrawn == withdrawn
            }
            versions = sorted(
                (candidate.version for candidate in candidates.values()), reverse=True
            )
            while versions:
                accepted = list(
                    version_range.filter(versions, assume_sorted="descending")
                )
                if not accepted:
                    break
                accepted.sort(key=lambda version: (len(version.release), str(version)))
                accepted.sort(reverse=not lowest)
                preferred = self.preferences.get(base)
                if preferred is not None and preferred in accepted and not lowest:
                    picked = next(
                        (
                            version
                            for version in accepted
                            if str(version) == str(preferred)
                        ),
                        accepted[accepted.index(preferred)],
                    )
                    accepted = [
                        version for version in accepted if str(version) != str(picked)
                    ]
                    accepted.insert(0, picked)
                result.extend(candidates[str(version)] for version in accepted)
                exhausted = {str(version) for version in accepted}
                versions = [
                    version for version in versions if str(version) not in exhausted
                ]
        return result

    def adopt(
        self, selected: Mapping[str, Candidate]
    ) -> tuple[dict[str, Version], Provider]:
        """Return selected versions and their restricted metadata provider."""
        final = self.provider
        permissions = set()
        for package, candidate in selected.items():
            data = self.facts[candidate]
            assert data is not None
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
            if candidate.withdrawn:
                permissions.add((base, candidate.version))
        for candidate in selected.values():
            final.defer_extra_deps((candidate.base, candidate.version))
        final.yank_admissions = frozenset(permissions)
        return {name: candidate.version for name, candidate in selected.items()}, final


def candidate_dependencies(
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
    for counter in fields(source):
        name = counter.name
        setattr(target, name, getattr(target, name) + getattr(source, name))
