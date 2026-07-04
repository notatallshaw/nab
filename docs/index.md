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

## Extras are additive

nab treats extras additively: requesting `pkg[gpu]` installs everything a
plain `pkg` installs, plus the dependencies `pkg` gates on the `gpu` extra.

One consequence is that a dependency guarded by a negative extra marker is
kept even when the extra is requested. Given

```
Requires-Dist: onnxruntime ; extra != "gpu"
Provides-Extra: gpu
Requires-Dist: onnxruntime-gpu ; extra == "gpu"
```

`extra != "gpu"` is true for a plain install, so `onnxruntime` is a base
dependency of `pkg`. Installing `pkg[gpu]` therefore keeps `onnxruntime`
alongside `onnxruntime-gpu`, even where the author meant the extra to
replace it. For the same reason, requesting `pkg` and `pkg[gpu]` together
installs the union of both, not the dependencies of the merged extra set.

This is a property of the additive-extra model. pip's default resolver and
uv behave the same way. `extra` comparisons themselves follow the
dependency-specifiers specification: `==` is membership in the requested
extras, `!=` is non-membership, and any other operator is treated as false.
