# Add a VCS dependency

nab resolves dependencies pulled from git URLs through `[tool.nab.vcs]` and
`[[tool.nab.vcs-sources]]`. Only git is supported. nab blocks Git sources by default; you must allow
their schemes and repository URLs before resolving them.

## Default posture

```toml
[tool.nab.vcs]
policy = "block"
allowed-schemes = []
allowed-repos = []
require-pin = true
```

`policy = "block"` means every VCS URL is rejected. Switching to `"allow"` is necessary but not
sufficient: you also have to declare allowlists.

## Layered allowlist

```toml
[tool.nab.vcs]
policy = "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["https://github.com/myorg/"]
require-pin = true
```

nab checks every URL against all three settings:

1. `allowed-schemes`: the supported pip-style prefixes are `git+https`, `git+ssh`, `git+http`,
   `git+file`, and `git+git`. Empty means "no scheme is allowed". `git+http` and `git+git` are
   unauthenticated; prefer `git+https` or `git+ssh`.
2. `allowed-repos`: prefix match against the URL after the `git+` prefix is stripped, so the value
   includes the inner scheme. `https://github.com/myorg/` matches every HTTPS repo under that org.
   Empty means "no repo is allowed".
3. `require-pin`: when `true`, the URL must include a 40-char hex commit SHA after `@`. Branch and
   tag references are rejected because they are mutable.

Everything after the final `@` is the ref, so the URL has to name a repository ahead of it:
`git+https://github.com` and `git+https://github.com@<sha>` are both refused;
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

The named package becomes a single-version source at the specified commit. nab clones the
repository, reads its static metadata, and treats the result as a normal dependency. The URL uses
the same admission as a project-root requirement, so it must pass `allowed-schemes`,
`allowed-repos`, and `require-pin`.

Reading static metadata from the cloned tree works at any `build-policy` level. If the clone lacks
usable static metadata, nab can invoke its backend only with `build-policy = "build-remote"`. Cloned
sources count as remote even after downloading.

See [build policy](../reference/build-policy.md) for the metadata requirements.

## Offline runs

`--offline` stops nab from cloning. A commit already in the clone cache is served from there;
anything else fails instead of reaching the remote. An unpinned ref fails even with a warm cache,
because resolving a branch or tag to a commit needs `git ls-remote`.

A `git+file` repo is reached through the filesystem rather than the network, so it still clones
offline, and a floating ref on one still resolves. Where that path leads is left to the operating
system, exactly as it is for a `file:` index: a `git+file` URL under an NFS or SMB mount clones
offline too.

## `pkg @ git+...` at the project root

PEP 508 lets you write a direct-URL requirement under `[project].dependencies`:

```toml
[project]
dependencies = ["my-fork @ git+https://github.com/myorg/my-fork.git@<sha>"]
```

The admission check (allowed schemes, allowed repos, required pin) fires on this form too, but the
resolver path is not implemented for the project-root case: nab raises `NotImplementedError`. Use a
`[[tool.nab.vcs-sources]]` entry instead.

## What nab records in the lock

nab records the repository URL, resolved 40-character commit SHA and optional `subdirectory`.
Annotated tags resolve to their underlying commit. Git entries have no artifact hash and are skipped
by `nab download`.
