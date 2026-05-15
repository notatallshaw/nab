# VCS dependencies

nab resolves dependencies pulled from version-control URLs (git,
hg, svn) through `[tool.nab.vcs]` and `[[tool.nab.vcs-sources]]`.
The default posture is fully restrictive: nothing VCS-shaped
resolves without an explicit policy.

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
allowed-repos = ["github.com/myorg/"]
require-pin = true
```

Three layers, each AND-checked:

1. `allowed-schemes`: pip-style scheme prefixes (`git+https`,
   `git+ssh`, `hg+https`, ...).  Empty means "no scheme is allowed".
2. `allowed-repos`: prefix match against the URL after the scheme
   strip.  `github.com/myorg/` matches every repo under that org.
   Empty means "no repo is allowed".
3. `require-pin`: when `true`, the URL must include a commit
   identifier (a tag or a sha after `@`).  Bare branch references
   are rejected because they are mutable.

## Pinned VCS sources

The supported entry point is `[[tool.nab.vcs-sources]]`:

```toml
[tool.nab.vcs]
policy = "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["github.com/myorg/"]
require-pin = true

[[tool.nab.vcs-sources]]
name = "my-fork"
url  = "git+https://github.com/myorg/my-fork.git@<sha>"
```

The named package becomes a single-version source pinned to the
commit you specified.  nab clones the repo, reads the static
metadata (Layer 2: clone + static metadata), and treats the
result as a normal dependency for the rest of the resolve.

Reading static dependencies from the cloned tree works at any
`build-policy` level.  Dynamic dependencies on a VCS clone
require `build-policy = "build-remote"` (a clone is considered
"remote" for build purposes because the source bytes are
network-fetched, even though they end up on disk before the
backend runs).  See the [build policy](build-policy.md) page.

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
repo URL, the resolved commit id, and an optional `subdirectory`.
They do not carry a `sha256` (pip does not hash-check VCS forms),
so `nab download` skips them.
