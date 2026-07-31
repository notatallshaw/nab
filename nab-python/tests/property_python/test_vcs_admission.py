"""Property tests for :mod:`nab_python._vcs_admission` Layer-1 policy.

Invariants:

* total partition: any URL/config either returns a recognised scheme
  or raises UnsupportedVcsError; no other exception ever escapes
  (robustness over arbitrary text);
* determinism: same inputs, same decision;
* docstring-derived oracle agrees with the implementation;
* fragment irrelevance: appending a ``#fragment`` never changes the
  decision (``has_full_commit_sha`` strips fragments; prefix checks
  are on the head of the URL);
* repo path required: a URL whose path names no repository (nothing
  before the final ``@``) is refused, so the ref of an appended pin
  can never land in the authority;
* pin monotonicity: a URL admitted under ``require_pin=False`` is
  admitted under ``require_pin=True`` once ``@<40-hex-sha>`` is
  appended;
* admission/clone agreement: a URL admitted under
  ``require_pin=True`` must parse (``VcsRequest.parse``) to a ref
  that IS a full commit sha, i.e. the admission layer's "pinned"
  promise holds at clone time;
* containment: a path that normalises above where it is written is
  refused however permissive the allowlists are, whether written raw,
  percent-encoded, or with backslashes.
"""

from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.vcs import VcsRequest
from nab_python._vcs_admission import (
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
    admit_vcs_url,
)

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

VALID_SCHEMES = ["git+https", "git+ssh", "git+http", "git+file", "git+git"]
ALL_SCHEMES = [
    *VALID_SCHEMES,
    "https",
    "http",
    "file",
    "hg+https",
    "svn+ssh",
    "GIT+HTTPS",
]

SHA = "a" * 39 + "b"
UPPER_SHA = SHA.upper()


def _is_full_sha(ref: str) -> bool:
    """Hand-transcribed "exactly 40 hex chars" rule.

    Kept independent of the implementation's ``FULL_GIT_SHA_RE`` so the
    oracle checks the documented policy rather than the regex against
    itself.
    """
    return len(ref) == 40 and all(c in "0123456789abcdefABCDEF" for c in ref)


configs = st.builds(
    VcsConfig,
    policy=st.sampled_from([VcsPolicy.BLOCK, VcsPolicy.ALLOW]),
    allowed_schemes=st.frozensets(st.sampled_from(VALID_SCHEMES), max_size=5),
    allowed_repos=st.lists(
        st.sampled_from(
            [
                "https://github.com/org",
                "https://example.org/",
                "ssh://git@github.com/org/repo.git",
                "",
            ]
        ),
        max_size=2,
    ).map(tuple),
    require_pin=st.booleans(),
)


@st.composite
def structured_urls(draw: st.DrawFn) -> str:
    scheme = draw(st.sampled_from(ALL_SCHEMES))
    user = draw(st.sampled_from(["", "git@", "user@"]))
    host = draw(st.sampled_from(["github.com", "example.org", "h"]))
    segments = draw(
        st.lists(
            st.sampled_from(
                [
                    "org",
                    "orgx",
                    "repo.git",
                    "repo.gitx",
                    "repo",
                    "a",
                    "..",
                    "%2e%2e",
                    "..%2f..",
                    "..\\..",
                ]
            ),
            max_size=3,
        )
    )
    ref = draw(
        st.sampled_from(
            [
                "",
                f"@{SHA}",
                "@main",
                f"@{UPPER_SHA}",
                "@release/1.0",
                "@",
                f"@{'a' * 39}",  # 39 chars: one short of a full SHA
                f"@{'a' * 41}",  # 41 chars: one over
                f"@{'g' * 40}",  # 40 chars but not all hex
            ]
        )
    )
    frag = draw(st.sampled_from(["", "#subdirectory=src", "#egg=foo", "#x@y"]))
    # Keep any ref in the path component; a ref directly after the
    # authority is not generated here.
    if ref and not segments:
        segments = ["repo"]
    path = "/" + "/".join(segments) if segments else ""
    return f"{scheme}://{user}{host}{path}{ref}{frag}"


@st.composite
def escaping_urls(draw: st.DrawFn) -> str:
    """A pinned URL whose path resolves one level above where it is written."""
    scheme = draw(st.sampled_from(VALID_SCHEMES))
    host = draw(st.sampled_from(["github.com", "h"]))
    escape = draw(st.sampled_from(["..", "%2e%2e", "%2E%2E", "..%2f..", "..\\.."]))
    tail = draw(st.sampled_from(["other", "other/repo", "repo"]))
    return f"{scheme}://{host}/org/repo/{escape}/{tail}@{SHA}"


def _decision(url: str, config: VcsConfig) -> tuple[str, str]:
    try:
        return ("admit", admit_vcs_url(url, config))
    except UnsupportedVcsError:
        return ("refuse", "")


