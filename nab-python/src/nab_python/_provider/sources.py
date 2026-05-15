"""Local-source and VCS-source materialisation for the provider.

A ``LocalSource`` becomes the only candidate for a package: PyPI is
not consulted.  A ``VcsSource`` clones the repo and reuses the
``LocalSource`` extraction path.  Both produce a single synthetic
``SdistFile`` whose version is read from ``[project].version``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nab_index.client import SdistFile
from nab_index.vcs import VcsCloneError, VcsRequest

from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from .._vendor.packaging.version import Version
    from ..metadata import WheelMetadata
    from ..provider import LocalSource, Provider, VcsSource


def index_local_sources(
    provider: Provider,  # noqa: ARG001  (signature parity with index_vcs_sources)
    sources: list[LocalSource],
) -> dict[str, LocalSource]:
    """Validate ``LocalSource`` entries and return a canonical-name map.

    Admitted at every :class:`~nab_python.provider.BuildPolicy` level; the
    policy only governs whether the backend may run when the static
    pyproject read returns nothing usable (see
    :func:`extract_source_metadata`).
    """
    if not sources:
        return {}
    out: dict[str, LocalSource] = {}
    for src in sources:
        canonical = canonicalize_name(src.name)
        if canonical in out:
            msg = f"duplicate local source for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out


def materialize_local_source(
    provider: Provider,
    normalized: str,
    source: LocalSource,
) -> list[tuple[Version, SdistFile]]:
    """Read metadata from ``source`` and seed caches with one synthetic version.

    Static path: ``extract_static_metadata`` reads ``pyproject.toml``
    directly.  Backend path: requires :attr:`BuildPolicy.BUILD_LOCAL`
    or looser; raises :class:`UnsupportedSdistError` otherwise.
    """
    path = Path(source.path)
    metadata = extract_source_metadata(
        provider,
        path,
        descriptor=f"local source {source.name!r}",
        package=canonicalize_name(source.name),
        kind="local",
    )
    return seed_synthetic_listing(provider, normalized, path, metadata)


def extract_source_metadata(
    provider: Provider,
    path: Path,
    *,
    descriptor: str,
    package: str,
    kind: str,
) -> WheelMetadata:
    """Read metadata from a directory; gates the backend path on policy.

    ``kind`` is ``"local"`` for :class:`LocalSource` directories
    (admitted at :attr:`BuildPolicy.BUILD_LOCAL` and above) or
    ``"vcs"`` for :class:`VcsSource` clones (admitted only at
    :attr:`BuildPolicy.BUILD_REMOTE`).
    """
    # Module-level attribute access lets tests patch
    # ``nab_python.build_backend.extract_metadata`` from the source.
    from .. import build_backend
    from ..build_backend import BuildBackendError, extract_static_metadata
    from ..provider import BuildPolicy, UnsupportedSdistError

    metadata = extract_static_metadata(path)
    if metadata is not None:
        return metadata
    effective = provider.effective_build_policy(package)
    if kind == "local":
        allowed = {BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE}
        minimum = BuildPolicy.BUILD_LOCAL
    else:
        allowed = {BuildPolicy.BUILD_REMOTE}
        minimum = BuildPolicy.BUILD_REMOTE
    if effective not in allowed:
        provider.stats.excluded_by_build_policy += 1
        msg = (
            f"{descriptor} at {path} has dynamic metadata; building requires"
            f" BuildPolicy.{minimum.name} but the effective policy is"
            f" {effective.value}"
        )
        raise UnsupportedSdistError(msg)
    try:
        return build_backend.extract_metadata(
            path,
            config=provider.build_config,
            python_version=provider.python_version,
        )
    except BuildBackendError as exc:
        msg = f"{descriptor}: {exc}"
        raise UnsupportedSdistError(msg) from exc


def seed_synthetic_listing(
    provider: Provider,
    normalized: str,
    path: Path,
    metadata: WheelMetadata,
) -> list[tuple[Version, SdistFile]]:
    """Produce a one-version listing for a materialised source."""
    synthetic_file = SdistFile(
        filename=f"{normalized}-{metadata.version}.tar.gz",
        url=path.as_uri(),
        version=str(metadata.version),
        requires_python=(
            str(metadata.requires_python)
            if metadata.requires_python is not None
            else None
        ),
        upload_time=None,
    )
    version = metadata.version
    provider.metadata_cache[(normalized, version)] = metadata
    return [(version, synthetic_file)]


def index_vcs_sources(
    provider: Provider,
    sources: list[VcsSource],
) -> dict[str, VcsSource]:
    """Validate VCS sources and return a canonical-name map.

    Admitted at every :class:`~nab_python.provider.BuildPolicy` level; the
    policy only governs whether the backend may run on the clone (see
    :func:`extract_source_metadata`).  ``VcsPolicy.BLOCK`` still refuses
    any declaration up-front because that is an independent decision
    about whether VCS fetching is permitted at all.

    Each URL is passed through :func:`admit_vcs_url` so the scheme,
    repo, and pin allowlists apply to ``[[tool.nab.vcs-sources]]``
    just like project-root direct-URL requirements.
    """
    # Late import: ``pypi`` imports this module at module load.
    from .._vcs_admission import admit_vcs_url
    from ..provider import VcsPolicy

    if not sources:
        return {}

    if provider.vcs_config.policy is VcsPolicy.BLOCK:
        msg = (
            "vcs_sources require VcsPolicy.ALLOW; current policy is"
            f" {provider.vcs_config.policy.value}.  Set vcs_config to"
            " a permissive VcsConfig before declaring sources."
        )
        raise ValueError(msg)

    out: dict[str, VcsSource] = {}
    for src in sources:
        admit_vcs_url(src.url, provider.vcs_config)
        canonical = canonicalize_name(src.name)
        if canonical in out or canonical in provider.local_sources:
            msg = f"duplicate source declared for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out


def materialize_vcs_source(
    provider: Provider,
    normalized: str,
    source: VcsSource,
) -> list[tuple[Version, SdistFile]]:
    """Clone ``source`` and materialise it via the same path as a LocalSource."""
    # Module-level attribute access lets tests patch
    # ``nab_index.vcs.prepare_clone`` from the source.
    from nab_index import vcs as _vcs

    from ..provider import UnsupportedSdistError

    if provider.vcs_cache_dir is None:
        msg = (
            f"vcs source {source.name!r} declared but no"
            f" vcs_cache_dir was supplied to Provider"
        )
        raise UnsupportedSdistError(msg)
    try:
        request = VcsRequest.parse(source.url)
        clone = _vcs.prepare_clone(
            provider.vcs_cache_dir,
            request,
            require_pin=provider.vcs_config.require_pin,
        )
    except VcsCloneError as exc:
        msg = f"vcs source {source.name!r}: {exc}"
        raise UnsupportedSdistError(msg) from exc
    provider.vcs_pins[canonicalize_name(source.name)] = clone.commit_sha
    path = clone.path / clone.subdirectory if clone.subdirectory else clone.path
    metadata = extract_source_metadata(
        provider,
        path,
        descriptor=f"vcs source {source.name!r}",
        package=canonicalize_name(source.name),
        kind="vcs",
    )
    return seed_synthetic_listing(provider, normalized, path, metadata)
