"""The value records a resolve is expressed in.

None of them fetches anything or is typed against a version or requirement
class, so the fetching side and the resolving side can both name them.
"""

# ruff: noqa: SLF001 - Construction and deferred parsing share this module's slots.

from __future__ import annotations

__lazy_modules__ = (
    "nab_provider.digest",
    "sys",
    "urllib.parse",
)

import enum
import sys
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit, urlunsplit

from ._compat import override
from .digest import is_hex_digest
from .serialization import SimpleSerialization
from .value import HashableRecord, Record

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ACCEPTED_HASH_ALGORITHMS",
    "DEFAULT_INDEX_NAME",
    "DEFAULT_INDEX_URL",
    "DistFile",
    "IndexConfig",
    "RangeMetadataResult",
    "RangeOutcome",
    "SdistFile",
    "WheelFile",
    "defer_hashes",
    "defer_sidecar_hash",
    "parse_hash_table",
    "rehydrated_sdist",
    "rehydrated_wheel",
    "release_wheel_payload",
    "select_artifact_hash",
    "sidecar_hash",
]

# Verification order; sha256 is pip's hash-checking baseline.
ACCEPTED_HASH_ALGORITHMS: tuple[str, ...] = ("sha256", "sha384", "sha512")


def parse_hash_table(value: object) -> tuple[tuple[str, str], ...]:
    """Return a PEP 691 ``hashes`` table as ``(algorithm, hex_digest)`` pairs.

    PEP 503/691 mandate no case for either half, so both are lowercased here
    and no later comparison has to.  A digest that is not hex can never match a
    file's bytes, so it is dropped.  Algorithm names are interned: they come
    from a tiny fixed vocabulary and repeat once per file.
    """
    if not isinstance(value, dict):
        return ()

    # The common case is a single hash; skip the list build.
    if len(value) == 1:
        ((algo, digest),) = value.items()
        if isinstance(algo, str) and isinstance(digest, str) and is_hex_digest(digest):
            return ((sys.intern(algo.lower()), digest.lower()),)
        return ()

    out: list[tuple[str, str]] = []
    for algo, digest in value.items():
        if isinstance(algo, str) and isinstance(digest, str) and is_hex_digest(digest):
            out.append((sys.intern(algo.lower()), digest.lower()))

    return tuple(out)


def select_artifact_hash(
    hashes: tuple[tuple[str, str], ...],
) -> tuple[str, str] | None:
    """Pick the preferred ``(algo, hex)`` to verify, or ``None`` if none qualify.

    Walks :data:`ACCEPTED_HASH_ALGORITHMS` in order, so sha256 is preferred,
    then sha384, then sha512. An empty set, an empty digest, or only unaccepted
    algorithms (md5) yields ``None``.
    """
    by_algo = {algo.lower(): digest.lower() for algo, digest in hashes}
    for algo in ACCEPTED_HASH_ALGORITHMS:
        digest = by_algo.get(algo)
        if digest:
            return (algo, digest)
    return None


def sidecar_hash(value: object) -> tuple[str, str] | None:
    """Return the ``(algo, hex)`` to verify a PEP 714 metadata value against.

    ``value`` is the entry's ``core-metadata`` / ``dist-info-metadata`` field.
    A bare ``true`` (sidecar exists, no hash), a digest that is not hex, or a
    table with no accepted algorithm yields ``None``.
    """
    if not isinstance(value, dict):
        return None

    published = tuple(
        (algo, digest)
        for algo, digest in value.items()
        if isinstance(algo, str) and isinstance(digest, str) and is_hex_digest(digest)
    )
    return select_artifact_hash(published)


def _compact_table(table: dict[object, object]) -> object:
    """Return the form of ``table`` a record holds until something reads it.

    An index almost always publishes one sha256 digest, and that is held as the
    digest alone.  Any other one-entry table with a string name is held as an
    interned ``(algo, digest)`` pair, and anything else as the dict it was
    served as.
    """
    if len(table) == 1:
        # Looking sha256 up directly skips building the items view.
        sha256 = table.get("sha256")
        if type(sha256) is str:
            return sha256

        ((algo, digest),) = table.items()
        if type(algo) is str:
            return (sys.intern(algo), digest)
    return table


