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

import enum
import re
from typing import TYPE_CHECKING

from nab_provider.diagnostics import Diagnostic
from nab_provider.records import SdistFile, WheelFile

from ..errors import InvalidUploadTimeError
from ..policy import DistPolicy
from . import listing as _listing
from .listing import DropCause

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider._vendor.packaging.version import Version

    from ..provider import DistFile, Provider
    from .listing import ListingPolicy


# The configuration key each cause belongs to, spelled the way the user
# writes it.  A short line naming two causes names their keys, so the two
# upload-time rungs and the two dist-policy rungs share one entry here.
FILTER_KEYS: dict[DropCause, str] = {
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


class CutoffLayer(enum.Enum):
    """Which config layer set the upload-time cutoff a candidate was judged by.

    The project level splits in two because the remedy does: a package that
    already sets ``uploaded-prior-to`` over some other version range cannot
    be given a second, bare-name entry, which the config layer refuses as
    two entries setting one field over overlapping ranges.
    """

    GLOBAL = "global"
    GLOBAL_SCOPED_ENTRY = "global-scoped-entry"
    PACKAGE = "package"
    INDEX = "index"


class ReasonKind(enum.Enum):
    """Which situation left ``choose_version`` with no version to return."""

    OFFLINE_MISS = "offline-miss"
    UNREADABLE_ONLY = "unreadable-only"
    YANKED_ONLY = "yanked-only"
    ABSENT = "absent"
    FILTERED_EMPTY = "filtered-empty"
    BLOCKERS = "blockers"
    EXTRA_UNDECLARED = "extra-undeclared"
    EXTRA_METADATA = "extra-metadata"
    EXTRA_NARROWED = "extra-narrowed"
    NO_MATCH = "no-match"


class BlockerKind(enum.Enum):
    """What the look-ahead found holding a dependency away from the range."""

    DECIDED = "decided"
    HELD = "held"
    ROOT = "root"


# The value types below are hand-written rather than dataclasses: every nab
# invocation imports this module, and declaring a frozen slots dataclass
# costs tens of times more at import than a plain class with ``__slots__``.


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
    """One dependency the look-ahead found no candidate could satisfy."""

    __slots__ = ("declared", "held", "kind", "package")

    def __init__(
        self, kind: BlockerKind, package: str, declared: str, held: str
    ) -> None:
        """Record that ``package`` is wanted in ``declared`` but stands at ``held``."""
        self.kind = kind
        self.package = package
        self.declared = declared
        self.held = held


class MetadataBlock:
    """One version whose metadata no rung of the ladder could read.

    ``message`` is the failure as every other caller of the raising code
    reads it.  ``diagnostic`` is the whole-package entry the raiser could
    build for it, which only the rung that knows a named filter took the
    sdist can.
    """

    __slots__ = ("diagnostic", "message")

    def __init__(self, message: str, diagnostic: Diagnostic | None = None) -> None:
        """Record ``message`` as this version's failure."""
        self.message = message
        self.diagnostic = diagnostic


class NoVersionsReason:
    """What the resolve recorded when a package ran out of candidates."""

    __slots__ = ("blockers", "kind", "metadata", "version_range")

    def __init__(
        self,
        kind: ReasonKind,
        blockers: tuple[Blocker, ...] = (),
        metadata: tuple[MetadataBlock, ...] = (),
        version_range: VersionRange | None = None,
    ) -> None:
        """Mark ``kind`` as the situation, with what the failure cannot re-derive.

        Recorded during the resolve and rendered only if the resolve then
        fails, so it holds the look-ahead rejections (which reset per scan)
        and the range that was asked, and nothing else.
        """
        self.kind = kind
        self.blockers = blockers
        self.metadata = metadata
        self.version_range = version_range

    @property
    def is_generic(self) -> bool:
        """Whether this reason says only that nothing matched."""
        return self.kind is ReasonKind.NO_MATCH


# The situations that carry nothing beyond their kind, built once so the
# record path allocates nothing for them.
OFFLINE_MISS = NoVersionsReason(ReasonKind.OFFLINE_MISS)
UNREADABLE_ONLY = NoVersionsReason(ReasonKind.UNREADABLE_ONLY)
YANKED_ONLY = NoVersionsReason(ReasonKind.YANKED_ONLY)
ABSENT = NoVersionsReason(ReasonKind.ABSENT)
FILTERED_EMPTY = NoVersionsReason(ReasonKind.FILTERED_EMPTY)


NO_MATCH = Diagnostic("no version matches the requirement")

# The four situations the index client answers on its own.  None of them is
# a walk, so the ``-v`` body states the same fact at more length rather than
# deepening into clauses.
FIXED_DIAGNOSTICS: dict[ReasonKind, Diagnostic] = {
    ReasonKind.OFFLINE_MISS: Diagnostic(
        "offline mode skipped an index with no cached listing",
        (
            (
                "offline mode is on and no index in the cache holds a listing"
                " for this package"
            ),
            (
                "note: running without --offline, or setting offline = false,"
                " lets nab fetch it"
            ),
        ),
        "run without --offline (or offline = false)",
    ),
    ReasonKind.UNREADABLE_ONLY: Diagnostic(
        "no file is a wheel or a .tar.gz sdist (the only formats nab reads)",
        (
            (
                "every file the index served uses a format nab does not read"
                " (not a wheel, not a .tar.gz sdist)"
            ),
        ),
    ),
    ReasonKind.YANKED_ONLY: Diagnostic(
        "the index lists this package but every file is yanked",
        (
            "every file the index served for this package is yanked (PEP 592)",
            (
                "a yanked file is never admitted, so the listing reaches the"
                " resolver empty"
            ),
        ),
    ),
    ReasonKind.ABSENT: Diagnostic(
        "package not found on any configured index",
        ("no configured index served a listing for this package",),
    ),
}

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
    dropped, survivors, sdist_install = _shared_base_pass(
        provider, normalized, files, policy
    )
    tag_dropped, kept = _tag_pass(provider, normalized, survivors, sdist_install)

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
    normalized: str,
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


# One short line per cause, for the listing the filter emptied.  Read only
# where exactly one clause has anything to say, so each states the whole of
# what went wrong.
_SHORT_EMPTY: dict[DropCause, str] = {
    DropCause.UPLOAD_TIME_MISSING: (
        "uploaded-prior-to excluded every file; none carries an upload time"
    ),
    DropCause.UPLOAD_TIME_UNPARSEABLE: (
        "uploaded-prior-to excluded every file; their upload times are unreadable"
    ),
    DropCause.UPLOAD_TIME_NAIVE: (
        "uploaded-prior-to excluded every file; their upload times carry no timezone"
    ),
    DropCause.UPLOAD_TIME_AFTER_CUTOFF: (
        "uploaded-prior-to excluded every file; all are newer than the cutoff"
    ),
    DropCause.DIST_POLICY: (
        'dist-policy = "{policy}" excluded every file; none is {want}'
    ),
    DropCause.SDIST_INSTALL_NO_SDIST: (
        'dist-policy = "sdist-install" excluded every version; none publishes an sdist'
    ),
    DropCause.REQUIRES_PYTHON: (
        "requires-python excluded every file; none supports your target Python"
    ),
    DropCause.WHEEL_TAGS: (
        "no wheel matches this platform or Python, and no sdist to build from"
    ),
    DropCause.INVALID_VERSION: "every file carries a version PEP 440 cannot parse",
}

_SHORT_UNNAMED = (
    "every file was refused, and this report cannot name the filter that did it"
)
_SHORT_UNNAMED_IN_RANGE = (
    "every matching version was refused, and this report cannot name the filter"
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
        record.cause is DropCause.SDIST_INSTALL_NO_SDIST for record in diagnosis.dropped
    ):
        clauses.append(_NO_SDIST_TAIL)

    layers = _cutoff_layers(provider, normalized, diagnosis, diagnosis.dropped)
    return Diagnostic(
        _empty_short(groups, diagnosis.unexplained),
        (*clauses, *_note_lines(layers, normalized)),
        _remedy(normalized, groups, layers),
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

    layers = _cutoff_layers(provider, normalized, diagnosis, asked)
    clauses = [_clause(cause, records, diagnosis) for cause, records in groups]
    return Diagnostic(
        _in_range_short(groups),
        (*clauses, *_note_lines(layers, normalized)),
        _remedy(normalized, groups, layers),
    )


_Groups = list[tuple[DropCause, list[DroppedFile]]]


def _empty_short(groups: _Groups, unexplained: int) -> str:
    """Return the one line an emptied listing gets at default verbosity."""
    if not groups:
        return _SHORT_UNNAMED
    if len(groups) == 1 and not unexplained:
        cause, records = groups[0]
        return _single_cause_short(cause, records[0])
    return f"{_join_keys(groups)} excluded every file (-v for detail)"


def _single_cause_short(cause: DropCause, record: DroppedFile) -> str:
    """Return the line for a listing one rung emptied on its own."""
    if cause is DropCause.DIST_POLICY:
        return _SHORT_EMPTY[cause].format(
            policy=record.detail, want="an sdist" if record.is_wheel else "a wheel"
        )
    return _SHORT_EMPTY[cause]


def _in_range_short(groups: _Groups) -> str:
    """Return the one line a filtered-out requirement gets at default verbosity."""
    if len(groups) == 1:
        key = FILTER_KEYS[groups[0][0]]
        return f"{key} excluded every version matching the requirement"
    return f"{_join_keys(groups)} excluded every matching version (-v for detail)"


def _join_keys(groups: _Groups) -> str:
    """Join the config keys the groups name, in report order, without repeats."""
    keys = list(dict.fromkeys(FILTER_KEYS[cause] for cause, _records in groups))
    if len(keys) == 1:
        return keys[0]
    return f"{', '.join(keys[:-1])} and {keys[-1]}"


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


def _groups(records: Sequence[DroppedFile]) -> _Groups:
    """Group ``records`` into one clause each, in report order.

    A version-scoped override can give one listing two effective cutoffs
    or two effective dist policies, and each is quoted in a clause of its
    own.  Every other cause has one clause, since nothing else its
    template states varies by file.
    """
    grouped: dict[tuple[DropCause, tuple[object, ...]], list[DroppedFile]] = {}
    for record in records:
        grouped.setdefault((record.cause, _shared_values(record)), []).append(record)

    return [
        (cause, group)
        for (cause, _values), group in sorted(
            grouped.items(), key=lambda item: item[0][0].value
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


# A remedy names a setting rather than the table that holds it: the same key
# is spelled under [tool.nab] in pyproject.toml and at the top level of a
# nab.toml.
_REMEDIES: dict[CutoffLayer, str] = {
    CutoffLayer.GLOBAL: (
        "the project-level uploaded-prior-to set that cutoff; setting"
        ' packages."{package}".uploaded-prior-to = false lifts it for this package'
    ),
    CutoffLayer.GLOBAL_SCOPED_ENTRY: (
        "the project-level uploaded-prior-to set that cutoff; {package} already"
        " sets uploaded-prior-to over another version range, so widen that entry"
        " over this version or drop the project-level cutoff"
    ),
    CutoffLayer.PACKAGE: (
        "the per-package uploaded-prior-to for {label} set that cutoff; setting"
        " it to false there lifts it"
    ),
    CutoffLayer.INDEX: (
        'the per-index uploaded-prior-to for index "{label}" set that cutoff;'
        " setting it to false there lifts it"
    ),
}

# The assignment cut out of each remedy, for the one ``try:`` line the
# default report prints.  It states what to set and not what follows:
# lifting a filter admits files rather than promising a resolve.
_TRY_CUTOFF: dict[CutoffLayer, str] = {
    CutoffLayer.GLOBAL: 'packages."{package}".uploaded-prior-to = false',
    CutoffLayer.GLOBAL_SCOPED_ENTRY: (
        'widen packages."{label}" to this version, or unset uploaded-prior-to'
    ),
    CutoffLayer.PACKAGE: 'packages."{label}".uploaded-prior-to = false',
    CutoffLayer.INDEX: 'index."{label}".uploaded-prior-to = false',
}

_TRY_DIST_POLICY = 'packages."{package}".dist-policy = "wheel-or-sdist"'


def _cutoff_layers(
    provider: Provider,
    normalized: str,
    diagnosis: ListingDiagnosis,
    records: Iterable[DroppedFile],
) -> list[tuple[CutoffLayer, str]]:
    """Return the config layers that set the cutoffs ``records`` were judged by.

    One entry per layer, so a listing judged by two of them is answered
    about both, and empty when no upload-time rung refused anything.
    """
    by_cutoff: dict[datetime | None, list[DroppedFile]] = {}
    for record in records:
        if record.cause in UPLOAD_TIME_CAUSES:
            by_cutoff.setdefault(record.cutoff, []).append(record)

    return list(
        dict.fromkeys(
            provider.uploaded_prior_to_source(
                normalized, _version_of(_newest(group)), diagnosis.index_name
            )
            for group in by_cutoff.values()
        )
    )


def _note_lines(
    layers: Sequence[tuple[CutoffLayer, str]], package: str
) -> tuple[str, ...]:
    """Return the ``note:`` lines naming what lifts each cutoff that applied.

    Offered for the upload-time causes alone.  Requires-Python gets none:
    the per-package override replaces the package's declared metadata, so
    offering it as a fix would be telling the user to lie to the resolver.
    """
    return tuple(
        "note: " + _REMEDIES[layer].format(package=package, label=label)
        for layer, label in layers
    )


def _remedy(
    package: str, groups: _Groups, layers: Sequence[tuple[CutoffLayer, str]]
) -> str | None:
    """Return the assignment the ``try:`` line states, or ``None``.

    Where several rungs fired, the first in report order that has a remedy
    answers: lifting it is what admits files again, and the report cannot
    promise that the next rung then keeps them.
    """
    for cause, _records in groups:
        if cause in UPLOAD_TIME_CAUSES:
            layer, label = layers[0]
            return _TRY_CUTOFF[layer].format(package=package, label=label)
        if cause in DIST_POLICY_CAUSES:
            return _TRY_DIST_POLICY.format(package=package)
    return None


# The single-file clause for a rung that took one named artifact, which the
# metadata ladder needs: there the file, not the count, is the evidence.
_FILE_CLAUSES: dict[DropCause, str] = {
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
) -> tuple[str, Diagnostic] | None:
    """Name the rung that took ``version``'s sdist, for the metadata ladder.

    Returns the config key that refused it and the report entry for the
    package, or ``None`` when no rung this report can name refused that
    sdist, which leaves the ladder its own untargeted sentence.
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
    key = FILTER_KEYS[record.cause]
    layers = _cutoff_layers(provider, normalized, diagnosis, refused)
    return key, Diagnostic(
        f"{key} excluded the sdist, and the index has no PEP 658 metadata",
        (
            f"{normalized} {version} has no PEP 658 metadata on the index",
            _FILE_CLAUSES[record.cause].format(
                filename=record.filename,
                detail=record.detail,
                cutoff="" if record.cutoff is None else record.cutoff.isoformat(),
                py=diagnosis.target_python,
            ),
            *_note_lines(layers, normalized),
        ),
        _remedy(normalized, [(record.cause, [record])], layers),
    )


_BLOCKER_DETAIL: dict[BlockerKind, str] = {
    BlockerKind.DECIDED: (
        "requires {package} in {declared} but solution has it at {held}"
    ),
    BlockerKind.HELD: "requires {package} in {declared} but solution has it in {held}",
    BlockerKind.ROOT: "requires {package} in {declared} but root has it in {held}",
}

_BLOCKER_SHORT: dict[BlockerKind, str] = {
    BlockerKind.DECIDED: (
        "every version needs {package} in {declared},"
        " but the resolve chose {package} {held}"
    ),
    BlockerKind.HELD: (
        "every version needs {package} in {declared},"
        " but the resolve holds {package} in {held}"
    ),
    BlockerKind.ROOT: (
        "every version needs {package} in {declared},"
        " but your project requires {package} {held}"
    ),
}

_UNREADABLE_METADATA = "no version in range has readable metadata (-v for the errors)"


def blockers_diagnostic(
    blockers: Sequence[Blocker], metadata: Sequence[MetadataBlock]
) -> Diagnostic:
    """Say what the look-ahead found rejecting every candidate in range."""
    detail = [
        _BLOCKER_DETAIL[blocker.kind].format(
            package=blocker.package, declared=blocker.declared, held=blocker.held
        )
        for blocker in blockers
    ]
    detail.extend(block.message for block in metadata)

    if len(blockers) + bool(metadata) == 1:
        if not blockers:
            return metadata_diagnostic(metadata)
        blocker = blockers[0]
        short = _BLOCKER_SHORT[blocker.kind].format(
            package=blocker.package, declared=blocker.declared, held=blocker.held
        )
        return Diagnostic(short, tuple(detail))

    return Diagnostic(_several_blockers_short(blockers, metadata), tuple(detail))


def _several_blockers_short(
    blockers: Sequence[Blocker], metadata: Sequence[MetadataBlock]
) -> str:
    """Name the packages holding every candidate out, without their ranges."""
    names = list(dict.fromkeys(blocker.package for blocker in blockers))
    if not metadata:
        return f"every version is blocked by {_join_names(names)} (-v for the ranges)"
    if not names:
        return _UNREADABLE_METADATA
    return (
        f"every version is blocked by {_join_names(names)}"
        " or has unreadable metadata (-v for detail)"
    )


def _join_names(names: Sequence[str]) -> str:
    """Join package names with "and", the way the short lines read them."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def metadata_diagnostic(blocks: Sequence[MetadataBlock]) -> Diagnostic:
    """Say that no version's metadata could be read, at the depth it is known.

    One version keeps its own sentence, which names the rung that gave up.
    Where the raiser built a whole-package entry for it, that entry is the
    answer: it names the filter that took the sdist the ladder wanted.
    """
    if len(blocks) == 1:
        block = blocks[0]
        if block.diagnostic is not None:
            return block.diagnostic
        return Diagnostic(block.message, (block.message,))
    return Diagnostic(_UNREADABLE_METADATA, tuple(block.message for block in blocks))


_EXTRA_SHORT: dict[ReasonKind, str] = {
    ReasonKind.EXTRA_UNDECLARED: "no version of {base} declares this extra",
    ReasonKind.EXTRA_METADATA: (
        "every version of {base} declaring this extra has unreadable metadata"
    ),
    ReasonKind.EXTRA_NARROWED: (
        "another requirement holds {base} at versions that do not declare this extra"
    ),
}


def extra_diagnostic(
    base: str, extra: str, recorded: NoVersionsReason, held: str | None
) -> Diagnostic:
    """Say why an extras proxy ran out of versions of its base package.

    ``held`` is how the resolve narrowed ``base``, which only the
    narrowed case reads.
    """
    short = _EXTRA_SHORT[recorded.kind].format(base=base)
    if recorded.kind is ReasonKind.EXTRA_METADATA:
        return Diagnostic(short, tuple(block.message for block in recorded.metadata))
    if recorded.kind is ReasonKind.EXTRA_NARROWED:
        return Diagnostic(
            short,
            (
                (
                    f"the resolve holds {base} in {held}; the versions declaring"
                    " the extra are outside that range"
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
            f"the range considered: {recorded.version_range}",
        ),
    )
