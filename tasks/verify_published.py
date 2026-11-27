"""Verify the files a release just published to PyPI.

Runs after the publish job in the release workflow. For every file the release
built (an sdist and a wheel for all workspace packages), it confirms two
things about what PyPI now serves:

* the PEP 740 attestation verifies against this repository, and
* the published bytes match the bytes the workflow built, when the built
  ``dist/`` tree is passed with ``--dist-dir``.

A freshly published file can lag on PyPI's CDN, so a not-yet-available file is
retried within a time budget for the run. A bad signature,
wrong repository, or digest mismatch fails at once and is never retried.

Run it against a release tag::

    python tasks/verify_published.py v0.0.11 --dist-dir dist
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import build_dists
import release
from packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterator

# The publishing repository the attestations must be signed by.
REPOSITORY = "https://github.com/notatallshaw/nab"

# PyPI returns 404 for a version whose files have not propagated yet.
_HTTP_NOT_FOUND = 404

# Build order and package set come from the release build, so the checked files
# are exactly the set the release publishes.
PACKAGES = build_dists.PACKAGES

# Total wall-clock a run spends waiting for CDN propagation, shared across every
# file. A real failure never consumes it; it fails immediately.
RETRY_BUDGET_SECONDS = 600.0
DigestCache = dict[tuple[str, str], dict[str, str]]

# Substrings pypi-attestations prints when a file or its provenance has not
# propagated yet. These are the only attestation failures worth retrying.
_PENDING_MARKERS = (
    "Could not find the artifact",
    "was not found",
)


class VerificationError(Exception):
    """A file failed verification; the run must not retry it."""


class NotYetAvailableError(Exception):
    """A file has not propagated to PyPI yet; retry within the budget."""


def _file_stem(package: str) -> str:
    """Return the PEP 625 normalized name a package's filenames start with."""
    return canonicalize_name(package).replace("-", "_")


def expected_files(version: str) -> list[tuple[str, str]]:
    """Return ``(pypi_name, filename)`` for every published sdist and wheel."""
    files: list[tuple[str, str]] = []
    for package in PACKAGES:
        stem = _file_stem(package)
        files.append((package, f"{stem}-{version}.tar.gz"))
        files.append((package, f"{stem}-{version}-py3-none-any.whl"))
    return files


def _retry_waits() -> Iterator[float]:
    """Yield an increasing backoff, capped so no single wait dominates."""
    wait = 15.0
    while True:
        yield wait
        wait = min(wait * 2.0, 60.0)


def _published_files(pypi_name: str, version: str) -> dict[str, str]:
    """Fetch one package's current file digests without retaining failures."""
    url = f"https://pypi.org/pypi/{pypi_name}/{version}/json"
    try:
        # Host and scheme are fixed above; the request only varies by name.
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == _HTTP_NOT_FOUND:
            msg = f"{pypi_name} {version} is not on PyPI yet (HTTP 404)"
            raise NotYetAvailableError(msg) from error
        msg = f"PyPI JSON API failed for {pypi_name} {version}: {error}"
        raise VerificationError(msg) from error
    except urllib.error.URLError as error:
        msg = f"network error reaching {url}: {error.reason}"
        raise NotYetAvailableError(msg) from error
    return {
        entry["filename"]: entry.get("digests", {}).get("sha256", "")
        for entry in data.get("urls", [])
        if entry.get("filename")
    }


def _published_sha256(
    pypi_name: str, version: str, filename: str, digests: DigestCache | None = None
) -> str:
    """Reuse listed files, refreshing a snapshot when the requested file is absent."""
    cache = {} if digests is None else digests
    key = (pypi_name, version)
    files = cache.get(key, {})
    if filename not in files:
        files = _published_files(pypi_name, version)
        cache[key] = files
    if filename in files:
        digest = files[filename]
        if not digest:
            msg = f"{filename}: PyPI lists no sha256 digest"
            raise VerificationError(msg)
        return digest
    msg = f"{filename} is not listed for {pypi_name} {version} yet"
    raise NotYetAvailableError(msg)


