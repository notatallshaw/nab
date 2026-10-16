# Add a VCS dependency

Use `[develop.sources]` to select and permit a Git repository. No second repository or scheme
allowlist is needed for that entry. Only Git is supported; Mercurial, Subversion and Bazaar are not.

## Default posture

```toml
[tool.nab.vcs]
policy = "declared"
allowed-schemes = []
allowed-repos = []
require-pin = true
```

`policy = "declared"` permits only repositories selected by the project. `require-pin = true`
requires a full 40-character commit SHA. A declaration does not waive the pin or build policy. Set
`policy = "block"` to refuse even declared Git sources.

## Pinned VCS sources

```toml
[develop.sources]
my-fork = { git = "https://github.com/myorg/my-fork.git", rev = "<sha>" }
```

The entry permits this repository and revision only. A dependency's source table does not grant
permission to clone other repositories. To select a branch or tag deliberately, set
`require-pin = false` under `[tool.nab.vcs]`; the lock still records the resolved commit.

Reading static metadata works at every build-policy level. Dynamic metadata needs
`build-policy = "build-remote"`. See [Build policy](../reference/build-policy.md).

## Additional URLs in backends and library callers

Backend requirements come from dependency metadata and do not grant permission to acquire their
URLs. Those requirements and library callers can use explicit allowlists:

```toml
[tool.nab.vcs]
policy = "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["https://github.com/myorg/"]
require-pin = true
```

Both allowlists must match for an additional URL. Supported schemes are `git+https`, `git+ssh`,
`git+http`, `git+file` and `git+git`; HTTP and the Git protocol are unauthenticated. An empty list
permits no additional URLs. These allowlists do not need to repeat repositories already selected by
the project. Pin and build policies still apply.

Everything after the final `@` is the ref. A URL must name a repository before it:
`git+https://github.com@<sha>` is refused; `git+https://github.com/myorg/pkg@<sha>` names a
repository.

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

This form permits the specified repository and uses the same pin and build checks as a Git source
entry. The lock records the resolved commit. A matching `[develop.sources]` entry does not replace
the direct reference.

## Lockfile shape

VCS pins land in the lockfile as `VcsPin` records carrying the repo URL, the resolved 40-char commit
SHA, and an optional `subdirectory`. Annotated tags are dereferenced to their underlying commit so
the lock never records a tag-object SHA. VcsPins do not carry a `sha256` (pip does not hash-check
VCS forms), so `nab download` skips them.
