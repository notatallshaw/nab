"""Failure-time attribution for the listing filter's drops.

The filter drops a file with a bare ``continue``, so nothing on the resolve
path records which rung refused it.  When a package ends up with no usable
version and the resolve then fails, the provider walks that package's raw
listing a second time through the predicates in
:mod:`nab_provider._provider.listing`, in the order the filter applies them,
and records which one refused each file.  The sentence the user reads is
built here, from that record.

Nothing in this module runs on a resolve that succeeds.  The walk partitions
the whole listing rather than a difference, so every file it sees is kept or
refused exactly once, and a version it keeps that the filter did not is
counted as unexplained rather than passed over.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nab_provider.records import SdistFile, WheelFile

from ..errors import InvalidUploadTimeError
from ..policy import DistPolicy
from . import listing as _listing
from .listing import DropCause

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider._vendor.packaging.version import Version

    from ..provider import DistFile, Provider
    from .listing import _ListingPolicy


# The filter each cause belongs to, as the in-range lead names it.  One
# table for both leads, so the two cannot enumerate different filter sets
# for the same drop.
FILTER_LABELS: dict[DropCause, str] = {
    DropCause.UPLOAD_TIME_MISSING: "upload-time",
    DropCause.UPLOAD_TIME_UNPARSEABLE: "upload-time",
    DropCause.UPLOAD_TIME_NAIVE: "upload-time",
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: "upload-time",
    DropCause.DIST_POLICY: "dist-policy",
    DropCause.SDIST_INSTALL_NO_SDIST: "dist-policy",
    DropCause.REQUIRES_PYTHON: "requires-python",
    DropCause.WHEEL_TAGS: "wheel tags",
    # Unreachable from the in-range lead, which compares versions against a
    # range and so cannot see a file whose version never parsed.
    DropCause.INVALID_VERSION: "version parsing",
}

UPLOAD_TIME_CAUSES = frozenset(
    {
        DropCause.UPLOAD_TIME_MISSING,
        DropCause.UPLOAD_TIME_UNPARSEABLE,
        DropCause.UPLOAD_TIME_NAIVE,
        DropCause.UPLOAD_TIME_AFTER_CUTOFF,
    }
)


class CutoffLayer(enum.Enum):
    """Which config layer set the upload-time cutoff a candidate was judged by."""

    GLOBAL = "global"
    PACKAGE = "package"
    INDEX = "index"


@dataclass(frozen=True, slots=True)
class CutoffSource:
    """The layer that set a cutoff, and the config entry that carries it.

    ``label`` is the per-package override's ``source_label`` or the index's
    name; it is empty for the project-level cutoff, which no entry names.
    """

    layer: CutoffLayer
    label: str


@dataclass(slots=True)
class DroppedFile:
    """One file the diagnosis walk attributed to a rung.

    ``version`` is ``None`` only for :attr:`DropCause.INVALID_VERSION`,
    where the filename's version never parsed.  ``detail`` is the one value
    that cause's clause quotes: the effective Requires-Python specifier, the
    raw upload time, or the effective dist policy.  Deliberately not frozen:
    the walk builds one per dropped file and never mutates it, and a frozen
    dataclass costs several times as much to construct.
    """

    filename: str
    raw_version: str
    version: Version | None
    is_wheel: bool
    cause: DropCause
    detail: str | None = None
    cutoff: datetime | None = None


@dataclass(frozen=True, slots=True)
class ListingDiagnosis:
    """Every refusal the filter made over one package's raw listing, for one target.

    ``kept`` is what the walk admitted, and ``unexplained`` counts the
    versions in it the filter did not keep: the walk's own check that it
    modelled every rung, since a drop it cannot name would otherwise read
    as a file nothing refused.
    """

    package: str
    index_name: str | None
    dropped: tuple[DroppedFile, ...]
    kept: frozenset[Version]
    published_sdist: bool
    unexplained: int
    target_python: str | None


class ReasonKind(enum.Enum):
    """Which situation left ``choose_version`` with no version to return."""

    OFFLINE_MISS = "offline-miss"
    UNREADABLE_ONLY = "unreadable-only"
    ABSENT = "absent"
    FILTERED_EMPTY = "filtered-empty"
    BLOCKERS = "blockers"
    NO_MATCH = "no-match"


@dataclass(frozen=True, slots=True)
class NoVersionsReason:
    """What the resolve recorded when a package ran out of candidates.

    Recorded during the resolve and rendered only if the resolve then
    fails, so it holds what the failure path cannot re-derive and nothing
    else: which situation applied, the look-ahead blocker strings (which
    reset per scan), and the range that was asked.
    """

    kind: ReasonKind
    blockers: tuple[str, ...] = ()
    version_range: VersionRange | None = None

    @property
    def is_generic(self) -> bool:
        """Whether this reason says only that nothing matched.

        A generic reason loses to a metadata ban, which names a cause.
        """
        return self.kind is ReasonKind.NO_MATCH


# The four situations that carry nothing beyond their kind, built once so
# the record path allocates nothing for them.
OFFLINE_MISS = NoVersionsReason(ReasonKind.OFFLINE_MISS)
UNREADABLE_ONLY = NoVersionsReason(ReasonKind.UNREADABLE_ONLY)
ABSENT = NoVersionsReason(ReasonKind.ABSENT)
FILTERED_EMPTY = NoVersionsReason(ReasonKind.FILTERED_EMPTY)


OFFLINE_MISS_TEXT = "offline mode skipped an index with no cached listing"
UNREADABLE_ONLY_TEXT = (
    "found on index but no file is a wheel or a .tar.gz sdist (the formats nab reads)"
)
ABSENT_TEXT = "package not found on any configured index"
NO_MATCH_TEXT = "no version matches the requirement"

# The kinds whose sentence is a constant, copied character for character
# from the classifier they replace.
FIXED_TEXTS: dict[ReasonKind, str] = {
    ReasonKind.OFFLINE_MISS: OFFLINE_MISS_TEXT,
    ReasonKind.UNREADABLE_ONLY: UNREADABLE_ONLY_TEXT,
    ReasonKind.ABSENT: ABSENT_TEXT,
}

_EMPTY_LEAD = "found on index but no distribution is compatible"
_IN_RANGE_LEAD = (
    "found on index but every version matching the requirement was filtered"
)
_NO_SDIST_TAIL = "no sdist is available to build from"


def walk_listing(provider: Provider, normalized: str) -> ListingDiagnosis | None:
    """Re-walk ``normalized``'s raw listing, recording what refused each file.

    Returns ``None`` when the index served nothing, which leaves the walk
    with nothing to attribute.  Every predicate it calls is the one the
    filter called, so a file the filter kept is refused by nothing here.
    The counter bumps some of those predicates make are undone by the
    caller; see :meth:`~nab_provider.provider.Provider.diagnose_listing`.
    """
    files = provider.coordinator.index.get_listing(normalized)
    if not files:
        return None

    policy = _listing.listing_policy(provider, normalized)
    dropped, survivors, sdist_install = _base_pass(provider, normalized, files, policy)
    tag_dropped, kept = _tag_pass(provider, normalized, survivors, sdist_install)
    dropped.extend(tag_dropped)

    filtered = provider.versions_cache.get(normalized) or []
    return ListingDiagnosis(
        package=normalized,
        index_name=policy.index_name,
        dropped=tuple(dropped),
        kept=frozenset(kept),
        published_sdist=any(isinstance(dist, SdistFile) for dist in files),
        unexplained=len(kept - {version for version, _ in filtered}),
        target_python=(
            None if provider.target is None else provider.target.python_version
        ),
    )


def _base_pass(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
    policy: _ListingPolicy,
) -> tuple[list[DroppedFile], list[tuple[Version, DistFile]], set[Version]]:
    """Partition ``files`` by every rung that runs before the wheel-tag pass.

    Returns the refusals, the survivors in listing order, and the versions
    the dist-policy rung judged SDIST_INSTALL, which the tag pass needs.
    """
    dropped: list[DroppedFile] = []
    survivors: list[tuple[Version, DistFile]] = []
    sdist_install: set[Version] = set()

    for dist in files:
        version = _listing.parsed_version(dist.version)
        if version is None:
            dropped.append(_record(dist, None, DropCause.INVALID_VERSION))
            continue

        if policy.overridden:
            effective = provider.effective_dist_policy(
                normalized, version, policy.index_name
            )
        else:
            effective = policy.default_dist_policy

        if _listing.excluded_by_dist_policy(dist, effective):
            dropped.append(
                _record(dist, version, DropCause.DIST_POLICY, detail=effective.value)
            )
            continue

        if effective is DistPolicy.SDIST_INSTALL:
            sdist_install.add(version)

        cause = python_or_time_verdict(provider, normalized, version, dist, policy)
        if cause is not None:
            dropped.append(
                _detailed(provider, normalized, version, dist, cause, policy)
            )
            continue

        survivors.append((version, dist))

    return dropped, survivors, sdist_install


def _tag_pass(
    provider: Provider,
    normalized: str,
    survivors: list[tuple[Version, DistFile]],
    sdist_install: set[Version],
) -> tuple[list[DroppedFile], set[Version]]:
    """Partition the base pass's survivors by the two whole-target rungs."""
    no_sdist = _listing.sdist_install_wheel_only(survivors, sdist_install)
    tags = provider.wheel_tags

    dropped: list[DroppedFile] = []
    kept: set[Version] = set()
    for version, dist in survivors:
        if version in no_sdist:
            dropped.append(_record(dist, version, DropCause.SDIST_INSTALL_NO_SDIST))
            continue

        if tags is not None and _listing.excluded_by_wheel_tags(
            provider, normalized, version, dist, tags
        ):
            dropped.append(_record(dist, version, DropCause.WHEEL_TAGS))
            continue

        kept.add(version)

    return dropped, kept


