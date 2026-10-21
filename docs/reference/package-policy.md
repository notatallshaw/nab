# Package and index policy

nab applies overrides by package name and version (`[tool.nab.packages.<name>]` or
`[[tool.nab.package-rules]]`), or by serving index (`[tool.nab.index.<name>]`). Each overridden
field replaces the global setting for matching packages. Fields with no override keep their global
defaults.

## Per-package overrides

A per-package override sets policy for matching package names and versions. Both forms below may
appear in one file.

Use a name-keyed table for one package:

```toml
# Build lxml from source and trust its (pre-PEP-643) PKG-INFO deps.
[tool.nab.packages.lxml]
dist-policy = { policy = "sdist-only", trust-unverified-deps = true }

# Quote the key to carry a version specifier on the requirement.
[tool.nab.packages."numpy > 2"]
dist-policy = "wheel-only"
```

`[[tool.nab.package-rules]]` is an array whose `match` selector lists the requirements one body
applies to. The same overrides written as rules:

```toml
[[tool.nab.package-rules]]
match = ["lxml"]
dist-policy = { policy = "sdist-only", trust-unverified-deps = true }

# A match entry takes the same name-plus-specifier requirement.
[[tool.nab.package-rules]]
match = ["numpy > 2"]
dist-policy = "wheel-only"
```

A rule is the form to reach for when one body covers several packages at once, most often routing a
set of internal packages to one index:

```toml
[[tool.nab.package-rules]]
match = ["acme-core", "acme-plugins", "acme-utils"]
index = "internal"
```

To scope an override to a range of versions, put a PEP 508 specifier in the (quoted) table key or in
a `match` entry. Two non-overlapping ranges for one package are two entries, in either form:

```toml
# Both forms take specifiers and mix in one file: old numpy as sdists,
# newer numpy as wheels.
[tool.nab.packages."numpy <= 1.21"]
dist-policy = "sdist-only"

[[tool.nab.package-rules]]
match = ["numpy >= 1.22"]
dist-policy = "wheel-only"
```

A body sets any combination of:

- `dist-policy`: an enum string, or `{ policy = "...", trust-unverified-deps = true|false }`.
- `allow-zip-sdists`: a boolean admitting or refusing {ref}`legacy ZIP sdists <legacy-zip-sdists>`.
- `build-policy`: an enum string.
- `assume-universal-deps`: a boolean permitting dependency metadata to be reused across targets (see
  {ref}`Build policy <assuming-dependencies-are-universal>`). An explicit `false` cancels a global
  `true`.
- `uploaded-prior-to`: a datetime with an explicit timezone offset, a `P<n>D` duration, or `false`
  (no cutoff for the selected versions).
- `index`: route the selected packages to this declared index (a strict pin: only that index is
  consulted). Routing requires bare-name selectors, because the routing decision happens before any
  version is known; a version specifier alongside `index` is rejected.
- `dependencies`: a list of PEP 508 requirement strings that replaces the package's declared runtime
  dependencies (see [Metadata overrides](metadata-overrides.md)).
- `requires-python`: a PEP 440 specifier that replaces the package's declared Python requirement for
  the selected versions (see [Metadata overrides](metadata-overrides.md)).
- `provides-extra`: a list of extra names that replaces the package's declared extras for the
  selected versions (see [Metadata overrides](metadata-overrides.md)).

A selector is a name plus an optional version specifier, with no extras, marker, or URL. Package
names are canonicalised, so `Foo-Bar`, `foo_bar`, and `foo-bar` refer to the same package; two
entries that set the same field for it are an overlap error (below).

The version ranges of two per-package entries (from either form) that set the **same field** for the
**same package** must not overlap. Overlapping ranges are a parse-time error:

```toml
# ERROR: <= 2 and >= 1 overlap on [1, 2], and both set dist-policy.
[tool.nab.packages."lxml <= 2"]
dist-policy = "sdist-only"

[tool.nab.packages."lxml >= 1"]
dist-policy = "wheel-only"
```

A package can have at most one index route, because routing requires a bare-name selector.

(per-index-policy)=

## Per-index overrides

`[tool.nab.index.<name>]` is keyed by a declared index name. Each entry sets policy only
(`dist-policy`, `allow-zip-sdists`, `assume-universal-deps`, `build-policy`,
`uploaded-prior-to`, `assume-fresh-seconds`) and applies to every package served from that index.
Routing and version selectors are per-package settings.

```toml
# Everything served from PyPI is wheel-only.
[tool.nab.index.pypi]
dist-policy = "wheel-only"

[tool.nab.index.internal]
build-policy = "build-remote"
uploaded-prior-to = "2026-05-01T00:00:00Z"
# Trust this index's package listings as fresh for an hour.
assume-fresh-seconds = 3600
```

`assume-fresh-seconds` extends a cached listing's freshness window. A release published inside that
window stays hidden until it lapses. The setting never affects metadata or artifacts cached by hash.

## Cooldown exceptions

### Allow a recently uploaded release

Keep a seven-day cooldown, but admit the original files for
[`certifi` 2026.10.19](https://pypi.org/project/certifi/2026.10.19/):

```toml
[tool.nab]
uploaded-prior-to = "P7D"

[tool.nab.packages."certifi==2026.10.19"]
uploaded-prior-to = "2026-10-20T02:24:17Z"
```

The fixed timestamp admits that release's original wheel and sdist. Nab still excludes later uploads
of the same version. Other versions and packages keep the seven-day cooldown.

### Use an index without upload times

For a hypothetical internal index that does not publish upload times, disable the cutoff only for
that index:

```toml
[tool.nab]
uploaded-prior-to = "P7D"

[[tool.nab.indexes]]
name = "internal"
url = "https://packages.example.com/simple/"

[[tool.nab.indexes]]
name = "pypi"
url = "https://pypi.org/simple/"

[tool.nab.index.internal]
uploaded-prior-to = false
```

PyPI keeps the seven-day cooldown. Files without upload times are excluded whenever a cutoff
applies; see {ref}`Index serialization <index-serialization>`.

## Conflicts between package and index overrides

Per-package and per-index overrides have equal precedence. If both set the same field for a
candidate version, the resolve fails instead of choosing one. Remove one setting.

The same package at a version *outside* the per-package range is governed only by the per-index
entry, with no conflict.

When no override sets a field, the flat global value (then the built-in default) applies.
