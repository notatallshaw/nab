# Package and index policy

Overrides set policy for a package name and version, or for every package served by an index. Each
field replaces its global setting independently. Omitted fields keep the global value.

## Package selectors

Use a name-keyed table for one package, or a rule for several packages:

```toml
[tool.nab.packages."numpy>2"]
dist-policy = "wheel-only"

[[tool.nab.package-rules]]
match = ["lxml", "xmlsec"]
dist-policy = "sdist-only"
metadata-policy = "prefer-wheel"
```

Both forms can appear in one file. A selector is a package name with an optional PEP 508 version
specifier. Extras, markers and URLs are not accepted.

Names are canonicalised: `Foo-Bar`, `foo_bar` and `foo-bar` identify the same package. Quote a table
key containing a specifier.

## Override fields

Package overrides accept these fields:

| Field                   | Effect                                                                                           |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| `dist-policy`           | Choose installation artifacts; also accepts the \`{ policy = "...", trust-unverified-deps = true |
| `metadata-policy`       | Choose `match-artifact` or `prefer-wheel` metadata selection                                     |
| `build-policy`          | Permit or forbid metadata builds                                                                 |
| `assume-universal-deps` | Allow dependency declarations to be reused across targets                                        |
| `allow-zip-sdists`      | Admit or refuse [legacy ZIP sdists](distribution-policy.md#supported-source-archives)            |
| `uploaded-prior-to`     | Set a timezone-aware datetime or `P<n>D` cooldown; `false` removes the cutoff                    |
| `index`                 | Route the package to one declared index; no other index is consulted                             |
| `dependencies`          | Replace runtime dependency declarations with a list of PEP 508 requirements                      |
| `requires-python`       | Replace the Python requirement with a PEP 440 specifier                                          |
| `provides-extra`        | Replace the list of declared extra names                                                         |

See [Distribution policy](distribution-policy.md), [Build policy](build-policy.md) and
[Metadata overrides](metadata-overrides.md) for the value formats. An explicit scoped `false`
cancels a global `true` for boolean settings.

Index routing requires a bare package name. Nab must choose the index before it knows any release
versions:

```toml
[[tool.nab.package-rules]]
match = ["acme-core", "acme-plugins"]
index = "internal"
```

The `internal` index must be declared in `[[tool.nab.indexes]]`.

## Version ranges and conflicts

Two entries for the same package may set the same field only over disjoint version ranges:

```toml
[tool.nab.packages."numpy<=1.21"]
dist-policy = "sdist-only"

[[tool.nab.package-rules]]
match = ["numpy>=1.22"]
dist-policy = "wheel-only"
```

An overlap is a parse-time error. These entries conflict because both set `dist-policy` for
`lxml==2`:

```toml
[tool.nab.packages."lxml<=2"]
dist-policy = "sdist-only"

[tool.nab.packages."lxml>=1"]
dist-policy = "wheel-only"
```

Different fields can overlap. A rule may choose source installation while another chooses its
metadata source.

<a name="per-index-policy"></a>

## Index overrides

`[tool.nab.index.<name>]` applies to every package served by a declared index:

```toml
[tool.nab.index.pypi]
dist-policy = "wheel-only"

[tool.nab.index.internal]
build-policy = "build-remote"
uploaded-prior-to = "2026-05-01T00:00:00Z"
assume-fresh-seconds = 3600
```

Index overrides accept `dist-policy`, `metadata-policy`, `build-policy`, `assume-universal-deps`,
`allow-zip-sdists`, `uploaded-prior-to` and `assume-fresh-seconds`. Routing, version selectors and
metadata replacements are package-only settings.

`assume-fresh-seconds` extends a cached listing's freshness window. A release published during that
window stays hidden until it expires. Metadata and artifacts cached by hash are unaffected.

Package and index overrides have equal precedence. If both set a field for a candidate version, the
resolve fails. Remove one setting. Outside the package selector's version range, only the index
setting applies.

## Cooldown exceptions

A version-scoped cutoff can admit a reviewed release while other packages keep a seven-day cooldown:

```toml
[tool.nab]
uploaded-prior-to = "P7D"

[tool.nab.packages."certifi==2026.10.19"]
uploaded-prior-to = "2026-10-20T02:24:17Z"
```

This timestamp admits that release's original files. Later uploads of the same version remain
excluded.

For an index without upload times, disable the cutoff only there:

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

PyPI keeps the cooldown. Whenever a cutoff applies, files without upload times are excluded; see
[Index serialization](indexes.md#serialization).
