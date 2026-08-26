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
import re
from typing import TYPE_CHECKING

from nab_provider.records import SdistFile, WheelFile

from ..errors import InvalidUploadTimeError
from ..policy import DistPolicy
from . import listing as _listing
from .listing import DropCause

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider._vendor.packaging.version import Version

    from ..provider import DistFile, Provider
    from .listing import ListingPolicy


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


class ReasonKind(enum.Enum):
    """Which situation left ``choose_version`` with no version to return."""

    OFFLINE_MISS = "offline-miss"
    UNREADABLE_ONLY = "unreadable-only"
    ABSENT = "absent"
    FILTERED_EMPTY = "filtered-empty"
    BLOCKERS = "blockers"
    NO_MATCH = "no-match"


# The three value types below are hand-written rather than dataclasses:
# every nab invocation imports this module, and declaring a frozen slots
# dataclass costs tens of times more at import than a plain class with
# ``__slots__``.


class DroppedFile:
    """One file the diagnosis walk attributed to a rung."""

    __slots__ = (
        "cause",
        "cutoff",
        "detail",
        "filename",
        "is_wheel",
        "raw_version",
        "version",
    )

    def __init__(
        self,
        dist: DistFile,
        version: Version | None,
        cause: DropCause,
        detail: str | None = None,
        cutoff: datetime | None = None,
    ) -> None:
        """Record ``dist`` as refused by ``cause``.

        ``version`` is ``None`` only for :attr:`DropCause.INVALID_VERSION`,
        where the filename's version never parsed.  ``detail`` is the one
        value that cause's clause quotes: the effective Requires-Python
        specifier, the raw upload time, or the effective dist policy.
        """
        self.filename = dist.filename
        self.raw_version = dist.version
        self.version = version
        self.is_wheel = isinstance(dist, WheelFile)
        self.cause = cause
        self.detail = detail
        self.cutoff = cutoff


class ListingDiagnosis:
    """Every refusal the filter made over one package's raw listing, for one target."""

    __slots__ = (
        "dropped",
        "index_name",
        "kept",
        "published_sdist",
        "target_python",
        "unexplained",
    )

    def __init__(
        self,
        *,
        index_name: str | None,
        dropped: tuple[DroppedFile, ...],
        kept: frozenset[Version],
        published_sdist: bool,
        unexplained: int,
        target_python: str | None,
    ) -> None:
        """Record what the walk kept and what it refused.

        ``kept`` is what the walk admitted, and ``unexplained`` counts the
        versions in it the filter did not keep: the walk's own check that
        it modelled every rung, since a drop it cannot name would
        otherwise read as a file nothing refused.
        """
        self.index_name = index_name
        self.dropped = dropped
        self.kept = kept
        self.published_sdist = published_sdist
        self.unexplained = unexplained
        self.target_python = target_python


class NoVersionsReason:
    """What the resolve recorded when a package ran out of candidates."""

    __slots__ = ("blockers", "kind", "version_range")

    def __init__(
        self,
        kind: ReasonKind,
        blockers: tuple[str, ...] = (),
        version_range: VersionRange | None = None,
    ) -> None:
        """Mark ``kind`` as the situation, with what the failure cannot re-derive.

        Recorded during the resolve and rendered only if the resolve then
        fails, so it holds the look-ahead blocker strings (which reset per
        scan) and the range that was asked, and nothing else.
        """
        self.kind = kind
        self.blockers = blockers
        self.version_range = version_range

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


NO_MATCH_TEXT = "no version matches the requirement"

# The kinds whose sentence is a constant, copied character for character
# from the classifier they replace.
FIXED_TEXTS: dict[ReasonKind, str] = {
    ReasonKind.OFFLINE_MISS: "offline mode skipped an index with no cached listing",
    ReasonKind.UNREADABLE_ONLY: (
        "found on index but no file is a wheel or a .tar.gz sdist"
        " (the formats nab reads)"
    ),
    ReasonKind.ABSENT: "package not found on any configured index",
}

_EMPTY_LEAD = "found on index but no distribution is compatible"
_IN_RANGE_LEAD = (
    "found on index but every version matching the requirement was filtered"
)
_NO_SDIST_TAIL = "no sdist is available to build from"


