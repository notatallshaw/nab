"""Re-resolve a tuple using its specific wheel's metadata.

When :mod:`validate` reports a ``divergent`` finding, the lock's pins
were chosen against the resolver's baseline metadata, not the
metadata of the wheel the tuple would actually install. This module
provides an opt-in second pass that re-resolves the affected tuple
with the wheel-specific metadata so the lock surfaces any new or
removed pins driven by the divergence.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._vendor.packaging.utils import canonicalize_name
from .._vendor.packaging.version import Version
from ..provider import (
    ArchiveSource,
    BuildPolicy,
    DistPolicy,
    LocalSource,
    VcsConfig,
    VcsSource,
)
from .matrix import Matrix as _Matrix
from .resolve import resolve_with_coordinator
from .validate import _dependencies_override_for

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from ..config import IndexOverride, NabProjectConfig, PackageOverride
    from ..fetch import FetchCoordinator
    from ..target import ResolveTarget
    from .resolve import UniversalResult
    from .validate import PinValidation, ValidationReport


__all__ = [
    "ReresolveDiff",
    "reresolve_divergent_tuples",
]


@dataclass
class ReresolveDiff:
    """Difference between the original and the wheel-aware re-resolve."""

    tuple_label: str
    added: dict[str, str] = field(default_factory=dict)
    removed: dict[str, str] = field(default_factory=dict)
    version_changed: dict[str, tuple[str, str]] = field(default_factory=dict)


def reresolve_divergent_tuples(  # noqa: PLR0913 - mirrors the original resolve
    coordinator: FetchCoordinator,
    requirements: list[str],
    original: UniversalResult,
    report: ValidationReport,
    *,
    constraints: list[str] | None = None,
    uploaded_prior_to: datetime | None = None,
    dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
    build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
    package_overrides: Sequence[PackageOverride] = (),
    index_overrides: Mapping[str, IndexOverride] | None = None,
    vcs_config: VcsConfig | None = None,
    local_sources: list[LocalSource] | None = None,
    vcs_sources: list[VcsSource] | None = None,
    vcs_cache_dir: Path | None = None,
    archive_sources: list[ArchiveSource] | None = None,
    archive_cache_dir: Path | None = None,
    build_config: NabProjectConfig | None = None,
    resolution_strategy: str = "highest",
) -> dict[str, ReresolveDiff]:
    """Re-resolve every tuple with at least one divergent pin.

    Returns a mapping from tuple_label to its diff against the
    original pins.  Tuples with no divergent findings are skipped.

    The resolve is run with the *same* configuration the original
    matrix used (``uploaded_prior_to``, sdist/build policies,
    resolution strategy).  Mismatched options would compare apples
    to oranges; passing them through keeps the diff meaningful.
    """
    new_pins_per_tuple = _reresolve_one_step(
        coordinator,
        requirements,
        original,
        report,
        constraints=constraints,
        uploaded_prior_to=uploaded_prior_to,
        dist_policy=dist_policy,
        build_policy=build_policy,
        package_overrides=package_overrides,
        index_overrides=index_overrides,
        vcs_config=vcs_config,
        local_sources=local_sources,
        vcs_sources=vcs_sources,
        vcs_cache_dir=vcs_cache_dir,
        archive_sources=archive_sources,
        archive_cache_dir=archive_cache_dir,
        build_config=build_config,
        resolution_strategy=resolution_strategy,
    )
    diffs: dict[str, ReresolveDiff] = {}
    for tr in original.tuple_results:
        if not tr.success:
            continue
        if tr.tuple_.label not in new_pins_per_tuple:
            continue
        original_pins = {k: str(v) for k, v in tr.pins.items()}
        diffs[tr.tuple_.label] = _diff_pins(
            tr.tuple_.label,
            new_pins=new_pins_per_tuple[tr.tuple_.label],
            original_pins=original_pins,
        )
    return diffs


def _reresolve_one_step(  # noqa: PLR0913
    coordinator: FetchCoordinator,
    requirements: list[str],
    current: UniversalResult,
    report: ValidationReport,
    *,
    constraints: list[str] | None,
    uploaded_prior_to: datetime | None,
    dist_policy: DistPolicy,
    build_policy: BuildPolicy,
    package_overrides: Sequence[PackageOverride] = (),
    index_overrides: Mapping[str, IndexOverride] | None = None,
    vcs_config: VcsConfig | None = None,
    local_sources: list[LocalSource] | None = None,
    vcs_sources: list[VcsSource] | None = None,
    vcs_cache_dir: Path | None = None,
    archive_sources: list[ArchiveSource] | None = None,
    archive_cache_dir: Path | None = None,
    build_config: NabProjectConfig | None = None,
    resolution_strategy: str,
) -> dict[str, dict[str, str]]:
    """Run one re-resolve sweep; return ``{tuple_label: {package: version}}``.

    Tuples without a divergent finding are absent from the output.  A
    tuple whose re-resolve fails (returns no pins) IS present with an
    empty value; callers represent that as "all pins removed", not
    "no work attempted".
    """
    by_tuple = _findings_by_tuple_label(report)
    new_pins_per_tuple: dict[str, dict[str, str]] = {}
    for tr in current.tuple_results:
        if not tr.success:
            continue
        findings = by_tuple.get(tr.tuple_.label, [])
        # A pin whose deps came from a ``dependencies`` override would
        # re-resolve identically (the override reapplies), so skip it: the
        # diff would be empty and the fetch wasted.
        divergent = [
            f
            for f in findings
            if f.status == "divergent"
            and not _dependencies_override_for(
                package_overrides, canonicalize_name(f.package), Version(f.version)
            )
        ]
        if not divergent:
            continue
        wheel_metadata = _collect_wheel_metadata_overrides(coordinator, divergent)
        if not wheel_metadata:  # pragma: no cover - divergent always has metadata
            continue
        new_pins = _resolve_one_tuple_with_overrides(
            coordinator,
            tr.tuple_,
            requirements,
            wheel_metadata,
            constraints=constraints,
            uploaded_prior_to=uploaded_prior_to,
            dist_policy=dist_policy,
            build_policy=build_policy,
            package_overrides=package_overrides,
            index_overrides=index_overrides,
            vcs_config=vcs_config,
            local_sources=local_sources,
            vcs_sources=vcs_sources,
            vcs_cache_dir=vcs_cache_dir,
            archive_sources=archive_sources,
            archive_cache_dir=archive_cache_dir,
            build_config=build_config,
            resolution_strategy=resolution_strategy,
        )
        new_pins_per_tuple[tr.tuple_.label] = new_pins
    return new_pins_per_tuple


def _resolve_one_tuple_with_overrides(  # noqa: PLR0913
    coordinator: FetchCoordinator,
    tup: ResolveTarget,
    requirements: list[str],
    wheel_metadata: dict[tuple[str, str], str],
    *,
    constraints: list[str] | None,
    uploaded_prior_to: datetime | None,
    dist_policy: DistPolicy,
    build_policy: BuildPolicy,
    package_overrides: Sequence[PackageOverride] = (),
    index_overrides: Mapping[str, IndexOverride] | None = None,
    vcs_config: VcsConfig | None = None,
    local_sources: list[LocalSource] | None = None,
    vcs_sources: list[VcsSource] | None = None,
    vcs_cache_dir: Path | None = None,
    archive_sources: list[ArchiveSource] | None = None,
    archive_cache_dir: Path | None = None,
    build_config: NabProjectConfig | None = None,
    resolution_strategy: str,
) -> dict[str, str]:
    """Re-resolve ``tup`` with the given metadata overrides; return pins."""
    # Carry the original target's patch release so markers gated on
    # python_full_version evaluate the same in both passes.
    spec = tup.platform_spec
    assert spec is not None  # every matrix target declares one
    one_tuple_matrix = _Matrix(
        python=f"=={tup.python_version}",
        platforms=(spec,),
        python_patches={tup.python_version: tup.python_full_version},
        implementations=(tup.implementation,),
    )
    with _override_metadata(coordinator, wheel_metadata):
        result = resolve_with_coordinator(
            coordinator,
            one_tuple_matrix,
            requirements,
            constraints=constraints,
            uploaded_prior_to=uploaded_prior_to,
            dist_policy=dist_policy,
            build_policy=build_policy,
            package_overrides=package_overrides,
            index_overrides=index_overrides,
            vcs_config=vcs_config,
            local_sources=local_sources,
            vcs_sources=vcs_sources,
            vcs_cache_dir=vcs_cache_dir,
            archive_sources=archive_sources,
            archive_cache_dir=archive_cache_dir,
            build_config=build_config,
            resolution_strategy=resolution_strategy,
        )
    if not result.success:
        return {}
    return {k: str(v) for k, v in result.tuple_results[0].pins.items()}


def _findings_by_tuple_label(
    report: ValidationReport,
) -> dict[str, list[PinValidation]]:
    """Group validation findings by tuple label."""
    out: defaultdict[str, list[PinValidation]] = defaultdict(list)
    for f in report.findings:
        out[f.tuple_label].append(f)
    return out


def _collect_wheel_metadata_overrides(
    coordinator: FetchCoordinator,
    findings: list[PinValidation],
) -> dict[tuple[str, str], str]:
    """For each divergent finding, fetch the chosen wheel's metadata text."""
    out: dict[tuple[str, str], str] = {}
    for f in findings:
        if f.chosen_wheel is None:  # pragma: no cover
            continue
        normalized = canonicalize_name(f.package)
        text = coordinator.index.get_metadata(
            normalized, f"{f.version}#{f.chosen_wheel}"
        )
        if text is not None:
            out[(normalized, f.version)] = text
    return out


