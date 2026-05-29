# nab

A PubGrub-based dependency resolver for Python packages.

nab is resolve-only: it produces a pinned set of versions (or a
PEP 751 lockfile) but never installs.  Hand the lockfile to whatever
installer you trust.

```{toctree}
:maxdepth: 2
:caption: Guides

guides/installation
guides/getting-started
guides/configuration
guides/cli
guides/lockfile
guides/build-policy
guides/local-sources
guides/vcs
guides/multi-index
guides/workspaces
guides/universal
guides/conflicts
```

## Status

* Single-environment resolution against PyPI
* Multiple indexes, per-package routing, and local-checkout sources
* VCS dependency admission with policy controls (Layer 2: clone +
  static metadata)
* PEP 751 lockfile emission via the upstream `packaging` library
* Universal resolution across a user-declared `(python, platform)`
  matrix.  Opt-in via `[tool.nab].mode = "universal"`; the API and
  output format are still subject to change.
* Mutually-exclusive extras and dependency groups via
  `[tool.nab].conflicts`: fail fast in specific mode, fork the resolve
  in universal mode.  See [conflicts](guides/conflicts.md).