def python_or_time_verdict(
    provider: Provider,
    normalized: str,
    version: Version,
    dist: DistFile,
    policy: _ListingPolicy,
) -> DropCause | None:
    """Answer the filter's Requires-Python and upload-time question, totally.

    Calls the filter's own
    :func:`~nab_provider._provider.listing.python_or_time_cause` rather than
    a copy of it, so the two cannot disagree, and turns the refusal it
    raises on a timezone-naive upload time back into the cause it came
    from.  The walk runs inside an error path: an exception here would lose
    the report it was building.  The counters that call raises are taken
    back by :meth:`~nab_provider.provider.Provider.diagnose_listing`.
    """
    try:
        return _listing.python_or_time_cause(
            provider, normalized, version, dist, policy
        )
    except InvalidUploadTimeError:
        return DropCause.UPLOAD_TIME_NAIVE


def _record(
    dist: DistFile,
    version: Version | None,
    cause: DropCause,
    *,
    detail: str | None = None,
    cutoff: datetime | None = None,
) -> DroppedFile:
    """Build the record for one refused file."""
    return DroppedFile(
        filename=dist.filename,
        raw_version=dist.version,
        version=version,
        is_wheel=isinstance(dist, WheelFile),
        cause=cause,
        detail=detail,
        cutoff=cutoff,
    )


