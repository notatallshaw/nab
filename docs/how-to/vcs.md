# Add a VCS dependency

nab resolves dependencies pulled from git URLs through
`[tool.nab.vcs]` and `[[tool.nab.vcs-sources]]`.  Only git is
supported; hg, svn, and bzr are not.  The default posture is fully
restrictive: nothing VCS-shaped resolves without an explicit policy.

## Default posture

```toml
[tool.nab.vcs]
policy = "block"
allowed-schemes = []
allowed-repos = []
require-pin = true
```

`policy = "block"` means every VCS URL is rejected.  Switching to
`"allow"` is necessary but not sufficient: you also have to declare
allowlists.

## Layered allowlist

```toml
[tool.nab.vcs]
policy = "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["https://github.com/myorg/"]
require-pin = true
```

Three layers, each AND-checked:

1. `allowed-schemes`: pip-style scheme prefixes.  The supported set
   is `git+https`, `git+ssh`, `git+http`, `git+file`, `git+git`.
   Empty means "no scheme is allowed".  `git+http` and `git+git` are
   unauthenticated; prefer `git+https` or `git+ssh`.
2. `allowed-repos`: prefix match against the URL after the
   `git+` prefix is stripped, so the value includes the inner
   scheme.  `https://github.com/myorg/` matches every HTTPS repo
   under that org.  Empty means "no repo is allowed".
3. `require-pin`: when `true`, the URL must include a 40-char hex
   commit SHA after `@`.  Branch and tag references are rejected
   because they are mutable.

Everything after the final `@` is the ref, so the URL has to name a
repository ahead of it: `git+https://github.com` and
`git+https://github.com@<sha>` are both refused;
`git+https://github.com/myorg/pkg@<sha>` is not.

## Pinned VCS sources

The supported entry point is `[[tool.nab.vcs-sources]]`:

```toml
[tool.nab.vcs]
policy = "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["https://github.com/myorg/"]
require-pin = true

[[tool.nab.vcs-sources]]
name = "my-fork"
url  = "git+https://github.com/myorg/my-fork.git@<sha>"
```

The named package becomes a single-version source pinned to the
commit you specified.  nab clones the repo, reads the static
metadata (Layer 2: clone + static metadata), and treats the
result as a normal dependency for the rest of the resolve.  The
URL in each `[[tool.nab.vcs-sources]]` table is run through the
same admission as project-root requirements, so it must pass
`allowed-schemes`, `allowed-repos`, and `require-pin`.

Reading static dependencies from the cloned tree works at any
`build-policy` level.  Dynamic dependencies on a VCS clone
require `build-policy = "build-remote"` (a clone is considered
"remote" for build purposes because the source bytes are
network-fetched, even though they end up on disk before the
backend runs).  See the [build policy](../reference/build-policy.md) page.

## Offline runs

`--offline` stops nab from cloning.  A commit already in the
clone cache is served from there; anything else fails instead of
reaching the remote.  An unpinned ref fails even with a warm
cache, because resolving a branch or tag to a commit needs
`git ls-remote`.

A `git+file` repo is on local disk rather than the network, so it
still clones, as long as its URL names no host.
`git+file:///srv/repo.git` clones offline;
`git+file://server/srv/repo.git` is a UNC share and does not.

## `pkg @ git+...` at the project root

PEP 508 lets you write a direct-URL requirement under
`[project].dependencies`:

```toml
[project]
dependencies = ["my-fork @ git+https://github.com/myorg/my-fork.git@<sha>"]
```

The admission check (allowed schemes, allowed repos, required
pin) fires on this form too, but the resolver path is not
implemented for the project-root case: nab raises
`NotImplementedError`.  Use a `[[tool.nab.vcs-sources]]` entry
instead.

## Lockfile shape

VCS pins land in the lockfile as `VcsPin` records carrying the
repo URL, the resolved 40-char commit SHA, and an optional
`subdirectory`.  Annotated tags are dereferenced to their
underlying commit so the lock never records a tag-object SHA.
VcsPins do not carry a `sha256` (pip does not hash-check VCS
forms), so `nab download` skips them.
