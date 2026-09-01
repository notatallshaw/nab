"""Failure-time attribution for the listing filter's drops.

The filter drops a file with a bare ``continue``, so nothing on the resolve
path records which rung refused it.  When a package ends up with no usable
version and the resolve then fails, the provider walks that package's raw
listing a second time through the predicates in
:mod:`nab_provider._provider.listing`, in the order the filter applies them,
and records which one refused each file.  The two sentences the user reads,
the default line and the ``-v`` block, are built here from that record.

The record path builds a marker and nothing else: no listing is walked, no
version parsed and no sentence built until
:meth:`~nab_provider.provider.Provider.get_no_versions_reason` asks for one,
which happens once the resolve has already failed.  The walk partitions the
whole listing, so every file it sees is kept or refused exactly once, and a
version it keeps that the filter did not is counted as unexplained rather
than passed over.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from nab_provider._vendor.packaging.version import Version
from nab_provider.diagnostics import Diagnostic
from nab_provider.records import SdistFile, WheelFile

from ..errors import InvalidUploadTimeError
from ..policy import DistPolicy
from . import listing as _listing
from .listing import DropCause

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from typing import Literal, TypeAlias

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_resolver.types import RangeProtocol

    from ..provider import DistFile, Provider
    from .listing import Cause, ListingPolicy

    # The configuration keys a remedy can change.
    Field: TypeAlias = Literal["uploaded-prior-to", "dist-policy"]

    # The values :class:`OverrideLayer`, :class:`ReasonKind` and
    # :class:`BlockerKind` name.
    Layer: TypeAlias = Literal[
        "global",
        "global-scoped-entry",
        "global-bare-entry",
        "package",
        "index",
    ]
    Kind: TypeAlias = Literal[
        "offline-miss",
        "unreadable-only",
        "unreachable-only",
        "yanked-only",
        "absent",
        "pinned-absent",
        "filtered-empty",
        "blockers",
        "extra-undeclared",
        "extra-metadata",
        "extra-narrowed",
        "extra-base-empty",
        "no-match",
    ]
    Blocked: TypeAlias = Literal["decided", "held", "root"]

    # One clause's worth of refusals, keyed by what refused them.
    _Groups: TypeAlias = "list[tuple[Cause, list[DroppedFile]]]"


# The configuration key each cause belongs to, spelled the way the user
# writes it.  A short line naming two causes names their keys, so the two
# upload-time rungs and the two dist-policy rungs share one entry here.
FILTER_KEYS: dict[Cause, str] = {
    DropCause.UPLOAD_TIME_MISSING: "uploaded-prior-to",
    DropCause.UPLOAD_TIME_UNPARSEABLE: "uploaded-prior-to",
    DropCause.UPLOAD_TIME_NAIVE: "uploaded-prior-to",
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: "uploaded-prior-to",
    DropCause.DIST_POLICY: "dist-policy",
    DropCause.SDIST_INSTALL_NO_SDIST: "dist-policy",
    DropCause.REQUIRES_PYTHON: "requires-python",
    # No configuration turns the tag pass on, so this is a description
    # rather than a key.  It appears only where it shares a line with one.
    DropCause.WHEEL_TAGS: "wheel tags",
    DropCause.INVALID_VERSION: "unparseable versions",
}

UPLOAD_TIME_CAUSES = frozenset(
    {
        DropCause.UPLOAD_TIME_MISSING,
        DropCause.UPLOAD_TIME_UNPARSEABLE,
        DropCause.UPLOAD_TIME_NAIVE,
        DropCause.UPLOAD_TIME_AFTER_CUTOFF,
    }
)

DIST_POLICY_CAUSES = frozenset(
    {DropCause.DIST_POLICY, DropCause.SDIST_INSTALL_NO_SDIST}
)


class OverrideLayer:
    """Which config layer set the policy field a candidate was judged by.

    The project level splits in three because the remedy does.  A package
    that already sets the field over some other version range cannot be
    given a second, bare-name entry, which the config layer refuses as two
    entries setting one field over overlapping ranges.  A package that
    already has a bare-name entry setting some other field cannot be given
    one either, since the table holding it is declared and TOML refuses a
    second declaration of the same table.
    """

    GLOBAL: Final = "global"
    GLOBAL_SCOPED_ENTRY: Final = "global-scoped-entry"
    GLOBAL_BARE_ENTRY: Final = "global-bare-entry"
    PACKAGE: Final = "package"
    INDEX: Final = "index"


class ReasonKind:
    """Which situation left ``choose_version`` with no version to return."""

    OFFLINE_MISS: Final = "offline-miss"
    UNREADABLE_ONLY: Final = "unreadable-only"
    UNREACHABLE_ONLY: Final = "unreachable-only"
    NONE_USABLE: Final = "none-usable"
    YANKED_ONLY: Final = "yanked-only"
    ABSENT: Final = "absent"
    PINNED_ABSENT: Final = "pinned-absent"
    FILTERED_EMPTY: Final = "filtered-empty"
    BLOCKERS: Final = "blockers"
    EXTRA_UNDECLARED: Final = "extra-undeclared"
    EXTRA_METADATA: Final = "extra-metadata"
    EXTRA_NARROWED: Final = "extra-narrowed"
    EXTRA_BASE_EMPTY: Final = "extra-base-empty"
    NO_MATCH: Final = "no-match"


class BlockerKind:
    """What the look-ahead found holding a dependency away from the range."""

    DECIDED: Final = "decided"
    HELD: Final = "held"
    ROOT: Final = "root"


# The value types below are hand-written rather than dataclasses or named
# tuples, and the constants above are strings rather than Enum members, for
# one reason: every nab invocation imports this module, and each of those
# declarations costs several times what the plain form does.


class Remedy:
    """The setting one config entry made, and what changing it would take.

    ``label`` names the entry the way the config file has it, so a line
    asking for a change to an entry that exists can point the reader
    straight at it.  ``selector`` is the requirement itself, which a line
    writing a setting that does not exist yet keys it by: a config path is
    not a package selector.  ``covers`` is how many packages the entry
    matches, so a note sending the reader to a rule can say that changing
    it moves more than the package being reported.
    """

    __slots__ = ("covers", "field", "label", "layer", "selector")

    def __init__(
        self,
        field: Field,
        layer: Layer,
        label: str,
        selector: str,
        covers: int = 1,
    ) -> None:
        """Record ``layer`` as the layer that set ``field`` for a candidate."""
        self.field = field
        self.layer = layer
        self.label = label
        self.selector = selector
        self.covers = covers

    def identity(self) -> tuple[str, str, str, str]:
        """Return what makes two remedies the same change."""
        return (self.field, self.layer, self.label, self.selector)


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
        cause: Cause,
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
        versions in it the filter did not keep, so a drop no rung models is
        reported rather than read as a file nothing refused.  That is one
        direction only: a file the walk refuses and the filter kept counts
        zero here, and the differential-oracle test is what covers the
        other way round.
        """
        self.index_name = index_name
        self.dropped = dropped
        self.kept = kept
        self.published_sdist = published_sdist
        self.unexplained = unexplained
        self.target_python = target_python


