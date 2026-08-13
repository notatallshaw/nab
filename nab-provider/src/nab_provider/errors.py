"""The exception types that cross module boundaries inside nab.

An exception is a declaration: the module that raises it and the module that
catches it both need the class, and neither can own it without the other
importing it.  Keeping them here means the config loader and the provider can
raise each other's errors without importing each other, the build subtree can
catch a provider error without importing the provider, and the resolving side
can name a fetch failure without importing an HTTP client.

:class:`ConfigError` lives here rather than in :mod:`nab_python.config`
for the same reason it used to live in :mod:`nab_python.config_sources`:
it is the base of every config-parse error, including
``SourceConfigError`` in the lowest config layer.  Both modules re-export
it under their own names.
"""

from __future__ import annotations

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
    """Raised when ``[tool.nab]`` configuration is invalid.

    The base for every config-parse error.  :mod:`nab_python.config` and
    :mod:`nab_python.config_sources` both re-export it as their public
    name, and hang their own subclasses (``SourceConfigError``,
    ``ConflictSelectionError``, :class:`OverrideConflictError`) off it.
    """


class OverrideConflictError(ConfigError):
    """A per-package and a per-index override set the same field for one candidate.

    Raised at resolve time when a candidate ``(package, version)`` served
    from an index is governed by both a per-package override (whose range
    contains the version) and a per-index override that each set the same
    policy field.  The two surfaces are deliberately not ranked, so an
    overlap is an error rather than a precedence call.
    """


class MissingExtraError(Exception):
    """Raised when a user-requested extra is not provided by the package."""


class MetadataError(Exception):
    """Raised when dependency metadata cannot be extracted."""


class UnsupportedSdistError(MetadataError):
    """Sdist or source tree needs a backend invocation the policy disallows.

    Raised when extraction would require a build the current
    :class:`BuildPolicy` (or its per-package override) does not permit:
    dynamic metadata under :attr:`BuildPolicy.NEVER`, a VCS clone under
    :attr:`BuildPolicy.BUILD_LOCAL`, or a remote sdist build failure
    under :attr:`BuildPolicy.BUILD_REMOTE`.  For a PyPI sdist it is
    caught by :meth:`Provider._look_ahead_ok`, so the resolver skips
    the version.  A declared source (local, VCS, archive, or workspace
    member) is read while listing its one version, so the error ends
    the resolve instead.
    """


class SourceBuildPolicyError(UnsupportedSdistError):
    """A declared source needs a backend run the effective policy refuses.

    Its own class because the two halves of the decision sit on opposite sides
    of the fetch port: the provider resolves the policy and counts the
    exclusion, and the host is the one that discovers the static read yielded
    nothing.  Callers that only care that the source is unusable catch
    :class:`UnsupportedSdistError` and see no difference.
    """


class ForeignMetadataError(MetadataError):
    """An index candidate's METADATA declares a different release.

    Core metadata ``Name`` and ``Version`` say which release an artifact is, so
    a candidate whose METADATA (or :pep:`658` sidecar) names another project or
    version describes some other release's dependencies.  Caught by
    :meth:`Provider._look_ahead_ok` so the resolver skips the version.
    """


class IncompatiblePythonError(MetadataError):
    """An index candidate's METADATA Requires-Python excludes the resolve target.

    The Simple-API ``requires-python`` hint is optional, so the listing gate
    admits a version whose listing omits it.  Once the wheel METADATA (or sdist
    PKG-INFO) is fetched, its authoritative ``Requires-Python`` is checked and
    an incompatible candidate is rejected.  Caught by
    :meth:`Provider._look_ahead_ok` so the resolver skips the version.
    """


# Deliberately not a MetadataError: _look_ahead_ok catches MetadataError
# and would silently reject the version; a naive upload-time is a hard error.
class InvalidUploadTimeError(Exception):
    """Raised when an index upload-time is not the timezone-aware UTC PEP 700 needs."""


# Deliberately not a MetadataError: _look_ahead_ok catches those and skips the
# version, but tie-ranked wheels that disagree on a target's dependencies are an
# ambiguity nab cannot resolve, so it must abort rather than drop the version.
class SiblingMetadataDivergenceError(Exception):
    """Raised when a version's tie-ranked wheels declare different target deps.

    nab reads one wheel's dependencies per version and treats it as
    authoritative, so a tie whose wheels declare different dependencies is an
    ambiguity: pinning from one silently disagrees with an install of the other.
    """


# Deliberately not a MetadataError: _look_ahead_ok catches those and skips the
# version, but a name mismatch is a misconfiguration that must abort.
class SourceNameMismatchError(Exception):
    """Raised when a materialised source's project name differs from its declaration.

    A local, VCS, or archive source maps a declared ``name`` to a directory,
    repo, or archive and becomes the only candidate for that package.  When the
    source's own ``[project].name`` does not canonicalise to the declared name,
    it provides a different distribution, so pinning it would carry the wrong
    version and dependencies.
    """


class IndexAccessError(Exception):
    """An index could not produce a usable answer.

    A remote index fails with :class:`HttpError`, a ``file://`` index with
    :class:`~nab_index.local_index.LocalIndexError`.  Catching this covers
    both without naming a backend.
    """


class HttpError(IndexAccessError):
    """A request failed, or answered with a status the caller cannot use.

    Transports raise this from ``get`` and ``raise_for_status`` so callers
    can handle index failures without importing a specific HTTP backend.
    """


class UnserveableUrlError(HttpError):
    """The index reached a verdict that it will not serve this URL.

    Raised for a client-error status the retry policy does not treat as a
    blip, so not a 408 or a 429: a 404 on an advertised PEP 658 sidecar, a
    403, a 410.  The status is a property of the URL, so asking again gets
    the same answer, and a caller may treat the artifact as unavailable.

    A 5xx that outlived the retry budget, and a connection that failed,
    stay a bare :class:`HttpError`.  Those say nothing about the URL, so a
    caller must not read them as a verdict.
    """


class MalformedSimpleResponseError(HttpError):
    """The index served a 200 response that is not a usable Simple-API body.

    Covers a listing that is neither valid JSON nor decodable HTML, and a
    PEP 658 metadata sidecar that is not valid UTF-8. Subclasses
    :class:`HttpError` so a broken body is caught alongside transport and
    4xx/5xx failures.
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
