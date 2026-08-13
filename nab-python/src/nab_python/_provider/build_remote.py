"""BUILD_REMOTE path: pick the sdist to build, and check what came back.

Invoked from :func:`resolve_dynamic_sdist` when neither the
:pep:`643` static-deps path nor the bundled ``pyproject.toml``
fallback yields usable dependency metadata, and the effective
:class:`~nab_python.provider.BuildPolicy` for the package is
:attr:`~nab_python.provider.BuildPolicy.BUILD_REMOTE`.

The build itself is the host's, behind
:meth:`~nab_python.fetch_port.FetchPort.request_built_metadata`.  What is left
here is the part that needs provider state: which sdist of the listing to
build, and whether what the build declared is the candidate that was asked for.

A failure here raises :class:`~nab_python.provider.UnsupportedSdistError`
so :func:`nab_python._provider.lookahead.look_ahead_ok` can skip the
version.  If every candidate fails the resolver surfaces the
accumulated reasons as a no-version-satisfies error: invoking
``BUILD_REMOTE`` does not turn a broken sdist into a usable one,
it just turns silence into a real diagnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name
from ..errors import UnsupportedSdistError
from .metadata_resolver import find_sdist

if TYPE_CHECKING:
    from .._vendor.packaging.version import Version
    from ..metadata import WheelMetadata
    from ..provider import Provider


def build_remote_sdist(
    provider: Provider,
    package: str,
    version: Version,
) -> WheelMetadata:
    """Have the host build ``(package, version)``, and check what it declared.

    ``package`` is the canonical package name; ``version`` matches an
    entry in ``provider.versions_cache``.  A built sdist whose
    ``Requires-Python`` excludes the resolve target is rejected, since the
    Simple-API listing filter only sees the listing's own (possibly absent)
    Requires-Python, not the value the build produces.  A per-package
    ``requires-python`` metadata override substitutes for the built value
    here, matching the listing gate.  A built sdist whose declared name or
    version disagrees with the requested candidate is also rejected rather
    than used for the wrong package.
    """
    canonical = canonicalize_name(package)
    versions = provider.versions_cache.get(canonical, [])
    sdist = find_sdist(versions, version)
    if sdist is None:
        msg = (
            f"{package}=={version} build-remote requested but no sdist"
            " is available in the listing"
        )
        raise UnsupportedSdistError(msg)

    ver_str = str(version)
    event = provider.coordinator.request_built_metadata(
        canonical, ver_str, sdist.url, sdist.hashes
    )
    event.wait()
    built = provider.coordinator.index.get_built_metadata(canonical, ver_str)
    # The port answers inline and raises on failure, so a request that returned
    # has left the metadata behind.
    assert built is not None

    target = provider.target
    override_rp = provider.effective_requires_python(canonical, version)
    spec = (
        SpecifierSet(override_rp) if override_rp is not None else built.requires_python
    )
    if (
        spec is not None
        and target is not None
        and not target.admits_requires_python(spec)
    ):
        msg = (
            f"{package}=={version} built sdist requires Python {spec} but the"
            f" resolve targets Python {target.python_version}"
        )
        raise UnsupportedSdistError(msg)
    if canonicalize_name(built.name) != canonical or built.version != version:
        msg = (
            f"{package}=={version} built sdist declares"
            f" {built.name}=={built.version}, which does not match the"
            " requested candidate"
        )
        raise UnsupportedSdistError(msg)
    return built
