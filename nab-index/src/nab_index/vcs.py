"""Shallow VCS clone helper for nab-index.

Performs ``git clone --depth 1`` against a directory under the cache
root.  URL admission lives upstream in
:func:`nab_python._vcs_admission.admit_vcs_url`.

Cache layout under ``cache_root / "vcs"``:

    <repo-key>/<commit-sha>/

``repo-key`` is the 16-char prefix of a SHA-256 over the canonicalised
repo URL (``vcs+`` prefix stripped).  ``commit-sha`` is always a
concrete 40-char hash; floating refs are resolved via
``git ls-remote`` before the clone runs.  A finished clone carries a
``.git/nab-complete`` marker file; a tree without it is discarded and
recloned.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .subdir import subdirectory_escapes

__all__ = [
    "FULL_GIT_SHA_RE",
    "VcsClone",
    "VcsCloneError",
    "VcsRequest",
    "prepare_clone",
]


logger = logging.getLogger(__name__)


# Match a 40-char hex git/hg commit SHA (case-insensitive).  Exported so the
# VCS-admission code in ``nab_python.provider`` shares one definition with
# the clone-time validation in this module.
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_VCS_PREFIX_RE = re.compile(r"^git\+")

# Written inside ``.git`` after checkout.  ``git init`` creates ``.git``
# before the fetch, so the directory alone cannot prove a finished clone.
_COMPLETE_MARKER = "nab-complete"

# git applies no read timeout, so a remote that goes quiet after the handshake
# blocks forever.  git's own knobs are per-transport (``http.lowSpeed*`` is
# libcurl-only, ``GIT_SSH_COMMAND`` ssh-only) and neither reaches the ``git://``
# daemon protocol, so the bound goes on the subprocess.
_LS_REMOTE_TIMEOUT_SECONDS = 120

# A fetch gets the wider bound: a server can send nothing for minutes while it
# builds a large repo's pack.
_FETCH_TIMEOUT_SECONDS = 1800

# git reads these to pick which repository, and which of its refs, a command
# acts on, and they outrank ``cwd``.  git sets them itself around hooks, so an
# inherited one is not always deliberate.
_REPO_SELECTION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


class VcsCloneError(Exception):
    """Raised when a clone or ref resolution fails."""


@dataclass(frozen=True, slots=True)
class VcsClone:
    """Result of :func:`prepare_clone`.

    ``path`` is the absolute filesystem path to the (possibly
    cached) checked-out source tree.  ``commit_sha`` is the
    40-char hex SHA the clone is pinned to.  ``subdirectory`` is the
    relative path inside the checkout that contains the project
    pyproject.toml; ``""`` means the repo root.
    """

    path: Path
    commit_sha: str
    subdirectory: str = ""


@dataclass(frozen=True, slots=True)
class VcsRequest:
    """Parsed representation of a ``git+https://repo.git@ref#...`` URL.

    ``ref`` may be a 40-char SHA or a branch / tag name.  ``ref`` of
    ``""`` means "HEAD"; the caller is expected to have decided
    whether floating refs are permitted.  ``subdirectory`` is parsed
    from the ``#subdirectory=...`` fragment if present.
    """

    scheme: str
    repo_url: str
    ref: str
    subdirectory: str

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
        if subdirectory_escapes(subdirectory):
            msg = f"unsafe VCS subdirectory {subdirectory!r} in {url!r}"
            raise VcsCloneError(msg)
        return cls(scheme="git", repo_url=repo, ref=ref, subdirectory=subdirectory)


def _split_repo_ref(inner: str) -> tuple[str, str]:
    """Split ``inner`` (no ``vcs+`` prefix, no fragment) into ``(repo, ref)``.

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


def _repo_key(repo_url: str) -> str:
    return hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:16]


def _is_local_repo(repo_url: str) -> bool:
    """Return True when ``repo_url`` reaches its repo through the filesystem.

    Offline means nab issues no network request of its own.  A ``file``
    URL is read by path, so it stays available offline whatever the
    authority says: git drops the authority outright on POSIX, and on
    Windows turns it into a UNC path, which is a filesystem call like
    any other.  Whether that path is backed by a local disk, NFS, or SMB
    is the operating system's business, the same as it is for a
    ``file:`` index.
    """
    return urlsplit(repo_url).scheme == "file"


def prepare_clone(
    cache_root: Path,
    request: VcsRequest,
    *,
    require_pin: bool,
    offline: bool = False,
) -> VcsClone:
    """Resolve ``request.ref`` to a SHA and ensure a clone exists at it.

    When ``require_pin`` is True and the ref is not already a 40-char
    SHA, raises :class:`VcsCloneError` rather than fetching a floating
    ref.  When False, the helper consults ``git ls-remote`` to resolve
    the named ref to a SHA, then performs a shallow clone of that
    commit.

    ``offline`` withholds every git call that would reach the remote: a
    complete cached clone is still served; anything else raises
    :class:`VcsCloneError`.  A ``file://`` repo is read through the
    filesystem rather than the network, so it still clones.

    Idempotent: a destination carrying the completion marker is reused
    without a fetch.  A fresh clone lands in a temporary sibling
    directory and is renamed into place only once fully checked out, so
    a concurrent or interrupted run never leaves a partial tree at the
    cache path.
    """
    may_reach_remote = not offline or _is_local_repo(request.repo_url)
    sha = _resolve_sha(
        request, require_pin=require_pin, may_reach_remote=may_reach_remote
    )

    dest = cache_root / "vcs" / _repo_key(request.repo_url) / sha
    if _clone_complete(dest):
        return VcsClone(
            path=dest,
            commit_sha=sha,
            subdirectory=request.subdirectory,
        )

    if not may_reach_remote:
        msg = f"no cached clone of {request.repo_url} @ {sha} (offline mode)"
        raise VcsCloneError(msg)

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not _clone_complete(dest):
        # A concurrent run may have completed dest since the top check;
        # only wipe a partial.
        shutil.rmtree(dest)

    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f"{sha}.", suffix=".tmp"))
    _shallow_clone(request.repo_url, sha, tmp)
    try:
        tmp.rename(dest)
    except OSError as exc:
        # A concurrent run renamed its own finished clone first: use it.
        shutil.rmtree(tmp, ignore_errors=True)
        if not _clone_complete(dest):
            msg = (
                f"clone of {request.repo_url} @ {sha} could not be"
                f" moved into place: {exc}"
            )
            raise VcsCloneError(msg) from exc

    return VcsClone(
        path=dest,
        commit_sha=sha,
        subdirectory=request.subdirectory,
    )


