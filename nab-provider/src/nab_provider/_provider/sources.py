"""Validating declared sources and turning one into a synthetic listing.

A declared source becomes the only candidate for its package: PyPI is not
consulted.  The clone, the download and the directory read are the host's,
behind :meth:`~nab_provider.fetch_port.FetchPort.request_source_listing`; what
comes back becomes one synthetic ``SdistFile`` whose version the host read
from ``[project].version`` or took from the build backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.archive import ArchiveRequest
from nab_provider.records import SdistFile

from ..errors import SourceNameMismatchError
from ..vcs_admission import VcsPolicy, admit_vcs_url

if TYPE_CHECKING:
    from pathlib import Path

    from nab_provider._vendor.packaging.version import Version

    from ..metadata import WheelMetadata
    from ..policy import ArchiveSource, LocalSource, VcsSource
    from ..provider import Provider


def index_local_sources(
    provider: Provider,  # noqa: ARG001  (signature parity with index_vcs_sources)
    sources: list[LocalSource],
) -> dict[str, LocalSource]:
    """Validate ``LocalSource`` entries and return a canonical-name map.

    Admitted at every :class:`~nab_provider.provider.BuildPolicy` level; the
    policy only governs whether the backend may run when the static
    pyproject read returns nothing usable.
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


def seed_synthetic_listing(
    provider: Provider,
    normalized: str,
    path: Path,
    metadata: WheelMetadata,
    descriptor: str,
) -> list[tuple[Version, SdistFile]]:
    """Produce a one-version listing for a materialised source."""
    actual = canonicalize_name(metadata.name)
    if actual != normalized:
        msg = (
            f"{descriptor} declares package {normalized!r} but the project at"
            f" {path} is named {actual!r}; a source declared for one name must"
            f" not provide a different project"
        )
        raise SourceNameMismatchError(msg)

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
        local_path=path,
    )
    version = metadata.version
    provider.metadata_cache[(normalized, version)] = metadata
    return [(version, synthetic_file)]


def index_vcs_sources(
    provider: Provider,
    sources: list[VcsSource],
) -> dict[str, VcsSource]:
    """Validate VCS sources and return a canonical-name map.

    Admitted at every :class:`~nab_provider.provider.BuildPolicy` level, like a
    local source.  ``VcsPolicy.BLOCK`` still refuses any declaration up-front:
    whether VCS fetching is permitted at all is a separate decision.

    Each URL is passed through :func:`admit_vcs_url` so the scheme, repo, and
    pin allowlists apply to ``[[tool.nab.vcs-sources]]`` just like project-root
    direct-URL requirements.
    """
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


def index_archive_sources(
    provider: Provider,
    sources: list[ArchiveSource],
) -> dict[str, ArchiveSource]:
    """Validate archive sources and return a canonical-name map.

    Admitted at every :class:`~nab_provider.provider.BuildPolicy` level, like a
    local source.  There is no ``VcsPolicy``-style gate: the download is
    hash-verified, and which archive URLs are permitted is decided at config
    parse.
    """
    if not sources:
        return {}
    out: dict[str, ArchiveSource] = {}
    for src in sources:
        # Guarantee a usable hash at the provider layer (config parse already
        # checks it, but a directly-built Provider would otherwise IndexError
        # when materialisation reads the first digest, or verify an empty one).
        if not ArchiveRequest.parse(src.url).has_usable_hash:
            msg = f"archive source {src.name!r} has no hash in its URL: {src.url!r}"
            raise ValueError(msg)

        canonical = canonicalize_name(src.name)
        if (
            canonical in out
            or canonical in provider.local_sources
            or canonical in provider.vcs_sources
        ):
            msg = f"duplicate source declared for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out
