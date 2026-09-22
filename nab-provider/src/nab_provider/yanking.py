"""Withdrawal metadata carried only by yanked distribution records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Iterable

from .records import SdistFile, WheelFile, defer_hashes, defer_sidecar_hash


# Explicit slots avoid Python 3.10 duplicating inherited record fields.
@dataclass(frozen=True, kw_only=True)
class YankedWheelFile(WheelFile):
    """A withdrawn wheel and its optional reason."""

    __slots__ = ("yanked",)

    yanked: bool | str


@dataclass(frozen=True, kw_only=True)
class YankedSdistFile(SdistFile):
    """A withdrawn source distribution and its optional reason."""

    __slots__ = ("yanked",)

    yanked: bool | str


class YankedListing(list[WheelFile | SdistFile]):
    """A parsed listing that carries withdrawal state with its records."""

    __slots__ = ("withdrawn_versions",)
    has_yanked_files: ClassVar[bool] = True

    def __init__(
        self,
        files: list[WheelFile | SdistFile],
        *,
        withdrawn_versions: Iterable[str] | None = None,
    ) -> None:
        """Record withdrawn versions, deriving them when no sparse index is supplied."""
        super().__init__(files)
        self.withdrawn_versions = (
            {file.version for file in files if file.yanked}
            if withdrawn_versions is None
            else set(withdrawn_versions)
        )


def append_yanked_file(
    files: list[WheelFile | SdistFile],
    file: WheelFile | SdistFile,
    *,
    reason: bool | str,
) -> YankedListing:
    """Retain a withdrawn record and mark the listing without scanning live files."""
    result = (
        files
        if isinstance(files, YankedListing)
        else YankedListing(files, withdrawn_versions=())
    )
    result.withdrawn_versions.add(file.version)
    result.append(mark_yanked(file, reason=reason))
    return result


def mark_yanked(
    file: WheelFile | SdistFile, *, reason: bool | str
) -> WheelFile | SdistFile:
    """Copy one record while keeping its integrity tables deferred."""
    raw_hashes = file.raw_hashes()
    hashes = file.hashes if raw_hashes is None else ()
    result: WheelFile | SdistFile
    if isinstance(file, WheelFile):
        raw_sidecar = file.raw_sidecar()
        result = YankedWheelFile(
            filename=file.filename,
            url=file.url,
            version=file.version,
            requires_python=file.requires_python,
            has_metadata=file.has_metadata,
            upload_time=file.upload_time,
            hashes=hashes,
            size=file.size,
            local_path=file.local_path,
            metadata_hash=file.metadata_hash if raw_sidecar is None else None,
            yanked=reason,
        )
        defer_sidecar_hash(result, raw_sidecar)
    else:
        result = YankedSdistFile(
            filename=file.filename,
            url=file.url,
            version=file.version,
            requires_python=file.requires_python,
            upload_time=file.upload_time,
            hashes=hashes,
            size=file.size,
            local_path=file.local_path,
            yanked=reason,
        )
    defer_hashes(result, raw_hashes)
    return result