class Blocker:
    """One dependency the look-ahead found no candidate could satisfy.

    Neither side is rendered here.  A scan that exhausts its candidates is
    ordinary backtracking, so nothing reads them unless the resolve then
    fails, and the report spells them when it is built.
    """

    __slots__ = ("declared", "held", "kind", "package")

    def __init__(
        self,
        kind: Blocked,
        package: str,
        declared: tuple[RangeProtocol[Version], ...],
        held: Version | RangeProtocol[Version],
    ) -> None:
        """Record that ``package`` is wanted in ``declared`` but stands at ``held``.

        ``held`` is the version a decided blocker was pinned to, and the range
        a held or root blocker stands in.
        """
        self.kind = kind
        self.package = package
        self.declared = declared
        self.held = held


class MetadataBlock:
    """One version whose metadata no rung of the ladder could read.

    ``message`` is the failure as every other caller of the raising code
    reads it.  ``filtered_sdist_version`` is the ladder's marker for a
    failure the report can say more about: the walk can name which rung
    took that version's sdist.
    """

    __slots__ = ("filtered_sdist_version", "message")

    def __init__(
        self, message: str, filtered_sdist_version: Version | None = None
    ) -> None:
        """Record ``message`` as this version's failure."""
        self.message = message
        self.filtered_sdist_version = filtered_sdist_version


class NoVersionsReason:
    """What the resolve recorded when a package ran out of candidates."""

    __slots__ = ("blockers", "declaring_version", "kind", "metadata", "version_range")

    def __init__(
        self,
        kind: Kind,
        blockers: tuple[Blocker, ...] = (),
        metadata: tuple[MetadataBlock, ...] = (),
        version_range: VersionRange | None = None,
        declaring_version: Version | None = None,
    ) -> None:
        """Mark ``kind`` as the situation, with what the failure cannot re-derive.

        Recorded during the resolve and rendered only if the resolve then
        fails, so it holds what the render can no longer read for itself: the
        look-ahead rejections and metadata failures, which reset at the next
        scan, the range that was asked, and the version an extras proxy was
        narrowed off.  Everything else the render walks for.
        """
        self.kind = kind
        self.blockers = blockers
        self.metadata = metadata
        self.version_range = version_range
        self.declaring_version = declaring_version

    @property
    def is_generic(self) -> bool:
        """Whether this reason says only that nothing matched."""
        return self.kind == ReasonKind.NO_MATCH


# The situations that carry nothing beyond their kind, built once so the
# record path allocates nothing for them.
OFFLINE_MISS = NoVersionsReason(ReasonKind.OFFLINE_MISS)
UNREADABLE_ONLY = NoVersionsReason(ReasonKind.UNREADABLE_ONLY)
UNREACHABLE_ONLY = NoVersionsReason(ReasonKind.UNREACHABLE_ONLY)
NONE_USABLE = NoVersionsReason(ReasonKind.NONE_USABLE)
YANKED_ONLY = NoVersionsReason(ReasonKind.YANKED_ONLY)
ABSENT = NoVersionsReason(ReasonKind.ABSENT)
PINNED_ABSENT = NoVersionsReason(ReasonKind.PINNED_ABSENT)
FILTERED_EMPTY = NoVersionsReason(ReasonKind.FILTERED_EMPTY)
EXTRA_BASE_EMPTY = NoVersionsReason(ReasonKind.EXTRA_BASE_EMPTY)


NO_MATCH = Diagnostic("no version matches the requirement")

