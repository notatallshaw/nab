# nab

nab resolves Python project dependencies and writes a PEP 751 lock or
pinned requirements. It can download resolved artifacts, but it does
not install them.

## Start with a task

* [Make your first lock](tutorial/getting-started.md), then install its
  dependencies.
* [Use a lock](how-to/use-the-lock.md) with pip, hashed requirements,
  or an offline wheelhouse.
* [Configure a resolve](reference/configuration.md) and inspect
  effective values.
* Use [local projects](how-to/local-sources.md),
  [git sources](how-to/vcs.md),
  [multiple indexes](how-to/multi-index.md), or
  [workspaces](how-to/workspaces.md).
* [Use a direct archive source](how-to/archive-sources.md) with a
  required digest.
* [Reason about PEP 508 markers](how-to/reason-about-markers.md) as
  sets of environments.
* [Diagnose a failed resolve](reference/diagnostics.md) from its error
  and `Diagnostics:` section.
* [Check the CLI](reference/cli.md), output
  [formats](reference/formats.md), or [cache](reference/cache.md).
* [Embed the generic resolver](how-to/embed-the-resolver.md) in another
  tool.

## Status

nab is experimental. The table below states the current boundary;
feature pages carry their own stability warnings.

| Area | Current boundary |
| --- | --- |
| Runtime | CPython 3.10 and newer. Other interpreters are not tested. |
| Commands | `nab lock` resolves and writes a lock. `nab download` resolves again and fetches artifacts. Neither command installs packages. |
| Sources | Simple indexes, local checkouts, declared git sources, hash-pinned `.tar.gz` archives, and workspace members. Project-root `name @ git+...` requirements are not resolved yet. |
| Output | PEP 751 `pylock.toml`, requirements with recorded index hashes, and requirements without those hashes. |
| Experimental features | Universal locks, multiple indexes, and workspaces. Their feature pages state the current boundary. |
| Embedding | `nab-resolver` has a path-stable public API. `nab-markersets` documents its supported surface but remains experimental; the other component APIs are experimental. |

```{toctree}
:maxdepth: 1
:caption: Tutorial

tutorial/getting-started
```

```{toctree}
:maxdepth: 1
:caption: How-to guides

how-to/install
how-to/use-the-lock
how-to/local-sources
how-to/archive-sources
how-to/vcs
how-to/multi-index
how-to/workspaces
how-to/embed-the-resolver
how-to/reason-about-markers
```

```{toctree}
:maxdepth: 1
:caption: Reference

reference/cli
reference/selection
reference/formats
reference/diagnostics
reference/configuration
reference/lockfile
reference/build-policy
reference/cache
```

```{toctree}
:maxdepth: 1
:caption: Explanation

explanation/universal
explanation/conflicts
explanation/packages
```

```{toctree}
:maxdepth: 1
:caption: Project

contributing
```
