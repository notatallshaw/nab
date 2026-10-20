# Distribution policy

`[tool.nab].dist-policy` controls which artifact kinds the resolver considers and which end up in
the lockfile. The default `wheel-or-sdist` treats wheels and sdists symmetrically.

| Value                      | Wheel admitted to resolve? | Sdist admitted to resolve? | What ends up in the lock |
| -------------------------- | -------------------------- | -------------------------- | ------------------------ |
| `wheel-only`               | yes                        | no                         | wheel                    |
| `prefer-wheel`             | yes (preferred)            | yes (fallback)             | whichever was used       |
| `wheel-or-sdist` (default) | yes                        | yes                        | both                     |
| `sdist-only`               | no                         | yes                        | sdist                    |
| `sdist-install`            | yes                        | yes                        | sdist                    |

nab drops every sdist format but `.tar.gz` when it parses an index listing, before any policy sees
it, so a version whose only sdist is a `.zip` counts as shipping none. A wheel-admitting policy sees
only its wheels, and `sdist-only` and `sdist-install` skip it, since both need an sdist to lock.

`wheel-only` is the equivalent of pip's `--only-binary :all:` and `sdist-only` of
`--no-binary :all:`, scoped per package or per index via an override.

A wheel without a PEP 658 sidecar still resolves under any wheel-admitting policy. nab recovers its
METADATA with an HTTP range read. If the index cannot serve usable ranges, nab downloads the whole
wheel instead.

Use `sdist-install` when the lock must select an sdist, typically so an installer can link the
package against system libraries.

The lockfile pins only the sdist, so `pip install --require-hashes` materialises that archive.

For resolution, nab prefers wheel metadata and falls back to the sdist when no wheel exists. Dynamic
sdist metadata can still require a build allowed by `build-policy`.

A version with no sdist is skipped, as under `sdist-only`. The resolver selects an allowed version
that ships one.

Scope the policy to a subset of packages with a per-package override:

```toml
[[tool.nab.package-rules]]
match = ["lxml", "xmlsec"]
dist-policy = "sdist-install"
```

The same five values are accepted. Package names are canonicalised, so `Foo-Bar`, `foo_bar`, and
`foo-bar` refer to the same package.

To trust dependencies in a pre-PEP-643 sdist's `PKG-INFO` without verification, use the table form.
This flag defaults to `false`; see [Build policy](build-policy.md) for when a backend is needed.

```toml
[tool.nab]
dist-policy = { policy = "wheel-or-sdist", trust-unverified-deps = false }
```