# The six situations the index client answers on its own with a fixed line.
# None of them is a walk, so only the two with something to add carry a ``-v``
# body: for the others, saying the same fact at more length is not detail.
FIXED_DIAGNOSTICS: dict[Kind, Diagnostic] = {
    ReasonKind.OFFLINE_MISS: Diagnostic(
        "offline mode skipped an index with no cached listing",
        (
            (
                "note: --no-offline turns offline mode off for this run"
                " wherever it was set: a nab.toml, NAB_OFFLINE, or this"
                " command line"
            ),
        ),
        "re-run with --no-offline",
    ),
    ReasonKind.UNREADABLE_ONLY: Diagnostic(
        "no file the index served is one nab can read",
        ("nab reads wheels and .tar.gz sdists whose name and version parse",),
    ),
    ReasonKind.UNREACHABLE_ONLY: Diagnostic(
        "the index lists this package but nab cannot reach any of its links"
    ),
    ReasonKind.NONE_USABLE: Diagnostic(
        "the index lists this package but nab can use none of the files it names"
    ),
    ReasonKind.YANKED_ONLY: Diagnostic(
        "the index lists this package but every file is yanked"
    ),
    ReasonKind.ABSENT: Diagnostic("package not found on any configured index"),
}


def pinned_index_diagnostic(index_name: str) -> Diagnostic:
    """Say that the one index the package is routed to does not carry it.

    Missing from a pinned index is not missing from the configured set: the
    route is why no other index was asked.  Built rather than fixed, since
    the line names the index.
    """
    return Diagnostic(
        f"not found on index {index_name!r}, the only index this package is routed to"
    )


_NO_SDIST_TAIL = "the files nab read hold no sdist to build from"


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
    dropped, survivors, sdist_install = _shared_base_pass(
        provider, normalized, files, policy
    )
    tag_dropped, kept = _tag_pass(provider, survivors, sdist_install)

    filtered = provider.versions_cache.get(normalized) or []
    return ListingDiagnosis(
        index_name=policy.index_name,
        dropped=(*dropped, *tag_dropped),
        kept=frozenset(kept),
        published_sdist=any(isinstance(dist, SdistFile) for dist in files),
        unexplained=len(kept - {version for version, _ in filtered}),
        target_python=(
            None if provider.target is None else provider.target.python_version
        ),
    )


def _shared_base_pass(
    provider: Provider,
    normalized: str,
    files: Sequence[WheelFile | SdistFile],
    policy: ListingPolicy,
) -> tuple[list[DroppedFile], list[tuple[Version, DistFile]], set[Version]]:
    """Run the base pass through the memo the listing filter's own base pass uses.

    The rungs before the tag pass read the listing, the policy config and
    the target Python and nothing else, which is the key
    :meth:`~nab_provider.provider.ListingFilterCache.filtered` already
    shares the filter's base pass under.  A matrix whose tuples differ only
    by platform therefore walks each listing once per Python rather than
    once per failing tuple.  The result is held by every target that shares
    the memo, so callers must not mutate it.
    """
    cache = provider.listing_filter_cache
    if cache is None:
        return _base_pass(provider, normalized, files, policy)

    return cache.diagnosed(
        normalized,
        provider.python_version,
        lambda: _base_pass(provider, normalized, files, policy),
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
    survivors: Sequence[tuple[Version, DistFile]],
    sdist_install: set[Version],
) -> tuple[list[DroppedFile], set[Version]]:
    """Partition the base pass's survivors by the two whole-target rungs."""
    no_sdist = _listing.sdist_install_wheel_only(list(survivors), sdist_install)
    tags = provider.wheel_tags

    dropped: list[DroppedFile] = []
    kept: set[Version] = set()
    for version, dist in survivors:
        if version in no_sdist:
            dropped.append(
                DroppedFile(
                    dist,
                    version,
                    DropCause.SDIST_INSTALL_NO_SDIST,
                    DistPolicy.SDIST_INSTALL.value,
                )
            )
            continue

        if tags is not None and _listing.excluded_by_wheel_tags(dist, tags):
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
) -> Cause | None:
    """Answer the filter's own question without raising on a naive stamp.

    The walk runs while a failure is being rendered, where the
    :class:`~nab_provider.errors.InvalidUploadTimeError` the filter raises
    would lose the report, so it comes back as the cause it was raised for.
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
    cause: Cause,
    policy: ListingPolicy,
) -> DroppedFile:
    """Record a Requires-Python or upload-time refusal with the value it quotes.

    Re-reads the effective value through the same provider methods
    :func:`~nab_provider._provider.listing.python_or_time_cause` read it
    through, so the clause quotes what the filter applied.
    """
    if cause == DropCause.REQUIRES_PYTHON:
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


# One short line per cause, for the listing the filter emptied.  Read only
# where every group says the same one, so each states the whole of what went
# wrong.  A why-clause is here only where the key does not carry it: an
# excluding uploaded-prior-to reads as a cutoff nothing was old enough for
# unless the line says otherwise, and the dist-policy value says which half
# of the listing that key kept.  The two rungs this report never offers a
# remedy for name the target they judged against instead, which is the half
# a reader can move.
_SHORT_EMPTY: dict[Cause, str] = {
    DropCause.UPLOAD_TIME_MISSING: (
        "uploaded-prior-to excluded every file; none is dated"
    ),
    DropCause.UPLOAD_TIME_UNPARSEABLE: (
        "uploaded-prior-to excluded every file; no date is readable"
    ),
    DropCause.UPLOAD_TIME_NAIVE: (
        "uploaded-prior-to excluded every file; no date has a timezone"
    ),
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: "uploaded-prior-to excluded every file",
    DropCause.DIST_POLICY: "{subject} excluded every file",
    DropCause.SDIST_INSTALL_NO_SDIST: "{subject} excluded every version",
    DropCause.REQUIRES_PYTHON: "no file supports Python {py}",
    DropCause.WHEEL_TAGS: "no wheel matches this platform or Python",
    DropCause.INVALID_VERSION: "every file carries a version PEP 440 cannot parse",
}

# The line for a requirement whose matching releases were the ones refused,
# where saying so is not the generic "<subject> excluded every version in
# range": a line naming the target has to carry the range somewhere else.
_SHORT_IN_RANGE: dict[Cause, str] = {
    DropCause.REQUIRES_PYTHON: "no version in range supports Python {py}",
    DropCause.WHEEL_TAGS: "no wheel in range matches this platform or Python",
}

_SHORT_UNNAMED = "every file was refused; the filter cannot be named"
_SHORT_UNNAMED_IN_RANGE = (
    "every version in range was refused; the filter cannot be named"
)


def empty_listing_diagnostic(
    provider: Provider, normalized: str, diagnosis: ListingDiagnosis
) -> Diagnostic:
    """Say why nothing on ``normalized``'s listing is usable on this target."""
    groups = _groups(diagnosis.dropped)
    clauses = [_clause(cause, records, diagnosis) for cause, records in groups]
    if diagnosis.unexplained:
        clauses.append(_render(None, diagnosis.unexplained))

    if not diagnosis.published_sdist and not any(
        record.cause == DropCause.SDIST_INSTALL_NO_SDIST for record in diagnosis.dropped
    ):
        clauses.append(_NO_SDIST_TAIL)

    remedies = _remedies(provider, normalized, diagnosis, groups)
    return Diagnostic(
        _empty_short(groups, diagnosis),
        (*clauses, *_note_lines(remedies)),
        _try_line(remedies),
    )


