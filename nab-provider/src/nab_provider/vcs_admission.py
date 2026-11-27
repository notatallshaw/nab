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

__lazy_modules__ = (
    "nab_provider.vcs_request",
    "nab_resolver.diagnostics",
    "posixpath",
    "urllib.parse",
)

import enum
import posixpath
import re
from typing import Final
from urllib.parse import unquote, urlsplit

from nab_resolver.diagnostics import DiagnosticMessage, Suggestion

from .value import HashableRecord
from .vcs_request import (
    FULL_GIT_SHA_RE,
    VcsCloneError,
    VcsRequest,
    is_mercurial_node,
    mercurial_transport,
)

__all__ = [
    "UnsupportedVcsError",
    "VcsConfig",
    "VcsPolicy",
]


class VcsPolicy(enum.Enum):
    """Whether to honor VCS direct-URL requirements (``pkg @ git+https://...``).

    Cloning a remote repo is one short step from arbitrary code execution
    (modern Python projects build via PEP 517 backends, which run user
    code). By default, each source declaration selects one repository.
    """

    BLOCK = "block"
    """Refuse any requirement whose URL is non-empty."""

    DECLARED = "declared"
    """Allow only URLs chosen by a VCS source declaration."""

    ALLOW = "allow"
    """Honor VCS subject to scheme + repo allowlists.

    Cloning a repo is staged in two phases: admission (this enum decides
    whether a URL is even considered) and materialisation (the clone
    happens lazily through :class:`VcsSource` when the resolver asks
    for the package's metadata).
    """


class UnsupportedVcsError(Exception):
    """A VCS / direct-URL requirement was refused by policy.

    Raised eagerly during requirement ingestion; not surfaced as a
    "no candidates" backtrack so the user sees a clear diagnostic.
    """


class VcsConfig(HashableRecord):
    """Bundle of VCS opt-in knobs passed through the resolver stack.

    Defaults to declared sources, empty general allowlists, and required pins.
    """

    __match_args__ = ("policy", "allowed_schemes", "allowed_repos", "require_pin")

    policy: Final[VcsPolicy]
    allowed_schemes: Final[frozenset[str]]
    allowed_repos: Final[tuple[str, ...]]
    require_pin: Final[bool]

    def __init__(
        self,
        *,
        policy: VcsPolicy = VcsPolicy.DECLARED,
        allowed_schemes: frozenset[str] = frozenset(),
        allowed_repos: tuple[str, ...] = (),
        require_pin: bool = True,
    ) -> None:
        """Retain the repository admission and revision-pin policies."""
        self.policy = policy
        self.allowed_schemes = allowed_schemes
        self.allowed_repos = allowed_repos
        self.require_pin = require_pin


_VCS_SCHEMES: frozenset[str] = frozenset(
    {
        "git+https",
        "git+ssh",
        "git+http",
        "git+file",
        "git+git",
        "hg+https",
        "hg+file",
    }
)


def known_vcs_schemes() -> frozenset[str]:
    """Return the schemes allowed in a ``vcs.allowed-schemes`` list."""
    return _VCS_SCHEMES


_VCS_INSECURE_SCHEMES: frozenset[str] = frozenset({"git+git", "git+http"})
_MALFORMED_VCS_URL = "refusing malformed VCS URL: the URL does not parse."


def split_vcs_scheme(url: str) -> tuple[str | None, str]:
    """Strip a recognized VCS scheme prefix.

    ``"git+https://example.com/r.git@v1"`` -> ``("git+https", "https://example.com/r.git@v1")``.
    ``"https://example.com/file.whl"``     -> ``(None,        "https://example.com/file.whl")``.

    Returns ``(None, url)`` for non-VCS URLs (e.g. plain ``https://``
    archives or ``file://`` paths) so the caller can refuse them
    separately.
    """
    for vcs_scheme in _VCS_SCHEMES:
        if url.startswith(f"{vcs_scheme}://"):
            return (vcs_scheme, url.partition("+")[2])
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
_GIT_SUFFIX = ".git"


