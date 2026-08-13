"""Direct-URL / VCS requirement admission for the provider.

Cloning a remote repo is one short step from arbitrary code
execution because modern Python projects build via PEP 517
backends, which run user code.  This module owns the policy types
(:class:`VcsPolicy`, :class:`VcsConfig`), the URL classifier
(:func:`split_vcs_scheme`, :func:`has_full_commit_sha`), and the
admit-or-refuse decision (:func:`admit_vcs_url`) called eagerly
during requirement ingestion.

The actual clone path lives in :mod:`nab_index.vcs` and runs only
after admission lets the URL through.
"""

from __future__ import annotations

import enum
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from nab_index.vcs import FULL_GIT_SHA_RE

__all__ = [
    "UnsupportedVcsError",
    "VcsConfig",
    "VcsPolicy",
]


class VcsPolicy(enum.Enum):
    """Whether to honor VCS direct-URL requirements (``pkg @ git+https://...``).

    Cloning a remote repo is one short step from arbitrary code execution
    (modern Python projects build via PEP 517 backends, which run user
    code).  Default posture is :attr:`BLOCK`; opt-in is per-protocol via
    ``vcs.allowed-schemes`` and per-repo via ``vcs.allowed-repos``.
    """

    BLOCK = "block"
    """Refuse any requirement whose URL is non-empty."""

    ALLOW = "allow"
    """Honor VCS subject to scheme + repo allowlists.

    Cloning a repo is staged in two phases: admission (this enum gates
    whether a URL is even considered) and materialisation (the clone
    happens lazily through :class:`VcsSource` when the resolver asks
    for the package's metadata).
    """


class UnsupportedVcsError(Exception):
    """A VCS / direct-URL requirement was refused by policy.

    Raised eagerly during requirement ingestion; not surfaced as a
    "no candidates" backtrack so the user sees a clear diagnostic.
    """


@dataclass(frozen=True)
class VcsConfig:
    """Bundle of VCS opt-in knobs passed through the resolver stack.

    Default is fully restrictive (``BLOCK`` policy, empty allowlists,
    pin required).
    """

    policy: VcsPolicy = VcsPolicy.BLOCK
    allowed_schemes: frozenset[str] = frozenset()
    allowed_repos: tuple[str, ...] = ()
    require_pin: bool = True


_VCS_SCHEMES: frozenset[str] = frozenset(
    {
        "git+https",
        "git+ssh",
        "git+http",
        "git+file",
        "git+git",
    }
)


def known_vcs_schemes() -> frozenset[str]:
    """Return the VCS URL schemes nab recognises (e.g. ``git+https``).

    nab is git-only, so every entry is a ``git+`` scheme.  Exposed so
    config parsing can reject an unknown ``vcs.allowed-schemes`` entry
    without importing the private scheme set.
    """
    return _VCS_SCHEMES


_VCS_INSECURE_SCHEMES: frozenset[str] = frozenset({"git+git", "git+http"})


def split_vcs_scheme(url: str) -> tuple[str | None, str]:
    """Strip a recognized VCS scheme prefix.

    ``"git+https://example.com/r.git@v1"`` -> ``("git+https", "https://example.com/r.git@v1")``.
    ``"https://example.com/file.whl"``     -> ``(None,        "https://example.com/file.whl")``.

    Returns ``(None, url)`` for non-VCS URLs (e.g. plain ``https://``
    archives or ``file://`` paths) so the caller can refuse them
    separately.  Only ``git+`` schemes are recognised; ``hg+``/``bzr+``/``svn+``
    are intentionally absent so they are refused as non-VCS.
    """
    for vcs_scheme in _VCS_SCHEMES:
        if url.startswith(f"{vcs_scheme}://"):
            return (vcs_scheme, url[len("git+") :])
    return (None, url)


# The authority ends at the first "/", "?" or "#"; its userinfo ends at
# the last "@" before that.
_AUTHORITY_USERINFO_RE = re.compile(r"^([^/?#]*//)[^/?#]*@")


