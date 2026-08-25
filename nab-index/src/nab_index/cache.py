"""On-disk cache for nab-index.

Stores PEP 691 Simple API responses (JSON body plus a sidecar cache
policy file) and PEP 658 wheel metadata (raw text, treated as
immutable). The cache is consulted by :class:`CachedAsyncSimpleClient`
before any HTTP transport call.

Layout under ``root``:

    simple-v2/<index>[-<serialization>]/<package>.json    <- PEP 691 JSON body
    simple-v2/<index>[-<serialization>]/<package>.policy  <- {fetched_at, max_age,
                                                              etag, page_url,
                                                              body_digest}
    simple-parsed-v0/<index>[-<serialization>]/<package>.parsed  <- parsed blob
    simple-neg-v0/<index>[-<serialization>]/<package>.neg <- {fetched_at, max_age, etag}
    metadata-v1/<index>/<package>/<url digest>.metadata
    sdist-v2/<index>/<package>/<version>.record  <- pkg_info and pyproject,
                                                    length-prefixed UTF-8
    sdist-v1/<index>/<package>/<version>.json    <- the same pair, as JSON

An index pinned to one serialization gets its own listings directory,
since a stored body records nothing about which serialization it came from.

A versioned bucket name (``simple-v2``) gives zero-cost schema
migration: when the on-disk format changes, bump the suffix and the
old directory is harmless. ``sdist-v1`` is the exception, still read
when ``sdist-v2`` misses, since rebuilding one of its records means
downloading the archive again.

When the index serves PEP 503 HTML the stored body is nab's own rendering
of the page. A 304 revalidation keeps the old body, so changing that
rendering also needs the suffix bumped to reach warm caches.

A resolve writes two more buckets under the same root, holding upstream
source trees:

    vcs-v1/vcs/<repo key>/<commit sha>/   <- shallow clone
    archive-v1/<archive digest>/          <- extracted archive
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from nab_provider.serialization import SimpleSerialization

from ._json_decode import decode_json
from .atomic import atomic_write
from .parsed_listing import corruption_reason as _parsed_corruption

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_BUCKET",
    "VCS_BUCKET",
    "CacheBackend",
    "CachePolicy",
    "NullCache",
    "OfflineError",
    "OnDiskCache",
    "is_recognized_bucket",
    "is_sendable_etag",
]


CACHE_VERSION_SIMPLE = "v2"
CACHE_VERSION_SIMPLE_PARSED = "v0"
CACHE_VERSION_SIMPLE_NEG = "v0"
CACHE_VERSION_METADATA = "v1"
CACHE_VERSION_SDIST = "v2"
# Retired bucket of JSON sdist records, read when the current bucket misses
# and rewritten there, since rebuilding one costs an archive download.
_CACHE_VERSION_SDIST_JSON = "v1"
CACHE_VERSION_VCS = "v1"
CACHE_VERSION_ARCHIVE = "v1"

# Buckets of nab-written records. simple-neg-* is covered by the simple- prefix.
ENTRY_BUCKET_PREFIXES = ("simple-", "metadata-", "sdist-")

# Buckets a resolve fills with upstream source trees. nab owns the directories,
# not the files inside them.
VCS_BUCKET = f"vcs-{CACHE_VERSION_VCS}"
ARCHIVE_BUCKET = f"archive-{CACHE_VERSION_ARCHIVE}"
SOURCE_BUCKETS = (VCS_BUCKET, ARCHIVE_BUCKET)
_LEGACY_SOURCE_BUCKETS = ("vcs", "archive")
_RECOGNIZED_SOURCE_BUCKETS = SOURCE_BUCKETS + _LEGACY_SOURCE_BUCKETS

DEFAULT_PYPI_URLS = frozenset(
    [
        "https://pypi.org/simple",
        "http://pypi.org/simple",
    ]
)


class OfflineError(Exception):
    """Raised when offline mode is set and a needed entry is not cached."""


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """RFC 9111-style freshness policy for one Simple API entry.

    ``fetched_at`` is the start of the freshness window: when nab received the
    response, less any Age a relaying shared cache reported.

    ``etag`` is the entity tag to revalidate with. A tag nab cannot send back
    in a request header is dropped.

    ``page_url`` is the URL the stored body was retrieved from, the base its
    relative entries resolve against. It is ``None`` for the negative
    sentinel, which has no body, and for an entry cached without it.

    ``body_digest`` is the sha256 hex of the raw body this policy governs, and
    binds a parsed-listing blob to that body. It is ``None`` for an older
    policy or a bodyless negative entry.
    """

    fetched_at: int
    max_age: int
    etag: str | None
    page_url: str | None = None
    body_digest: str | None = None

    def is_fresh(self, now: int | None = None) -> bool:
        """Return True if the entry is still within its freshness window."""
        current = int(time.time()) if now is None else now
        return current - self.fetched_at < self.max_age


def _is_entry_bucket(name: str) -> bool:
    """Whether ``name`` is a bucket of records nab writes and parses."""
    return any(name.startswith(prefix) for prefix in ENTRY_BUCKET_PREFIXES)


def is_recognized_bucket(name: str) -> bool:
    """Whether ``name`` is a bucket directory nab owns under a cache root."""
    return _is_entry_bucket(name) or name in _RECOGNIZED_SOURCE_BUCKETS


def _index_dirname(index_url: str) -> str:
    """Return a stable, filesystem-safe directory name for an index URL."""
    if index_url.rstrip("/") in DEFAULT_PYPI_URLS:
        return "pypi"
    return hashlib.sha256(index_url.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, data: bytes) -> None:
    """Create the cache bucket for ``path``, then write ``data`` into it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, data)