def walk_listing(provider: Provider, normalized: str) -> ListingDiagnosis | None:
    """Re-walk ``normalized``'s raw listing, recording what refused each file.

    Returns ``None`` when the index served nothing, which leaves the walk
    with nothing to attribute.  The predicates are the filter's own; the
    order it asks them in is re-expressed here, and the differential-oracle
    test is what holds the two in step.  Counter bumps those predicates
    make are taken back by the caller, see
    :meth:`~nab_provider.provider.Provider.diagnose_listing`.
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
    policy: ListingPolicy,
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
            dropped.append(DroppedFile(dist, None, DropCause.INVALID_VERSION))
            continue

        if policy.overridden:
            effective = provider.effective_dist_policy(
                normalized, version, policy.index_name
            )
        else:
            effective = policy.default_dist_policy

        if _listing.excluded_by_dist_policy(dist, effective):
            dropped.append(
                DroppedFile(dist, version, DropCause.DIST_POLICY, effective.value)
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
            dropped.append(DroppedFile(dist, version, DropCause.SDIST_INSTALL_NO_SDIST))
            continue

        if tags is not None and _listing.excluded_by_wheel_tags(
            provider, normalized, version, dist, tags
        ):
            dropped.append(DroppedFile(dist, version, DropCause.WHEEL_TAGS))
            continue

        kept.add(version)

    return dropped, kept


def python_or_time_verdict(
    provider: Provider,
    normalized: str,
    version: Version,
    dist: DistFile,
    policy: ListingPolicy,
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


def _detailed(
    provider: Provider,
    normalized: str,
    version: Version,
    dist: DistFile,
    cause: DropCause,
    policy: ListingPolicy,
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
        return DroppedFile(dist, version, cause, spec)

    if policy.overridden:
        cutoff = provider.effective_uploaded_prior_to(
            normalized, version, policy.index_name
        )
    else:
        cutoff = policy.default_cutoff
    return DroppedFile(dist, version, cause, dist.upload_time, cutoff)


def empty_listing_reason(
    provider: Provider, normalized: str, diagnosis: ListingDiagnosis
) -> str:
    """Say why nothing on ``normalized``'s listing is usable on this target."""
    clauses = _clauses(diagnosis)
    if diagnosis.unexplained:
        clauses.append(_render(None, diagnosis.unexplained))
    if not diagnosis.published_sdist and not any(
        record.cause is DropCause.SDIST_INSTALL_NO_SDIST for record in diagnosis.dropped
    ):
        clauses.append(_NO_SDIST_TAIL)

    lead = f"{_EMPTY_LEAD}: {'; '.join(clauses)}" if clauses else _EMPTY_LEAD
    return lead + _note(provider, normalized, diagnosis, diagnosis.dropped)


