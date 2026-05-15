"""On-disk cache for nab-index.

Stores PEP 691 Simple API responses (raw JSON body plus a sidecar
cache policy file) and PEP 658 wheel metadata (raw text, treated as
immutable). The cache is consulted by :class:`CachedAsyncSimpleClient`
before any HTTP transport call.

Layout under ``root``:

    simple-v0/<index>/<package>.json       <- raw PyPI JSON body
    simple-v0/<index>/<package>.policy     <- {fetched_at, max_age, etag}
    metadata-v0/<index>/<package>/<version>.metadata
    sdist-pkginfo-v0/<index>/<package>/<version>.txt

A versioned bucket name (``simple-v0``) gives zero-cost schema
migration: when the on-disk format changes, bump the suffix and the
old directory is harmless.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "CacheBackend",
    "CachePolicy",
    "NullCache",
    "OfflineError",
    "OnDiskCache",
]


CACHE_VERSION_SIMPLE = "v0"
CACHE_VERSION_METADATA = "v0"
CACHE_VERSION_SDIST = "v0"

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
    """RFC 9111-style freshness policy for one Simple API entry."""

    fetched_at: int
    max_age: int
    etag: str | None

    def is_fresh(self, now: int | None = None) -> bool:
        """Return True if the entry is still within its freshness window."""
        current = int(time.time()) if now is None else now
        return current - self.fetched_at < self.max_age


def _index_dirname(index_url: str) -> str:
    """Return a stable, filesystem-safe directory name for an index URL."""
    if index_url.rstrip("/") in DEFAULT_PYPI_URLS:
        return "pypi"
    return hashlib.sha256(index_url.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically via tempfile + replace.

    The temp file is created in the destination directory so the rename
    is a same-filesystem operation. A partial write or a crash leaves
    the target file untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _read_text(path: Path) -> str | None:
    """Return ``path``'s UTF-8 contents, or ``None`` if the file is absent."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


class OnDiskCache:
    """File-per-key cache for Simple API and wheel metadata."""

    def __init__(self, root: Path, index_url: str) -> None:
        """Create a cache rooted at ``root`` for ``index_url``."""
        self._root = root
        self._index = _index_dirname(index_url)
        self._simple_dir = root / f"simple-{CACHE_VERSION_SIMPLE}" / self._index
        self._metadata_dir = root / f"metadata-{CACHE_VERSION_METADATA}" / self._index
        self._sdist_dir = root / f"sdist-pkginfo-{CACHE_VERSION_SDIST}" / self._index
        self._sdist_pyproject_dir = (
            root / f"sdist-pyproject-{CACHE_VERSION_SDIST}" / self._index
        )

    def _simple_paths(self, package: str) -> tuple[Path, Path]:
        body = self._simple_dir / f"{package}.json"
        policy = self._simple_dir / f"{package}.policy"
        return (body, policy)

    def get_simple(self, package: str) -> tuple[bytes, CachePolicy] | None:
        """Return ``(body_bytes, policy)`` if cached, else ``None``."""
        body_path, policy_path = self._simple_paths(package)
        try:
            policy_bytes = policy_path.read_bytes()
            body = body_path.read_bytes()
        except OSError:
            return None
        try:
            policy_doc = json.loads(policy_bytes)
            policy = CachePolicy(
                fetched_at=int(policy_doc["fetched_at"]),
                max_age=int(policy_doc["max_age"]),
                etag=policy_doc.get("etag"),
            )
        except (ValueError, KeyError, TypeError):
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

    def get_metadata(self, package: str, version: str) -> str | None:
        """Return cached PEP 658 metadata text, or ``None`` on miss."""
        return _read_text(self._metadata_dir / package / f"{version}.metadata")

    def put_metadata(self, package: str, version: str, text: str) -> None:
        """Write PEP 658 metadata text. Treated as immutable."""
        _atomic_write(
            self._metadata_dir / package / f"{version}.metadata",
            text.encode("utf-8"),
        )

    def get_sdist_pkginfo(self, package: str, version: str) -> str | None:
        """Return cached sdist PKG-INFO text, or ``None`` on miss."""
        return _read_text(self._sdist_dir / package / f"{version}.txt")

    def put_sdist_pkginfo(self, package: str, version: str, text: str) -> None:
        """Write sdist PKG-INFO text. Treated as immutable."""
        _atomic_write(
            self._sdist_dir / package / f"{version}.txt", text.encode("utf-8")
        )

    def get_sdist_pyproject(self, package: str, version: str) -> str | None:
        """Return cached sdist pyproject.toml text, or ``None`` on miss."""
        return _read_text(self._sdist_pyproject_dir / package / f"{version}.toml")

    def put_sdist_pyproject(self, package: str, version: str, text: str) -> None:
        """Write sdist pyproject.toml text. Treated as immutable."""
        _atomic_write(
            self._sdist_pyproject_dir / package / f"{version}.toml",
            text.encode("utf-8"),
        )


def _encode_policy(policy: CachePolicy) -> bytes:
    return json.dumps(
        {
            "fetched_at": policy.fetched_at,
            "max_age": policy.max_age,
            "etag": policy.etag,
        }
    ).encode("utf-8")


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

    def get_metadata(self, package: str, version: str) -> str | None:
        """Return cached PEP 658 metadata text, or ``None`` on miss."""
        ...

    def put_metadata(self, package: str, version: str, text: str) -> None:
        """Store PEP 658 metadata text. Treated as immutable."""
        ...

    def get_sdist_pkginfo(self, package: str, version: str) -> str | None:
        """Return cached sdist PKG-INFO text, or ``None`` on miss."""
        ...

    def put_sdist_pkginfo(self, package: str, version: str, text: str) -> None:
        """Store sdist PKG-INFO text. Treated as immutable."""
        ...

    def get_sdist_pyproject(self, package: str, version: str) -> str | None:
        """Return cached sdist pyproject.toml text, or ``None`` on miss."""
        ...

    def put_sdist_pyproject(self, package: str, version: str, text: str) -> None:
        """Store sdist pyproject.toml text. Treated as immutable."""
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

    def get_metadata(self, package: str, version: str) -> str | None:
        """Return ``None`` (always a miss)."""

    def put_metadata(self, package: str, version: str, text: str) -> None:
        """Discard the entry."""

    def get_sdist_pkginfo(self, package: str, version: str) -> str | None:
        """Return ``None`` (always a miss)."""

    def put_sdist_pkginfo(self, package: str, version: str, text: str) -> None:
        """Discard the entry."""

    def get_sdist_pyproject(self, package: str, version: str) -> str | None:
        """Return ``None`` (always a miss)."""

    def put_sdist_pyproject(self, package: str, version: str, text: str) -> None:
        """Discard the entry."""
