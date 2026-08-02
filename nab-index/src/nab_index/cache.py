"""On-disk cache for nab-index.

Stores PEP 691 Simple API responses (JSON body plus a sidecar cache
policy file) and PEP 658 wheel metadata (raw text, treated as
immutable). The cache is consulted by :class:`CachedAsyncSimpleClient`
before any HTTP transport call.

Layout under ``root``:

    simple-v1/<index>[-<serialization>]/<package>.json    <- PEP 691 JSON body
    simple-v1/<index>[-<serialization>]/<package>.policy  <- {fetched_at, max_age, etag}
    simple-neg-v0/<index>[-<serialization>]/<package>.neg <- {fetched_at, max_age, etag}
    metadata-v1/<index>/<package>/<url digest>.metadata
    sdist-v1/<index>/<package>/<version>.json  <- {pkg_info, pyproject}

An index pinned to one serialization gets its own listings directory,
since a stored body records nothing about which serialization it came from.

A versioned bucket name (``simple-v1``) gives zero-cost schema
migration: when the on-disk format changes, bump the suffix and the
old directory is harmless.

When the index serves PEP 503 HTML the stored body is nab's own rendering
of the page. A 304 revalidation keeps the old body, so changing that
rendering also needs the suffix bumped to reach warm caches.

A resolve writes two more buckets under the same root, holding upstream
source trees:

    vcs/vcs/<repo key>/<commit sha>/   <- shallow clone
    archive/<archive digest>/          <- extracted archive
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .atomic import atomic_write
from .serialization import SimpleSerialization

if TYPE_CHECKING:
    from collections.abc import Iterator

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
]


CACHE_VERSION_SIMPLE = "v1"
CACHE_VERSION_SIMPLE_NEG = "v0"
CACHE_VERSION_METADATA = "v1"
CACHE_VERSION_SDIST = "v1"

# Buckets of nab-written records. simple-neg-* is covered by the simple- prefix.
ENTRY_BUCKET_PREFIXES = ("simple-", "metadata-", "sdist-")

# Buckets a resolve fills with upstream source trees. nab owns the directories,
# not the files inside them.
VCS_BUCKET = "vcs"
ARCHIVE_BUCKET = "archive"
SOURCE_BUCKETS = (VCS_BUCKET, ARCHIVE_BUCKET)

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
    """

    fetched_at: int
    max_age: int
    etag: str | None

    def is_fresh(self, now: int | None = None) -> bool:
        """Return True if the entry is still within its freshness window."""
        current = int(time.time()) if now is None else now
        return current - self.fetched_at < self.max_age


def _is_entry_bucket(name: str) -> bool:
    """Whether ``name`` is a bucket of records nab writes and parses."""
    return any(name.startswith(prefix) for prefix in ENTRY_BUCKET_PREFIXES)


