# Metadata overrides

`dependencies`, `requires-python`, and `provides-extra` replace published metadata for the selected
versions, like uv's `dependency-metadata`. Use them when metadata is wrong, absent, dynamic, or
unbuildable. nab trusts the replacement and does not verify it against the artifact.

Each field replaces its own field independently, so a `dependencies` override on one range and a
`requires-python` override on another (overlapping) range coexist; only two entries setting the
**same** field over overlapping ranges are an error.

## `dependencies`

`dependencies` states the runtime dependencies for the selected versions, replacing whatever the
distribution declares. Each item is a full PEP 508 requirement, so extras, markers, and version
specifiers are all allowed on a value (unlike the selector key, which takes only a name and an
optional specifier).

```toml
# Replace chumpy's declared runtime deps for every version.
[tool.nab.packages.chumpy]
dependencies = ["numpy>=1.8.1", "scipy>=0.13.0", "six>=1.11.0"]

# Version-scoped: only versions <= 1.0.
[tool.nab.packages."broken-pkg <= 1.0"]
dependencies = ["requests>=2"]

# The many-package form.
[[tool.nab.package-rules]]
match = ["some-pkg <= 2.0"]
dependencies = ["requests>=2"]
```

The list is the complete replacement, not an addition: the declared dependencies for the matched
versions are dropped and the override's list is used instead. An empty list removes all runtime
dependencies:

```toml
# Resolve broken-pkg <= 1.0 with no runtime dependencies at all.
[tool.nab.packages."broken-pkg <= 1.0"]
dependencies = []
```

Omitting the key keeps the declared dependencies; `[]` removes them. An empty list still sets the
field, so two entries setting `dependencies` over overlapping ranges are a
[configuration error](package-policy.md).

## `requires-python`

`requires-python` is a single PEP 440 specifier that replaces the package's declared Python
requirement for the selected versions. A bare version like `"3.13"` is rejected (it is not a
specifier); write `">=3.13"` or `"==3.13"`.

```toml
[tool.nab.packages.flask]
requires-python = ">=3.6"
```

The override applies at the point that filters candidates by Python, so it both widens and narrows:

- Widen: a package that declares `>=3.10` but actually runs on `3.9` can be admitted for a `3.9`
  resolve with `requires-python = ">=3.9"`.
- Narrow: a package that declares `>=3.6` can be held to `>=3.11` so older Pythons reject it.

The override uses the same comparison as a declared `Requires-Python` value. That comparison is made
at the language version: a micro segment neither admits a target nor excludes one, so `>=3.13.2`,
`==3.13.4` and `==3.13.*` all admit a 3.13 target, while `!=3.13` excludes every 3.13 interpreter.

An empty string (`requires-python = ""`) removes the Python requirement and admits every target.

For an index pin, the lock records the overridden specifier. A PEP 751 installer checks it against
its actual interpreter, including the patch version. A local-path or VCS pin has no
`requires-python` field, but nab still uses the override for its Python check.

## `provides-extra`

`provides-extra` replaces the selected versions'
[extra names](https://packaging.python.org/en/latest/specifications/core-metadata/#provides-extra-multiple-use),
normalized before use.

```toml
[tool.nab.packages.flask]
dependencies = ["werkzeug>=0.14", "click>=5.1 ; extra == 'dotenv'"]
provides-extra = ["dotenv"]
```

When `provides-extra` is set it is authoritative for the whole extra set: it is never merged with
the package's declared extras. An extra exists only if `provides-extra` lists it. nab evaluates
dependency markers for those extras, using replacement `dependencies` when set or the parsed
`Requires-Dist` otherwise.

- When `provides-extra` is absent, the extras fall back to the package's parsed extras, unless
  `dependencies` is also replaced. A `requires-python`-only override therefore keeps the package's
  declared extras and their dependencies. Replacing `dependencies` without declaring
  `provides-extra` drops the extras, since the parsed dependency lines conditioned on the extra are
  gone once the list is replaced.
- A dropped extra has no effect unless it is requested. Requesting a dropped extra directly (a
  root/user `flask[async]`) raises in the error-user and backtrack extras modes; a transitive
  request (another package depending on `flask[async]`) warns and drops the extra's dependencies,
  except in the backtrack mode, which skips versions that lack the extra instead.
- An empty list (`provides-extra = []`) declares no extras and is distinct from omitting the key.

## Skip-fetch: resolving without the artifact

When an entry sets `dependencies` (an empty list counts), nab resolves the matched versions from the
declared metadata alone: the resolver computes their dependencies without fetching or building the
per-version metadata. This is what lets a package that nab cannot fetch or build (an sdist-only or
dynamic-metadata package under `build-policy = "never"`) still resolve:

```toml
# Resolve a dynamic-metadata, sdist-only package without building it.
[tool.nab.packages.legacy-pkg]
dependencies = ["numpy>=1.8.1"]
```

The package listing is still fetched (the lock needs the files, hashes, and upload times, and the
Python filter still runs), but the resolver does not fetch or build the per-version metadata to
compute dependencies. A partial override that sets only `requires-python` still needs the artifact
for its dependencies and does not skip.

Under skip-fetch a co-set `trust-unverified-deps` becomes moot, since no sdist is parsed. A co-set
`dist-policy` is not: it still filters the candidate listing, so `dist-policy = "wheel-only"` on an
sdist-only package removes every version before the override can apply.

## Scope and the per-index rule

These are per-package fields with no per-index form: an index serves many packages, so a single
dependency list, Python requirement, or extra set for all of them is meaningless. Writing any of
them under `[tool.nab.index.<name>]` is rejected.

A metadata override annotates versions already reached; it never introduces one. An unmatched
version or unvisited package does nothing. Local, VCS, and archive sources need a bare-name override
because their version is unknown until materialisation.