def _detailed(
    provider: Provider,
    normalized: str,
    version: Version,
    dist: DistFile,
    cause: DropCause,
    policy: _ListingPolicy,
) -> DroppedFile:
    """Record a Requires-Python or upload-time refusal with the value it quotes.

    Re-reads the effective value through the same provider methods
    :func:`~nab_provider._provider.listing.python_or_time_cause` read it
    through, so the clause quotes what the filter applied.
    """
    if cause is DropCause.REQUIRES_PYTHON:
        override = (
            provider.effective_requires_python(normalized, version)
            if policy.overridden
            else None
        )
        spec = override if override is not None else dist.requires_python
        return _record(dist, version, cause, detail=spec)

    if policy.overridden:
        cutoff = provider.effective_uploaded_prior_to(
            normalized, version, policy.index_name
        )
    else:
        cutoff = policy.default_cutoff
    return _record(dist, version, cause, detail=dist.upload_time, cutoff=cutoff)


def empty_listing_reason(
    provider: Provider, normalized: str, diagnosis: ListingDiagnosis
) -> str:
    """Say why nothing on ``normalized``'s listing is usable on this target."""
    clauses = _clauses(diagnosis)
    if diagnosis.unexplained:
        clauses.append(_unexplained_clause(diagnosis.unexplained))
    if not diagnosis.published_sdist and not any(
        record.cause is DropCause.SDIST_INSTALL_NO_SDIST for record in diagnosis.dropped
    ):
        clauses.append(_NO_SDIST_TAIL)

    line = f"{_EMPTY_LEAD}: {'; '.join(clauses)}" if clauses else _EMPTY_LEAD
    remedy = _remedy(provider, normalized, diagnosis)
    return line if remedy is None else f"{line}\n    note: {remedy}"


