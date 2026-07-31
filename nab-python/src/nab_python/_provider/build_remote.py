"""BUILD_REMOTE path: fetch, extract, and build a remote sdist.

Invoked from :func:`resolve_dynamic_sdist` when neither the
:pep:`643` static-deps path nor the bundled ``pyproject.toml``
fallback yields usable dependency metadata, and the effective
:class:`~nab_python.provider.BuildPolicy` for the package is
:attr:`~nab_python.provider.BuildPolicy.BUILD_REMOTE`.

A failure here raises :class:`~nab_python.provider.UnsupportedSdistError`
so :func:`nab_python._provider.lookahead.look_ahead_ok` can skip the
version.  If every candidate fails the resolver surfaces the
accumulated reasons as a no-version-satisfies error: invoking
``BUILD_REMOTE`` does not turn a broken sdist into a usable one,
it just turns silence into a real diagnostic.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from nab_index.client import SdistFile, WheelFile, extract_sdist_archive

from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .._vendor.packaging.version import Version
    from ..metadata import WheelMetadata
    from ..provider import Provider


def build_remote_sdist(
    provider: Provider,
    package: str,
    version: Version,
) -> WheelMetadata:
    """Download the sdist for ``(package, version)``, extract, and build.

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
    # Late imports: ``provider`` imports this module at module load.
    from .. import build_backend
    from ..build_backend import BuildBackendError
    from ..provider import UnsupportedSdistError

    canonical = canonicalize_name(package)
    versions = provider.versions_cache.get(canonical, [])
    sdist = _find_sdist(versions, version)
    if sdist is None:
        msg = (
            f"{package}=={version} build-remote requested but no sdist"
            " is available in the listing"
        )
        raise UnsupportedSdistError(msg)

    ver_str = str(version)
    event = provider.coordinator.request_sdist_archive(
        canonical, ver_str, sdist.url, sdist.hashes
    )
    event.wait()
    integrity_error = provider.coordinator.index.get_sdist_archive_error(
        canonical, ver_str
    )
    if integrity_error is not None:
        raise integrity_error
    data = provider.coordinator.index.get_sdist_archive(canonical, ver_str)
    if data is None:
        msg = (
            f"{package}=={version} build-remote requested but sdist archive"
            f" fetch from {sdist.url} failed"
        )
        raise UnsupportedSdistError(msg)

    with tempfile.TemporaryDirectory(prefix="nab-build-remote-") as td:
        try:
            source_dir = extract_sdist_archive(data, Path(td))
        except ValueError as exc:
            msg = f"{package}=={version} sdist archive could not be extracted: {exc}"
            raise UnsupportedSdistError(msg) from exc
        try:
            built = build_backend.extract_metadata(
                source_dir,
                config=provider.build_config,
                offline=provider.coordinator.offline,
            )
        except BuildBackendError as exc:
            msg = f"{package}=={version} build-remote backend failed: {exc}"
            raise UnsupportedSdistError(msg) from exc

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
            f" resolve targets Python {target.python_full_version}"
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


def _find_sdist(
    versions: Sequence[tuple[Version, WheelFile | SdistFile]],
    version: Version,
) -> SdistFile | None:
    for v, d in versions:
        if v == version and isinstance(d, SdistFile):
            return d
    return None