def _compact_shared_table(table: dict[object, object]) -> object:
    """Compact ``table`` for a caller whose rows already share one name object.

    One cached listing's rows are decoded together and get a single object for
    a repeated name, and :func:`parse_hash_table` interns whatever a reader
    parses out of the pair, so :func:`_compact_table`'s per-row intern buys
    nothing here.
    """
    if len(table) == 1:
        sha256 = table.get("sha256")
        if type(sha256) is str:
            return sha256

        ((algo, digest),) = table.items()
        if type(algo) is str:
            return (algo, digest)
    return table


def _expand_table(held: object) -> object:
    """Return ``held`` as the index served it, undoing :func:`_compact_table`."""
    if type(held) is str:
        return {"sha256": held}
    if isinstance(held, tuple):
        algo, digest = held
        return {algo: digest}
    return held


class _DeferredIntegrity:
    """Parses a record's ``hashes`` table the first time something reads it.

    A deferred record has nothing in the parsed slot and the index's own table
    beside it, so a listing pays the integrity parse only for the files a
    resolve reads.  ``__getattr__`` runs only once ordinary lookup has failed,
    so a record whose slot holds a parsed value reads it straight out.

    A read leaves the raw table in place.  The fetching and the resolving
    thread hold the same records, so both may run the parse at once, and both
    then store the same value.
    """

    __slots__ = ("_raw_hashes",)

    _raw_hashes: object

    def __getattr__(self, name: str) -> object:
        if name != "hashes":
            raise AttributeError(name)

        value = parse_hash_table(_expand_table(self._raw_hashes))
        setattr(self, name, value)
        return value

    def raw_hashes(self) -> object:
        """Return the ``hashes`` table this record was built from, else ``None``."""
        try:
            return _expand_table(self._raw_hashes)
        except AttributeError:
            return None


def defer_hashes(record: WheelFile | SdistFile, table: object) -> None:
    """Hold ``table`` unparsed, so ``record.hashes`` parses it on first read.

    Only a PEP 691 table has pairs to defer; any other value leaves
    ``record.hashes`` as the caller built it.
    """
    if not isinstance(table, dict):
        return
    del record.hashes
    record._raw_hashes = _compact_table(table)


def defer_sidecar_hash(wheel: WheelFile, table: object) -> None:
    """Hold ``table`` unparsed, so ``wheel.metadata_hash`` parses it on first read.

    PEP 714's bare ``true`` promises a sidecar without publishing a digest, so
    only a table defers.
    """
    if not isinstance(table, dict):
        return
    del wheel.metadata_hash
    wheel._raw_metadata = _compact_table(table)


