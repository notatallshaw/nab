# nab

A PubGrub-based dependency resolver for Python packages.

nab is resolve-only: it produces a pinned set of versions (or a
PEP 751 lockfile) but never installs.  Hand the lockfile to whatever
installer you trust.

## Where to start

The guides are grouped by what you came to do:

* New to nab? Follow the [getting-started tutorial](guides/getting-started.md).
* Have a task in mind? The how-to guides cover installation, local and
  VCS sources, multiple indexes, and workspaces.
* Looking something up? The reference documents the
  [configuration](guides/configuration.md) keys and the
  [CLI](guides/cli.md).
* Want the concepts? The explanations cover the lockfile, build policy,
  universal resolution, and conflicts.

```{toctree}
:maxdepth: 2
:caption: Tutorials

guides/getting-started
```

```{toctree}
:maxdepth: 2
:caption: How-to guides

guides/installation
guides/local-sources
guides/vcs
guides/multi-index
guides/workspaces
```

```{toctree}
:maxdepth: 2
:caption: Reference

guides/configuration
guides/cli
```

```{toctree}
:maxdepth: 2
:caption: Explanation

guides/lockfile
guides/build-policy
guides/universal
guides/conflicts
```

## Status

* Single-environment resolution against PyPI
* Multiple indexes, per-package routing, and local-checkout sources
* VCS dependency admission with policy controls (Layer 2: clone +
  static metadata)
* Direct-URL `.tar.gz` archive sources, hash-verified and pinned as
  PEP 751 `packages.archive`
* PEP 751 lockfile emission via the upstream `packaging` library
* Universal resolution across a user-declared `(python, platform)`
  matrix.  Opt-in via `[tool.nab].mode = "universal"`; the API and
  output format are still subject to change.
* Mutually-exclusive extras and dependency groups via
  `[tool.nab].conflicts`: fail fast in specific mode, fork the resolve
  in universal mode.  See [conflicts](guides/conflicts.md).
