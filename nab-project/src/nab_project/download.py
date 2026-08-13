"""Download every distribution referenced by a finished resolve.

Consumes a :class:`~nab_project.lockfile.LockInput` and writes every
recorded wheel, sdist, and direct-URL archive into a target directory,
verifying the recorded hash.  Local and VCS pins are skipped: their
contents live elsewhere on disk and the lockfile carries no ``sha256``
for them.  An artefact with a local ``file://`` URL (a find-links
index entry or a direct-URL archive) is copied from its on-disk path
rather than fetched over HTTP.

Use as a one-shot from the CLI ``nab download`` command, or
programmatically after :func:`~nab_project.resolve.build_lock_input`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from nab_index.atomic import atomic_write
from nab_index.client import AsyncSimpleClient
from nab_index.local_index import parse_file_url
from nab_provider.errors import HttpError

from .lockfile import ArchivePin, IndexPin, LocalPin, LockInput, VcsPin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nab_index.transport import AsyncHttpTransport

    from .lockfile import PinShape

__all__ = [
    "DownloadEntry",
    "DownloadError",
    "DownloadResult",
    "download_lock",
    "iter_artifacts",
]


logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """A downloaded artefact failed hash verification or the HTTP fetch."""


@dataclass(frozen=True, slots=True)
class DownloadEntry:
    """One artefact to fetch into the output directory.

    ``hash_algo`` is one of ``sha256``, ``sha384``, ``sha512`` and
    ``digest`` is the recorded hex digest under that algorithm.
    The downloader verifies against the first acceptable algorithm the
    index published, preferring sha256 over sha384 over sha512.

    ``local_path`` is set for an artefact read from disk instead of
    fetched over ``url``: a wheel or sdist from a local ``file://``
    (find-links) index, or a direct-URL archive whose ``url`` is
    ``file://``.
    """

    package: str
    version: str
    filename: str
    url: str
    hash_algo: str
    digest: str
    local_path: Path | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Summary of a download run."""

    written: tuple[Path, ...]
    skipped: tuple[Path, ...]


def iter_artifacts(lock_input: LockInput) -> Iterable[DownloadEntry]:
    """Yield every downloadable artefact referenced by ``lock_input``.

    A wheel, sdist, or direct-URL archive for each pin, deduplicated
    by URL across the targets the resolve ran against so an artefact
    shared by several of them is downloaded once.  Local and VCS pins
    carry no downloadable URL and are skipped.
    """
    seen: set[str] = set()
    for label in sorted(lock_input.targets):
        for canonical, pin in sorted(lock_input.targets[label].pins.items()):
            for entry in _entries_for_pin(canonical, pin):
                if entry.url not in seen:
                    seen.add(entry.url)
                    yield entry


def _entries_for_pin(canonical: str, pin: PinShape) -> Iterable[DownloadEntry]:
    # Index and archive pins carry a downloadable, hash-verified URL; local and
    # VCS pins live elsewhere on disk and carry no hash, so they are skipped.
    if isinstance(pin, IndexPin):
        yield from _iter_index_pin(canonical, pin)
    elif isinstance(pin, ArchivePin):
        algo, digest = pin.primary_digest
        parts = urlsplit(pin.url)
        local_path = parse_file_url(pin.url) if parts.scheme == "file" else None
        yield DownloadEntry(
            package=canonical,
            version=pin.version,
            filename=posixpath.basename(parts.path),
            url=pin.url,
            hash_algo=algo,
            digest=digest.lower(),
            local_path=local_path,
        )
    elif isinstance(pin, (LocalPin, VcsPin)):
        return
    else:  # pragma: no cover - exhaustive
        msg = f"unknown pin shape: {pin!r}"
        raise TypeError(msg)


def _iter_index_pin(canonical: str, pin: IndexPin) -> Iterable[DownloadEntry]:
    # Recorded digests are lowercased to match hashlib.hexdigest() output:
    # index-fed flows already lowercase, but a caller-built LockInput may not.
    if pin.sdist is not None:
        algo, digest = pin.sdist.primary_digest
        yield DownloadEntry(
            package=canonical,
            version=pin.version,
            filename=pin.sdist.filename,
            url=pin.sdist.url,
            hash_algo=algo,
            digest=digest.lower(),
            local_path=pin.sdist.local_path,
        )
    for wheel in pin.wheels:
        algo, digest = wheel.primary_digest
        yield DownloadEntry(
            package=canonical,
            version=pin.version,
            filename=wheel.filename,
            url=wheel.url,
            hash_algo=algo,
            digest=digest.lower(),
            local_path=wheel.local_path,
        )