def _skip_optional_git_suffix(rest: str) -> str:
    """Return ``rest`` without a leading ``.git`` that ends the repo path.

    ``.git`` is optional only where a repo name ends, so it is skipped
    when nothing but a trailing ``/``, the ref or the fragment follows
    it.  In ``foo.git/bar`` it is part of a directory name and stays.
    """
    if not rest.startswith(_GIT_SUFFIX):
        return rest

    tail = rest[len(_GIT_SUFFIX) :]
    after_slashes = tail.lstrip("/")
    if after_slashes and after_slashes[0] not in _REPO_BOUNDARY_CHARS:
        return rest
    return tail


def _repo_prefix_matches(inner_url: str, prefix: str, *, git: bool) -> bool:
    r"""Return True if ``inner_url`` names a repo under ``prefix``.

    A bare :meth:`str.startswith` would admit a sibling repo whose URL
    merely begins with an allowed entry (``.../airflow.git`` would admit
    ``.../airflow.git.other``).  The match here requires the prefix to end
    at a path-segment boundary: the candidate must equal the prefix, the
    prefix must already end in a separator, or the next candidate
    character must be ``/`` (path), ``@`` (ref) or ``#`` (fragment).
    Git treats a trailing ``.git`` as optional, so it is stripped from the
    prefix and skipped on the candidate by :func:`_skip_optional_git_suffix`
    before the boundary check.
    Both URLs have their authority ``user[:pass]@`` / ``git@`` stripped
    by the caller.

    """
    if git:
        prefix = prefix.removesuffix(_GIT_SUFFIX)
    if not inner_url.startswith(prefix):
        return False

    rest = inner_url[len(prefix) :]
    if git:
        rest = _skip_optional_git_suffix(rest)
    if not rest or not prefix or prefix[-1] in _REPO_BOUNDARY_CHARS:
        return True
    return rest[0] in _REPO_BOUNDARY_CHARS


def _git_path_is_literal(inner_url: str) -> bool:
    """Check the path Git will receive after removing the revision."""
    parts = urlsplit(inner_url)
    remainder = f"{parts.path}?{parts.query}" if parts.query else parts.path
    repo = _drop_ref(remainder)
    # The query can contribute to the path of a file URL.
    return not any(_rewritten_by_git(part) for part in (repo, repo.partition("?")[0]))


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
    return bool(FULL_GIT_SHA_RE.fullmatch(ref))


