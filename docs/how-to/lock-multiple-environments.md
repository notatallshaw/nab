# Change a universal lock's targets

Start with [Your first universal lock](../tutorial/getting-started.md). Edit the matrix, then run
`nab lock` to replace `pylock.toml`.

## Select Python versions and platforms

```toml
[tool.nab.matrix]
python = ">=3.12,<3.16"
platforms = ["linux_x86_64", "windows_amd64"]
```

This selects CPython 3.12 through 3.15 on Linux and Windows. Multiple targets select universal mode
automatically. Nab covers the declared combinations; dependency markers do not add targets.

## Add another implementation or build

Add this after the matrix above to include PyPy 3.11 on the same platforms:

```toml
[[tool.nab.matrix.include]]
python = "==3.11"
implementations = ["pypy"]
```

Or add free-threaded CPython 3.15:

```toml
[[tool.nab.matrix.include]]
python = "==3.15"
free-threaded = true
```

Both entries can coexist. Omitted settings inherit the shared defaults. The project's
`requires-python` must admit every selected minor, including 3.11 in the PyPy example. See
[Target matrix](../reference/target-matrix.md) for conditional overrides and patch pins.

## Change defaults for one run

```bash
nab lock --project-matrix-platforms macos_arm64
```

This replaces the shared platform list. Explicit platform lists in overrides or includes retain
precedence. For a project without a matrix, supply both required settings:

```bash
nab lock \
  --project-matrix-python '>=3.12,<3.16' \
  --project-matrix-platforms linux_x86_64 windows_amd64
```

## Inspect or write per-target requirements

```bash
# Print each target's pins in a labelled block.
nab lock --format requirements-without-hashes --output -

# Write an installable file for each Python/platform combination.
nab lock --format requirements \
  --output 'requirements-{python_version}-{platform_id}.txt'
```

These commands resolve again; they do not export the saved lock. The combined stdout format is for
inspection. File templates must distinguish every target, so use the second command with the base
matrix only. See [Output formats](../reference/formats.md) for implementation/ABI collisions and
conflict selections.

For one shared installable file, keep pylock output and follow [Use a lock](use-the-lock.md). If any
target fails, Nab writes no new output; see [Resolution failures](../reference/diagnostics.md).