def in_range_reason(
    provider: Provider,
    normalized: str,
    version_range: VersionRange,
    diagnosis: ListingDiagnosis,
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
    asked = sorted(
        (record for record in named if record.version in in_range),
        key=lambda record: record.cause.value,
    )

    labels: list[str] = []
    for record in asked:
        if FILTER_LABELS[record.cause] not in labels:
            labels.append(FILTER_LABELS[record.cause])
    if not labels:
        # A drop no rung models leaves no filter to name, but the release
        # is still gone from a listing that published it, so the lead alone
        # says more than the no-match line it would fall back to.
        return _IN_RANGE_LEAD if diagnosis.unexplained else None

    lead = f"{_IN_RANGE_LEAD} (by {_join_labels(labels)})"
    return lead + _note(provider, normalized, diagnosis, asked)


def _join_labels(labels: list[str]) -> str:
    """Join filter labels the way the sentence reads them."""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:  # noqa: PLR2004 - "a or b" against "a, b, or c"
        return f"{labels[0]} or {labels[1]}"
    return f"{', '.join(labels[:-1])}, or {labels[-1]}"


# One template per cause, with the residual drop under ``None``, so every
# sentence a clause can print is in one table.  ``{n}`` is the count and
# ``[singular|plural]`` picks the form it takes; ``{v}`` is the version the
# clause quotes and ``{detail}`` the one value that cause carries.
_CLAUSE_TEMPLATES: dict[DropCause | None, str] = {
    DropCause.UPLOAD_TIME_MISSING: (
        "the uploaded-prior-to cutoff {cutoff} excluded {n} [file that"
        " publishes|files that publish] no upload time ([|newest: ]{v})"
    ),
    DropCause.UPLOAD_TIME_UNPARSEABLE: (
        "the uploaded-prior-to cutoff excluded {n} [file|files] whose upload"
        " time is not ISO 8601 ([|newest: ]{v}, {detail!r})"
    ),
    DropCause.UPLOAD_TIME_NAIVE: (
        "the uploaded-prior-to cutoff could not judge {n} [file|files] whose"
        " upload time carries no timezone ([|newest: ]{v}, {detail!r})"
    ),
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: (
        "the uploaded-prior-to cutoff {cutoff} excluded {n} [file uploaded at"
        " {detail} ({v})|files uploaded on or after it (newest: {v}, uploaded"
        " {detail})]"
    ),
    DropCause.DIST_POLICY: (
        'dist-policy = "{detail}" excluded {n} [{kind} ({v})|{kind}s (newest: {v})]'
    ),
    DropCause.SDIST_INSTALL_NO_SDIST: (
        'dist-policy = "sdist-install" excluded {n} [version that'
        " publishes|versions that publish] no sdist ([|newest: ]{v})"
    ),
    DropCause.REQUIRES_PYTHON: (
        "requires-python excluded {n} [file|files] ([|newest: ]{v} requires"
        " {detail}, the resolve targets Python {py})"
    ),
    DropCause.WHEEL_TAGS: (
        "none of the wheel's tags are compatible with the resolve target"
        " ({n} [wheel|wheels] rejected)"
    ),
    DropCause.INVALID_VERSION: (
        "{n} [file carries|files carry] a version PEP 440 cannot parse"
        " ([|first: ]{raw!r})"
    ),
    # The walk kept a version the filter did not, so no rung names it.
    None: (
        "{n} [version was|versions were] dropped for a reason this report cannot name"
    ),
}

_ALTERNATIVES = r"\[([^|\]]*)\|([^\]]*)\]"


def _render(cause: DropCause | None, count: int, **fields: object) -> str:
    """Fill ``cause``'s template, taking the singular form when ``count`` is 1."""
    template = re.sub(
        _ALTERNATIVES, r"\1" if count == 1 else r"\2", _CLAUSE_TEMPLATES[cause]
    )
    return template.format(n=count, **fields)


# The fields a cause's template states of every file it counts, rather
# than of the one file it quotes.  Records that disagree on one of them
# make two clauses, so no clause asserts a cutoff or a policy of a file
# that was judged by another.
_SHARED_FIELDS: dict[DropCause, tuple[str, ...]] = {
    DropCause.UPLOAD_TIME_MISSING: ("cutoff",),
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: ("cutoff",),
    DropCause.DIST_POLICY: ("detail", "is_wheel"),
}


def _clauses(diagnosis: ListingDiagnosis) -> list[str]:
    """Render one clause per cause that fired, in report order.

    A version-scoped override can give one listing two effective cutoffs
    or two effective dist policies, and each is quoted in a clause of its
    own, in the order the index served the files.  Every other cause has
    one clause, since nothing else its template states varies by file.
    """
    groups: dict[tuple[DropCause, tuple[object, ...]], list[DroppedFile]] = {}
    for record in diagnosis.dropped:
        groups.setdefault((record.cause, _shared_values(record)), []).append(record)
    return [
        _clause(cause, records, diagnosis)
        for (cause, _values), records in sorted(
            groups.items(), key=lambda group: group[0][0].value
        )
    ]


def _shared_values(record: DroppedFile) -> tuple[object, ...]:
    """Return what ``record`` carries for the fields its clause shares."""
    fields = _SHARED_FIELDS.get(record.cause, ())
    return tuple(getattr(record, name) for name in fields)


def _clause(
    cause: DropCause, records: list[DroppedFile], diagnosis: ListingDiagnosis
) -> str:
    """Render one group's clause, quoting the file its evidence comes from.

    The count is files, except the whole-version rung, and the detail comes
    from the highest version the group refused, so the release a user most
    likely wanted is the one named.  An unparseable version has none to
    rank by, so that cause quotes the first file it refused.
    """
    if cause is DropCause.SDIST_INSTALL_NO_SDIST:
        count = len({record.version for record in records})
    else:
        count = len(records)

    quoted = records[0] if cause is DropCause.INVALID_VERSION else _newest(records)
    return _render(
        cause,
        count,
        v=quoted.version,
        raw=quoted.raw_version,
        detail=quoted.detail,
        kind="wheel" if quoted.is_wheel else "sdist",
        cutoff="" if quoted.cutoff is None else quoted.cutoff.isoformat(),
        py=diagnosis.target_python,
    )


def _newest(records: Sequence[DroppedFile]) -> DroppedFile:
    """Return the record carrying the highest version of a group."""
    return max(records, key=_version_of)


def _version_of(record: DroppedFile) -> Version:
    """Return a record's version, which every cause but INVALID_VERSION has."""
    assert record.version is not None
    return record.version


_REMEDIES: dict[CutoffLayer, str] = {
    CutoffLayer.GLOBAL: (
        "the project-level uploaded-prior-to set that cutoff; uploaded-prior-to"
        ' = false under [tool.nab.packages."{package}"] lifts it for this package'
    ),
    CutoffLayer.PACKAGE: (
        "the per-package uploaded-prior-to for {label} set that cutoff; setting"
        " it to false there lifts it"
    ),
    CutoffLayer.INDEX: (
        'the per-index uploaded-prior-to for index "{label}" set that cutoff;'
        " uploaded-prior-to = false under [tool.nab.index.{label}] lifts it"
    ),
}


def _note(
    provider: Provider,
    normalized: str,
    diagnosis: ListingDiagnosis,
    records: Sequence[DroppedFile],
) -> str:
    """Return the ``note:`` continuation naming what lifts the cutoff, or "".

    Offered for the upload-time causes alone, and only for the drops the
    lead it follows describes.  Requires-Python gets none: the per-package
    override replaces the package's declared metadata, so offering it as a
    fix would be telling the user to lie to the resolver.
    """
    refused = [record for record in records if record.cause in UPLOAD_TIME_CAUSES]
    if not refused:
        return ""

    layer, label = provider.uploaded_prior_to_source(
        normalized, _version_of(_newest(refused)), diagnosis.index_name
    )
    return f"\n    note: {_REMEDIES[layer].format(package=normalized, label=label)}"
