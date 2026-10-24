# Use a local checkout as a source

`[[tool.nab.local-sources]]` lets you treat a directory on disk as the only candidate for a named
package. The typical use case is iterating on a fork of a dependency without publishing wheels or
pushing to a VCS host.

## Declaring a local source

```toml
[[tool.nab.local-sources]]
name = "my-fork"
path = "../my-fork"
```

Relative `path` entries resolve against the directory holding `pyproject.toml`, not the process's
current working directory. Absolute paths are recorded as-is.

The checkout's project name must match the declared `name` after name normalization. nab fails the
resolve if `path` points to a different project.

### Version and source selection

The directory is read once. nab reads the version from `[project].version` in the local
pyproject.toml. A `version` listed in `[project].dynamic` comes from the build backend instead, one
of the cases under "Dynamic metadata" below.

The named package becomes the only candidate the resolver will consider for that name; the resolver
does not fall back to PyPI for it. This is the same single-source semantics as
`[[tool.nab.vcs-sources]]`.

## Reading static metadata

Reading static metadata from a local pyproject works at every `build-policy` level:

```toml
[tool.nab]
# build-policy defaults to "build-local"; "never" is the strictest setting.

[[tool.nab.local-sources]]
name = "my-fork"
path = "../my-fork"
```

See [build policy](../reference/build-policy.md) for each policy's permissions.

## Dynamic metadata

If the checkout lacks usable static metadata, nab invokes its build backend under
`build-policy = "build-local"` (the default) or `"build-remote"`. This happens when `[project]` is
missing or malformed, or its `dynamic` list includes `version`, `requires-python`, `dependencies` or
`optional-dependencies`.

nab spins up an isolated venv, installs the declared build requirements, and asks the PEP 517
backend for the wheel `METADATA`, building the wheel when the backend does not supply that metadata
on its own.

Setting `"never"` instead ends the resolve. nab reads the source while listing its one version, so
there is no candidate to reject. The error names the source and the policy that forbade the build.

## What nab records in the lock

The lock records the checkout's path with POSIX separators, relative to the lockfile's own
directory, so a committed lockfile keeps working on another machine as long as the checkout moves
with it. nab records no artifact hash for the directory and skips it during `nab download`.

See [Portable paths](../reference/lockfile.md) for the two cases where the path is not relative to
the lockfile.