def _display_url(url: str) -> str:
    """Show the repository location without userinfo, query or fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<malformed URL>"

    if not parts.netloc:
        if parts.path and parts.scheme not in {"file", "git+file", "hg+file"}:
            return "<URL without a host>"
        # Preserve the empty authority in git+file:///path and scheme-only prefixes.
        return url.partition("?")[0].partition("#")[0]

    host = parts.netloc.rpartition("@")[2]
    return parts._replace(netloc=host, query="", fragment="").geturl()


def _mercurial_repo_url(url: str) -> str:
    """Require an unambiguous transport URL and a complete Mercurial node."""
    try:
        request = VcsRequest.parse(url)
    except VcsCloneError:
        msg = "refusing malformed Mercurial source URL"
        raise UnsupportedVcsError(msg) from None
    try:
        mercurial_transport(request.repo_url)
    except VcsCloneError as exc:
        raise UnsupportedVcsError(str(exc)) from None
    if not is_mercurial_node(request.ref):
        msg = "Mercurial sources require a full 40-character changeset ID"
        raise UnsupportedVcsError(msg)

    return request.repo_url


def _check_vcs_policy(url: str, policy: VcsPolicy, *, declared: bool) -> None:
    """Distinguish source-specific permission from permission for other URLs."""
    if policy is VcsPolicy.BLOCK:
        report = DiagnosticMessage(
            (
                f"refusing VCS requirement\n    {_display_url(url)}\n"
                '    reason: vcs.policy is "block".'
            ),
            suggestions=(
                Suggestion(
                    'set vcs.policy = "declared" to permit declared sources, or '
                    'configure vcs.policy = "allow" and its allowlists'
                ),
            ),
        )
        raise UnsupportedVcsError(report)

    if policy is VcsPolicy.DECLARED and not declared:
        report = DiagnosticMessage(
            (
                f"refusing VCS requirement\n    {_display_url(url)}\n"
                '    reason: vcs.policy is "declared" and the requirement'
                " uses a direct URL."
            ),
            suggestions=(
                Suggestion("declare the package's repository in vcs-sources"),
            ),
        )
        raise UnsupportedVcsError(report)


def _require_git_repo_path(url: str, inner_url: str) -> None:
    """Require a Git repository path before a revision."""
    try:
        path = _repo_path(inner_url)
    except ValueError:
        raise UnsupportedVcsError(_MALFORMED_VCS_URL) from None
    if path.strip("/"):
        return

    report = DiagnosticMessage(
        (
            f"refusing VCS URL that names no repository\n    {_display_url(url)}\n"
            "    reason: the URL has no repository path."
        ),
        suggestions=(
            Suggestion(
                "use a repository URL like git+https://host/org/repo.git@<ref>",
                detail="Everything after the final @ is the ref.",
            ),
        ),
    )
    raise UnsupportedVcsError(report)


def admit_vcs_url(url: str, config: VcsConfig, *, declared: bool = False) -> str:
    """Admit a VCS URL, with explicit declarations confined to their own URL."""
    scheme, inner_url = split_vcs_scheme(url)
    if scheme is None:
        report = DiagnosticMessage(
            (
                "refusing direct-URL requirement (not a recognized VCS scheme)\n    "
                f"{_display_url(url)}"
            ),
            suggestions=(Suggestion("use one of " + ", ".join(sorted(_VCS_SCHEMES))),),
        )
        raise UnsupportedVcsError(report)

    _check_vcs_policy(url, config.policy, declared=declared)

    if config.policy is VcsPolicy.ALLOW and scheme not in config.allowed_schemes:
        allowed_str = ", ".join(sorted(config.allowed_schemes)) or "<empty>"
        msg = (
            "refusing VCS scheme\n"
            f"    {_display_url(url)}\n"
            f'    reason: scheme "{scheme}" not in vcs.allowed-schemes='
            f"{{{allowed_str}}}."
        )
        suggestions = (
            (Suggestion(f'use an https/ssh variant ("{scheme}" is unauthenticated)'),)
            if scheme in _VCS_INSECURE_SCHEMES
            else ()
        )
        raise UnsupportedVcsError(DiagnosticMessage(msg, suggestions=suggestions))

    git = scheme.startswith("git+")
    if git:
        _require_git_repo_path(url, inner_url)
    else:
        inner_url = _mercurial_repo_url(url)

    # Stripping userinfo can unbalance an IPv6 bracket, so the match may raise
    # on a netloc that parsed whole above.
    try:
        candidate = _without_userinfo(inner_url)
        literal = not git or _git_path_is_literal(candidate)
        repo_admitted = literal and (
            config.policy is VcsPolicy.DECLARED
            or any(
                _repo_prefix_matches(candidate, _without_userinfo(prefix), git=git)
                for prefix in config.allowed_repos
            )
        )
    except ValueError:
        raise UnsupportedVcsError(_MALFORMED_VCS_URL) from None

    if not repo_admitted:
        if config.policy is VcsPolicy.DECLARED:
            msg = "refusing VCS source with a repository path Git would rewrite"
            raise UnsupportedVcsError(msg)
        allowed_str = (
            ", ".join(sorted(_display_url(repo) for repo in config.allowed_repos))
            or "<empty>"
        )
        msg = (
            "refusing VCS repo\n"
            f"    {_display_url(url)}\n"
            "    reason: repo URL prefix not in vcs.allowed-repos.\n"
            f"    allowed prefixes: {allowed_str}"
        )
        raise UnsupportedVcsError(msg)

    if git and config.require_pin and not has_full_commit_sha(url):
        report = DiagnosticMessage(
            (
                f"refusing unpinned VCS ref\n    {_display_url(url)}\n"
                "    reason: vcs.require-pin is true and no 40-char commit hash "
                "present."
            ),
            suggestions=(
                Suggestion(
                    (
                        "pin the requirement to a 40-char commit hash, or set "
                        "vcs.require-pin = false"
                    ),
                    detail="An unpinned requirement is not reproducible.",
                ),
            ),
        )
        raise UnsupportedVcsError(report)

    return scheme
