"""Download every distribution referenced by a finished resolve.

Consumes a :class:`~nab_python.lockfile.LockInput` and writes every
recorded wheel and sdist into a target directory, verifying the
recorded hash.  Local and VCS pins are skipped: their contents live
elsewhere on disk and the lockfile carries no ``sha256`` for them.

Use as a one-shot from the CLI ``nab download`` command, or
programmatically after :func:`~nab_python.resolve.resolve_pyproject_to_lock`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nab_index.client import AsyncSimpleClient

from .lockfile import IndexPin, LockInput

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

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
    The downloader picks the strongest acceptable algorithm the
    index published; it does not re-hash with a weaker one.
    """

    package: str
    version: str
    filename: str
    url: str
    hash_algo: str
    digest: str


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Summary of a download run."""

    written: tuple[Path, ...]
    skipped: tuple[Path, ...]


def iter_artifacts(lock_input: LockInput) -> Iterable[DownloadEntry]:
    """Yield every wheel/sdist artefact referenced by ``lock_input``.

    A universal lock (``per_tuple_pins`` populated) yields the union of
    every tuple's artefacts, deduplicated by URL so a wheel shared
    across platform tuples is downloaded once.
    """
    if lock_input.per_tuple_pins:
        seen: set[str] = set()
        for label in sorted(lock_input.per_tuple_pins):
            for canonical, pin in sorted(lock_input.per_tuple_pins[label].items()):
                for entry in _entries_for_pin(canonical, pin):
                    if entry.url not in seen:
                        seen.add(entry.url)
                        yield entry
        return
    for canonical, pin in sorted(lock_input.pins.items()):
        yield from _entries_for_pin(canonical, pin)


def _entries_for_pin(canonical: str, pin: PinShape) -> Iterable[DownloadEntry]:
    # Only index pins have downloadable artefacts; local and VCS pins are skipped.
    if isinstance(pin, IndexPin):
        yield from _iter_index_pin(canonical, pin)


def _iter_index_pin(canonical: str, pin: IndexPin) -> Iterable[DownloadEntry]:
    if pin.sdist is not None:
        algo, digest = pin.sdist.primary_digest
        yield DownloadEntry(
            package=canonical,
            version=pin.version,
            filename=pin.sdist.filename,
            url=pin.sdist.url,
            hash_algo=algo,
            digest=digest,
        )
    for wheel in pin.wheels:
        algo, digest = wheel.primary_digest
        yield DownloadEntry(
            package=canonical,
            version=pin.version,
            filename=wheel.filename,
            url=wheel.url,
            hash_algo=algo,
            digest=digest,
        )


def download_lock(
    lock_input: LockInput,
    transport: AsyncHttpTransport,
    output_dir: Path,
    *,
    max_concurrency: int = 8,
) -> DownloadResult:
    """Download every artefact in ``lock_input`` into ``output_dir``.

    Already-present files whose digest matches the recorded
    algorithm are left alone so the command is idempotent.
    Mismatched files are re-fetched and overwritten.  HTTP failures
    and post-download hash mismatches both raise
    :class:`DownloadError` after the fetcher has shut down.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    artefacts = list(iter_artifacts(lock_input))
    return asyncio.run(
        _run_downloads(artefacts, transport, output_dir, max_concurrency)
    )


async def _run_downloads(
    artefacts: list[DownloadEntry],
    transport: AsyncHttpTransport,
    output_dir: Path,
    max_concurrency: int,
) -> DownloadResult:
    sem = asyncio.Semaphore(max_concurrency)
    client = AsyncSimpleClient(transport)
    written: list[Path] = []
    skipped: list[Path] = []

    async def _one(entry: DownloadEntry) -> None:
        async with sem:
            target = output_dir / entry.filename
            if _already_present(target, entry.hash_algo, entry.digest):
                skipped.append(target)
                logger.info("skip %s (%s matches)", entry.filename, entry.hash_algo)
                return
            data = await client.download(entry.url)
            actual = hashlib.new(entry.hash_algo, data).hexdigest()
            if actual != entry.digest:
                msg = (
                    f"{entry.package}=={entry.version}: {entry.hash_algo}"
                    f" mismatch for {entry.filename}\n"
                    f"  expected: {entry.digest}\n  actual:   {actual}"
                )
                raise DownloadError(msg)
            target.write_bytes(data)
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


def _already_present(target: Path, algo: str, expected_digest: str) -> bool:
    if not target.exists():
        return False
    return hashlib.new(algo, target.read_bytes()).hexdigest() == expected_digest
