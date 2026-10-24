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

## Supported artifacts

nab admits `.tar.gz` sdists by default. Legacy `.zip` sdists require `allow-zip-sdists = true`;
other sdist formats are unsupported. A release with only a refused `.zip` sdist has no usable sdist,
so `sdist-only` and `sdist-install` skip it. A wheel-admitting policy may still select its wheels.

`wheel-only` is the equivalent of pip's `--only-binary :all:` and `sdist-only` of
`--no-binary :all:`, scoped per package or per index via an override.

(legacy-zip-sdists)=

## Legacy ZIP sdists

`allow-zip-sdists` defaults to `false`. Enable it for a package whose required release was published
only as a ZIP sdist:

```toml
[tool.nab.packages."legacy-package == 1.0"]
allow-zip-sdists = true
```

Set it globally under `[tool.nab]`, or use [package and index overrides](package-policy.md). A
scoped `false` cancels a global `true`. For one command, use `--project-allow-zip-sdists` or
`--no-project-allow-zip-sdists`.

ZIP admission leaves build permission and metadata trust unchanged. A package that needs a backend
still requires `build-policy = "build-remote"`; declared targets also require
{ref}`assume-universal-deps <assuming-dependencies-are-universal>`. Direct `archive-sources` accept
only `.tar.gz` archives.

An index serving only ZIP sdists still owns the package under the first-index policy. Refusing those
sdists does not fall through to another index. When a release has both archive formats, nab prefers
`.tar.gz`.

## Reading dependency metadata

A wheel without a PEP 658 sidecar still resolves under any wheel-admitting policy. nab recovers its
METADATA with an HTTP range read. If the index cannot serve usable ranges, nab downloads the whole
wheel instead.

## Lock an sdist for installation

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

## Trusting older sdist metadata

To trust dependencies in a pre-PEP-643 sdist's `PKG-INFO` without verification, use the table form.
This flag defaults to `false`; see [Build policy](build-policy.md) for when a backend is needed.

```toml
[tool.nab]
dist-policy = { policy = "wheel-or-sdist", trust-unverified-deps = false }
```

To reuse same-version wheel dependencies for a dynamic sdist, use
{ref}`assume-universal-deps <assuming-dependencies-are-universal>` instead.
