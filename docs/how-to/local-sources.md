# Use a local checkout as a source

## Declare development sources

Use `[develop.sources]` to select a local checkout for a dependency:

```toml
[develop.sources]
my-fork = { path = "../my-fork", editable = true }
```

The directory entry counts as opt-in to that checkout. Build policy still controls backend
execution.

The package becomes the only candidate for that name. nab does not fall back to an index if the
checkout cannot be read or its version does not satisfy the dependency. The checkout's
`[project].name` must match the source key after name normalization.

Relative paths use the directory containing the declaration. `editable` defaults to `false`; set it
to `true` to record an editable install. Use `subdirectory` to select a project below the source
root.

The table also supports Git repositories, wheels, ZIP/tar source archives and package indexes. An
index entry restricts the named package to an [index selected for that package](multi-index.md). Git
retains its [commit-pin requirement](vcs.md); direct wheels and archives retain
[expected-hash requirements](archive-sources.md). VCS systems other than Git are unsupported.

## Select sources for different environments

Alternatives are considered in order; nab uses the first entry whose selectors match:

```toml
[develop.sources]
my-fork = [
    { path = "../my-fork-py310", marker = "python_version < '3.11'" },
    { path = "../my-fork" },
]
```

An entry can also select an `extra` or dependency `group`. An entry cannot select both. A resolution
that selects different sources for the same package fails; mutually exclusive extras or groups can
produce separate valid locks.

## Use a direct reference

A dependency can name a wheel, source archive or Git repository directly:

```toml
[project]
dependencies = ["my-fork @ https://example.com/my_fork-1.0-py3-none-any.whl#sha256=<hex>"]

```

Direct references take precedence over matching source entries and still apply with `--no-sources`.
Archive and VCS permissions still apply with `--no-sources`. Active references to the same package
must agree on the URL. Git references require the [VCS allowlists](vcs.md).

If a dependency declares another direct reference, repeat it in your project or select that package
in `[develop.sources]`. Matching references reuse the selected source; conflicting URLs fail.

## Control which declarations apply

nab reads the target project's table and the invocation directory's table when the target is beneath
that directory. Target entries take precedence. Other ancestor directories and source tables inside
dependencies are not read.

Keep `[tool.nab.workspace].members` for workspace discovery and `--no-emit-workspace`. Members
supply fallback local sources; matching `[develop.sources]` entries select the source instead. If no
entry matches the current target or selection, the workspace source remains available.

`--no-emit-workspace` omits local pins at the declared member directories. A replacement from
another checkout, wheel, repository or index stays in the lock, even when its name matches a member.

Pass `--no-sources` to `nab lock` or `nab download` to ignore `[develop.sources]`. Workspace
settings still apply.

Sources whose selectors apply but match no resolved dependency produce a warning.

Build requirements ignore `[develop.sources]`. A `build-group` lock fails if a build dependency
would use this table; use a separate `--build-requirements` lock instead.

## Read metadata without building

Static `[project]` metadata is read at every build-policy level. A `version` listed in
`[project].dynamic` comes from the build backend instead.

When metadata is missing or dynamic, a local checkout needs `build-policy = "build-local"` or
`"build-remote"`. nab creates an isolated environment, installs `[build-system].requires` and asks
the PEP 517 backend for wheel metadata. Under `"never"`, the source ends the resolve with an error.
See [Build policy](../reference/build-policy.md).

## Lockfile shape

Local sources are recorded as directory entries in `pylock.toml`, with paths relative to the
lockfile. They have no content hash, and `nab download` skips them. The `editable` and
`subdirectory` values are retained for the installer.

See [Portable paths](../reference/lockfile.md) for cases where a path is absolute.