def _diff_pins(
    tuple_label: str,
    *,
    new_pins: dict[str, str],
    original_pins: dict[str, str] | None = None,
) -> ReresolveDiff:
    """Compute (added, removed, version_changed) between two pin sets."""
    diff = ReresolveDiff(tuple_label=tuple_label)
    if original_pins is None:
        diff.added = dict(new_pins)
        return diff
    for name, new_ver in new_pins.items():
        if name not in original_pins:
            diff.added[name] = new_ver
        elif original_pins[name] != new_ver:
            diff.version_changed[name] = (original_pins[name], new_ver)
    for name, old_ver in original_pins.items():
        if name not in new_pins:
            diff.removed[name] = old_ver
    return diff


@contextmanager
def _override_metadata(
    coordinator: FetchCoordinator,
    overrides: dict[tuple[str, str], str],
) -> Iterator[None]:
    """Snapshot, override, and restore baseline metadata for one re-resolve.

    Stores ``overrides`` at the ``(name, version)`` key the provider
    reads, then restores the previous values on exit.  Also evicts
    the matching entries from the coordinator's parsed-metadata
    cache so a subsequent ``get_parsed_metadata`` re-parses the
    overridden raw text rather than serving the prior tuple's view.
    """
    text_snapshot: dict[tuple[str, str], str | None] = {}
    from_sdist_snapshot: dict[tuple[str, str], bool] = {}
    parsed_snapshot: dict[tuple[str, str], object | None] = {}
    for key in overrides:
        text_snapshot[key] = coordinator.index.get_metadata(*key)
        from_sdist_snapshot[key] = coordinator.index.metadata_from_sdist(*key)
        parsed_snapshot[key] = coordinator.index.pop_parsed_metadata(*key)
    try:
        for key, text in overrides.items():
            coordinator.index.store_metadata(*key, text)
        yield
    finally:
        # Restore through the matching store call so the slot's sdist
        # provenance survives the override cycle.
        for key, prior in text_snapshot.items():
            if from_sdist_snapshot[key]:
                coordinator.index.store_sdist_metadata(*key, prior)
            else:
                coordinator.index.store_metadata(*key, prior)
        for key, parsed in parsed_snapshot.items():
            coordinator.index.pop_parsed_metadata(*key)
            if parsed is not None:
                coordinator.index.store_parsed_metadata(*key, parsed)