def in_range_diagnostic(
    provider: Provider,
    normalized: str,
    version_range: VersionRange,
    diagnosis: ListingDiagnosis,
) -> Diagnostic | None:
    """Say which filters dropped the releases matching ``version_range``.

    Returns ``None`` when the walk explains every drop and none of them
    falls inside the range, which is the caller's signal that the
    requirement asks for a version the index never published.  A refused
    version equal to one in ``kept`` survived under another spelling and
    does not count.
    """
    named = [
        record
        for record in diagnosis.dropped
        if record.version is not None and record.version not in diagnosis.kept
    ]
    in_range = set(version_range.filter({record.version for record in named}))
    asked = [record for record in named if record.version in in_range]

    groups = _groups(asked)
    if not groups:
        # A drop no rung models leaves no filter to name, but the release is
        # still gone from a listing that published it, so saying so beats
        # the no-match line the caller falls back to.
        return Diagnostic(_SHORT_UNNAMED_IN_RANGE) if diagnosis.unexplained else None

    remedies = _remedies(provider, normalized, diagnosis, groups)
    clauses = [_clause(cause, records, diagnosis) for cause, records in groups]
    return Diagnostic(
        _in_range_short(groups, diagnosis),
        (*clauses, *_note_lines(remedies)),
        _try_line(remedies),
    )


def _empty_short(groups: _Groups, diagnosis: ListingDiagnosis) -> str:
    """Return the one line an emptied listing gets at default verbosity.

    Groups that would say the same sentence say it once: one key judging a
    package under two entries is still one key, and naming it twice would
    imply two filters fired.
    """
    if not groups:
        return _SHORT_UNNAMED
    sentences = {
        _single_cause_short(cause, records[0], diagnosis) for cause, records in groups
    }
    if len(sentences) == 1 and not diagnosis.unexplained:
        return sentences.pop()
    return f"{_join_keys(groups)} excluded every file"


def _single_cause_short(
    cause: Cause, record: DroppedFile, diagnosis: ListingDiagnosis
) -> str:
    """Return the line for a listing one rung emptied on its own."""
    return _SHORT_EMPTY[cause].format(
        subject=_subject(cause, record), py=diagnosis.target_python
    )


def _in_range_short(groups: _Groups, diagnosis: ListingDiagnosis) -> str:
    """Return the one line a filtered-out requirement gets at default verbosity."""
    sentences = {
        _single_cause_in_range(cause, records[0], diagnosis)
        for cause, records in groups
    }
    if len(sentences) == 1:
        return sentences.pop()
    return f"{_join_keys(groups)} excluded every version in range"


def _single_cause_in_range(
    cause: Cause, record: DroppedFile, diagnosis: ListingDiagnosis
) -> str:
    """Return the line for a range one rung emptied on its own."""
    template = _SHORT_IN_RANGE.get(cause)
    if template is None:
        return f"{_subject(cause, record)} excluded every version in range"
    return template.format(py=diagnosis.target_python)


def _subject(cause: Cause, record: DroppedFile) -> str:
    """Name the key that refused, carrying the value where the user set one."""
    value = _subject_value(cause, record)
    key = FILTER_KEYS[cause]
    return key if value is None else f"{key} = {value}"


def _subject_value(cause: Cause, record: DroppedFile) -> str | None:
    """Return the value a key carries into a line, where it carries one.

    ``sdist-only`` and ``sdist-install`` fail for different reasons, so a
    dist-policy line without its value leaves the reader guessing which of
    the two they wrote.  No other key has a value worth the room.
    """
    if cause in DIST_POLICY_CAUSES:
        return f'"{record.detail}"'
    return None