def is_recognized_bucket(name: str) -> bool:
    """Whether ``name`` is a bucket directory nab owns under a cache root."""
    return _is_entry_bucket(name) or name in SOURCE_BUCKETS


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
    """
    if component in ("", ".", "..") or component != Path(component).name:
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
    """File-per-key cache for Simple API and wheel metadata."""

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
        self._neg_dir = root / f"simple-neg-{CACHE_VERSION_SIMPLE_NEG}" / simple_index
        self._metadata_dir = root / f"metadata-{CACHE_VERSION_METADATA}" / self._index
        self._sdist_dir = root / f"sdist-{CACHE_VERSION_SDIST}" / self._index

    def _simple_paths(self, package: str) -> tuple[Path, Path]:
        segment = _require_single_segment(package)
        body = self._simple_dir / f"{segment}.json"
        policy = self._simple_dir / f"{segment}.policy"
        return (body, policy)

    def _neg_path(self, package: str) -> Path:
        segment = _require_single_segment(package)
        return self._neg_dir / f"{segment}.neg"

    def _sdist_path(self, package: str, version: str) -> Path:
        package_segment = _require_single_segment(package)
        version_segment = _require_single_segment(version)
        return self._sdist_dir / package_segment / f"{version_segment}.json"

    def _metadata_path(self, package: str, metadata_url: str) -> Path:
        """Return the file holding the sidecar published at ``metadata_url``.

        :pep:`658` attaches a sidecar to one file, so the wheels of a version
        each have their own. The URL is digested to keep the key a single
        path segment whatever path shape the index serves.
        """
        package_segment = _require_single_segment(package)
        digest = hashlib.sha256(metadata_url.encode("utf-8")).hexdigest()
        return self._metadata_dir / package_segment / f"{digest}.metadata"

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

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> None:
        """Write the body and the policy sidecar atomically."""
        body_path, policy_path = self._simple_paths(package)
        _atomic_write(body_path, body)
        _atomic_write(policy_path, _encode_policy(policy))

    def refresh_simple_policy(self, package: str, policy: CachePolicy) -> None:
        """Replace the policy sidecar without touching the body.

        Called after a 304 Not Modified, where the cached body is still
        valid but the freshness window has slid forward.
        """
        _, policy_path = self._simple_paths(package)
        _atomic_write(policy_path, _encode_policy(policy))

    def get_metadata(self, package: str, metadata_url: str) -> str | None:
        """Return the cached sidecar text for ``metadata_url``, or ``None``.

        A present file that is not valid UTF-8 is a corrupt entry: logged
        and treated as a miss. An absent file is a silent miss.
        """
        path = self._metadata_path(package, metadata_url)
        try:
            raw = path.read_bytes()
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
        _atomic_write(self._metadata_path(package, metadata_url), text.encode("utf-8"))

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
            return None
        try:
            doc = json.loads(raw)
            return (doc["pkg_info"], doc["pyproject"])
        except (ValueError, KeyError, TypeError):
            logger.warning(
                "Corrupt sdist cache record %s: not parseable; treating as a miss",
                path,
            )
            return None

    def put_sdist_files(
        self,
        package: str,
        version: str,
        pkg_info: str | None,
        pyproject_toml: str | None,
    ) -> None:
        """Write the ``(pkg_info, pyproject_toml)`` pair as one record."""
        _atomic_write(
            self._sdist_path(package, version),
            json.dumps({"pkg_info": pkg_info, "pyproject": pyproject_toml}).encode(
                "utf-8"
            ),
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
        _atomic_write(self._neg_path(package), _encode_policy(policy))

    def drop_negative(self, package: str) -> None:
        """Remove any negative entry for ``package``. A miss is not an error."""
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
        ``.neg`` decode as a policy, ``.metadata`` as UTF-8, ``.json`` as
        JSON (an sdist record also carries its two fields). Any other suffix
        is not a nab entry and is reported clean.
        """
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return f"unreadable: {exc.strerror or exc}"
        suffix = path.suffix
        if suffix in (".policy", ".neg"):
            return None if _decode_policy(raw) is not None else "policy not decodable"
        if suffix == ".metadata":
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                return "not valid UTF-8"
            return None
        if suffix == ".json":
            return self._read_json_reason(path, raw)
        return None

    def _read_json_reason(self, path: Path, raw: bytes) -> str | None:
        try:
            doc = json.loads(raw)
        except ValueError:
            return "not valid JSON"
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


def _encode_policy(policy: CachePolicy) -> bytes:
    return json.dumps(
        {
            "fetched_at": policy.fetched_at,
            "max_age": policy.max_age,
            "etag": policy.etag,
        }
    ).encode("utf-8")


def _decode_policy(policy_bytes: bytes) -> CachePolicy | None:
    try:
        doc = json.loads(policy_bytes)
        return CachePolicy(
            fetched_at=int(doc["fetched_at"]),
            max_age=int(doc["max_age"]),
            etag=doc.get("etag"),
        )
    except (ValueError, KeyError, TypeError):
        return None


class CacheBackend(Protocol):
    """Protocol shared by :class:`OnDiskCache` and :class:`NullCache`."""

    def get_simple(self, package: str) -> tuple[bytes, CachePolicy] | None:
        """Return ``(body_bytes, policy)`` if cached, else ``None``."""
        ...

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> None:
        """Store a Simple API body and its freshness policy."""
        ...

    def refresh_simple_policy(self, package: str, policy: CachePolicy) -> None:
        """Update the policy for an existing entry without rewriting the body."""
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
        """Discard the entry."""

    def refresh_simple_policy(self, package: str, policy: CachePolicy) -> None:
        """Discard the policy refresh."""

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
