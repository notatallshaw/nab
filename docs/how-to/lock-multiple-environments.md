# Change a universal lock's targets

Start with the project in [Your first universal lock](../tutorial/getting-started.md). Keep
`mode = "universal"` and change its matrix to match your deployments.

Each dimension covers the values you select. Universal mode does not add unrequested platforms or
implementations; see {ref}`explicit coverage <explicit-is-better-than-implicit>`.

## Change the project matrix

For Python 3.12 through 3.15 on Linux x86-64 and Windows x86-64:

```toml
[tool.nab.matrix]
python = ">=3.12,<3.16"
platforms = ["linux_x86_64", "windows_amd64"]
```

Run `nab lock` again to write one `pylock.toml` for those targets. See
[Target matrix](../reference/target-matrix.md) for implementation overrides and patch-release pins,
and [Platform tags](../reference/platform-tags.md) for minimum system versions.

## Override targets for one run

Replace the platform list without editing the project:

```bash
nab lock --project-matrix-platforms macos_arm64
```

Other matrix keys keep their configured values. An implementation-specific override still takes
precedence over the shared platform list.

For a project without a configured matrix, supply its required axes and select universal mode:

```bash
nab lock --project-mode universal \
  --project-matrix-python '>=3.12,<3.16' \
  --project-matrix-platforms linux_x86_64 windows_amd64
```

## Inspect or write per-target requirements

Print each target's pins in a labelled block:

```bash
nab lock --format requirements-without-hashes --output -
```

This performs a fresh resolve; it does not export the existing lock. The combined output is for
inspection. To install from requirements, write a separate file for each target:

```bash
nab lock --format requirements \
  --output 'requirements-{python_version}-{platform_id}.txt'
```

The template must distinguish every target. [Output formats](../reference/formats.md) covers
conflict selections, template collisions and matrices whose implementation or ABI differences cannot
be represented in filenames.

For one shared file, keep pylock output and follow [Use a lock](use-the-lock.md). If any target
fails, Nab writes no new lock or requirements output; see
[Resolution failures](../reference/diagnostics.md).