def _join_keys(groups: _Groups) -> str:
    """Name the config keys the groups fired, or count them past :data:`_MOST_NAMED`.

    Report order, without repeats.  One key is spelled with every value
    its entries set, since two version-scoped entries can judge one
    listing under two dist policies and the key alone would drop both.
    Several keys are spelled bare: there the line is a summary of what
    fired, and ``-v`` carries the values one to a clause.
    """
    values: dict[str, list[str]] = {}
    for cause, records in groups:
        seen = values.setdefault(FILTER_KEYS[cause], [])
        value = _subject_value(cause, records[0])
        if value is not None and value not in seen:
            seen.append(value)

    if len(values) == 1:
        key, seen = next(iter(values.items()))
        return _key_with_values(key, seen)
    return _named_or_counted(list(values), "filters")


def _key_with_values(key: str, values: Sequence[str]) -> str:
    """Spell one config key with whatever values its entries set it to."""
    if not values:
        return key
    return f"{key} = {_join_names(values)}"


# How many things a short line names before it counts them instead.
_MOST_NAMED: Final = 3


def _named_or_counted(names: Sequence[str], noun: str) -> str:
    """Name the things a line is about, or count them past :data:`_MOST_NAMED`.

    A line that lists what a resolve found grows with the project it ran
    on and stops being one glance's worth of reading, so past three names
    it says how many.  ``-v`` names them all, one to a clause.
    """
    if len(names) > _MOST_NAMED:
        return f"{len(names)} {noun}"
    return _join_names(names)


def _join_names(names: Sequence[str]) -> str:
    """Join names as prose, with ``and`` before the last."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


# One template per cause, with the residual drop under ``None``, so every
# sentence a clause can print is in one table.  ``{n}`` is the count and
# ``[singular|plural]`` picks the form it takes; ``{v}`` is the version the
# clause quotes and ``{detail}`` the one value that cause carries.
_CLAUSE_TEMPLATES: dict[Cause | None, str] = {
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
        'dist-policy = "{detail}" excluded {n} [version that'
        " publishes|versions that publish] no sdist ([|newest: ]{v})"
    ),
    DropCause.REQUIRES_PYTHON: (
        "requires-python excluded {n} [file|files] ([|newest: ]{v} requires"
        " {detail}, the resolve targets Python {py})"
    ),
    DropCause.WHEEL_TAGS: (
        "no wheel's tags are compatible with the resolve target"
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


def _render(cause: Cause | None, count: int, **fields: object) -> str:
    """Fill ``cause``'s template, taking the singular form when ``count`` is 1."""
    template = re.sub(
        _ALTERNATIVES, r"\1" if count == 1 else r"\2", _CLAUSE_TEMPLATES[cause]
    )
    return template.format(n=count, **fields)


# The fields a cause's template states of every file it counts, rather
# than of the one file it quotes.  Records that disagree on one of them
# make two clauses, so no clause asserts a cutoff or a policy of a file
# that was judged by another.
_SHARED_FIELDS: dict[Cause, tuple[str, ...]] = {
    DropCause.UPLOAD_TIME_MISSING: ("cutoff",),
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: ("cutoff",),
    DropCause.DIST_POLICY: ("detail", "is_wheel"),
}


def _groups(records: Sequence[DroppedFile]) -> _Groups:
    """Group ``records`` into one clause each, in report order.

    A version-scoped override can give one listing two effective cutoffs
    or two effective dist policies, and each is quoted in a clause of its
    own.  Every other cause has one clause, since nothing else its
    template states varies by file.
    """
    grouped: dict[tuple[Cause, tuple[object, ...]], list[DroppedFile]] = {}
    for record in records:
        grouped.setdefault((record.cause, _shared_values(record)), []).append(record)

    return [
        (cause, group)
        for (cause, _values), group in sorted(
            grouped.items(), key=lambda item: DropCause.REPORT_ORDER.index(item[0][0])
        )
    ]


def _shared_values(record: DroppedFile) -> tuple[object, ...]:
    """Return what ``record`` carries for the fields its clause shares."""
    fields = _SHARED_FIELDS.get(record.cause, ())
    return tuple(getattr(record, name) for name in fields)


def _clause(
    cause: Cause, records: list[DroppedFile], diagnosis: ListingDiagnosis
) -> str:
    """Render one group's clause, quoting the file its evidence comes from.

    The count is files, except the whole-version rung, and the detail comes
    from the highest version the group refused, so the release a user most
    likely wanted is the one named.  An unparseable version has none to
    rank by, so that cause quotes the first file it refused.
    """
    if cause == DropCause.SDIST_INSTALL_NO_SDIST:
        count = len({record.version for record in records})
    else:
        count = len(records)

    quoted = records[0] if cause == DropCause.INVALID_VERSION else _newest(records)
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


