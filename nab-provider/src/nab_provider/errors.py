"""The exception types that cross module boundaries inside nab.

The raising module and the catching module both need the class, so neither
can own it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nab_provider._vendor.packaging.version import Version

__all__ = [
    "ConfigError",
    "ForeignMetadataError",
    "HttpError",
    "IncompatiblePythonError",
    "IndexAccessError",
    "InvalidUploadTimeError",
    "MalformedSimpleResponseError",
    "MetadataError",
    "MetadataHashMismatchError",
    "MissingExtraError",
    "OverrideConflictError",
    "SdistHashMismatchError",
    "SiblingMetadataDivergenceError",
    "SourceBuildPolicyError",
    "SourceNameMismatchError",
    "UnserveableUrlError",
    "UnsupportedSdistError",
    "UnsupportedWheelError",
    "WheelHashMismatchError",
]


class ConfigError(ValueError):
    """Raised when ``[tool.nab]`` configuration is invalid."""


class OverrideConflictError(ConfigError):
    """A per-package and a per-index override set the same field for one candidate.

    Raised at resolve time, since it depends on the candidate's version and on
    the index serving it.  The two surfaces are not ranked, so an overlap is an
    error rather than a precedence call.
    """


class MissingExtraError(Exception):
    """Raised when a user-requested extra is not provided by the package."""


class MetadataError(Exception):
    """Raised when dependency metadata cannot be extracted.

    ``_look_ahead_ok`` treats it as a rejection and moves to the next version,
    so anything that has to end the resolve must not subclass it.

    ``filtered_sdist_version`` marks the one failure the report can say more
    about than the message does: the metadata ladder wanted an sdist the
    listing filter had removed.  Naming the rung that removed it means
    walking the listing, so the marker travels instead and the report walks
    only if the resolve goes on to fail.
    """

    filtered_sdist_version: Version | None = None


class UnsupportedSdistError(MetadataError):
    """An sdist or source tree yielded no dependency metadata.

    Raised for a build the effective :class:`BuildPolicy` refuses, and for any
    other failure to get metadata out of the artifact or tree.
    """


class SourceBuildPolicyError(UnsupportedSdistError):
    """A declared source needs a backend run the effective policy refuses.

    Its own class because the decision is split across the fetch port: the
    provider resolves the policy, the host finds the static read empty.
    """


class ForeignMetadataError(MetadataError):
    """An index candidate's METADATA names another project or version.

    ``Name`` and ``Version`` say which release an artifact is, so the
    dependency list beside them is that other release's.
    """


class IncompatiblePythonError(MetadataError):
    """An index candidate's METADATA Requires-Python excludes the resolve target.

    The Simple-API ``requires-python`` hint is optional, so the listing gate
    admits a version whose listing omits it; the fetched wheel METADATA (or
    sdist PKG-INFO) carries the authoritative value.
    """


class InvalidUploadTimeError(Exception):
    """Raised when an index upload-time is not the timezone-aware UTC PEP 700 needs."""


class SiblingMetadataDivergenceError(Exception):
    """Raised when a version's tie-ranked wheels declare different target deps.

    nab reads one wheel's dependencies per version, so a tie whose wheels
    disagree is an ambiguity: pinning from one silently contradicts an install
    of the other.
    """


class SourceNameMismatchError(Exception):
    """Raised when a materialised source's project name differs from its declaration.

    A declared source is the only candidate for the package it names, so a tree
    whose project name does not canonicalise to that name would pin another
    distribution.
    """


class IndexAccessError(Exception):
    """An index could not produce a usable answer.

    A remote index fails with :class:`HttpError`, a ``file://`` index with
    :class:`~nab_index.local_index.LocalIndexError`.
    """


class HttpError(IndexAccessError):
    """A request failed, or answered with a status the caller cannot use."""


class UnserveableUrlError(HttpError):
    """The index will not serve this URL, and asking again gets the same answer.

    Raised for a client-error status the retry policy does not treat as a blip,
    so not a 408 or a 429: a 404 on an advertised PEP 658 sidecar, a 403, a 410.
    A 5xx that outlived the retry budget, and a connection that failed, stay a
    bare :class:`HttpError`.
    """


class MalformedSimpleResponseError(HttpError):
    """The index served a 200 response that is not a usable Simple-API body.

    Covers a listing body that neither the JSON nor the HTML decoder will
    take, and a PEP 658 metadata sidecar that is not valid UTF-8.
    """


class MetadataHashMismatchError(Exception):
    """Fetched PEP 658 metadata did not match its published hash."""


class SdistHashMismatchError(Exception):
    """A fetched sdist archive did not match its published hash."""


class WheelHashMismatchError(Exception):
    """A range-recovered wheel's bytes did not match its published hash."""


class UnsupportedWheelError(Exception):
    """A wheel's ``.dist-info`` contradicts its own filename.

    Raised when a wheel carries more than one top-level ``.dist-info``
    directory, or a single one whose name does not canonicalise to the
    distribution named by the wheel's filename.
    """
