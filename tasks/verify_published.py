"""Verify the files a release just published to PyPI.

Runs after the publish job in the release workflow. For every file the release
built (an sdist and a wheel for all four workspace packages), it confirms two
things about what PyPI now serves:

* the PEP 740 attestation verifies against this repository, and
* the published bytes match the bytes the workflow built, when the built
  ``dist/`` tree is passed with ``--dist-dir``.

A freshly published file can lag on PyPI's CDN, so a not-yet-available file is
retried within a time budget shared across the whole run. A real verification
failure -- a bad signature, the wrong repository, or a digest mismatch -- fails
at once and is never retried.

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


def _published_sha256(pypi_name: str, version: str, filename: str) -> str:
    """Return the sha256 PyPI records for a file, or signal it is not up yet."""
    url = f"https://pypi.org/pypi/{pypi_name}/{version}/json"
    try:
        # Host and scheme are fixed above; the request only varies by name.
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == _HTTP_NOT_FOUND:
            msg = f"{pypi_name} {version} is not on PyPI yet (HTTP 404)"
            raise NotYetAvailableError(msg) from error
        msg = f"PyPI JSON API failed for {pypi_name} {version}: {error}"
        raise VerificationError(msg) from error
    except urllib.error.URLError as error:
        msg = f"network error reaching {url}: {error.reason}"
        raise NotYetAvailableError(msg) from error
    for entry in data.get("urls", []):
        if entry.get("filename") == filename:
            digest = entry.get("digests", {}).get("sha256")
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


def _find_local(dist_dir: Path, filename: str) -> Path | None:
    """Return the built copy of a file under ``dist_dir``, if present."""
    matches = sorted(dist_dir.rglob(filename))
    return matches[0] if matches else None


def _check_once(
    pypi_name: str,
    version: str,
    filename: str,
    repository: str,
    dist_dir: Path | None,
) -> str:
    """Run one attempt of every check for a file and return a status detail."""
    _verify_attestation(filename, repository)
    if dist_dir is None:
        return "attestation OK"
    local = _find_local(dist_dir, filename)
    if local is None:
        return "attestation OK; no local copy to compare"
    published = _published_sha256(pypi_name, version, filename)
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
) -> str:
    """Check a file, retrying not-yet-available results until the deadline."""
    waits = _retry_waits()
    attempt = 0
    while True:
        attempt += 1
        try:
            return _check_once(pypi_name, version, filename, repository, dist_dir)
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
) -> tuple[bool, str]:
    """Check one file and return ``(passed, detail-or-reason)``."""
    try:
        detail = _check_file(
            pypi_name, version, filename, repository, dist_dir, deadline
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
    if deadline is None:
        deadline = time.monotonic() + RETRY_BUDGET_SECONDS
    files = expected_files(version)
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
    for pypi_name, filename in files:
        passed, detail = _evaluate_file(
            pypi_name, version, filename, repository, dist_dir, deadline
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


def _version_from_tag(tag: str) -> str:
    """Return the release version named by a ``vX.Y.Z`` tag."""
    if not tag.startswith("v"):
        msg = f"release tag {tag!r} must start with 'v'"
        raise SystemExit(msg)
    version = tag[1:]
    if not release.is_release_version(version):
        msg = f"{tag!r} does not name a release version"
        raise SystemExit(msg)
    return version


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
    version = _version_from_tag(args.tag)
    verify_release(version, args.dist_dir)


if __name__ == "__main__":
    main()
