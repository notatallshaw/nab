"""Validating declared sources and turning one into a synthetic listing.

A declared source becomes the only candidate for its package: PyPI is not
consulted.  The clone, the download and the directory read are the host's,
behind :meth:`~nab_provider.fetch_port.FetchPort.request_source_listing`; tree
metadata becomes a synthetic ``SdistFile``. A direct wheel retains its
``WheelFile`` record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from nab_provider.archive import ArchiveRequest
from nab_provider.archive_admission import UnsupportedArchiveError, admit_archive_url
from nab_provider.errors import UnsupportedWheelError
from nab_provider.records import DistFile, SdistFile, WheelFile

from ..errors import SourceNameMismatchError
from ..policy import DistPolicy
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
    *,
    wheel: WheelFile | None = None,
) -> list[tuple[Version, DistFile]]:
    """Produce a one-version listing for a materialised source."""
    actual = canonicalize_name(metadata.name)
    if actual != normalized:
        msg = (
            f"{descriptor} declares package {normalized!r} but the project at"
            f" {path} is named {actual!r}; a source declared for one name must"
            f" not provide a different project"
        )
        raise SourceNameMismatchError(msg)

    if wheel is not None:
        provider.metadata_cache[(normalized, metadata.version)] = metadata
        return [(metadata.version, wheel)]

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


def validate_archive_source(provider: Provider, source: ArchiveSource) -> None:
    """Check acquisition policy and add source context to refusals."""
    try:
        admit_archive_url(
            source.url,
            provider.archive_config,
            declared=(canonicalize_name(source.name), source.url)
            in provider.declared_sources,
        )
    except UnsupportedArchiveError as exc:
        msg = f"{source.descriptor}: {exc}"
        raise UnsupportedArchiveError(msg) from exc


def validate_archive_wheel(provider: Provider, source: ArchiveSource) -> None:
    """Refuse a declared wheel the target cannot use before requesting its bytes."""
    filename = ArchiveRequest.parse(source.url).filename
    if not filename.endswith(".whl"):
        return
    try:
        wheel_name, version, _, _ = parse_wheel_filename(filename)
    except InvalidWheelFilename as exc:
        msg = f"{source.descriptor}: {exc}"
        raise UnsupportedWheelError(msg) from exc
    if wheel_name != canonicalize_name(source.name):
        msg = f"{source.descriptor}: wheel filename declares {wheel_name!r}"
        raise SourceNameMismatchError(msg)
    tags = provider.wheel_tags
    if tags is not None and not tags.accepts(filename):
        msg = f"{source.descriptor}: wheel {filename!r} is incompatible with target"
        raise UnsupportedWheelError(msg)
    if provider.effective_dist_policy(canonicalize_name(source.name), version) in {
        DistPolicy.SDIST_ONLY,
        DistPolicy.SDIST_INSTALL,
    }:
        msg = f"{source.descriptor}: wheel {filename!r} conflicts with sdist policy"
        raise UnsupportedWheelError(msg)


def index_vcs_sources(
    provider: Provider,
    sources: list[VcsSource],
) -> dict[str, VcsSource]:
    """Register VCS sources, deferring unused standard entries' permission checks."""
    if not sources:
        return {}

    native_sources = [
        src
        for src in sources
        if canonicalize_name(src.name) not in provider.redirected_dependencies
    ]
    if (
        any(
            (canonicalize_name(src.name), src.url) not in provider.declared_sources
            for src in native_sources
        )
        and provider.vcs_config.policy is not VcsPolicy.ALLOW
    ):
        msg = (
            "vcs_sources require VcsPolicy.ALLOW; current policy is"
            f" {provider.vcs_config.policy.value}.  Set vcs_config to"
            " a permissive VcsConfig before declaring sources."
        )
        raise ValueError(msg)

    out: dict[str, VcsSource] = {}
    for src in sources:
        canonical = canonicalize_name(src.name)
        if canonical not in provider.redirected_dependencies:
            admit_vcs_url(
                src.url,
                provider.vcs_config,
                declared=(canonical, src.url) in provider.declared_sources,
            )
        if canonical in out or canonical in provider.local_sources:
            msg = f"duplicate source declared for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out


def index_archive_sources(
    provider: Provider,
    sources: list[ArchiveSource],
) -> dict[str, ArchiveSource]:
    """Register sources, checking acquisition permissions for native declarations."""
    if not sources:
        return {}
    out: dict[str, ArchiveSource] = {}
    for src in sources:
        canonical = canonicalize_name(src.name)
        if canonical not in provider.redirected_dependencies:
            validate_archive_source(provider, src)
        if (
            canonical in out
            or canonical in provider.local_sources
            or canonical in provider.vcs_sources
        ):
            msg = f"duplicate source declared for {src.name!r}"
            raise ValueError(msg)
        out[canonical] = src
    return out
