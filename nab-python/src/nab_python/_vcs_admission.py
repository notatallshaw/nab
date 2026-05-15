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
from dataclasses import dataclass

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
    ``vcs_allowed_schemes`` and per-repo via ``vcs_allowed_repos``.
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
        "git",
        "hg+https",
        "hg+ssh",
        "hg+http",
        "hg+file",
        "hg+static-http",
        "bzr+https",
        "bzr+ssh",
        "bzr+sftp",
        "bzr+ftp",
        "bzr+lp",
        "bzr+file",
        "svn",
        "svn+https",
        "svn+ssh",
        "svn+http",
        "svn+file",
    }
)

_VCS_INSECURE_SCHEMES: frozenset[str] = frozenset(
    {"git", "git+git", "git+http", "hg+http", "bzr+http", "svn", "svn+http"}
)


def split_vcs_scheme(url: str) -> tuple[str | None, str]:
    """Strip a recognized VCS scheme prefix.

    ``"git+https://example.com/r.git@v1"`` -> ``("git+https", "https://example.com/r.git@v1")``.
    ``"svn://example.com/r"``              -> ``("svn",       "svn://example.com/r")``.
    ``"https://example.com/file.whl"``     -> ``(None,        "https://example.com/file.whl")``.

    Returns ``(None, url)`` for non-VCS URLs (e.g. plain ``https://``
    archives or ``file://`` paths) so the caller can refuse them
    separately.  Pip-compatible scheme list; not standardized by any PEP.
    """
    for vcs_scheme in _VCS_SCHEMES:
        if not url.startswith(f"{vcs_scheme}://"):
            continue
        inner_scheme, plus, _ = vcs_scheme.partition("+")
        if not plus:
            return (vcs_scheme, url)
        return (vcs_scheme, url[len(inner_scheme) + 1 :])
    return (None, url)


def has_full_commit_sha(url: str) -> bool:
    """Return True if the URL pins to a 40-char hex commit hash.

    Looks for ``@<sha>`` after the scheme://host portion; ignores any
    ``#`` fragment.  Tolerates ``user@host`` syntax by taking the last
    ``@`` in the path/ref portion.  Mercurial uses 40-char SHA1 too, so
    the same regex covers git+hg.  Bzr/svn revisions are not hashes;
    ``vcs_require_pin = False`` is the way to allow those.
    """
    fragmentless = url.split("#", 1)[0]
    after_authority = fragmentless.split("://", 1)[-1]
    if "@" not in after_authority:
        return False
    ref = after_authority.rsplit("@", 1)[1]
    return bool(FULL_GIT_SHA_RE.match(ref))


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
            "    note: nab supports git+/hg+/bzr+/svn+ schemes only;"
            " plain http(s)/file archive URLs are not supported."
        )
        raise UnsupportedVcsError(msg)

    if config.policy is VcsPolicy.BLOCK:
        msg = (
            "refusing VCS requirement\n"
            f"    {url}\n"
            "    reason: VcsPolicy is BLOCK (default).\n"
            "    to allow: set vcs_policy=VcsPolicy.ALLOW with appropriate\n"
            "              vcs_allowed_schemes (and optionally vcs_allowed_repos)."
        )
        raise UnsupportedVcsError(msg)

    if scheme not in config.allowed_schemes:
        allowed_str = ", ".join(sorted(config.allowed_schemes)) or "<empty>"
        msg = (
            "refusing VCS scheme\n"
            f"    {url}\n"
            f'    reason: scheme "{scheme}" not in vcs_allowed_schemes='
            f"{{{allowed_str}}}."
        )
        if scheme in _VCS_INSECURE_SCHEMES:
            msg += (
                f'\n    note: "{scheme}" is unauthenticated;'
                " consider an https/ssh variant."
            )
        raise UnsupportedVcsError(msg)

    if config.allowed_repos and not any(
        inner_url.startswith(prefix) for prefix in config.allowed_repos
    ):
        allowed_str = ", ".join(sorted(config.allowed_repos))
        msg = (
            "refusing VCS repo\n"
            f"    {url}\n"
            "    reason: repo URL prefix not in vcs_allowed_repos.\n"
            f"    allowed prefixes: {allowed_str}"
        )
        raise UnsupportedVcsError(msg)

    if config.require_pin and not has_full_commit_sha(url):
        msg = (
            "refusing unpinned VCS ref\n"
            f"    {url}\n"
            "    reason: vcs_require_pin is True and no 40-char commit hash"
            " present.\n"
            "    to allow: pin the requirement to a 40-char commit hash, or set\n"
            "              vcs_require_pin=False (not recommended for"
            " reproducible installs)."
        )
        raise UnsupportedVcsError(msg)

    return scheme
