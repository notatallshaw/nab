# Multi-index routing

> [!WARNING]
> Multi-index support is experimental. Cross-index attribution in
> the lockfile (which index served which package) is recorded but
> consumer behaviour across installers varies; the schema may
> tighten in future.

Resolve against PyPI plus a second index, with one package pinned to
the second index.

## Project setup

```toml
# pyproject.toml
[project]
name = "example"
version = "0.1.0"
dependencies = [
    "torch",
    "numpy",
]

[[tool.nab.indexes]]
name = "pypi"
url  = "https://pypi.org/simple/"

[[tool.nab.indexes]]
name = "torch-cpu"
url  = "https://download.pytorch.org/whl/cpu"

[tool.nab.packages.torch]
index = "torch-cpu"
```

Routing lives on the per-package override, via the `index` body field,
alongside the other per-package policies (see the
[configuration guide](configuration.md)).  To route several packages to
one index, list them in a `[[tool.nab.package-rules]]` entry instead:

```toml
[[tool.nab.package-rules]]
match = ["torch", "torchvision", "torchaudio"]
index = "torch-cpu"
```

A routing entry must use bare-name selectors: the routing decision
happens before any version is known, so a version specifier alongside
`index` is rejected, and a package may have only one route.

## How nab routes the request

* `numpy` has no routing override, so nab walks `[pypi, torch-cpu]` and
  picks the first index that lists it (PyPI).
* `torch` has a routing override, so nab consults only `torch-cpu`.

If `torch-cpu` does not list `torch`, resolution fails for that
requirement. Strict pinning is the point: silent fallthrough is a
foot-gun on an index the override was meant to govern.

## Per-index policy

`[tool.nab.index.<name>]` applies a policy to every package served
from an index. Here every package that comes from PyPI is wheel-only,
while packages from the torch index keep the global default:

```toml
[tool.nab.index.pypi]
dist-policy = "wheel-only"
```

## Policy across both surfaces is an error, not a precedence

The per-package and per-index surfaces are not ranked. If a per-package
override and the per-index override for the serving index both set the
same field for a candidate, the resolve raises a clear error rather
than picking one:

```toml
# torch is pinned to torch-cpu (above).  This per-package override sets
# dist-policy for torch, and the per-index override sets dist-policy for
# everything from torch-cpu: a torch candidate served from torch-cpu is
# governed by both, so the resolve errors.  Remove one of the two.
[tool.nab.packages.torch]
dist-policy = "wheel-only"

[tool.nab.index.torch-cpu]
dist-policy = "sdist-only"
```

## Run

```bash
nab lock pyproject.toml
```

## Notes

* Index ordering is significant. Reorder the
  `[[tool.nab.indexes]]` entries to change which index wins for
  any package without a route.
* Routing carries no version scope and no marker: a package has at most
  one route, fixed for the whole resolve.
