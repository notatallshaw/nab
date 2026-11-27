# Add a VCS dependency

Nab resolves Git and Mercurial dependencies declared in `[[tool.nab.vcs-sources]]`.

## Default posture

```toml
[tool.nab.vcs]
policy = "declared"
allowed-schemes = []
allowed-repos = []
require-pin = true
```

`policy = "declared"` permits the exact source each entry names. Use `"block"` to refuse all VCS
sources or `"allow"` to require scheme and repository allowlists.

## Layered allowlist

```toml
[tool.nab.vcs]
policy = "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["https://github.com/myorg/"]
require-pin = true
```

With `policy = "allow"`, nab checks each VCS source against the allowlists.

1. Choose the protocols in `allowed-schemes`. Git supports `git+https`, `git+ssh`, `git+http`,
   `git+file` and `git+git`. Prefer `git+https` and `git+ssh` for authenticated transport. Mercurial
   supports `hg+https` and `hg+file`. An empty list denies all schemes.
2. Use repository URL prefixes without `git+` or `hg+` in `allowed-repos`. For example,
   `https://github.com/myorg/` matches every HTTPS repository under that organization. An empty list
   denies all repositories.
3. Set `require-pin = true` to require a full 40-character hexadecimal Git commit ID after `@`.
   Mercurial always requires a full changeset ID.

Everything after the final `@` is the ref, so the URL has to name a repository ahead of it:
`git+https://github.com` and `git+https://github.com@<sha>` are both refused;
`git+https://github.com/myorg/pkg@<sha>` is not.

## Pinned VCS sources

The supported entry point is `[[tool.nab.vcs-sources]]`:

```toml
[[tool.nab.vcs-sources]]
name = "my-fork"
url  = "git+https://github.com/myorg/my-fork.git@<sha>"
```

The named package becomes a single-version source at the specified commit. Nab clones the repository
and reads its metadata. A full commit ID opts in that Git source by default. Set `[tool.nab.vcs]`
with `require-pin = false` to permit a floating Git revision for a declared source.

Reading static metadata from the cloned tree works at any `build-policy` level. If the clone lacks
usable static metadata, nab can invoke its backend only with `build-policy = "build-remote"`. Cloned
sources count as remote even after downloading.

See [build policy](../reference/build-policy.md) for the metadata requirements.

## Mercurial sources

Use Mercurial 7.2.4 or newer. For a repository you can reach over HTTPS, declare its URL and full
changeset ID.

```toml
[[tool.nab.vcs-sources]]
name = "demo"
url = "hg+https://hg.example.org/team/demo@0123456789abcdef0123456789abcdef01234567"
```

For a local repository, use an absolute `hg+file` URL. Nab refuses embedded credentials, URL
queries, HTTPS redirects, and changesets with subrepositories or links that lead outside the
checkout. It ignores Mercurial configuration inherited from the host or repository and turns off
server clone bundles. The selected changeset and the ancestors needed to pull it are cached locally.
Installers use their own VCS settings when they read the lock.

## Offline runs

`--offline` stops nab from cloning. A commit already in the clone cache is served from there;
anything else fails instead of reaching the remote. An unpinned ref fails even with a warm cache,
because resolving a branch or tag to a commit needs `git ls-remote`.

A `git+file` or `hg+file` repo is reached through the filesystem and still clones offline. Git also
resolves floating refs for `git+file`. Where that path leads is left to the operating system,
exactly as it is for a `file:` index: a `git+file` URL under an NFS or SMB mount clones offline too.

## `pkg @ git+...` at the project root

PEP 508 lets you write a direct-URL requirement under `[project].dependencies`:

```toml
[project]
dependencies = ["my-fork @ git+https://github.com/myorg/my-fork.git@<sha>"]
```

To lock this dependency with nab, keep `my-fork` in the dependency list and move its URL to
`[[tool.nab.vcs-sources]]`. Keep any extras or environment marker on the dependency. The repository
uses the [same VCS policy](#pinned-vcs-sources).

```toml
[project]
dependencies = ["my-fork"]

[[tool.nab.vcs-sources]]
name = "my-fork"
url = "git+https://github.com/myorg/my-fork.git@<sha>"
```

## What nab records in the lock

Nab records the backend, repository URL, full commit or changeset ID, and optional `subdirectory`.
Git annotated tags resolve to their underlying commit. The `nab download` command skips VCS entries.