class WheelFile(_DeferredIntegrity, HashableRecord):
    """Wheel file record returned by the Simple-API client.

    ``hashes`` is a tuple of ``(algorithm, hex_digest)`` pairs in the
    order PEP 691 declared them (tuple form keeps the record
    hashable).  ``has_metadata`` says whether the index advertised a
    PEP 658/714 sidecar.

    ``local_path`` is set for a wheel served from a local index, so a
    caller need not reverse the ``file:`` URL, which is lossy across
    platforms.

    ``metadata_hash`` is the published ``(algorithm, hex_digest)`` for
    the PEP 658/714 sidecar, or ``None`` when the index advertised the
    sidecar without a hash.

    A record built from a listing holds the index's own tables and parses
    ``hashes`` and ``metadata_hash`` on first read (:func:`defer_hashes`,
    :func:`rehydrated_wheel`).
    """

    __match_args__ = (
        "filename",
        "url",
        "version",
        "requires_python",
        "has_metadata",
        "upload_time",
        "hashes",
        "size",
        "local_path",
        "metadata_hash",
    )

    __slots__ = (*__match_args__, "_metadata_url", "_raw_metadata")

    _metadata_url: str
    _raw_metadata: object
    filename: str
    url: str
    version: str
    requires_python: str | None
    has_metadata: bool
    upload_time: str | None
    hashes: tuple[tuple[str, str], ...]
    size: int | None
    local_path: Path | None
    metadata_hash: tuple[str, str] | None

    yanked: bool | str = False

    def __init__(  # noqa: PLR0913 - One argument per record field.
        self,
        filename: str,
        *,
        url: str,
        version: str,
        requires_python: str | None,
        has_metadata: bool,
        upload_time: str | None,
        hashes: tuple[tuple[str, str], ...] = (),
        size: int | None = None,
        local_path: Path | None = None,
        metadata_hash: tuple[str, str] | None = None,
    ) -> None:
        """Retain the distribution fields in their slots."""
        self.filename = filename
        self.url = url
        self.version = version
        self.requires_python = requires_python
        self.has_metadata = has_metadata
        self.upload_time = upload_time
        self.hashes = hashes
        self.size = size
        self.local_path = local_path
        self.metadata_hash = metadata_hash

    @override
    def __getattr__(self, name: str) -> object:
        """Parse deferred integrity hashes on first access."""
        if name != "metadata_hash":
            return super().__getattr__(name)

        value = sidecar_hash(_expand_table(self._raw_metadata))
        setattr(self, name, value)
        return value

    def raw_sidecar(self) -> object:
        """Return the sidecar value this wheel was built from, else ``None``."""
        try:
            return _expand_table(self._raw_metadata)
        except AttributeError:
            return None

    @property
    def metadata_url(self) -> str | None:
        """Return the PEP 658/714 metadata URL, or None when unsupported.

        The suffix goes on the path, so a PEP 503 hash fragment is dropped.
        An empty URL answers ``None``: no listing entry carries one, so it
        marks a record :func:`release_wheel_payload` has reached.
        """
        url = self.url
        if not self.has_metadata or not url:
            return None

        try:
            return self._metadata_url
        except AttributeError:
            parts = urlsplit(url)
            url = urlunsplit(parts._replace(path=parts.path + ".metadata", fragment=""))
            self._metadata_url = url
            return url


def release_wheel_payload(wheel: WheelFile) -> None:
    """Drop what only an installable wheel still reads.

    A wheel the target's tags refuse is never fetched, verified or written
    to a lock, so its URL, integrity tables and sidecar declaration have no
    reader left.  The record stays in the listing for the version tallies
    and the diagnosis walk.  Only the raw tables go, so a record that
    parsed its hashes at construction keeps them.

    The URL is cleared first so a concurrent reader can order itself
    against the release: an empty URL makes :attr:`WheelFile.metadata_url`
    answer ``None``, so a reader that takes a hash and then a URL that is
    still there took the hash before the release began.
    """
    wheel.url = ""
    wheel.has_metadata = False

    # A deferred slot holding ``None`` reads back as one holding nothing.
    wheel._raw_hashes = None
    wheel._raw_metadata = None


def rehydrated_wheel(  # noqa: PLR0913, PLR0917 - the record's fields, in its own order
    filename: str,
    url: str,
    version: str,
    requires_python: str | None,
    has_metadata: bool,  # noqa: FBT001 - Serialized record field, not a behavior flag.
    upload_time: str | None,
    hashes: tuple[tuple[str, str], ...] | dict[Any, Any],
    size: int | None,
    metadata_hash: tuple[str, str] | dict[Any, Any] | None,
) -> WheelFile:
    """Return a wheel rebuilt from a cached listing row.

    ``hashes`` and ``metadata_hash`` each take either a parsed value or the
    index's own table, and a table is held raw for the field's first read to
    parse.  Deferring a field means leaving its slot unwritten, which
    ``__init__`` cannot do, so this writes the slots directly.

    ``local_path`` is ``None``: a cached listing row carries none.
    """
    wheel = WheelFile.__new__(WheelFile)
    wheel.filename = filename
    wheel.url = url
    wheel.version = version
    wheel.requires_python = requires_python
    wheel.has_metadata = has_metadata
    wheel.upload_time = upload_time
    wheel.size = size
    wheel.local_path = None

    if isinstance(hashes, dict):
        wheel._raw_hashes = _compact_shared_table(hashes)
    else:
        wheel.hashes = hashes

    if isinstance(metadata_hash, dict):
        wheel._raw_metadata = _compact_shared_table(metadata_hash)
    else:
        wheel.metadata_hash = metadata_hash

    return wheel


