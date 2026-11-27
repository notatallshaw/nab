# Dependency sources

## VCS policy

`[tool.nab.vcs]` controls whether nab clones a VCS URL. The default `"declared"` policy permits the
exact source selected by a `[[tool.nab.vcs-sources]]` entry.

A direct-URL requirement (`pkg @ git+https://...`) at the project root or in a dependency's metadata
must pass the same policy checks. Even when the URL is allowed, nab cannot resolve it in that form.
For a root requirement, keep the package name in the dependencies and move the URL to
`[[tool.nab.vcs-sources]]`. A requirement whose marker excludes it, or one behind an extra the
resolve never requests, never reaches those checks. See [Add a VCS dependency](../how-to/vcs.md).

```toml
[tool.nab.vcs]
policy = "declared"             # "declared" | "block" | "allow"
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

Reading static metadata from a local pyproject.toml works at every `build-policy` level. If that
metadata is missing or dynamic, nab can invoke the checkout's backend only with
`build-policy = "build-local"` or `"build-remote"`. See [Build policy](build-policy.md) for the
metadata requirements.

## Pinned VCS sources

`[[tool.nab.vcs-sources]]` pins a package to a VCS URL. An explicit `vcs.policy = "block"` refuses
both new clones and cached checkouts.

With `vcs.policy = "allow"`, the scheme must be in `vcs.allowed-schemes` and the repository in
`vcs.allowed-repos`. Pins take a full 40-character hexadecimal commit or changeset ID. Git permits
floating revisions with `vcs.require-pin = false`. Both allowlists are empty by default.

Reading static metadata works at any `build-policy`. If that metadata is missing or dynamic, nab
needs `build-policy = "build-remote"` to invoke the clone's backend; see
[Build policy](build-policy.md).

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