# A remedy names a setting rather than the table that holds it: the same key
# is spelled under [tool.nab] in pyproject.toml and at the top level of a
# nab.toml.  Keyed by the field the entry set and the layer that set it,
# since the same key on two layers takes two different changes.
_REMEDIES: dict[tuple[Field, Layer], str] = {
    ("uploaded-prior-to", OverrideLayer.GLOBAL): (
        "the project-level uploaded-prior-to set that cutoff; setting"
        ' packages."{selector}".uploaded-prior-to = false lifts it for this package'
    ),
    ("uploaded-prior-to", OverrideLayer.GLOBAL_SCOPED_ENTRY): (
        "the project-level uploaded-prior-to set that cutoff; {label}{covers}"
        " already sets uploaded-prior-to over another version range, so widen"
        " that entry over this version or drop the project-level cutoff"
    ),
    ("uploaded-prior-to", OverrideLayer.GLOBAL_BARE_ENTRY): (
        "the project-level uploaded-prior-to set that cutoff; {label} already"
        " exists, so adding uploaded-prior-to = false there lifts it for this"
        " package"
    ),
    ("uploaded-prior-to", OverrideLayer.PACKAGE): (
        "the uploaded-prior-to on {label}{covers} set that cutoff; setting it"
        " to false there lifts it"
    ),
    ("uploaded-prior-to", OverrideLayer.INDEX): (
        "the per-index uploaded-prior-to for index {key} set that cutoff;"
        " setting it to false there lifts it"
    ),
    ("dist-policy", OverrideLayer.GLOBAL): (
        "the project-level dist-policy set that policy; setting"
        ' packages."{selector}".dist-policy = "wheel-or-sdist" admits both'
        " formats for this package"
    ),
    ("dist-policy", OverrideLayer.GLOBAL_SCOPED_ENTRY): (
        "the project-level dist-policy set that policy; {label}{covers} already"
        " sets dist-policy over another version range, so widen that entry over"
        " this version or drop the project-level policy"
    ),
    ("dist-policy", OverrideLayer.GLOBAL_BARE_ENTRY): (
        "the project-level dist-policy set that policy; {label} already exists,"
        ' so adding dist-policy = "wheel-or-sdist" there admits both formats for'
        " this package"
    ),
    ("dist-policy", OverrideLayer.PACKAGE): (
        "the dist-policy on {label}{covers} set that policy; setting it to"
        ' "wheel-or-sdist" there admits both formats'
    ),
    ("dist-policy", OverrideLayer.INDEX): (
        "the per-index dist-policy for index {key} set that policy; setting it"
        ' to "wheel-or-sdist" there admits both formats'
    ),
}

# The instruction cut out of each remedy, for the one ``try:`` line the
# default report prints.  It names a setting to change rather than a
# fragment to paste, since the table holding that setting usually exists
# already and a second one is a TOML error.  Three layers name an entry
# the file already holds.  The per-package and scoped-entry layers do it
# because the same override is written on two surfaces, only one of them
# spelled ``packages."<selector>"``, and a ``[[package-rules]]`` entry can
# match several packages, so naming the one being reported would send the
# reader to change the others too.  The bare-name layer does it because
# the table its key path would write is that entry.  It states what to
# set and not what follows: lifting a filter admits files rather than
# promising a resolve.
_TRY_LINES: dict[tuple[Field, Layer], str] = {
    ("uploaded-prior-to", OverrideLayer.GLOBAL): (
        'set packages."{selector}".uploaded-prior-to = false'
    ),
    ("uploaded-prior-to", OverrideLayer.GLOBAL_SCOPED_ENTRY): (
        "widen {label} over this version, or drop the project cutoff"
    ),
    ("uploaded-prior-to", OverrideLayer.GLOBAL_BARE_ENTRY): (
        "add uploaded-prior-to = false to {label}"
    ),
    ("uploaded-prior-to", OverrideLayer.PACKAGE): (
        "set uploaded-prior-to = false on {label}"
    ),
    ("uploaded-prior-to", OverrideLayer.INDEX): (
        "set index.{key}.uploaded-prior-to = false"
    ),
    ("dist-policy", OverrideLayer.GLOBAL): (
        'set packages."{selector}".dist-policy = "wheel-or-sdist"'
    ),
    ("dist-policy", OverrideLayer.GLOBAL_SCOPED_ENTRY): (
        "widen {label} over this version, or drop the project dist-policy"
    ),
    ("dist-policy", OverrideLayer.GLOBAL_BARE_ENTRY): (
        'add dist-policy = "wheel-or-sdist" to {label}'
    ),
    ("dist-policy", OverrideLayer.PACKAGE): (
        'set dist-policy = "wheel-or-sdist" on {label}'
    ),
    ("dist-policy", OverrideLayer.INDEX): (
        'set index.{key}.dist-policy = "wheel-or-sdist"'
    ),
}

# The field a cause's remedy changes, and the record attribute that tells
# one entry setting it from another: two version-scoped entries can give
# one listing two cutoffs or two policies, and each is answered separately.
_REMEDY_FIELDS: dict[Cause, tuple[Field, str]] = {
    DropCause.UPLOAD_TIME_MISSING: ("uploaded-prior-to", "cutoff"),
    DropCause.UPLOAD_TIME_UNPARSEABLE: ("uploaded-prior-to", "cutoff"),
    DropCause.UPLOAD_TIME_NAIVE: ("uploaded-prior-to", "cutoff"),
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: ("uploaded-prior-to", "cutoff"),
    DropCause.DIST_POLICY: ("dist-policy", "detail"),
    DropCause.SDIST_INSTALL_NO_SDIST: ("dist-policy", "detail"),
}


def _remedies(
    provider: Provider,
    normalized: str,
    diagnosis: ListingDiagnosis,
    groups: _Groups,
) -> list[Remedy]:
    """Return the config entries a remedy would change, in report order.

    Empty where nothing a config key set did the refusing: Requires-Python
    is left out on purpose, since the override that lifts it replaces the
    package's declared metadata, and the wheel-tag pass answers to no key
    at all.
    """
    found: dict[tuple[str, str, str, str], Remedy] = {}
    for cause, records in groups:
        setting = _REMEDY_FIELDS.get(cause)
        if setting is None:
            continue
        field, attribute = setting
        for group in _by_attribute(records, attribute):
            remedy = provider.override_source(
                normalized,
                _version_of(_newest(group)),
                diagnosis.index_name,
                field=field,
            )
            found.setdefault(remedy.identity(), remedy)
    return list(found.values())


