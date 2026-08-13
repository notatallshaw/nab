"""The exception types that cross module boundaries inside nab-python.

An exception is a declaration: the module that raises it and the module
that catches it both need the class, and neither can own it without the
other importing it.  Keeping them here means the config loader and the
provider can raise each other's errors without importing each other, and
the build subtree can catch a provider error without importing the
provider.

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
    "IncompatiblePythonError",
    "InvalidUploadTimeError",
    "MetadataError",
    "MissingExtraError",
    "OverrideConflictError",
    "SiblingMetadataDivergenceError",
    "SourceNameMismatchError",
    "UnsupportedSdistError",
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