def _without_userinfo(url: str) -> str:
    """Drop any authority ``user[:pass]@`` / SSH ``git@`` from ``url``.

    An ``allowed-repos`` prefix names a repo by scheme + host + path, not
    by credentials, so both the candidate URL and the prefix are stripped
    before the match. A URL with no userinfo is returned unchanged.

    The cut is made on the raw string, since
    :func:`urllib.parse.urlsplit` deletes every tab, CR and LF and does
    not record an empty ``?``: a URL rebuilt from the parse is not the
    one git is handed.
    """
    if "@" not in urlsplit(url).netloc:
        return url
    return _AUTHORITY_USERINFO_RE.sub(r"\1", url)


def _drop_ref(remainder: str) -> str:
    """Return ``remainder`` without a trailing ``@<ref>``, split on the last ``@``."""
    return remainder.rpartition("@")[0] if "@" in remainder else remainder


def _repo_path(inner_url: str) -> str:
    """Return the path of ``inner_url`` with any trailing ``@<ref>`` dropped.

    An empty result means the URL names no repo.
    """
    return _drop_ref(urlsplit(inner_url).path)


def _rewritten_by_git(path: str) -> bool:
    r"""Return True if git would rewrite ``path`` before it fetches.

    Git applies RFC 3986 dot-segment removal at fetch time.  ``path`` is
    decoded once so an encoded ``%2e%2e`` cannot slip past, and ``\`` is
    folded to ``/`` because Windows resolves it as a separator.  A trailing
    ``/`` is put back: RFC 3986 keeps it and :func:`posixpath.normpath`
    does not.
    """
    decoded = unquote(path).replace("\\", "/")
    normalised = posixpath.normpath(decoded)
    if decoded.endswith("/") and not normalised.endswith("/"):
        normalised += "/"

    return bool(decoded) and normalised != decoded


_REPO_BOUNDARY_CHARS: frozenset[str] = frozenset({"/", "@", "#"})


def _repo_prefix_matches(inner_url: str, prefix: str) -> bool:
    r"""Return True if ``inner_url`` names a repo under ``prefix``.

    A bare :meth:`str.startswith` would admit a sibling repo whose URL
    merely begins with an allowed entry (``.../airflow.git`` would admit
    ``.../airflow.git.other``).  The match here requires the prefix to end
    at a path-segment boundary: the candidate must equal the prefix, the
    prefix must already end in a separator, or the next candidate
    character must be ``/`` (path), ``@`` (ref) or ``#`` (fragment).
    Git treats the ``.git`` suffix as optional, so it is stripped from the
    prefix and skipped once on the candidate before the boundary check.
    Both URLs have their authority ``user[:pass]@`` / ``git@`` stripped
    by the caller.

    A path git would rewrite is refused first, since a ``..`` could pass
    the string match while git fetches a repo outside the prefix.  The ref
    is dropped before that check, off the whole post-authority remainder
    rather than the path alone, matching the split :mod:`nab_index.vcs`
    makes at clone time; otherwise a ``..`` in the final segment hides as
    the ordinary name ``..@<ref>``.  The prefix comparison below is on the
    raw URL.
    """
    parts = urlsplit(inner_url)
    remainder = f"{parts.path}?{parts.query}" if parts.query else parts.path
    repo = _drop_ref(remainder)

    # An http URL ends its path at the "?"; a file URL keeps it as a path
    # character, and either way the query reaches git.
    if any(_rewritten_by_git(part) for part in (repo, repo.partition("?")[0])):
        return False

    prefix = prefix.removesuffix(".git")
    if not inner_url.startswith(prefix):
        return False
    rest = inner_url[len(prefix) :].removeprefix(".git")
    if not rest or not prefix or prefix[-1] in _REPO_BOUNDARY_CHARS:
        return True
    return rest[0] in _REPO_BOUNDARY_CHARS


def has_full_commit_sha(url: str) -> bool:
    """Return True if the URL pins to a 40-char hex commit hash.

    Looks for ``@<sha>`` in the path component (after the authority);
    ignores any ``#`` fragment.  A ``user@host`` in the authority is
    left alone, matching the ref parsing in :mod:`nab_index.vcs`.
    """
    fragmentless = url.split("#", 1)[0]
    after_scheme = fragmentless.split("://", 1)[-1]
    path = after_scheme.partition("/")[2]
    if "@" not in path:
        return False
    ref = path.rsplit("@", 1)[1]
    return bool(FULL_GIT_SHA_RE.match(ref))