def _require_single_segment(component: str) -> str:
    """Return ``component`` if it names exactly one path segment.

    Each cache key component becomes one file or directory name under
    the cache root, so it must be a single path segment. A value with
    an embedded separator expands to a nested path, and ``.`` or ``..``
    names a parent, so either would read or write a different file than
    the key describes and return the wrong cache entry.

    Separator syntax follows the running platform: on Windows a backslash
    separates segments and ``C:`` prefixes a drive, while POSIX reads both
    as ordinary filename characters.
    """
    # A string test, not path handling: Path.name would build a path object per call.
    basename = os.path.basename(component)  # noqa: PTH119
    if component in ("", ".", "..") or component != basename:
        msg = f"cache key component is not a single path segment: {component!r}"
        raise ValueError(msg)
    return component


def _add_owner_mode(path: Path, bits: int) -> None:
    """Add ``bits`` to ``path``'s mode, leaving a symlink untouched."""
    if path.is_symlink():
        return
    path.chmod(path.stat().st_mode | bits)


def _make_removable(root: Path) -> None:
    """Give the owner write on ``root`` and everything under it.

    A clone carries read-only packfiles and an extracted archive keeps
    the mode bits the archive declared, so either can leave a tree
    ``rmtree`` cannot take apart. Symlinks are skipped so no chmod lands
    outside the root.
    """
    _add_owner_mode(root, stat.S_IRWXU)

    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            _add_owner_mode(Path(dirpath, name), stat.S_IRWXU)
        for name in filenames:
            _add_owner_mode(Path(dirpath, name), stat.S_IWUSR)