def _by_attribute(
    records: Sequence[DroppedFile], attribute: str
) -> list[list[DroppedFile]]:
    """Split ``records`` by the value of ``attribute``, keeping walk order."""
    split: dict[object, list[DroppedFile]] = {}
    for record in records:
        split.setdefault(getattr(record, attribute), []).append(record)
    return list(split.values())


def _note_lines(remedies: Sequence[Remedy]) -> tuple[str, ...]:
    """Return one ``note:`` line per entry, saying what changing it would take."""
    return tuple(
        "note: " + _fill(_REMEDIES[remedy.field, remedy.layer], remedy)
        for remedy in remedies
    )


def _try_line(remedies: Sequence[Remedy]) -> str | None:
    """Return the instruction the ``try:`` line states, or ``None``.

    Where several rungs fired, the first in report order that has a remedy
    answers: lifting it is what admits files again, and the report cannot
    promise that the next rung then keeps them.
    """
    if not remedies:
        return None
    remedy = remedies[0]
    return _fill(_TRY_LINES[remedy.field, remedy.layer], remedy)


def _fill(template: str, remedy: Remedy) -> str:
    """Fill one remedy template with the entry it is about."""
    return template.format(
        label=remedy.label,
        selector=remedy.selector,
        key=_toml_key(remedy.selector),
        covers=_covers_clause(remedy.covers),
    )


def _covers_clause(packages: int) -> str:
    """Say how wide an entry is, where it is wider than the package reported.

    A ``[[package-rules]]`` entry carries a ``match`` list, so changing it
    changes the setting for every package on that list.
    """
    if packages == 1:
        return ""
    return f", which matches {packages} packages,"


def _toml_key(name: str) -> str:
    """Quote ``name`` as a TOML key, in whichever form reads back as ``name``.

    An index is named by whatever the config called it, and nab accepts
    either quote in that name, which would otherwise close the key the
    remedy is writing it into.  A literal string takes a double quote and a
    basic string takes the rest, escaping where the name holds both.
    """
    if '"' not in name:
        return f'"{name}"'
    if "'" not in name:
        return f"'{name}'"
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# The single-file clause for a rung that took one named artifact, which the
# metadata ladder needs: there the file, not the count, is the evidence.
_FILE_CLAUSES: dict[Cause, str] = {
    DropCause.UPLOAD_TIME_MISSING: (
        "the uploaded-prior-to cutoff {cutoff} excluded {filename},"
        " which publishes no upload time"
    ),
    DropCause.UPLOAD_TIME_UNPARSEABLE: (
        "the uploaded-prior-to cutoff excluded {filename}, whose upload time"
        " {detail!r} is not ISO 8601"
    ),
    DropCause.UPLOAD_TIME_NAIVE: (
        "the uploaded-prior-to cutoff could not judge {filename}, whose upload"
        " time {detail!r} carries no timezone"
    ),
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: (
        "the uploaded-prior-to cutoff {cutoff} excluded {filename},"
        " uploaded at {detail}"
    ),
    DropCause.DIST_POLICY: 'dist-policy = "{detail}" excluded {filename}',
    DropCause.REQUIRES_PYTHON: (
        "requires-python excluded {filename} (requires {detail}, the resolve"
        " targets Python {py})"
    ),
}


def filtered_sdist_diagnostic(
    provider: Provider,
    normalized: str,
    version: Version,
    diagnosis: ListingDiagnosis,
) -> Diagnostic | None:
    """Name the rung that took ``version``'s sdist, for the metadata ladder.

    Returns the report entry for the package, or ``None`` when no rung this
    report can name refused that sdist, which leaves the ladder's own
    untargeted sentence standing.
    """
    refused = [
        record
        for record in diagnosis.dropped
        if record.version == version
        and not record.is_wheel
        and record.cause in _FILE_CLAUSES
    ]
    if not refused:
        return None

    record = refused[0]
    remedies = _remedies(provider, normalized, diagnosis, [(record.cause, [record])])
    return Diagnostic(
        f"{_subject(record.cause, record)} excluded the sdist nab needed for metadata",
        (
            f"{normalized} {version} has no PEP 658 metadata on the index",
            _FILE_CLAUSES[record.cause].format(
                filename=record.filename,
                detail=record.detail,
                cutoff="" if record.cutoff is None else record.cutoff.isoformat(),
                py=diagnosis.target_python,
            ),
            *_note_lines(remedies),
        ),
        _try_line(remedies),
    )


# One clause per kind, in the user's words rather than the resolver's:
# ``root`` is what the solver calls the project, and a reader never wrote it.
# One rejection puts its clause on the line; several put one clause each
# behind ``-v``, so both depths say the same thing.
_BLOCKER_CLAUSES: dict[Blocked, str] = {
    BlockerKind.DECIDED: (
        "needs {package} in {declared}, but the resolve chose {package} {held}"
    ),
    BlockerKind.HELD: (
        "needs {package} in {declared}, but the resolve holds {package} in {held}"
    ),
    BlockerKind.ROOT: (
        "needs {package} in {declared}, but your project requires {package} {held}"
    ),
}

