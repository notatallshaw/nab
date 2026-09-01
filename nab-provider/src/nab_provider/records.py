"""The value records a resolve is expressed in.

None of them fetches anything or is typed against a version or requirement
class, so the fetching side and the resolving side can both name them.
"""

from __future__ import annotations

import enum
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from .digest import is_hex_digest
from .serialization import SimpleSerialization

if TYPE_CHECKING:
    from collections.abc import Callable
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
        ((algo, digest),) = table.items()
        if algo == "sha256" and type(digest) is str:
            return digest
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
        ((algo, digest),) = table.items()
        if algo == "sha256" and type(digest) is str:
            return digest
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


class _MetadataUrlMemo:
    """Slot holding :attr:`WheelFile.metadata_url` once it has been derived.

    A slot rather than a dataclass field keeps the memo out of ``fields()``, so
    it reaches neither equality, the repr, nor the pickled state. It sits on a
    base class because ``@dataclass(slots=True)`` rejects a class that declares
    ``__slots__`` in its own body.
    """

    __slots__ = ("_metadata_url",)

    _metadata_url: str


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

    __slots__ = ()

    _raw_hashes: object

    def __getattr__(self, name: str) -> object:
        if name != "hashes":
            raise AttributeError(name)

        value = parse_hash_table(_expand_table(self._raw_hashes))
        object.__setattr__(self, "hashes", value)
        return value

    def raw_hashes(self) -> object:
        """Return the ``hashes`` table this record was built from, else ``None``."""
        try:
            return _expand_table(self._raw_hashes)
        except AttributeError:
            return None


class _WheelIntegrity(_DeferredIntegrity, _MetadataUrlMemo):
    """Carries a wheel's raw tables in slots of its own."""

    __slots__ = ("_raw_hashes", "_raw_metadata")

    _raw_metadata: object

    def __getattr__(self, name: str) -> object:
        if name != "metadata_hash":
            return super().__getattr__(name)

        value = sidecar_hash(_expand_table(self._raw_metadata))
        object.__setattr__(self, "metadata_hash", value)
        return value

    def raw_sidecar(self) -> object:
        """Return the sidecar value this wheel was built from, else ``None``."""
        try:
            return _expand_table(self._raw_metadata)
        except AttributeError:
            return None


class _SdistIntegrity(_DeferredIntegrity):
    """Carries a source distribution's raw ``hashes`` table in a slot of its own."""

    __slots__ = ("_raw_hashes",)


def defer_hashes(record: WheelFile | SdistFile, table: object) -> None:
    """Hold ``table`` unparsed, so ``record.hashes`` parses it on first read.

    Only a PEP 691 table has pairs to defer; any other value leaves
    ``record.hashes`` as the caller built it.
    """
    if not isinstance(table, dict):
        return
    object.__delattr__(record, "hashes")
    object.__setattr__(record, "_raw_hashes", _compact_table(table))


def defer_sidecar_hash(wheel: WheelFile, table: object) -> None:
    """Hold ``table`` unparsed, so ``wheel.metadata_hash`` parses it on first read.

    PEP 714's bare ``true`` promises a sidecar without publishing a digest, so
    only a table defers.
    """
    if not isinstance(table, dict):
        return
    object.__delattr__(wheel, "metadata_hash")
    object.__setattr__(wheel, "_raw_metadata", _compact_table(table))


def _slot_writer(cls: type, name: str) -> Callable[[object, object], None]:
    """Return the setter for one of ``cls``'s slots.

    A frozen dataclass refuses ``self.field = value``, so its generated
    ``__init__`` writes every field through ``object.__setattr__``. A record
    fills its slots through these instead, leaving ``__setattr__`` frozen for
    callers.
    """
    return cls.__dict__[name].__set__


@dataclass(frozen=True, slots=True, init=False)
class WheelFile(_WheelIntegrity):
    """Wheel file record returned by the Simple-API client.

    ``hashes`` is a tuple of ``(algorithm, hex_digest)`` pairs in the
    order PEP 691 declared them (tuple form keeps the dataclass
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

    filename: str
    url: str
    version: str
    requires_python: str | None
    has_metadata: bool
    upload_time: str | None
    hashes: tuple[tuple[str, str], ...] = ()
    size: int | None = None
    local_path: Path | None = None
    metadata_hash: tuple[str, str] | None = None

    def __init__(  # noqa: PLR0913, PLR0917 - the dataclass's own fields, in its own order
        self,
        filename: str,
        url: str,
        version: str,
        requires_python: str | None,
        has_metadata: bool,  # noqa: FBT001 - a field, not a flag
        upload_time: str | None,
        hashes: tuple[tuple[str, str], ...] = (),
        size: int | None = None,
        local_path: Path | None = None,
        metadata_hash: tuple[str, str] | None = None,
    ) -> None:
        """Write each field's slot directly, not through the frozen ``__setattr__``."""
        _set_wheel_filename(self, filename)
        _set_wheel_url(self, url)
        _set_wheel_version(self, version)
        _set_wheel_requires_python(self, requires_python)
        _set_wheel_has_metadata(self, has_metadata)
        _set_wheel_upload_time(self, upload_time)
        _set_wheel_hashes(self, hashes)
        _set_wheel_size(self, size)
        _set_wheel_local_path(self, local_path)
        _set_wheel_metadata_hash(self, metadata_hash)

    @property
    def metadata_url(self) -> str | None:
        """Return the PEP 658/714 metadata URL, or None when unsupported.

        The suffix goes on the path, so a PEP 503 hash fragment is dropped.
        """
        if not self.has_metadata:
            return None

        try:
            return self._metadata_url
        except AttributeError:
            parts = urlsplit(self.url)
            url = urlunsplit(parts._replace(path=parts.path + ".metadata", fragment=""))
            object.__setattr__(self, "_metadata_url", url)
            return url