def _malformed_vcs_error(url: str) -> UnsupportedVcsError:
    """Refusal for a URL whose authority ``urlsplit`` cannot parse."""
    return UnsupportedVcsError(
        f"refusing malformed VCS URL\n    {url}\n    reason: the URL does not parse."
    )


def admit_vcs_url(url: str, config: VcsConfig) -> str:
    """Admit a direct-URL requirement, or raise :class:`UnsupportedVcsError`.

    Returns the recognized VCS scheme on success.  Called eagerly when
    ingesting root requirements with a non-empty
    :attr:`Requirement.url <packaging.requirements.Requirement.url>`.
    """
    scheme, inner_url = split_vcs_scheme(url)
    if scheme is None:
        msg = (
            "refusing direct-URL requirement (not a recognized VCS scheme)\n"
            f"    {url}\n"
            "    note: nab supports git+https / git+ssh / git+http /"
            " git+file / git+git only; hg/bzr/svn and plain"
            " http(s)/file archive URLs are not supported."
        )
        raise UnsupportedVcsError(msg)

    if config.policy is VcsPolicy.BLOCK:
        msg = (
            "refusing VCS requirement\n"
            f"    {url}\n"
            '    reason: vcs.policy is "block" (default).\n'
            '    to allow: set vcs.policy = "allow" with appropriate\n'
            "              vcs.allowed-schemes (and vcs.allowed-repos)."
        )
        raise UnsupportedVcsError(msg)

    if scheme not in config.allowed_schemes:
        allowed_str = ", ".join(sorted(config.allowed_schemes)) or "<empty>"
        msg = (
            "refusing VCS scheme\n"
            f"    {url}\n"
            f'    reason: scheme "{scheme}" not in vcs.allowed-schemes='
            f"{{{allowed_str}}}."
        )
        if scheme in _VCS_INSECURE_SCHEMES:
            msg += (
                f'\n    note: "{scheme}" is unauthenticated;'
                " consider an https/ssh variant."
            )
        raise UnsupportedVcsError(msg)

    try:
        repo_path = _repo_path(inner_url)
    except ValueError:
        raise _malformed_vcs_error(url) from None

    # Without a repo path there is nothing to clone, and an appended pin's
    # "@" lands in the netloc instead of the path, where urlsplit reads
    # "host@<sha>" as userinfo "host" and host "<sha>".  Requiring a path
    # leaves the checks below a real host and a real repo to look at.
    if not repo_path.strip("/"):
        msg = (
            "refusing VCS URL that names no repository\n"
            f"    {url}\n"
            "    reason: the URL has no repository path.\n"
            "    note: everything after the final @ is the ref, so a repo URL\n"
            "          looks like git+https://host/org/repo.git@<ref>."
        )
        raise UnsupportedVcsError(msg)

    # Stripping userinfo can unbalance an IPv6 bracket, so the match may raise
    # on a netloc that parsed whole above.
    try:
        repo_admitted = any(
            _repo_prefix_matches(
                _without_userinfo(inner_url), _without_userinfo(prefix)
            )
            for prefix in config.allowed_repos
        )
    except ValueError:
        raise _malformed_vcs_error(url) from None

    # An empty allowed-repos denies every repo (deny-all), matching
    # allowed-schemes: under policy = "allow" the user must list at least
    # one repo prefix.
    if not repo_admitted:
        allowed_str = ", ".join(sorted(config.allowed_repos)) or "<empty>"
        msg = (
            "refusing VCS repo\n"
            f"    {url}\n"
            "    reason: repo URL prefix not in vcs.allowed-repos.\n"
            f"    allowed prefixes: {allowed_str}"
        )
        raise UnsupportedVcsError(msg)

    if config.require_pin and not has_full_commit_sha(url):
        msg = (
            "refusing unpinned VCS ref\n"
            f"    {url}\n"
            "    reason: vcs.require-pin is true and no 40-char commit hash"
            " present.\n"
            "    to allow: pin the requirement to a 40-char commit hash, or set\n"
            "              vcs.require-pin = false (not recommended for"
            " reproducible installs)."
        )
        raise UnsupportedVcsError(msg)

    return scheme
