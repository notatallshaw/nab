# Dependency sources

## VCS policy

`[tool.nab.vcs]` controls whether nab clones a VCS URL. It blocks cloning by default. The form that
resolves is a `[[tool.nab.vcs-sources]]` entry, described under "Pinned VCS sources" below.

A direct-URL requirement (`pkg @ git+https://...`) at the project root or in a dependency's metadata
passes the same policy checks, then fails the resolve, because nab has no resolver path for that
form. A requirement whose marker excludes it, or one behind an extra the resolve never requests,
never reaches those checks. See [Add a VCS dependency](../how-to/vcs.md).

```toml
[tool.nab.vcs]
policy = "block"                # "block" | "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["https://github.com/me/x"]
require-pin = true
```

## Local checkouts as sources

`[[tool.nab.local-sources]]` treats a directory on disk as the only candidate for the named package.

```toml
[[tool.nab.local-sources]]
name = "my-fork"
path = "../my-fork"
```

`editable` defaults to `false`; set it to `true` to record a PEP 660 editable install. Discovered
workspace members instead default to editable; see [Lock a workspace](../how-to/workspaces.md).
`subdirectory` locates the package below `path` in a monorepo.

Reading static metadata from a local pyproject.toml works at every `build-policy` level. When the
static read comes up empty, nab builds the checkout instead, which needs
`build-policy = "build-local"` or `"build-remote"`. See [Build policy](build-policy.md) for what
counts as empty.

## Pinned VCS sources

`[[tool.nab.vcs-sources]]` pins a package to a VCS URL. Declaring one while `vcs.policy` is left at
its default `block` is a contradiction and is rejected when the config is read, before any resolve
starts.

Each URL must satisfy the same `[tool.nab.vcs]` policy as a direct-URL requirement. Beyond
`vcs.policy = "allow"`, its scheme must be in `vcs.allowed-schemes`, its repository in
`vcs.allowed-repos`, and it must pin a 40-char commit hash unless `vcs.require-pin = false`. Both
allow lists are empty by default, so each denies every URL until configured.

Reading static metadata works at any `build-policy`. Building a clone whose static read comes up
empty needs `build-policy = "build-remote"`; see [Build policy](build-policy.md).

```toml
[[tool.nab.vcs-sources]]
name = "my-fork"
url  = "git+https://github.com/me/x.git@<sha>"
```

## Archive sources

`[[tool.nab.archive-sources]]` pins a package to a direct `.tar.gz` URL. The URL fragment must carry
a `sha256` (or `sha384`/`sha512`) hash; nab downloads the archive, verifies it against the hash, and
fails the resolve on a mismatch.

```toml
[[tool.nab.archive-sources]]
name = "my-fork"
url  = "https://example.com/my-fork-1.0.tar.gz#sha256=<hex>"
```

Only `.tar.gz` source archives are supported; other formats are refused. A `&subdirectory=` fragment
selects a package below the archive root. Static metadata works at every policy, but a backend needs
`build-policy = "build-remote"`; see [Build policy](build-policy.md).

The URL's own scheme applies: `file://` reads from disk and every other archive uses HTTP. The
extracted tree is cached with its verified hashes and can be reused offline. A new hash triggers
another download.