_set_wheel_filename = _slot_writer(WheelFile, "filename")
_set_wheel_url = _slot_writer(WheelFile, "url")
_set_wheel_version = _slot_writer(WheelFile, "version")
_set_wheel_requires_python = _slot_writer(WheelFile, "requires_python")
_set_wheel_has_metadata = _slot_writer(WheelFile, "has_metadata")
_set_wheel_upload_time = _slot_writer(WheelFile, "upload_time")
_set_wheel_hashes = _slot_writer(WheelFile, "hashes")
_set_wheel_size = _slot_writer(WheelFile, "size")
_set_wheel_local_path = _slot_writer(WheelFile, "local_path")
_set_wheel_metadata_hash = _slot_writer(WheelFile, "metadata_hash")

_set_wheel_raw_hashes = _slot_writer(_WheelIntegrity, "_raw_hashes")
_set_wheel_raw_metadata = _slot_writer(_WheelIntegrity, "_raw_metadata")


def rehydrated_wheel(  # noqa: PLR0913, PLR0917 - the record's fields, in its own order
    filename: str,
    url: str,
    version: str,
    requires_python: str | None,
    has_metadata: bool,  # noqa: FBT001 - a field, not a flag
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
    _set_wheel_filename(wheel, filename)
    _set_wheel_url(wheel, url)
    _set_wheel_version(wheel, version)
    _set_wheel_requires_python(wheel, requires_python)
    _set_wheel_has_metadata(wheel, has_metadata)
    _set_wheel_upload_time(wheel, upload_time)
    _set_wheel_size(wheel, size)
    _set_wheel_local_path(wheel, None)

    if isinstance(hashes, dict):
        _set_wheel_raw_hashes(wheel, _compact_shared_table(hashes))
    else:
        _set_wheel_hashes(wheel, hashes)

    if isinstance(metadata_hash, dict):
        _set_wheel_raw_metadata(wheel, _compact_shared_table(metadata_hash))
    else:
        _set_wheel_metadata_hash(wheel, metadata_hash)

    return wheel


@dataclass(frozen=True, slots=True, init=False)
class SdistFile(_SdistIntegrity):
    """A source distribution from the Simple API.

    See :class:`WheelFile` for the meaning of ``hashes``, ``size`` and
    ``local_path``.
    """

    filename: str
    url: str
    version: str
    requires_python: str | None
    upload_time: str | None
    hashes: tuple[tuple[str, str], ...] = ()
    size: int | None = None
    local_path: Path | None = None

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
        """Write each field's slot directly, not through the frozen ``__setattr__``."""
        _set_sdist_filename(self, filename)
        _set_sdist_url(self, url)
        _set_sdist_version(self, version)
        _set_sdist_requires_python(self, requires_python)
        _set_sdist_upload_time(self, upload_time)
        _set_sdist_hashes(self, hashes)
        _set_sdist_size(self, size)
        _set_sdist_local_path(self, local_path)


_set_sdist_filename = _slot_writer(SdistFile, "filename")
_set_sdist_url = _slot_writer(SdistFile, "url")
_set_sdist_version = _slot_writer(SdistFile, "version")
_set_sdist_requires_python = _slot_writer(SdistFile, "requires_python")
_set_sdist_upload_time = _slot_writer(SdistFile, "upload_time")
_set_sdist_hashes = _slot_writer(SdistFile, "hashes")
_set_sdist_size = _slot_writer(SdistFile, "size")
_set_sdist_local_path = _slot_writer(SdistFile, "local_path")

_set_sdist_raw_hashes = _slot_writer(_SdistIntegrity, "_raw_hashes")


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
    _set_sdist_filename(sdist, filename)
    _set_sdist_url(sdist, url)
    _set_sdist_version(sdist, version)
    _set_sdist_requires_python(sdist, requires_python)
    _set_sdist_upload_time(sdist, upload_time)
    _set_sdist_size(sdist, size)
    _set_sdist_local_path(sdist, None)

    if isinstance(hashes, dict):
        _set_sdist_raw_hashes(sdist, _compact_shared_table(hashes))
    else:
        _set_sdist_hashes(sdist, hashes)

    return sdist


# Either distribution shape a listing can offer for one version.
DistFile = WheelFile | SdistFile


@dataclass(frozen=True, slots=True)
class IndexConfig:
    """Declares one index in the ordered list of indexes.

    ``name`` is the index identifier used by overrides and lockfile
    output.  ``url`` is the Simple API root (HTTPS or ``file://``).
    Order is significant: callers walk the list left-to-right and
    presence-based first-index applies.  ``serialization`` pins which
    Simple-API serialization this index is asked for and read as.
    """

    name: str
    url: str
    serialization: SimpleSerialization = SimpleSerialization.NEGOTIATE


# The index a run uses when nothing declares one.
DEFAULT_INDEX_NAME = "pypi"
DEFAULT_INDEX_URL = "https://pypi.org/simple/"


class RangeOutcome(enum.Enum):
    """How rung 4 obtained (or failed to obtain) a wheel's METADATA."""

    PARTIAL = "partial"
    FULL_BODY = "full-body"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class RangeMetadataResult:
    """The recovered METADATA text and the outcome that produced it."""

    text: str | None
    outcome: RangeOutcome
