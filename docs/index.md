# nab

A PubGrub-based dependency resolver for Python packages.

nab is resolve-only: it produces a pinned set of versions (or a
PEP 751 lockfile) but never installs.  Hand the lockfile to whatever
installer you trust.

## Where to start

* New to nab: work through
  [getting started](tutorial/getting-started.md).
* A task in mind: the how-to guides cover installing nab, local
  checkouts, VCS sources, multiple indexes, workspaces, and
  [embedding the resolver](how-to/embed-the-resolver.md).
* Looking something up: the reference covers the
  [`[tool.nab]` keys](reference/configuration.md), the
  [CLI](reference/cli.md), the
  [lockfile formats](reference/lockfile.md), the
  [build policy](reference/build-policy.md), and the
  [on-disk cache](reference/cache.md).
* Want to know how something works: the explanations cover
  [universal resolution](explanation/universal.md),
  [conflicting extras and groups](explanation/conflicts.md), and
  [the five distributions](explanation/packages.md).

```{toctree}
:maxdepth: 1
:caption: Tutorial

tutorial/getting-started
```

```{toctree}
:maxdepth: 1
:caption: How-to guides

how-to/install
how-to/local-sources
how-to/vcs
how-to/multi-index
how-to/workspaces
how-to/embed-the-resolver
```

```{toctree}
:maxdepth: 1
:caption: Reference

reference/cli
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

## Status

* Single-environment resolution against PyPI
* Multiple indexes, per-package routing, and local-checkout sources
* VCS dependency admission with policy controls (Layer 2: clone +
  static metadata)
* Direct-URL `.tar.gz` archive sources, hash-verified and pinned as
  PEP 751 `packages.archive`
* PEP 751 lockfile emission via the upstream `packaging` library
* Universal resolution across a user-declared
  `(python, platform, implementation)` matrix.  Opt-in via
  `[tool.nab].mode = "universal"`; the API and output format are still
  subject to change.
* Mutually-exclusive extras and dependency groups via
  `[tool.nab].conflicts`: co-selected members fork the resolve, in
  specific and universal mode.  See [conflicts](explanation/conflicts.md).
