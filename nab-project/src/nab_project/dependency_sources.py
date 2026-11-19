"""Bind dependency inputs to local files or hash-verified archive declarations."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nab_index import dependency_input_cache
from nab_index.archive_files import read_archive_files, source_relative_path
from nab_index.client import verify_sdist_hash
from nab_index.urls import strip_userinfo
from nab_provider.archive import ArchiveRequest

from .dependency_inputs import dependency_input_requirements

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

__all__ = ["DependencySourceReader"]


def _archive_input(declaration: Mapping[str, object]) -> tuple[ArchiveRequest, str]:
    """Validate the archive location and immutable source identity."""
    url = declaration.get("url")
    digest = declaration.get("sha256")
    if not isinstance(url, str) or not isinstance(digest, str):
        msg = "remote dependency inputs need a URL and SHA-256 hash"
        raise TypeError(msg)
    if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        msg = "dependency input SHA-256 must contain 64 hexadecimal digits"
        raise ValueError(msg)
    digest = digest.lower()
    if "#" in url:
        msg = "use sha256 and projects.path instead of archive URL fragments"
        raise ValueError(msg)
    request = ArchiveRequest.parse(url)
    return request, digest


class DependencySourceReader:
    """Read declarations without keeping whole source trees in the cache."""

    def __init__(
        self,
        root: Path,
        cache_dir: Path | None,
        fetch: Callable[[str], bytes],
        *,
        offline: bool = False,
    ) -> None:
        """Keep cache and network policy for one input preparation operation."""
        self.root = root
        self.cache_dir = (
            None if cache_dir is None else cache_dir / dependency_input_cache.BUCKET
        )
        self.fetch = fetch
        self.offline = offline
        self.local_inputs: dict[Path, str] = {}

    def requirements(
        self,
        source: Mapping[str, object],
        selection: Mapping[str, object],
    ) -> list[str]:
        """Read selected declarations from one local or hash-pinned source."""
        return dependency_input_requirements(
            selection, self.bind(source), root=self.root
        )

    def bind(self, declaration: Mapping[str, object]) -> Callable[[str], str]:
        """Bind one declared source; fetching remains lazy until a file is needed."""
        if unknown := declaration.keys() - {"path", "url", "sha256"}:
            msg = f"unknown input source keys: {sorted(unknown)}"
            raise ValueError(msg)
        local = declaration.get("path")
        if local is not None:
            if not isinstance(local, str):
                msg = "dependency input path must be a string"
                raise TypeError(msg)
            if "url" in declaration or "sha256" in declaration:
                msg = "dependency input path cannot be combined with URL/hash"
                raise ValueError(msg)
            return _local_reader((self.root / local).resolve(), self.local_inputs)
        request, digest = _archive_input(declaration)
        return self._archive_reader(request, digest)

    def _archive_reader(
        self, request: ArchiveRequest, digest: str
    ) -> Callable[[str], str]:
        """Share one verified archive across the files read from this source."""
        url = request.url
        verified_archive: bytes | None = None

        def read_archive(relative: str) -> str:
            nonlocal verified_archive
            relative = source_relative_path(relative)
            if self.cache_dir is not None:
                cached = dependency_input_cache.read_text(
                    self.cache_dir, digest, relative
                )
                if cached is not None:
                    return cached
            if self.offline:
                msg = (
                    f"dependency input {strip_userinfo(url)}: {relative} is not cached"
                )
                raise ValueError(msg)
            if verified_archive is None:
                data = self.fetch(request.url)
                verify_sdist_hash(data, ("sha256", digest))
                verified_archive = data

            text = read_archive_files(verified_archive, [relative])[relative]
            if self.cache_dir is not None:
                dependency_input_cache.write_text(
                    self.cache_dir, digest, relative, text
                )
            return text

        return read_archive


def _local_reader(root: Path, inputs: dict[Path, str]) -> Callable[[str], str]:
    """Read from a local tree without following a file outside that tree."""

    def read(relative: str) -> str:
        file = (root / source_relative_path(relative)).resolve()
        if not file.is_relative_to(root):
            msg = f"dependency input file escapes {root}"
            raise ValueError(msg)
        text = file.read_text(encoding="utf-8")
        inputs.setdefault(file, text)
        return text

    return read