class OnDiskCache:
    """File-per-key cache for Simple API and wheel metadata.

    Stores are best-effort: a write the filesystem refuses is dropped,
    just as an entry that cannot be read is a miss.
    :meth:`put_simple_parsed` raises instead.
    """

    def __init__(
        self,
        root: Path,
        index_url: str,
        *,
        serialization: SimpleSerialization = SimpleSerialization.NEGOTIATE,
    ) -> None:
        """Create a cache rooted at ``root`` for ``index_url``."""
        self._root = root
        self._index = _index_dirname(index_url)
        simple_index = (
            self._index
            if serialization is SimpleSerialization.NEGOTIATE
            else f"{self._index}-{serialization.value}"
        )
        self._simple_dir = root / f"simple-{CACHE_VERSION_SIMPLE}" / simple_index
        self._parsed_dir = (
            root / f"simple-parsed-{CACHE_VERSION_SIMPLE_PARSED}" / simple_index
        )
        self._neg_dir = root / f"simple-neg-{CACHE_VERSION_SIMPLE_NEG}" / simple_index
        self._metadata_dir = root / f"metadata-{CACHE_VERSION_METADATA}" / self._index
        self._sdist_dir = root / f"sdist-{CACHE_VERSION_SDIST}" / self._index
        self._sdist_json_dir = root / f"sdist-{_CACHE_VERSION_SDIST_JSON}" / self._index
        self._store_failed = False

    def _store(self, path: Path, data: bytes) -> bool:
        """Write ``data`` to ``path``, returning False if the write failed.

        Only the first failure warns: a root that refuses one write
        refuses the rest.
        """
        try:
            _atomic_write(path, data)
        except OSError as exc:
            if not self._store_failed:
                self._store_failed = True
                logger.warning(
                    "cannot store cache entries under %s: %s", self._root, exc
                )
            return False
        return True

    def _simple_paths(self, package: str) -> tuple[Path, Path]:
        segment = _require_single_segment(package)
        body = self._simple_dir / f"{segment}.json"
        policy = self._simple_dir / f"{segment}.policy"
        return (body, policy)

    def _parsed_path(self, package: str) -> Path:
        segment = _require_single_segment(package)
        return self._parsed_dir / f"{segment}.parsed"

    def _neg_path(self, package: str) -> Path:
        segment = _require_single_segment(package)
        return self._neg_dir / f"{segment}.neg"

    def _sdist_path(self, package: str, version: str) -> Path:
        package_segment = _require_single_segment(package)
        version_segment = _require_single_segment(version)
        return self._sdist_dir / package_segment / f"{version_segment}.record"

    def _sdist_json_path(self, package: str, version: str) -> Path:
        package_segment = _require_single_segment(package)
        version_segment = _require_single_segment(version)
        return self._sdist_json_dir / package_segment / f"{version_segment}.json"

    def _metadata_file(self, package: str, metadata_url: str) -> str:
        """Return the path of the file holding the sidecar at ``metadata_url``.

        :pep:`658` attaches a sidecar to one file, so the wheels of a version
        each have their own. The URL is digested to keep the key a single
        path segment whatever path shape the index serves.

        A string rather than a ``Path`` because the read side only opens it.
        """
        package_segment = _require_single_segment(package)
        digest = hashlib.sha256(metadata_url.encode("utf-8")).hexdigest()
        return os.path.join(  # noqa: PTH118
            str(self._metadata_dir), package_segment, f"{digest}.metadata"
        )

    def _metadata_path(self, package: str, metadata_url: str) -> Path:
        """Return :meth:`_metadata_file` as a ``Path``."""
        return Path(self._metadata_file(package, metadata_url))

    def get_simple(self, package: str) -> tuple[bytes, CachePolicy] | None:
        """Return ``(body_bytes, policy)`` if cached, else ``None``."""
        body_path, policy_path = self._simple_paths(package)
        try:
            policy_bytes = policy_path.read_bytes()
            body = body_path.read_bytes()
        except OSError:
            return None
        policy = _decode_policy(policy_bytes)
        if policy is None:
            logger.warning(
                "Corrupt cache policy %s: not decodable; treating as a miss",
                policy_path,
            )
            return None
        return (body, policy)

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> str | None:
        """Write the body and the policy sidecar; return the body's digest.

        The sidecar goes out only once the body has landed, so a dropped
        body write cannot stamp an older body with the new one's ETag and
        freshness window.  The sha256 of ``body`` is stamped into the stored
        policy, overriding any value the caller passed, and returned as the
        binding key for the parsed blob the caller writes next.  A write that
        did not land returns ``None``, so no parsed blob is ever bound to a
        body the store does not hold.
        """
        body_path, policy_path = self._simple_paths(package)
        if not self._store(body_path, body):
            return None
        digest = hashlib.sha256(body).hexdigest()
        if not self._store(
            policy_path, _encode_policy(replace(policy, body_digest=digest))
        ):
            return None
        return digest

    def refresh_simple_policy(self, package: str, policy: CachePolicy) -> None:
        """Replace the policy sidecar without touching the body.

        Called after a 304 Not Modified, where the cached body is still
        valid but the freshness window has slid forward.
        """
        _, policy_path = self._simple_paths(package)
        self._store(policy_path, _encode_policy(policy))

    def get_simple_policy(self, package: str) -> CachePolicy | None:
        """Return the freshness policy for a cached Simple entry, without its body.

        The parsed-cache read path needs only the policy on a hit, so this reads
        the small sidecar without the body. A corrupt policy logs and misses,
        matching :meth:`get_simple`.
        """
        _, policy_path = self._simple_paths(package)
        try:
            policy_bytes = policy_path.read_bytes()
        except OSError:
            return None
        policy = _decode_policy(policy_bytes)
        if policy is None:
            logger.warning(
                "Corrupt cache policy %s: not decodable; treating as a miss",
                policy_path,
            )
        return policy

    def get_simple_parsed(self, package: str) -> bytes | None:
        """Return the opaque parsed-listing blob for ``package``, or ``None``.

        The blob is bytes to this layer; the record codec lives in
        ``parsed_listing``. An absent blob is a silent miss.
        """
        try:
            return self._parsed_path(package).read_bytes()
        except OSError:
            return None

    def get_simple_parsed_size(self, package: str) -> int | None:
        """Return the on-disk size of the parsed-listing blob in bytes, or ``None``.

        A single ``stat`` on the same path :meth:`get_simple_parsed` reads, so a
        caller can size the blob before deciding whether to read and decode it.
        An absent blob is a silent miss.
        """
        try:
            return self._parsed_path(package).stat().st_size
        except OSError:
            return None

    def put_simple_parsed(self, package: str, blob: bytes) -> None:
        """Write the opaque parsed-listing blob for ``package`` atomically.

        A refused write raises ``OSError``.
        """
        _atomic_write(self._parsed_path(package), blob)

    def get_metadata(self, package: str, metadata_url: str) -> str | None:
        """Return the cached sidecar text for ``metadata_url``, or ``None``.

        A present file that is not valid UTF-8 is a corrupt entry: logged
        and treated as a miss. An absent file is a silent miss.
        """
        path = self._metadata_file(package, metadata_url)
        try:
            # Unbuffered: the file is read whole, so buffering only adds a copy.
            with open(path, "rb", buffering=0) as handle:  # noqa: PTH123
                raw = handle.read()
        except OSError:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "Corrupt cached metadata %s: not valid UTF-8; treating as a miss",
                path,
            )
            return None

    def put_metadata(self, package: str, metadata_url: str, text: str) -> None:
        """Write the sidecar text served at ``metadata_url``. Immutable."""
        self._store(self._metadata_path(package, metadata_url), text.encode("utf-8"))

    def get_sdist_files(
        self, package: str, version: str
    ) -> tuple[str | None, str | None] | None:
        """Return the cached ``(pkg_info, pyproject_toml)`` pair, or ``None`` on miss.

        Written as one record, so a hit is always the complete pair. A hit
        whose ``pyproject_toml`` is ``None`` means the sdist ships no
        pyproject.toml, which is not the same as a miss.
        """
        path = self._sdist_path(package, version)
        try:
            raw = path.read_bytes()
        except OSError:
            return self._carry_over_json_record(package, version)
        record = _decode_sdist_record(raw)
        if record is None:
            logger.warning(
                "Corrupt sdist cache record %s: not decodable; treating as a miss",
                path,
            )
        return record

    def _carry_over_json_record(
        self, package: str, version: str
    ) -> tuple[str | None, str | None] | None:
        """Return a record from the retired JSON bucket, rewriting it in this one.

        Rebuilding an sdist record means downloading the archive again, so a
        cache filled before the format change stays warm. The JSON record is
        left in place, so a downgrade still reads it.
        """
        path = self._sdist_json_path(package, version)
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        record = _decode_json_sdist_record(raw)
        if record is None:
            logger.warning(
                "Corrupt sdist cache record %s: not decodable; treating as a miss",
                path,
            )
            return None
        self.put_sdist_files(package, version, *record)
        return record

    def put_sdist_files(
        self,
        package: str,
        version: str,
        pkg_info: str | None,
        pyproject_toml: str | None,
    ) -> None:
        """Write the ``(pkg_info, pyproject_toml)`` pair as one record."""
        self._store(
            self._sdist_path(package, version),
            _encode_sdist_record(pkg_info, pyproject_toml),
        )

    def get_negative(self, package: str) -> CachePolicy | None:
        """Return the freshness policy of a cached name-level 404, or ``None``."""
        neg_path = self._neg_path(package)
        try:
            neg_bytes = neg_path.read_bytes()
        except OSError:
            return None
        policy = _decode_policy(neg_bytes)
        if policy is None:
            logger.warning(
                "Corrupt negative cache entry %s: not decodable; treating as a miss",
                neg_path,
            )
        return policy

    def put_negative(self, package: str, policy: CachePolicy) -> None:
        """Record that ``package`` returned a name-level 404 from this index."""
        self._store(self._neg_path(package), _encode_policy(policy))

    def drop_negative(self, package: str) -> None:
        """Remove any negative entry for ``package``.

        A miss is not an error, and neither is a root that refuses the
        unlink: there is nothing cached to contradict the fresh listing
        the caller just fetched.
        """
        with suppress(OSError):
            self._neg_path(package).unlink(missing_ok=True)

    def _root_children(self) -> list[Path]:
        try:
            return list(self._root.iterdir())
        except OSError:
            return []

    def _bucket_dirs(self) -> list[Path]:
        """Return the recognized bucket entries directly under the root.

        Symlinks are included so the caller can decide how to handle one
        rather than following it out of the root.
        """
        return [
            child for child in self._root_children() if is_recognized_bucket(child.name)
        ]

    def _entry_bucket_dirs(self) -> list[Path]:
        """Return the bucket entries holding records nab wrote.

        The source buckets are excluded: they hold upstream files, not
        nab records.
        """
        return [
            child for child in self._root_children() if _is_entry_bucket(child.name)
        ]

    def iter_cache_entries(self) -> Iterator[Path]:
        """Yield each entry file inside the record buckets.

        A symlinked bucket or a symlinked file is skipped, never followed
        out of the tree.
        """
        for bucket in self._entry_bucket_dirs():
            if bucket.is_symlink() or not bucket.is_dir():
                continue
            for dirpath, _dirnames, filenames in os.walk(bucket, followlinks=False):
                base = Path(dirpath)
                for name in filenames:
                    entry = base / name
                    if not entry.is_symlink():
                        yield entry

    def read_cache_entry(self, path: Path) -> str | None:
        """Return a corruption reason for a cache entry, or ``None`` if it parses.

        Parses by suffix, matching each kind's read path: ``.policy`` and
        ``.neg`` decode as a policy, ``.metadata`` as UTF-8, ``.record`` as an
        sdist record, ``.json`` as JSON (a retired sdist record also carries
        its two fields), ``.parsed`` as a parsed-listing blob. Any other
        suffix is not a nab entry and is reported clean.
        """
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return f"unreadable: {exc.strerror or exc}"
        suffix = path.suffix
        if suffix in (".policy", ".neg"):
            return None if _decode_policy(raw) is not None else "policy not decodable"
        if suffix == ".json":
            return self._read_json_reason(path, raw)
        read_reason = _ENTRY_READERS.get(suffix)
        return read_reason(raw) if read_reason is not None else None

    def _read_json_reason(self, path: Path, raw: bytes) -> str | None:
        try:
            doc = decode_json(raw)
        except ValueError as exc:
            return str(exc)
        bucket = self._bucket_of(path)
        if bucket.startswith("sdist-") and not (
            isinstance(doc, dict) and "pkg_info" in doc and "pyproject" in doc
        ):
            return "sdist record missing fields"
        return None

    def _bucket_of(self, path: Path) -> str:
        try:
            rel = path.relative_to(self._root)
        except ValueError:  # pragma: no cover - entries always sit under the root
            return ""
        return rel.parts[0] if rel.parts else ""

    def clear_cache(self) -> list[str]:
        """Remove the recognized bucket directories in full, returning their names.

        A symlinked bucket has its link removed, never followed, so a
        target outside the root survives. A recognized-named plain file is
        left in place and not counted.
        """
        removed: list[str] = []
        for bucket in self._bucket_dirs():
            if bucket.is_symlink():
                bucket.unlink()
            elif bucket.is_dir():
                try:
                    shutil.rmtree(bucket)
                except PermissionError:
                    _make_removable(bucket)
                    shutil.rmtree(bucket)
            else:
                continue
            removed.append(bucket.name)
        return removed


