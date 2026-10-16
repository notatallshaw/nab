"""Permission to fetch explicitly selected archive and wheel URLs."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from urllib.parse import SplitResult, unquote, urlsplit

from .archive import ArchiveRequest, ArchiveRequestError, is_archive_url
from .errors import ConfigError

__all__ = [
    "ArchiveConfig",
    "ArchivePolicy",
    "UnsupportedArchiveError",
    "admit_archive_url",
    "validate_archive_prefix",
]


_ASCII_SPACE = 32
_ASCII_DEL = 127


class ArchivePolicy(enum.Enum):
    """Whether direct archive acquisition is permitted."""

    DECLARED = "declared"
    BLOCK = "block"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    """Immutable acquisition permissions shared by providers."""

    policy: ArchivePolicy = ArchivePolicy.DECLARED
    allowed_urls: tuple[str, ...] = ()
    require_hashes: bool = True


class UnsupportedArchiveError(ConfigError):
    """An archive URL was refused before acquisition."""


def _location(url: str) -> SplitResult:
    """Parse an absolute archive location without transport path rewriting."""
    parts = urlsplit(url)
    _ = parts.port
    path = unquote(parts.path)
    if (
        not is_archive_url(url)
        or (parts.path and not parts.path.startswith("/"))
        or (parts.scheme == "file" and not parts.path.startswith("/"))
        or (parts.scheme in {"http", "https"} and parts.hostname in {None, ""})
        or any(
            ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DEL
            for character in url
        )
        or any(
            ord(character) < _ASCII_SPACE or ord(character) == _ASCII_DEL
            for character in path
        )
        or "\\" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        msg = (
            "expected an absolute http(s) or file URL "
            "without dot segments or backslashes"
        )
        raise ValueError(msg)
    return parts


def validate_archive_prefix(prefix: str) -> None:
    """Require allowlists to name locations rather than credentials or queries."""
    parts = _location(prefix)
    if parts.username is not None or parts.query or parts.fragment:
        msg = (
            "archive.allowed-urls entries must omit credentials, queries and fragments"
        )
        raise ValueError(msg)


def _origin(parts: SplitResult) -> tuple[str, str, int | None]:
    """Compare hosts without credentials and with default HTTP ports expanded."""
    port = parts.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, parts.hostname or "", port


def _matches_prefix(parts: SplitResult, prefix: str) -> bool:
    """Match a permitted URL at an origin and path-component boundary."""
    validate_archive_prefix(prefix)
    allowed = _location(prefix)
    if _origin(parts) != _origin(allowed):
        return False
    path, prefix_path = unquote(parts.path), unquote(allowed.path)
    return path == prefix_path or path.startswith(prefix_path.rstrip("/") + "/")


def admit_archive_url(
    url: str, config: ArchiveConfig, *, declared: bool = False
) -> ArchiveRequest:
    """Check location permission and expected hashes before acquisition."""
    if config.policy is ArchivePolicy.BLOCK:
        msg = (
            'archive.policy="block" refuses archive and wheel sources; '
            'set [tool.nab.archive] policy="declared" to permit selected sources'
        )
        raise UnsupportedArchiveError(msg)
    if config.policy is ArchivePolicy.DECLARED and not declared:
        msg = (
            'archive.policy="declared" requires a selected source; '
            "declare the URL in [develop.sources] or a project dependency. "
            "For backend or library URLs, set [tool.nab.archive] "
            'policy="allow" and allowed-urls explicitly'
        )
        raise UnsupportedArchiveError(msg)
    try:
        request = ArchiveRequest.parse(url)
        parts = _location(request.url)
        allowed = declared or any(
            _matches_prefix(parts, prefix) for prefix in config.allowed_urls
        )
    except ArchiveRequestError as exc:
        msg = "archive URL has invalid hashes or subdirectory; check its fragment"
        raise UnsupportedArchiveError(msg) from exc
    except ValueError as exc:
        msg = f"invalid archive location or archive.allowed-urls: {exc}"
        raise UnsupportedArchiveError(msg) from exc
    if not allowed:
        msg = (
            "archive.allowed-urls does not permit this location; "
            "add the intended URL or directory prefix to "
            "[tool.nab.archive].allowed-urls"
        )
        raise UnsupportedArchiveError(msg)
    if any(not digest for _, digest in request.hashes):
        msg = "an expected archive hash cannot be empty"
        raise UnsupportedArchiveError(msg)
    if config.require_hashes and not request.has_usable_hash:
        msg = (
            "archive.require-hashes=true requires an expected digest; "
            'add hashes={sha256="<hex>"} to the source or #sha256=<hex> to its URL. '
            "To accept an observed digest instead, explicitly set "
            "[tool.nab.archive] require-hashes=false"
        )
        raise UnsupportedArchiveError(msg)
    return request
