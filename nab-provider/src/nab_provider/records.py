"""The value records a resolve is expressed in.

None of them fetches anything or is typed against a version or requirement
class, so the fetching side and the resolving side can both name them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from .serialization import SimpleSerialization

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
]

# Verification order; sha256 is pip's hash-checking baseline.
ACCEPTED_HASH_ALGORITHMS: tuple[str, ...] = ("sha256", "sha384", "sha512")


class _MetadataUrlMemo:
    """Slot holding :attr:`WheelFile.metadata_url` once it has been derived.

    A slot rather than a dataclass field keeps the memo out of ``fields()``, so
    it reaches neither equality, the repr, nor the pickled state. It sits on a
    base class because ``@dataclass(slots=True)`` rejects a class that declares
    ``__slots__`` in its own body.
    """

    __slots__ = ("_metadata_url",)

    _metadata_url: str


@dataclass(frozen=True, slots=True)
class WheelFile(_MetadataUrlMemo):
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


@dataclass(frozen=True, slots=True)
class SdistFile:
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
