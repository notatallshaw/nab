# nab

nab generates universal lockfiles for Python projects. Declare the Python versions and platforms you
support, then use one PEP 751 `pylock.toml` to share dependency selections across those
environments.

```toml
[tool.nab.matrix]
python = ">=3.12,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
```

nab resolves eight CPython targets from this matrix. It includes other platforms or interpreter
implementations only when you configure them; see
{ref}`how nab chooses coverage <explicit-is-better-than-implicit>`.

An installer such as pip uses the lock to install packages for its current environment. nab resolves
and records dependencies; it does not install them or manage virtual environments. Both nab and its
multi-target lockfile support are experimental.

Start with [What nab locks](explanation/locking.md), then
[make your first universal lock](tutorial/getting-started.md).

For an existing project, [browse the guides](how-to/index.md) to choose dependencies and targets,
use other package sources, or install from a lock. Use the
[configuration reference](reference/configuration.md) to look up settings.

## Status

nab is experimental. The table below states the current boundary; feature pages carry their own
stability warnings.

| Area                  | Current boundary                                                                                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Runtime               | CPython 3.10 and newer. Other interpreters are not tested.                                                                                                                     |
| Commands              | `nab lock` resolves and writes a lock. `nab download` resolves again and fetches artifacts. Neither command installs packages.                                                 |
| Sources               | Simple indexes, local checkouts, declared git sources, hash-pinned `.tar.gz` archives, and workspace members. Project-root `name @ git+...` requirements are not resolved yet. |
| Output                | PEP 751 `pylock.toml`, requirements with recorded index hashes, and requirements without those hashes.                                                                         |
| Experimental features | Universal locks, multiple indexes, and workspaces. Their feature pages state the current boundary.                                                                             |
| Embedding             | `nab-resolver` has stable public module paths. The other component APIs are experimental.                                                                                      |

```{toctree}
---
maxdepth: 1
caption: Start here
---
tutorial/getting-started
how-to/index
```

```{toctree}
---
maxdepth: 1
caption: Reference
---
reference/cli
reference/selection
reference/yanking
reference/formats
reference/diagnostics
reference/configuration
reference/lockfile
reference/build-policy
reference/cache
```

```{toctree}
---
maxdepth: 1
caption: Explanation
---
explanation/locking
explanation/universal
explanation/conflicts
explanation/packages
```

```{toctree}
---
maxdepth: 1
caption: Project
---
contributing
```