# A block is any metadata failure the ladder ended on, which is wider than
# metadata nothing could read: a version whose METADATA rules the target out
# is rejected on metadata that read perfectly well.
_METADATA_REJECTED = "every version in range was rejected on its metadata"


def blockers_diagnostic(
    provider: Provider,
    normalized: str,
    blockers: Sequence[Blocker],
    metadata: Sequence[MetadataBlock],
) -> Diagnostic:
    """Say what the look-ahead found rejecting every candidate in range.

    Blocker lines say "in range" because the scan saw only what the asked
    range admits: a version outside it may declare none of these
    dependencies.

    One rejection states its ranges on the line, and gets no ``-v`` body:
    the detail would be that same pair of ranges again.  Several name their
    packages and leave the ranges to ``-v``, since two pairs do not fit.
    ``provider`` spells every range, and ``normalized`` is read only where
    the one rejection is a metadata failure the walk can say more about.
    """
    if len(blockers) + bool(metadata) == 1:
        if not blockers:
            return metadata_diagnostic(provider, normalized, metadata)
        return Diagnostic(
            f"every version in range {_blocker_clause(provider, blockers[0])}"
        )

    detail = [_blocker_clause(provider, blocker) for blocker in blockers]
    detail.extend(block.message for block in metadata)
    return Diagnostic(_several_blockers_short(blockers, metadata), tuple(detail))


def _blocker_clause(provider: Provider, blocker: Blocker) -> str:
    """Say what one rejection wanted and what stands in its way."""
    declared, held = _render_blocker(provider, blocker)
    return _BLOCKER_CLAUSES[blocker.kind].format(
        package=blocker.package, declared=declared, held=held
    )


def _render_blocker(provider: Provider, blocker: Blocker) -> tuple[str, str]:
    """Spell one look-ahead rejection's two sides for the failure report.

    Returns what the rejected candidates asked of the blocker, and where the
    blocker stands: a decided one at a version, a held or root one over a
    range.
    """
    declared = " or ".join(_blocker_side(provider, part) for part in blocker.declared)
    held = blocker.held
    if isinstance(held, Version):
        return declared, str(held)
    return declared, _blocker_side(provider, held)


def _blocker_side(provider: Provider, constraint: RangeProtocol[Version]) -> str:
    """Render one side of a blocker clause, naming an unconstrained range.

    :meth:`~nab_provider.provider.Provider.format_range` spells an
    unconstrained range as nothing, which would end the clause on a
    dangling ``in``.
    """
    return provider.format_range(constraint) or "any version"


def _several_blockers_short(
    blockers: Sequence[Blocker], metadata: Sequence[MetadataBlock]
) -> str:
    """Name the packages holding every candidate out, without their ranges.

    Called only where more than one thing rejected the candidates, so at
    least one dependency is named however the metadata failures fall.
    """
    names = _named_or_counted(
        list(dict.fromkeys(blocker.package for blocker in blockers)), "packages"
    )
    if not metadata:
        return f"every version in range is blocked by {names}"
    return f"every version in range is blocked by {names} or rejected on its metadata"


def metadata_diagnostic(
    provider: Provider, normalized: str, blocks: Sequence[MetadataBlock]
) -> Diagnostic:
    """Say that no version's metadata could be read, at the depth it is known.

    The line is written here and the raising code's own strings go behind
    ``-v``, however many versions failed.  The one exception is the failure
    the listing filter caused: reaching this function means the resolve has
    failed, so the walk runs and names the filter instead.
    """
    if len(blocks) == 1:
        version = blocks[0].filtered_sdist_version
        if version is not None:
            named = provider.filtered_sdist_diagnostic(normalized, version)
            if named is not None:
                return named
    return Diagnostic(_METADATA_REJECTED, tuple(block.message for block in blocks))


# The bullet already names the proxy, so these say "the extra" rather than
# spelling it out again, and only the narrowed line names the base package.
_EXTRA_SHORT: dict[Kind, str] = {
    ReasonKind.EXTRA_UNDECLARED: "no version of {base} declares this extra",
    # nab never read the metadata that would say which versions declare the
    # extra, so this line cannot claim any version does.
    ReasonKind.EXTRA_METADATA: _METADATA_REJECTED,
    ReasonKind.EXTRA_NARROWED: (
        "another requirement holds {base} where this extra is undeclared"
    ),
}


def extra_diagnostic(
    base: str, extra: str, recorded: NoVersionsReason, searched: str
) -> Diagnostic:
    """Say why an extras proxy ran out of versions of its base package.

    ``searched`` is the range the proxy looked in, which the undeclared
    case reads.  The narrowed case names the version it was narrowed off
    instead: the range it was left with is the solver's own, which does
    not always spell as a specifier, and a full one has nothing outside it
    to point at.
    """
    short = _EXTRA_SHORT[recorded.kind].format(base=base)
    if recorded.kind == ReasonKind.EXTRA_METADATA:
        return Diagnostic(short, tuple(block.message for block in recorded.metadata))
    if recorded.kind == ReasonKind.EXTRA_NARROWED:
        return Diagnostic(
            short,
            (
                (
                    f"{base} {recorded.declaring_version} declares the extra,"
                    " and the resolve cannot choose that version"
                ),
            ),
        )
    return Diagnostic(
        short,
        (
            (
                f"no version of {base} in the range the resolve considered"
                f" declares Provides-Extra: {extra}"
            ),
            f"the range considered: {searched}",
        ),
    )
