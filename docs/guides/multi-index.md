# Multi-index routing

> [!WARNING]
> Multi-index support is experimental. Cross-index attribution in
> the lockfile (which index served which package) is recorded but
> consumer behaviour across installers varies; the schema may
> tighten in future.

Resolve against PyPI plus a second index, with one package
routed to the second index when running on Linux x86_64.

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

[[tool.nab.index-overrides]]
name   = "torch"
index  = "torch-cpu"
marker = "platform_machine == 'x86_64' and platform_system == 'Linux'"
```

## How nab routes the request

* `numpy` has no override, so nab walks `[pypi, torch-cpu]` and
  picks the first index that lists it (PyPI).
* `torch` has an override. On Linux x86_64 the marker holds; nab
  consults only `torch-cpu`. On any other host the override does
  not apply and nab falls back to the global ordering.

If `torch-cpu` does not list `torch` on the matched host,
resolution fails for that requirement. Strict pinning is the
point: silent fallthrough is a foot-gun on a host the override
was meant to govern.

## Run

```bash
nab lock pyproject.toml
```

## Notes

* Index ordering is significant. Reorder the
  `[[tool.nab.indexes]]` entries to change which index wins for
  any package without an override.
* Overrides are first-match-wins per package. Multiple overrides
  for the same package with different markers act as a routing
  table: declare the more specific markers first.
* Marker-gated overrides are evaluated against
  `[tool.nab.marker-environment]` in single-environment mode only.
  Under `mode = "universal"` there is no single environment, so
  marker entries on `[[tool.nab.index-overrides]]` are skipped and
  the global index order applies for every tuple.  Use overrides
  without a marker, or split the resolve into per-tuple specific
  locks, when you need per-environment routing.
