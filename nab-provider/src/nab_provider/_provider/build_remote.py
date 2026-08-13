"""BUILD_REMOTE path: pick the sdist to build, and check what came back.

Invoked from :func:`resolve_dynamic_sdist` when neither the
:pep:`643` static-deps path nor the bundled ``pyproject.toml``
fallback yields usable dependency metadata, and the effective
:class:`~nab_provider.provider.BuildPolicy` for the package is
:attr:`~nab_provider.provider.BuildPolicy.BUILD_REMOTE`.

The build itself is the host's, behind
:meth:`~nab_provider.fetch_port.FetchPort.request_built_metadata`.

A failure here raises :class:`~nab_provider.provider.UnsupportedSdistError`
so :func:`nab_provider._provider.lookahead.look_ahead_ok` can skip the version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.utils import canonicalize_name

from ..errors import UnsupportedSdistError
from .metadata_resolver import find_sdist

if TYPE_CHECKING:
    from nab_provider._vendor.packaging.version import Version

    from ..metadata import WheelMetadata
    from ..provider import Provider


def build_remote_sdist(
    provider: Provider,
    package: str,
    version: Version,
) -> WheelMetadata:
    """Have the host build ``(package, version)``, and check what it declared.

    ``package`` is the canonical package name; ``version`` matches an entry in
    ``provider.versions_cache``.

    A built sdist whose ``Requires-Python`` excludes the resolve target is
    rejected, since the Simple-API listing filter only sees the listing's own
    (possibly absent) Requires-Python, not the value the build produces.  A
    per-package ``requires-python`` metadata override substitutes for the built
    value here, matching the listing gate.  A built sdist whose declared name
    or version disagrees with the requested candidate is rejected too.
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

    # The port raises on failure, so a request that returned left the metadata.
    built = provider.coordinator.index.get_built_metadata(canonical, ver_str)
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