def in_range_reason(
    version_range: VersionRange, diagnosis: ListingDiagnosis
) -> str | None:
    """Say which filters dropped the releases matching ``version_range``.

    Returns ``None`` when no refused release falls inside the range, which
    is the caller's signal that the requirement asks for a version the index
    never published.  A refused version equal to one in ``kept`` survived
    under another spelling and does not count.
    """
    named = [
        record
        for record in diagnosis.dropped
        if record.version is not None and record.version not in diagnosis.kept
    ]
    in_range = set(version_range.filter({record.version for record in named}))

    labels: list[str] = []
    for record in sorted(named, key=lambda record: record.cause.value):
        if record.version in in_range and FILTER_LABELS[record.cause] not in labels:
            labels.append(FILTER_LABELS[record.cause])
    if not labels:
        return None
    return f"{_IN_RANGE_LEAD} (by {_join_labels(labels)})"


def _join_labels(labels: list[str]) -> str:
    """Join filter labels the way the sentence reads them."""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:  # noqa: PLR2004 - "a or b" against "a, b, or c"
        return f"{labels[0]} or {labels[1]}"
    return f"{', '.join(labels[:-1])}, or {labels[-1]}"


def _clauses(diagnosis: ListingDiagnosis) -> list[str]:
    """Render one clause per cause that fired, in report order."""
    groups: dict[DropCause, list[DroppedFile]] = {}
    for record in diagnosis.dropped:
        groups.setdefault(record.cause, []).append(record)

    clauses: list[str] = []
    for cause in sorted(groups, key=lambda cause: cause.value):
        records = groups[cause]
        if cause is DropCause.INVALID_VERSION:
            clauses.append(_invalid_version_clause(records))
            continue
        # The count is files, except the whole-version rung; the quoted
        # detail comes from the highest version the cause refused, so the
        # release a user most likely wanted is the one named.
        count = (
            len({record.version for record in records})
            if cause is DropCause.SDIST_INSTALL_NO_SDIST
            else len(records)
        )
        clauses.append(_CLAUSE_BUILDERS[cause](count, _newest(records), diagnosis))
    return clauses


def _newest(records: list[DroppedFile]) -> DroppedFile:
    """Return the record carrying the highest version of a cause's group."""
    return max(records, key=_version_of)


def _version_of(record: DroppedFile) -> Version:
    """Return a record's version, which every cause but INVALID_VERSION has."""
    assert record.version is not None
    return record.version


def _iso(cutoff: datetime | None) -> str:
    """Render the cutoff an upload-time clause quotes."""
    assert cutoff is not None
    return cutoff.isoformat()


def _missing_upload_time_clause(
    count: int, newest: DroppedFile, _diagnosis: ListingDiagnosis
) -> str:
    prefix = f"the uploaded-prior-to cutoff {_iso(newest.cutoff)} excluded"
    if count == 1:
        return f"{prefix} 1 file that publishes no upload time ({newest.version})"
    return (
        f"{prefix} {count} files that publish no upload time (newest: {newest.version})"
    )


def _unparseable_upload_time_clause(
    count: int, newest: DroppedFile, _diagnosis: ListingDiagnosis
) -> str:
    prefix = "the uploaded-prior-to cutoff excluded"
    if count == 1:
        return (
            f"{prefix} 1 file whose upload time is not ISO 8601"
            f" ({newest.version}, {newest.detail!r})"
        )
    return (
        f"{prefix} {count} files whose upload time is not ISO 8601"
        f" (newest: {newest.version}, {newest.detail!r})"
    )


def _naive_upload_time_clause(
    count: int, newest: DroppedFile, _diagnosis: ListingDiagnosis
) -> str:
    prefix = "the uploaded-prior-to cutoff could not judge"
    if count == 1:
        return (
            f"{prefix} 1 file whose upload time carries no timezone"
            f" ({newest.version}, {newest.detail!r})"
        )
    return (
        f"{prefix} {count} files whose upload time carries no timezone"
        f" (newest: {newest.version}, {newest.detail!r})"
    )


def _after_cutoff_clause(
    count: int, newest: DroppedFile, _diagnosis: ListingDiagnosis
) -> str:
    prefix = f"the uploaded-prior-to cutoff {_iso(newest.cutoff)} excluded"
    if count == 1:
        return f"{prefix} 1 file uploaded at {newest.detail} ({newest.version})"
    return (
        f"{prefix} {count} files uploaded on or after it"
        f" (newest: {newest.version}, uploaded {newest.detail})"
    )


