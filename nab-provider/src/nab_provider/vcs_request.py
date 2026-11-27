"""Parsing and vocabulary for a VCS requirement.

Running the clone belongs to :mod:`nab_index.vcs`, admitting a URL at all to
:func:`nab_provider.vcs_admission.admit_vcs_url`, and both need this
vocabulary.
"""

from __future__ import annotations

__lazy_modules__ = ("nab_provider.subdir", "urllib.parse")

import re
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlsplit

from .subdir import decode_subdirectory
from .value import SlottedValue

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "FULL_GIT_SHA_RE",
    "VcsClone",
    "VcsCloneError",
    "VcsRequest",
]

# Exported so VCS admission and clone-time validation share one definition.
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_VCS_PREFIX_RE = re.compile(r"^(git|hg)\+")
_ASCII_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def is_mercurial_node(ref: str) -> bool:
    """Return whether a ref identifies a complete, non-null changeset."""
    return bool(FULL_GIT_SHA_RE.fullmatch(ref)) and ref != "0" * 40


def mercurial_transport(repo_url: str) -> Literal["https", "file"]:
    """Accept an unambiguous HTTPS or local file repository location."""
    msg = "Mercurial requires an HTTPS or absolute local file URL without credentials"
    try:
        parts = urlsplit(repo_url)
        # Access the property to reject invalid or out-of-range ports.
        port = parts.port
        path = unquote(parts.path, errors="strict")
    except ValueError:
        raise VcsCloneError(msg) from None

    if (
        any(char.isspace() for char in repo_url)
        or _ASCII_CONTROL.search(repo_url)
        or _ASCII_CONTROL.search(path)
        or any(char in repo_url for char in "?#\\")
        or any(char in path for char in "?#\\%")
        or not path.startswith("/")
        or not path.strip("/")
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise VcsCloneError(msg)

    if (
        repo_url.startswith("https://")
        and parts.hostname is not None
        and not any(char in parts.netloc for char in "@%")
    ):
        return "https"
    if repo_url.startswith("file:///") and not parts.netloc and port is None:
        return "file"
    raise VcsCloneError(msg)


class VcsCloneError(Exception):
    """Raised when a clone or ref resolution fails."""


class VcsClone(SlottedValue):
    """Result of :func:`nab_index.vcs.prepare_clone`.

    ``path`` is the absolute filesystem path to the (possibly
    cached) checked-out source tree.  ``commit_sha`` is the
    40-char hex SHA the clone is pinned to.  ``subdirectory`` is the
    relative path inside the checkout that contains the project
    pyproject.toml; ``""`` means the repo root.
    """

    __slots__ = ("commit_sha", "path", "subdirectory")
    __match_args__ = ("path", "commit_sha", "subdirectory")

    def __init__(self, path: Path, commit_sha: str, subdirectory: str = "") -> None:
        """Record a checkout of ``commit_sha`` at ``path``."""
        self.path = path
        self.commit_sha = commit_sha
        self.subdirectory = subdirectory


class VcsRequest(SlottedValue):
    """Parsed representation of a ``git+https://repo.git@ref#...`` URL.

    ``ref`` may be a 40-char SHA or a branch / tag name.  ``ref`` of
    ``""`` means HEAD; policing floating refs is the caller's job.
    ``subdirectory`` is parsed from the ``#subdirectory=...`` fragment
    if present.
    """

    __slots__ = ("ref", "repo_url", "scheme", "subdirectory")
    __match_args__ = ("scheme", "repo_url", "ref", "subdirectory")

    def __init__(self, scheme: str, repo_url: str, ref: str, subdirectory: str) -> None:
        """Record one parsed VCS URL."""
        self.scheme = scheme
        self.repo_url = repo_url
        self.ref = ref
        self.subdirectory = subdirectory

    @classmethod
    def parse(cls, url: str) -> VcsRequest:
        """Parse a pip-style VCS URL into its components."""
        url_no_frag, _, fragment = url.partition("#")
        match = _VCS_PREFIX_RE.match(url_no_frag)
        if match is None:
            msg = f"not a recognised VCS URL: {url!r}"
            raise VcsCloneError(msg)
        inner = url_no_frag[len(match.group(0)) :]
        repo, ref = _split_repo_ref(inner)

        subdirectory = ""
        for fragment_part in fragment.split("&"):
            key, _, value = fragment_part.partition("=")
            if key == "subdirectory":
                subdirectory = value

        try:
            subdirectory = decode_subdirectory(subdirectory)
        except ValueError as exc:
            msg = f"unsafe VCS subdirectory {subdirectory!r} in {url!r}"
            raise VcsCloneError(msg) from exc

        return cls(
            scheme=match.group(1),
            repo_url=repo,
            ref=ref,
            subdirectory=subdirectory,
        )


def _split_repo_ref(inner: str) -> tuple[str, str]:
    """Split the transport URL and ref after removing the VCS prefix and fragment.

    For URL forms (``scheme://...``), the ref is everything after the
    last ``@`` that appears in the path component (after the netloc),
    so branch names containing ``/`` (e.g. ``release/1.0``) survive.
    A ``user@`` in the authority section is left alone.

    For the SSH shortcut form (``user@host:path[@<ref>]``), the first
    ``:`` separates the auth+host from the path; an optional
    ``@<ref>`` may follow the path.
    """
    if "://" not in inner:
        if ":" not in inner:
            return (inner, "")
        host_part, _, path_part = inner.partition(":")
        if "@" in path_part:
            path_repo, _, ref = path_part.rpartition("@")
            return (f"{host_part}:{path_repo}", ref)
        return (inner, "")

    scheme_part, _, rest = inner.partition("://")
    netloc, slash, path_part = rest.partition("/")
    if not slash or "@" not in path_part:
        return (inner, "")
    path_repo, _, ref = path_part.rpartition("@")
    return (f"{scheme_part}://{netloc}/{path_repo}", ref)