class SdistFile(_DeferredIntegrity, HashableRecord):
    """A source distribution from the Simple API.

    See :class:`WheelFile` for the meaning of ``hashes``, ``size`` and
    ``local_path``.
    """

    __slots__ = __match_args__ = (
        "filename",
        "url",
        "version",
        "requires_python",
        "upload_time",
        "hashes",
        "size",
        "local_path",
    )

    filename: str
    url: str
    version: str
    requires_python: str | None
    upload_time: str | None
    hashes: tuple[tuple[str, str], ...]
    size: int | None
    local_path: Path | None

    yanked: bool | str = False

    def __init__(
        self,
        filename: str,
        url: str,
        version: str,
        requires_python: str | None,
        upload_time: str | None,
        hashes: tuple[tuple[str, str], ...] = (),
        size: int | None = None,
        local_path: Path | None = None,
    ) -> None:
        """Retain the distribution fields in their slots."""
        self.filename = filename
        self.url = url
        self.version = version
        self.requires_python = requires_python
        self.upload_time = upload_time
        self.hashes = hashes
        self.size = size
        self.local_path = local_path


def rehydrated_sdist(
    filename: str,
    url: str,
    version: str,
    requires_python: str | None,
    upload_time: str | None,
    hashes: tuple[tuple[str, str], ...] | dict[Any, Any],
    size: int | None,
) -> SdistFile:
    """Return a source distribution rebuilt from a cached listing row.

    See :func:`rehydrated_wheel`; a source distribution has only ``hashes``
    to defer.
    """
    sdist = SdistFile.__new__(SdistFile)
    sdist.filename = filename
    sdist.url = url
    sdist.version = version
    sdist.requires_python = requires_python
    sdist.upload_time = upload_time
    sdist.size = size
    sdist.local_path = None

    if isinstance(hashes, dict):
        sdist._raw_hashes = _compact_shared_table(hashes)
    else:
        sdist.hashes = hashes

    return sdist


# Either distribution shape a listing can offer for one version.
DistFile = WheelFile | SdistFile


class IndexConfig(HashableRecord):
    """Declares one index in the ordered list of indexes.

    ``name`` is the index identifier used by overrides and lockfile
    output.  ``url`` is the Simple API root (HTTPS or ``file://``).
    Order is significant: callers walk the list left-to-right and
    presence-based first-index applies.  ``serialization`` pins which
    Simple-API serialization this index is asked for and read as.
    """

    __slots__ = __match_args__ = ("name", "url", "serialization")

    name: Final[str]
    url: Final[str]
    serialization: Final[SimpleSerialization]

    def __init__(
        self,
        name: str,
        url: str,
        serialization: SimpleSerialization = SimpleSerialization.NEGOTIATE,
    ) -> None:
        """Name an index and select its Simple API representation."""
        self.name = name
        self.url = url
        self.serialization = serialization


# The index a run uses when nothing declares one.
DEFAULT_INDEX_NAME = "pypi"
DEFAULT_INDEX_URL = "https://pypi.org/simple/"


class RangeOutcome(enum.Enum):
    """How rung 4 obtained (or failed to obtain) a wheel's METADATA."""

    PARTIAL = "partial"
    FULL_BODY = "full-body"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"


class RangeMetadataResult(Record):
    """The recovered METADATA text and the outcome that produced it."""

    __slots__ = __match_args__ = ("text", "outcome")

    text: str | None
    outcome: RangeOutcome

    def __init__(self, text: str | None, outcome: RangeOutcome) -> None:
        """Pair recovered metadata with its acquisition outcome."""
        self.text = text
        self.outcome = outcome