def _drop_login(url: str) -> str:
    """Strip any authority ``user[:pass]@`` / ``git@`` from ``url``."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _prefix_under_repo(inner: str, prefix: str) -> bool:
    r"""Boundary-aware repo-prefix check, transcribed from the documented policy.

    A candidate is under an allowed prefix only when the prefix ends at a
    path-segment boundary, so a sibling repo whose URL merely begins with
    the prefix (``.../airflow.git`` vs ``.../airflow.git.other``) is refused.
    Git's optional ``.git`` suffix is stripped from the prefix and skipped
    once on the candidate so an exact-repo prefix and the ``.git`` clone URL
    match either way round.  A candidate whose path git would rewrite is
    refused first: the whole post-authority remainder, decoded once and with
    ``\`` folded to ``/``, must equal its dot-segment-normalised form.
    """
    parts = urlsplit(inner)
    remainder = f"{parts.path}?{parts.query}" if parts.query else parts.path
    path = unquote(remainder).replace("\\", "/")

    if path and posixpath.normpath(path) != path:
        return False

    prefix = prefix.removesuffix(".git")
    if not inner.startswith(prefix):
        return False
    rest = inner[len(prefix) :].removeprefix(".git")
    if not rest or not prefix or prefix[-1] in "/@#":
        return True
    return rest[0] in "/@#"


def _oracle(url: str, config: VcsConfig) -> str:
    """Decision procedure transcribed from the documented policy."""
    scheme = next((s for s in VALID_SCHEMES if url.startswith(f"{s}://")), None)
    if scheme is None:
        return "refuse"
    if config.policy is VcsPolicy.BLOCK:
        return "refuse"
    if scheme not in config.allowed_schemes:
        return "refuse"
    inner = url[len("git+") :]
    path = urlsplit(inner).path
    repo_path = path.rpartition("@")[0] if "@" in path else path
    if not repo_path.strip("/"):
        return "refuse"
    inner = _drop_login(inner)
    if not any(_prefix_under_repo(inner, _drop_login(p)) for p in config.allowed_repos):
        return "refuse"
    if config.require_pin:
        fragmentless = url.split("#", 1)[0]
        after = fragmentless.split("://", 1)[-1]
        if "@" not in after:
            return "refuse"
        if not _is_full_sha(after.rsplit("@", 1)[1]):
            return "refuse"
    return "admit"


@PROPERTY_SETTINGS
@given(url=st.text(max_size=80), config=configs)
def test_total_partition_and_determinism_arbitrary_text(
    url: str, config: VcsConfig
) -> None:
    first = _decision(url, config)
    second = _decision(url, config)
    assert first == second
    if first[0] == "admit":
        assert first[1] in VALID_SCHEMES
        assert url.startswith(f"{first[1]}://")


@PROPERTY_SETTINGS
@given(url=structured_urls(), config=configs)
def test_matches_documented_oracle(url: str, config: VcsConfig) -> None:
    assert _decision(url, config)[0] == _oracle(url, config)


@PROPERTY_SETTINGS
@given(url=escaping_urls())
def test_escaping_path_refused_under_allow_all(url: str) -> None:
    """An allow-all config still refuses a path that resolves above itself.

    ``""`` is a prefix of every URL, so only the containment check can
    refuse these.
    """
    config = VcsConfig(
        policy=VcsPolicy.ALLOW,
        allowed_schemes=frozenset(VALID_SCHEMES),
        allowed_repos=("",),
    )
    assert _decision(url, config)[0] == "refuse"


@PROPERTY_SETTINGS
@given(
    url=structured_urls(),
    config=configs,
    frag=st.sampled_from(["#subdirectory=pkg", "#egg=a@b", "#", "#x#y"]),
)
def test_fragment_append_never_changes_decision(
    url: str, config: VcsConfig, frag: str
) -> None:
    if "#" in url:
        return
    assert _decision(url, config)[0] == _decision(url + frag, config)[0]


@PROPERTY_SETTINGS
@given(url=structured_urls(), config=configs)
def test_pin_append_monotonicity(url: str, config: VcsConfig) -> None:
    if "#" in url:
        return
    unpinned_cfg = VcsConfig(
        policy=config.policy,
        allowed_schemes=config.allowed_schemes,
        allowed_repos=config.allowed_repos,
        require_pin=False,
    )
    pinned_cfg = VcsConfig(
        policy=config.policy,
        allowed_schemes=config.allowed_schemes,
        allowed_repos=config.allowed_repos,
        require_pin=True,
    )
    if _decision(url, unpinned_cfg)[0] == "admit":
        assert _decision(f"{url}@{SHA}", pinned_cfg)[0] == "admit"


@PROPERTY_SETTINGS
@given(url=structured_urls())
def test_admitted_pin_is_a_real_pin_at_clone_time(url: str) -> None:
    config = VcsConfig(
        policy=VcsPolicy.ALLOW,
        allowed_schemes=frozenset(VALID_SCHEMES),
        # "" is an allow-all prefix (every inner URL starts with it), so the
        # repo gate passes and the pin/clone-agreement assertion is reached;
        # an empty tuple would deny every repo and make this test vacuous.
        allowed_repos=("",),
        require_pin=True,
    )
    kind, _scheme = _decision(url, config)
    if kind != "admit":
        return
    request = VcsRequest.parse(url)
    assert _is_full_sha(request.ref), (
        f"admission said pinned, clone parser sees ref={request.ref!r} for {url!r}"
    )