def _verify_attestation(filename: str, repository: str) -> None:
    """Verify a file's PEP 740 attestation, or signal it is not up yet."""
    command = [
        sys.executable,
        "-m",
        "pypi_attestations",
        "verify",
        "pypi",
        "--repository",
        repository,
        f"pypi:{filename}",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return
    output = (result.stdout + result.stderr).strip()
    if any(marker in output for marker in _PENDING_MARKERS):
        raise NotYetAvailableError(output)
    msg = f"{filename}: attestation did not verify:\n{output}"
    raise VerificationError(msg)


def _local_sha256(path: Path) -> str:
    """Return the sha256 of a local file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_local(dist_dir: Path, filename: str) -> Path:
    """Require one local artifact when comparing built and published files."""
    matches = sorted(path for path in dist_dir.rglob(filename) if path.is_file())
    if len(matches) != 1:
        msg = (
            f"expected exactly one local artifact for {filename}, found {len(matches)}"
        )
        raise VerificationError(msg)
    return matches[0]


def _check_once(
    pypi_name: str,
    version: str,
    filename: str,
    repository: str,
    dist_dir: Path | None,
    *,
    digests: DigestCache | None = None,
) -> str:
    """Run one attempt of every check for a file and return a status detail."""
    local = _find_local(dist_dir, filename) if dist_dir is not None else None
    _verify_attestation(filename, repository)
    if local is None:
        return "attestation OK"
    published = _published_sha256(pypi_name, version, filename, digests)
    built = _local_sha256(local)
    if built != published:
        msg = (
            f"{filename}: published bytes differ from built bytes "
            f"(PyPI sha256 {published}, built {built})"
        )
        raise VerificationError(msg)
    return "attestation OK; published bytes match built bytes"


def _check_file(
    pypi_name: str,
    version: str,
    filename: str,
    repository: str,
    dist_dir: Path | None,
    deadline: float,
    *,
    digests: DigestCache | None = None,
) -> str:
    """Check a file, retrying not-yet-available results until the deadline."""
    waits = _retry_waits()
    attempt = 0
    while True:
        attempt += 1
        try:
            return _check_once(
                pypi_name, version, filename, repository, dist_dir, digests=digests
            )
        except NotYetAvailableError as pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"{filename}: still unavailable after the retry budget: {pending}"
                raise VerificationError(msg) from pending
            wait = min(next(waits), remaining)
            print(
                f"  {filename}: not available yet ({pending}); "
                f"retrying in {wait:.0f}s [attempt {attempt}]",
                flush=True,
            )
            time.sleep(wait)


def _evaluate_file(
    pypi_name: str,
    version: str,
    filename: str,
    repository: str,
    dist_dir: Path | None,
    deadline: float,
    *,
    digests: DigestCache | None = None,
) -> tuple[bool, str]:
    """Check one file and return ``(passed, detail-or-reason)``."""
    try:
        detail = _check_file(
            pypi_name,
            version,
            filename,
            repository,
            dist_dir,
            deadline,
            digests=digests,
        )
    except VerificationError as error:
        return False, str(error)
    return True, detail


def verify_release(
    version: str,
    dist_dir: Path | None = None,
    repository: str = REPOSITORY,
    deadline: float | None = None,
) -> None:
    """Verify every published file for a release, raising on any failure."""
    version = release.normalized_release_version(version)
    if deadline is None:
        deadline = time.monotonic() + RETRY_BUDGET_SECONDS
    files = expected_files(version)
    if dist_dir is not None:
        try:
            for _, filename in files:
                _find_local(dist_dir, filename)
        except VerificationError as error:
            raise SystemExit(str(error)) from error
    print(
        f"Verifying {len(files)} published files for nab {version} "
        f"against {repository}.",
        flush=True,
    )
    if dist_dir is None:
        print(
            "No --dist-dir given; checking attestations only "
            "(skipping the built-vs-published digest comparison).",
            flush=True,
        )
    failures: list[tuple[str, str]] = []
    digests: DigestCache = {}
    for pypi_name, filename in files:
        passed, detail = _evaluate_file(
            pypi_name,
            version,
            filename,
            repository,
            dist_dir,
            deadline,
            digests=digests,
        )
        if passed:
            print(f"  ok   {filename}: {detail}", flush=True)
        else:
            print(f"  FAIL {filename}", flush=True)
            failures.append((filename, detail))
    if failures:
        print(f"\n{len(failures)} of {len(files)} files failed verification:")
        for _filename, reason in failures:
            print(f"  - {reason}")
        msg = f"{len(failures)} published file(s) failed verification"
        raise SystemExit(msg)
    print(f"\nAll {len(files)} published files verified.")


def main() -> None:
    """Parse arguments and verify the tag's published files."""
    parser = argparse.ArgumentParser(description="Verify a release's PyPI files.")
    parser.add_argument("tag", help="release tag, for example v0.0.11")
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="tree of built artifacts to compare against the published files",
    )
    args = parser.parse_args()
    if not args.tag.startswith("v"):
        msg = f"release tag {args.tag!r} must start with 'v'"
        raise SystemExit(msg)
    verify_release(args.tag[1:], args.dist_dir)


if __name__ == "__main__":
    main()
