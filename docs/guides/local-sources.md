# Local sources

`[[tool.nab.local-sources]]` lets you treat a directory on disk
as the only candidate for a named package.  The typical use case
is iterating on a fork of a dependency without publishing wheels
or pushing to a VCS host.

## Declaring a local source

```toml
[[tool.nab.local-sources]]
name = "my-fork"
path = "../my-fork"
```

Relative `path` entries resolve against the directory holding
`pyproject.toml`, not the process's current working directory.
Absolute paths are recorded as-is.

The directory is read once.  nab takes the version from
`[project].version` in the local pyproject.toml; if absent, the
package is pinned to the synthetic version `0.0.0+local`.

The named package becomes the only candidate the resolver will
consider for that name; the resolver does not fall back to
PyPI for it.  This is the same single-source semantics as
`[[tool.nab.vcs-sources]]`.

## Reading static metadata

Reading static `[project].dependencies` from a local pyproject
works at every `build-policy` level:

```toml
[tool.nab]
# build-policy defaults to "build-local"; "never" is the strictest setting.

[[tool.nab.local-sources]]
name = "my-fork"
path = "../my-fork"
```

See [build policy](build-policy.md) for the full ladder.

## Dynamic dependencies

Local sources whose pyproject.toml lists `dynamic = ["dependencies"]`
require `build-policy = "build-local"` (the default) or
`"build-remote"`.  nab spins up an isolated venv, installs the
declared build requirements, and invokes the PEP 517 backend
through `pyproject_hooks` to extract the live dependency list.
Setting `"never"` instead skips the version with
`UnsupportedSdistError` so the resolver moves on.

## Lockfile shape

Local pins land in the lockfile as `LocalPin` records.  The path
is written relative to the lockfile's own directory, with POSIX
separators, as PEP 751 requires, so a committed lockfile stays
usable on another machine.  They do not carry a `sha256` (the
contents are not under nab's control), so `nab download` skips
them.