def _coalesce_download_targets(
    artefacts: list[DownloadEntry],
) -> list[DownloadEntry]:
    """Coalesce casefold-equivalent names sharing ``(hash_algo, digest)``.

    The first spelling wins; a different identity raises :class:`DownloadError`.
    """
    by_name: dict[str, DownloadEntry] = {}
    unique: list[DownloadEntry] = []
    for entry in artefacts:
        key = entry.filename.casefold()
        prior = by_name.get(key)
        if prior is not None and (prior.hash_algo, prior.digest) != (
            entry.hash_algo,
            entry.digest,
        ):
            msg = (
                "artefacts collide on casefold-equivalent output filenames"
                f" {prior.filename!r} and {entry.filename!r}, but their entries"
                " record different hash identities."
                " Download them to separate directories."
            )
            raise DownloadError(msg)
        if prior is None:
            by_name[key] = entry
            unique.append(entry)
    return unique


def download_lock(
    lock_input: LockInput,
    transport: AsyncHttpTransport,
    output_dir: Path,
    *,
    max_concurrency: int = 8,
    offline: bool = False,
) -> DownloadResult:
    """Download every artefact in ``lock_input`` into ``output_dir``.

    Already-present files whose digest matches the recorded
    algorithm are left alone so the command is idempotent.
    Mismatched files are re-fetched and overwritten.  HTTP failures
    and post-download hash mismatches both raise
    :class:`DownloadError` after the fetcher has shut down.

    ``offline`` refuses any artefact that would need an HTTP fetch;
    already-present files and local ``file://`` artefacts are still
    served.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    artefacts = _coalesce_download_targets(list(iter_artifacts(lock_input)))
    return asyncio.run(
        _run_downloads(
            artefacts, transport, output_dir, max_concurrency, offline=offline
        )
    )


async def _run_downloads(
    artefacts: list[DownloadEntry],
    transport: AsyncHttpTransport,
    output_dir: Path,
    max_concurrency: int,
    *,
    offline: bool,
) -> DownloadResult:
    sem = asyncio.Semaphore(max_concurrency)
    client = AsyncSimpleClient(transport)
    written: list[Path] = []
    skipped: list[Path] = []

    async def _one(entry: DownloadEntry) -> None:
        async with sem:
            # The filename comes from the index or an archive URL; reject
            # anything but a plain basename so a crafted name cannot escape
            # output_dir on write.
            if not entry.filename or Path(entry.filename).name != entry.filename:
                msg = (
                    f"{entry.package}=={entry.version}:"
                    f" unsafe artefact filename: {entry.filename!r}"
                )
                raise DownloadError(msg)
            target = output_dir / entry.filename
            if _already_present(target, entry.hash_algo, entry.digest):
                skipped.append(target)
                logger.info("skip %s (%s matches)", entry.filename, entry.hash_algo)
                return
            data = await _fetch_bytes(entry, client, offline=offline)
            actual = hashlib.new(entry.hash_algo, data).hexdigest()
            if actual != entry.digest:
                msg = (
                    f"{entry.package}=={entry.version}: {entry.hash_algo}"
                    f" mismatch for {entry.filename}\n"
                    f"  expected: {entry.digest}\n  actual:   {actual}"
                )
                raise DownloadError(msg)
            atomic_write(target, data)
            written.append(target)
            logger.info("wrote %s", target)

    tasks = [asyncio.create_task(_one(a)) for a in artefacts]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await client.aclose()
    return DownloadResult(written=tuple(written), skipped=tuple(skipped))


async def _fetch_bytes(
    entry: DownloadEntry, client: AsyncSimpleClient, *, offline: bool
) -> bytes:
    """Read a local artefact from disk, else fetch it over HTTP.

    ``offline`` refuses the HTTP half rather than fetching.
    """
    if entry.local_path is not None:
        try:
            return entry.local_path.read_bytes()
        except OSError as exc:
            msg = (
                f"{entry.package}=={entry.version}: failed to read"
                f" {entry.filename} from {entry.local_path}: {exc}"
            )
            raise DownloadError(msg) from exc
    if offline:
        msg = (
            f"{entry.package}=={entry.version}: artefact fetch unavailable"
            f" in offline mode ({entry.filename})"
        )
        raise DownloadError(msg)
    try:
        return await client.download(entry.url)
    except HttpError as exc:
        msg = (
            f"{entry.package}=={entry.version}: failed to fetch {entry.filename}: {exc}"
        )
        raise DownloadError(msg) from exc


def _already_present(target: Path, algo: str, expected_digest: str) -> bool:
    if not target.exists():
        return False
    return hashlib.new(algo, target.read_bytes()).hexdigest() == expected_digest
