"""Shallow VCS clone helper for nab-index.

Performs ``git clone --depth 1`` against a directory under the cache
root.  URL admission lives upstream in
:func:`nab_python._vcs_admission.admit_vcs_url`.

Cache layout under ``cache_root / "vcs"``:

    <repo-key>/<commit-sha>/

``repo-key`` is the 16-char prefix of a SHA-256 over the canonicalised
repo URL (``vcs+`` prefix stripped).  ``commit-sha`` is always a
concrete 40-char hash; floating refs are resolved via
``git ls-remote`` before the clone runs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "FULL_GIT_SHA_RE",
    "VcsClone",
    "VcsCloneError",
    "VcsRequest",
    "prepare_clone",
]


logger = logging.getLogger(__name__)


# Match a 40-char lower-case hex git/hg commit SHA.  Exported so the
# VCS-admission code in ``nab_python.provider`` shares one definition with
# the clone-time validation in this module.
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VCS_PREFIX_RE = re.compile(r"^(git|hg|bzr|svn)\+")


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
        scheme = match.group(1)
        inner = url_no_frag[len(match.group(0)) :]
        repo, ref = _split_repo_ref(inner)

        subdirectory = ""
        for fragment_part in fragment.split("&"):
            key, _, value = fragment_part.partition("=")
            if key == "subdirectory":
                subdirectory = value
        return cls(scheme=scheme, repo_url=repo, ref=ref, subdirectory=subdirectory)


def _split_repo_ref(inner: str) -> tuple[str, str]:
    """Split ``inner`` (no ``vcs+`` prefix, no fragment) into ``(repo, ref)``.

    For URL forms (``scheme://...``), the ref is everything after the
    last ``@`` that appears in the path component (after the netloc),
    so branch names containing ``/`` (e.g. ``release/1.0``) survive.
    A ``user@`` in the authority section is left alone.

    For the SSH shortcut form (``user@host:path``) there is no ref;
    pip and uv require explicit ``@<ref>`` only on URL forms.
    """
    if "://" not in inner:
        return (inner, "")

    scheme_part, _, rest = inner.partition("://")
    netloc, slash, path_part = rest.partition("/")
    if not slash or "@" not in path_part:
        return (inner, "")
    path_repo, _, ref = path_part.rpartition("@")
    return (f"{scheme_part}://{netloc}/{path_repo}", ref)


def _repo_key(repo_url: str) -> str:
    return hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:16]


def prepare_clone(
    cache_root: Path,
    request: VcsRequest,
    *,
    require_pin: bool,
) -> VcsClone:
    """Resolve ``request.ref`` to a SHA and ensure a clone exists at it.

    When ``require_pin`` is True and the ref is not already a 40-char
    SHA, raises :class:`VcsCloneError` rather than fetching a floating
    ref.  When False, the helper consults ``git ls-remote`` to resolve
    the named ref to a SHA, then performs a shallow clone of that
    commit.

    Idempotent: if the destination already exists with a populated
    ``.git`` folder, no fetch happens.
    """
    sha = _resolve_sha(request, require_pin=require_pin)
    dest = cache_root / "vcs" / _repo_key(request.repo_url) / sha
    if (dest / ".git").is_dir():
        return VcsClone(
            path=dest,
            commit_sha=sha,
            subdirectory=request.subdirectory,
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Partial clone from a prior failure: wipe and retry.
        shutil.rmtree(dest)
    _shallow_clone(request.repo_url, sha, dest)
    return VcsClone(
        path=dest,
        commit_sha=sha,
        subdirectory=request.subdirectory,
    )


def _resolve_sha(request: VcsRequest, *, require_pin: bool) -> str:
    """Return a 40-char SHA for ``request.ref``.

    Raises when ``require_pin`` is True and ``ref`` is not already a
    SHA.  Otherwise consults ``git ls-remote`` to look up the ref.
    """
    if request.ref and FULL_GIT_SHA_RE.match(request.ref):
        return request.ref

    if require_pin:
        msg = (
            f"refusing to resolve floating ref {request.ref!r} for"
            f" {request.repo_url!r}: vcs_require_pin is True"
        )
        raise VcsCloneError(msg)

    target = request.ref or "HEAD"
    ls_remote_args = ["git", "ls-remote", request.repo_url, target]
    try:
        proc = subprocess.run(  # noqa: S603 - URL admission upstream
            ls_remote_args,
            check=True,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        msg = f"git ls-remote {request.repo_url} {target}: {exc}"
        raise VcsCloneError(msg) from exc

    line = proc.stdout.strip().splitlines()
    if not line:
        msg = f"no ref {target!r} found at {request.repo_url}"
        raise VcsCloneError(msg)

    sha = line[0].split()[0]
    if not FULL_GIT_SHA_RE.match(sha):
        msg = f"unexpected ls-remote output: {line[0]!r}"
        raise VcsCloneError(msg)

    return sha


def _shallow_clone(repo_url: str, sha: str, dest: Path) -> None:
    """Shallow-clone ``repo_url`` at exactly ``sha`` to ``dest``.

    Uses ``git init`` + ``git fetch --depth 1`` to land precisely the
    chosen commit without pulling history.  Hosts that disallow
    direct sha fetch surface as a :class:`VcsCloneError` from the
    fetch step.
    """
    dest.mkdir(parents=True)
    init_args = ["git", "init", "--quiet"]
    fetch_args = ["git", "fetch", "--quiet", "--depth", "1", repo_url, sha]
    checkout_args = ["git", "checkout", "--quiet", "FETCH_HEAD"]
    try:
        subprocess.run(  # noqa: S603 - git is a runtime dep
            init_args,
            check=True,
            cwd=dest,
            env=_git_env(),
        )
        subprocess.run(  # noqa: S603 - URL admission upstream
            fetch_args,
            check=True,
            cwd=dest,
            env=_git_env(),
        )
        subprocess.run(  # noqa: S603 - git is a runtime dep
            checkout_args,
            check=True,
            cwd=dest,
            env=_git_env(),
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        # Roll back the partial clone so the cache stays clean.
        shutil.rmtree(dest, ignore_errors=True)
        msg = f"failed to clone {repo_url} @ {sha}: {exc}"
        raise VcsCloneError(msg) from exc


def _git_env() -> dict[str, str]:
    """Return an environment for git subprocesses.

    Disables interactive prompts and any auto-detected user config
    so the clone fails fast on unauthenticated repos rather than
    hanging.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return env