def _clone_complete(dest: Path) -> bool:
    """Return True when ``dest`` holds a fully fetched and checked-out clone."""
    return (dest / ".git" / _COMPLETE_MARKER).is_file()


def _resolve_sha(
    request: VcsRequest,
    *,
    require_pin: bool,
    may_reach_remote: bool = True,
) -> str:
    """Return a 40-char SHA for ``request.ref``.

    Raises when ``require_pin`` is True and ``ref`` is not already a
    SHA.  Otherwise consults ``git ls-remote`` to look up the ref, which
    ``may_reach_remote=False`` refuses: no cache holds a ref-to-SHA
    mapping.
    """
    if request.ref and FULL_GIT_SHA_RE.match(request.ref):
        return request.ref

    if require_pin:
        msg = (
            f"refusing to resolve floating ref {request.ref!r} for"
            f" {request.repo_url!r}: vcs.require-pin is true"
        )
        raise VcsCloneError(msg)

    target = request.ref or "HEAD"
    if not may_reach_remote:
        msg = (
            f"cannot resolve ref {target!r} at {request.repo_url}"
            f" (offline mode): only a pinned commit resolves from cache"
        )
        raise VcsCloneError(msg)

    # Also request the peeled form: an exact-ref ls-remote omits the
    # companion refs/tags/<name>^{} line, so without it an annotated tag
    # would resolve to the tag object rather than the commit it points at.
    ls_remote_args = ["git", "ls-remote", request.repo_url, target, f"{target}^{{}}"]
    try:
        proc = subprocess.run(  # noqa: S603 - URL admission upstream
            ls_remote_args,
            check=True,
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        msg = f"git ls-remote {request.repo_url} {target}: {exc}"
        raise VcsCloneError(msg) from exc

    lines = proc.stdout.strip().splitlines()
    if not lines:
        msg = f"no ref {target!r} found at {request.repo_url}"
        raise VcsCloneError(msg)

    # ``git ls-remote`` advertises the tag-object SHA on the
    # ``refs/tags/<name>`` line for annotated tags, with the peeled
    # commit on a companion ``refs/tags/<name>^{}`` line.  Prefer the
    # peeled commit so the lock pins a commit, not a tag object.
    peeled = next((ln for ln in lines if ln.split()[-1].endswith("^{}")), None)
    chosen = peeled if peeled is not None else lines[0]

    sha = chosen.split()[0]
    if not FULL_GIT_SHA_RE.match(sha):
        msg = f"unexpected ls-remote output: {chosen!r}"
        raise VcsCloneError(msg)

    return sha


def _shallow_clone(repo_url: str, sha: str, dest: Path) -> None:
    """Shallow-clone ``repo_url`` at exactly ``sha`` to ``dest``.

    Uses ``git init`` + ``git fetch --depth 1`` to land precisely the
    chosen commit without pulling history.  Hosts that disallow
    direct sha fetch surface as a :class:`VcsCloneError` from the
    fetch step.  The completion marker is written last, so its
    presence proves the checkout finished.

    ``dest`` is the temporary directory ``prepare_clone`` created with
    :func:`tempfile.mkdtemp`, so it already exists.
    """
    dest.mkdir(parents=True, exist_ok=True)
    env = _git_env()

    init_args = ["git", "init", "--quiet"]
    fetch_args = ["git", "fetch", "--quiet", "--depth", "1", repo_url, sha]
    checkout_args = ["git", "checkout", "--quiet", "FETCH_HEAD"]

    try:
        subprocess.run(  # noqa: S603 - git is a runtime dep
            init_args,
            check=True,
            cwd=dest,
            env=env,
        )
        subprocess.run(  # noqa: S603 - URL admission upstream
            fetch_args,
            check=True,
            cwd=dest,
            env=env,
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
        subprocess.run(  # noqa: S603 - git is a runtime dep
            checkout_args,
            check=True,
            cwd=dest,
            env=env,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        # Roll back the partial clone so the cache stays clean.
        shutil.rmtree(dest, ignore_errors=True)
        msg = f"failed to clone {repo_url} @ {sha}: {exc}"
        raise VcsCloneError(msg) from exc

    try:
        (dest / ".git" / _COMPLETE_MARKER).touch()
    except OSError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        msg = f"clone of {repo_url} @ {sha} could not be marked complete: {exc}"
        raise VcsCloneError(msg) from exc


def _git_env() -> dict[str, str]:
    """Return an environment for git subprocesses.

    Disables interactive prompts and any auto-detected user config so
    the clone fails fast on unauthenticated repos rather than hanging,
    and drops the inherited repo-selection variables so every call acts
    on the directory nab passes as ``cwd``.
    """
    env = dict(os.environ)
    for name in _REPO_SELECTION_VARS:
        env.pop(name, None)

    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return env