def _dist_policy_clause(
    count: int, newest: DroppedFile, _diagnosis: ListingDiagnosis
) -> str:
    kind = "wheel" if newest.is_wheel else "sdist"
    prefix = f'dist-policy = "{newest.detail}" excluded'
    if count == 1:
        return f"{prefix} 1 {kind} ({newest.version})"
    return f"{prefix} {count} {kind}s (newest: {newest.version})"


def _sdist_install_clause(
    count: int, newest: DroppedFile, _diagnosis: ListingDiagnosis
) -> str:
    prefix = 'dist-policy = "sdist-install" excluded'
    if count == 1:
        return f"{prefix} 1 version that publishes no sdist ({newest.version})"
    return f"{prefix} {count} versions that publish no sdist (newest: {newest.version})"


def _requires_python_clause(
    count: int, newest: DroppedFile, diagnosis: ListingDiagnosis
) -> str:
    target = f"the resolve targets Python {diagnosis.target_python}"
    if count == 1:
        return (
            f"requires-python excluded 1 file"
            f" ({newest.version} requires {newest.detail}, {target})"
        )
    return (
        f"requires-python excluded {count} files"
        f" (newest: {newest.version} requires {newest.detail}, {target})"
    )


def _wheel_tags_clause(
    count: int, _newest: DroppedFile, _diagnosis: ListingDiagnosis
) -> str:
    wheels = "wheel" if count == 1 else "wheels"
    return (
        "none of the wheel's tags are compatible with the resolve target"
        f" ({count} {wheels} rejected)"
    )


def _invalid_version_clause(records: list[DroppedFile]) -> str:
    """Quote the first unparseable version, which has none to rank by."""
    first = records[0].raw_version
    if len(records) == 1:
        return f"1 file carries a version PEP 440 cannot parse ({first!r})"
    return (
        f"{len(records)} files carry a version PEP 440 cannot parse (first: {first!r})"
    )


def _unexplained_clause(count: int) -> str:
    """Say that the walk kept versions the filter did not, rather than hide it."""
    if count == 1:
        return "1 version was dropped for a reason this report cannot name"
    return f"{count} versions were dropped for a reason this report cannot name"


_CLAUSE_BUILDERS: dict[
    DropCause, Callable[[int, DroppedFile, ListingDiagnosis], str]
] = {
    DropCause.UPLOAD_TIME_MISSING: _missing_upload_time_clause,
    DropCause.UPLOAD_TIME_UNPARSEABLE: _unparseable_upload_time_clause,
    DropCause.UPLOAD_TIME_NAIVE: _naive_upload_time_clause,
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: _after_cutoff_clause,
    DropCause.DIST_POLICY: _dist_policy_clause,
    DropCause.SDIST_INSTALL_NO_SDIST: _sdist_install_clause,
    DropCause.REQUIRES_PYTHON: _requires_python_clause,
    DropCause.WHEEL_TAGS: _wheel_tags_clause,
}


def _remedy(
    provider: Provider, normalized: str, diagnosis: ListingDiagnosis
) -> str | None:
    """Name the config layer that set the cutoff, and what lifts it there.

    Offered for the upload-time causes alone.  Requires-Python gets none:
    the per-package override replaces the package's declared metadata, so
    offering it as a fix would be telling the user to lie to the resolver.
    """
    records = [
        record for record in diagnosis.dropped if record.cause in UPLOAD_TIME_CAUSES
    ]
    if not records:
        return None

    source = provider.uploaded_prior_to_source(
        normalized, _version_of(_newest(records)), diagnosis.index_name
    )
    if source.layer is CutoffLayer.PACKAGE:
        return (
            f"the per-package uploaded-prior-to for {source.label} set that cutoff;"
            " setting it to false there lifts it"
        )
    if source.layer is CutoffLayer.INDEX:
        return (
            f'the per-index uploaded-prior-to for index "{source.label}" set that'
            f" cutoff; uploaded-prior-to = false under [tool.nab.index.{source.label}]"
            " lifts it"
        )
    return (
        "the project-level uploaded-prior-to set that cutoff; uploaded-prior-to ="
        f' false under [tool.nab.packages."{normalized}"] lifts it for this package'
    )