# A record is this magic, the two field lengths, a newline, then the fields.
# A change to that shape bumps both this and CACHE_VERSION_SDIST.
_SDIST_MAGIC = b"nabsdist1 "
# Length stored for a file the sdist does not ship. An empty file stores 0.
_SDIST_ABSENT = -1


def _decode_json_sdist_record(raw: bytes) -> tuple[str | None, str | None] | None:
    """Decode a retired JSON sdist record, or ``None`` when it is not one.

    A field holds a file's text, or ``null`` for a file the sdist does not
    ship. Any other JSON type is corruption, since the pair is re-encoded
    into the current bucket.
    """
    try:
        doc = decode_json(raw)
        pkg_info, pyproject = doc["pkg_info"], doc["pyproject"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(pkg_info, str | None) or not isinstance(pyproject, str | None):
        return None
    return (pkg_info, pyproject)


def _encode_sdist_field(text: str | None) -> tuple[bytes, int]:
    """Return one field's bytes and the length the header stores for it.

    ``surrogatepass`` keeps every ``str`` a caller can store round-tripping,
    including a lone surrogate with no UTF-8 form.
    """
    if text is None:
        return (b"", _SDIST_ABSENT)
    blob = text.encode("utf-8", "surrogatepass")
    return (blob, len(blob))


def _encode_sdist_record(pkg_info: str | None, pyproject: str | None) -> bytes:
    """Encode the pair as a header of byte lengths followed by the two texts."""
    pkg_blob, pkg_len = _encode_sdist_field(pkg_info)
    pyproject_blob, pyproject_len = _encode_sdist_field(pyproject)
    header = f"{pkg_len} {pyproject_len}\n".encode("ascii")
    return b"".join([_SDIST_MAGIC, header, pkg_blob, pyproject_blob])


def _sdist_record_header(raw: bytes) -> tuple[int, int, int] | None:
    """Return the two field lengths and the body offset, or ``None``.

    A length of ``-1`` marks a file the sdist does not ship. Bytes in any
    other format fail the magic rather than misdecoding.
    """
    if not raw.startswith(_SDIST_MAGIC):
        return None
    header_end = raw.find(b"\n", len(_SDIST_MAGIC))
    if header_end < 0:
        return None
    try:
        pkg_len, pyproject_len = (
            int(field) for field in raw[len(_SDIST_MAGIC) : header_end].split(b" ")
        )
    except ValueError:
        return None
    if pkg_len < _SDIST_ABSENT or pyproject_len < _SDIST_ABSENT:
        return None
    return (pkg_len, pyproject_len, header_end + 1)


def _decode_sdist_record(raw: bytes) -> tuple[str | None, str | None] | None:
    """Decode stored sdist-record bytes, or ``None`` when they are not a record.

    A truncated or overlong body is rejected by the total length, so a field
    is only ever decoded from the bytes the writer put in it.
    """
    header = _sdist_record_header(raw)
    if header is None:
        return None
    pkg_len, pyproject_len, pkg_start = header
    pyproject_start = pkg_start + max(pkg_len, 0)
    if len(raw) != pyproject_start + max(pyproject_len, 0):
        return None
    view = memoryview(raw)
    try:
        return (
            _decode_sdist_field(view, pkg_start, pkg_len),
            _decode_sdist_field(view, pyproject_start, pyproject_len),
        )
    except UnicodeDecodeError:
        return None


def _decode_sdist_field(view: memoryview, start: int, length: int) -> str | None:
    """Decode one record field, or ``None`` for a file the sdist does not ship.

    Decodes from a slice of the view, so the field is never copied out of the
    record first.
    """
    if length == _SDIST_ABSENT:
        return None
    return str(view[start : start + length], "utf-8", "surrogatepass")


def _read_sdist_reason(raw: bytes) -> str | None:
    if _decode_sdist_record(raw) is None:
        return "sdist record not decodable"
    return None


def _read_metadata_reason(raw: bytes) -> str | None:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "not valid UTF-8"
    return None


def _read_parsed_reason(raw: bytes) -> str | None:
    # Structural decodability only; the digest binding retires a stale-but-valid
    # blob at read time, so verify does not check it. The codec owns the check so
    # verify and the read path agree on what counts as corrupt.
    return _parsed_corruption(raw)


# Suffixes whose corruption check is one reader over the bytes. ``.policy`` and
# ``.neg`` share a reader and ``.json`` also needs the path, so both stay inline.
_ENTRY_READERS: dict[str, Callable[[bytes], str | None]] = {
    ".parsed": _read_parsed_reason,
    ".metadata": _read_metadata_reason,
    ".record": _read_sdist_reason,
}


def _encode_policy(policy: CachePolicy) -> bytes:
    doc: dict[str, object] = {
        "fetched_at": policy.fetched_at,
        "max_age": policy.max_age,
        "etag": _policy_etag(policy.etag),
        "page_url": policy.page_url,
    }
    # Emit only when set so an older policy or a bodyless negative entry keeps
    # its previous form; absence decodes back to None.
    if policy.body_digest is not None:
        doc["body_digest"] = policy.body_digest
    return json.dumps(doc).encode("utf-8")


def _policy_page_url(value: object) -> str | None:
    """Page URL from a decoded policy, or None when it is unusable as a base."""
    return value if isinstance(value, str) and value else None


def is_sendable_etag(value: str) -> bool:
    """Whether an entity tag can go back out in a request header.

    Only printable ASCII passes. RFC 9110 8.8.3 admits obs-text in an
    entity-tag, and httpx raises on a non-ASCII request header value; a tag
    read out of a line-folded field carries the fold's CR and LF, which
    RFC 9112 5.2 forbids a sender to generate.
    """
    return value.isascii() and value.isprintable()


def _policy_etag(value: object) -> str | None:
    """Entity tag from a policy, or None when it cannot be sent back."""
    return value if isinstance(value, str) and is_sendable_etag(value) else None


def _decode_policy(policy_bytes: bytes) -> CachePolicy | None:
    """Decode stored policy bytes, or ``None`` when they are not a policy.

    ``json`` decodes a number outside float range (``1e400``, ``Infinity``) to
    an infinity, and ``int()`` on one raises :class:`OverflowError` rather than
    :class:`ValueError`.
    """
    try:
        doc = decode_json(policy_bytes)
        return CachePolicy(
            fetched_at=int(doc["fetched_at"]),
            max_age=int(doc["max_age"]),
            etag=_policy_etag(doc.get("etag")),
            page_url=_policy_page_url(doc.get("page_url")),
            body_digest=doc.get("body_digest"),
        )
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


class CacheBackend(Protocol):
    """Protocol shared by :class:`OnDiskCache` and :class:`NullCache`."""

    def get_simple(self, package: str) -> tuple[bytes, CachePolicy] | None:
        """Return ``(body_bytes, policy)`` if cached, else ``None``."""
        ...

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> str | None:
        """Store a Simple API body and its freshness policy; return the digest.

        The returned value is the sha256 hex of the body the backend stored,
        the binding key for the parsed-listing blob the caller writes next, or
        ``None`` when the backend stored nothing. A caller writes a parsed blob
        only for a digest it was given, so no derived entry ever claims to
        describe a body the store does not hold.
        """
        ...

    def refresh_simple_policy(self, package: str, policy: CachePolicy) -> None:
        """Update the policy for an existing entry without rewriting the body."""
        ...

    def get_simple_policy(self, package: str) -> CachePolicy | None:
        """Return a Simple entry's freshness policy without its body, or ``None``."""
        ...

    def get_simple_parsed(self, package: str) -> bytes | None:
        """Return the opaque parsed-listing blob for ``package``, or ``None``."""
        ...

    def get_simple_parsed_size(self, package: str) -> int | None:
        """Return the parsed-listing blob's on-disk size in bytes, or ``None``."""
        ...

    def put_simple_parsed(self, package: str, blob: bytes) -> None:
        """Store the opaque parsed-listing blob for ``package``."""
        ...

    def get_metadata(self, package: str, metadata_url: str) -> str | None:
        """Return the cached sidecar text for ``metadata_url``, or ``None``."""
        ...

    def put_metadata(self, package: str, metadata_url: str, text: str) -> None:
        """Store the sidecar text served at ``metadata_url``. Immutable."""
        ...

    def get_sdist_files(
        self, package: str, version: str
    ) -> tuple[str | None, str | None] | None:
        """Return the cached ``(pkg_info, pyproject_toml)`` pair, or ``None``."""
        ...

    def put_sdist_files(
        self,
        package: str,
        version: str,
        pkg_info: str | None,
        pyproject_toml: str | None,
    ) -> None:
        """Store the ``(pkg_info, pyproject_toml)`` pair as one record."""
        ...

    def get_negative(self, package: str) -> CachePolicy | None:
        """Return the freshness policy of a cached name-level 404, or ``None``."""
        ...

    def put_negative(self, package: str, policy: CachePolicy) -> None:
        """Record that ``package`` returned a name-level 404 from this index."""
        ...

    def drop_negative(self, package: str) -> None:
        """Remove any negative entry for ``package``."""
        ...

    def iter_cache_entries(self) -> Iterator[Path]:
        """Yield each entry file inside the record buckets."""
        ...

    def read_cache_entry(self, path: Path) -> str | None:
        """Return a corruption reason for a cache entry, or ``None`` if it parses."""
        ...

    def clear_cache(self) -> list[str]:
        """Remove the recognized bucket directories, returning the names removed."""
        ...


class NullCache:
    """No-op cache backend used when persistence is disabled.

    Lets :class:`CachedAsyncSimpleClient` be used unconditionally so
    the call site does not branch on whether a cache is configured.
    Each method is a docstring-only stub: gets implicitly return
    ``None`` (a permanent miss) and puts implicitly do nothing.
    Argument names match :class:`CacheBackend` for Protocol conformance.
    """

    def get_simple(self, package: str) -> tuple[bytes, CachePolicy] | None:
        """Return ``None`` (always a miss)."""

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> None:
        """Discard the entry; return ``None`` since no body was stored.

        A disabled cache holds nothing for a parsed blob to describe, so the
        caller skips building one rather than encoding records into a store
        that would drop them.
        """

    def refresh_simple_policy(self, package: str, policy: CachePolicy) -> None:
        """Discard the policy refresh."""

    def get_simple_policy(self, package: str) -> CachePolicy | None:
        """Return ``None`` (always a miss)."""

    def get_simple_parsed(self, package: str) -> bytes | None:
        """Return ``None`` (always a miss)."""

    def get_simple_parsed_size(self, package: str) -> int | None:
        """Return ``None`` (always a miss)."""

    def put_simple_parsed(self, package: str, blob: bytes) -> None:
        """Discard the entry."""

    def get_metadata(self, package: str, metadata_url: str) -> str | None:
        """Return ``None`` (always a miss)."""

    def put_metadata(self, package: str, metadata_url: str, text: str) -> None:
        """Discard the entry."""

    def get_sdist_files(
        self, package: str, version: str
    ) -> tuple[str | None, str | None] | None:
        """Return ``None`` (always a miss)."""

    def put_sdist_files(
        self,
        package: str,
        version: str,
        pkg_info: str | None,
        pyproject_toml: str | None,
    ) -> None:
        """Discard the entry."""

    def get_negative(self, package: str) -> CachePolicy | None:
        """Return ``None`` (always a miss)."""

    def put_negative(self, package: str, policy: CachePolicy) -> None:
        """Discard the entry."""

    def drop_negative(self, package: str) -> None:
        """Do nothing."""

    def iter_cache_entries(self) -> Iterator[Path]:
        """Yield nothing (no persistent entries)."""
        return iter(())

    def read_cache_entry(self, path: Path) -> str | None:
        """Return ``None`` (a disabled cache never holds a corrupt entry)."""

    def clear_cache(self) -> list[str]:
        """Return an empty list (nothing to remove)."""
        return []
