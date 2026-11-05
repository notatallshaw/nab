# Distribution policy

Set `dist-policy` to choose installation artifacts and `metadata-policy` to choose dependency
metadata. Put project defaults under `[tool.nab]`; use
[package or index overrides](package-policy.md) for exceptions.

## Installation artifacts

The distribution policy applies to index-served releases:

| `dist-policy`              | Artifacts recorded in the lock                           |
| -------------------------- | -------------------------------------------------------- |
| `wheel-only`               | Wheels; skip releases without a compatible wheel         |
| `wheel-or-sdist` (default) | Available wheels and sdist                               |
| `sdist-if-needed`          | Wheels, plus the sdist when wheel coverage is incomplete |
| `sdist-only`               | Sdist; skip releases without an admitted sdist           |

`resolution` chooses versions within these restrictions. With `sdist-if-needed`, a newer release
needing a source build can still win over an older release with wheels.

For installation artifacts, `wheel-only` corresponds to pip's `--only-binary=:all:` and `sdist-only`
to `--no-binary=:all:`.

## Dependency metadata

Metadata selection is separate from installation. For example, Nab can read wheel metadata while
recording only the source archive:

```toml
[tool.nab.packages.lxml]
dist-policy = "sdist-only"
metadata-policy = "prefer-wheel"
```

The metadata policy determines which artifact supplies dependencies:

| `metadata-policy`          | Metadata source                                                                  |
| -------------------------- | -------------------------------------------------------------------------------- |
| `match-artifact` (default) | The target's preferred install artifact: a compatible wheel, otherwise the sdist |
| `prefer-wheel`             | Prefer compatible wheel metadata even for source-only installation               |

When sources are allowed and no usable wheel remains, Nab reads source metadata. Dynamic metadata
may need a [permitted build](build-policy.md). Wheel metadata can come from a
[sidecar, range request or full download](lockfile.md#metadata-selection).

## Include sdists only when needed

Use this setting to omit source archives when wheels cover the environments that need each selected
release:

```toml
[tool.nab]
dist-policy = "sdist-if-needed"
```

For a release needed on both Linux and Windows, the result depends on its admitted artifacts:

| Available artifacts               | Recorded artifacts    |
| --------------------------------- | --------------------- |
| Linux wheel, Windows wheel, sdist | Both wheels           |
| Linux wheel and sdist             | Linux wheel and sdist |
| Sdist only                        | Sdist                 |

Only targets selecting that release count. A Windows-only dependency needs wheel coverage only on
Windows.

Coverage follows the configured Python, implementation, ABI and platform settings, upload cutoff and
yanking rules. Nab keeps the sdist if wheel tags are unknown or coverage cannot be proved.

Wheels must also cover the target's Python range. A wheel requiring Python 3.12.4 or newer leaves a
gap in a Python 3.12 target, so Nab keeps the sdist.

<a name="legacy-zip-sdists"></a>

## Supported source archives

Nab accepts `.tar.gz` sdists. Enable legacy `.zip` sdists for a package that needs them:

```toml
[tool.nab.packages."legacy-package==1.0"]
allow-zip-sdists = true
```

Other source archive formats are unsupported. When a release offers both `.tar.gz` and `.zip`, Nab
prefers `.tar.gz`. An index serving only refused ZIP archives still owns the package under the
[first-index policy](indexes.md); refusing those archives does not try another index.

Set `allow-zip-sdists` globally, per package or per index. A scoped `false` cancels a global `true`.
The CLI overrides are `--project-allow-zip-sdists` and `--no-project-allow-zip-sdists`.

Dynamic ZIP metadata needs the same build permissions as other sdists. Declared targets also need
[assume-universal-deps](build-policy.md#assuming-dependencies-are-universal) for host builds. Direct
`archive-sources` accept only `.tar.gz`.

## Migrating combined policies

Replace the removed `dist-policy` values with two settings at the original scope:

| Previous `dist-policy` | New `dist-policy` | New `metadata-policy` |
| ---------------------- | ----------------- | --------------------- |
| `prefer-wheel`         | `wheel-or-sdist`  | `prefer-wheel`        |
| `sdist-install`        | `sdist-only`      | `prefer-wheel`        |

For a table-form setting, replace its `policy` value and keep any `trust-unverified-deps` field. Add
`metadata-policy` beside `dist-policy`, outside that table.

The CLI form for source installation with wheel metadata is:

```bash
nab lock --project-dist-policy sdist-only --project-metadata-policy prefer-wheel
```

Python callers pair `DistPolicy.WHEEL_OR_SDIST` or `DistPolicy.SDIST_ONLY` with
`MetadataPolicy.PREFER_WHEEL`. Existing `wheel-only`, `wheel-or-sdist` and `sdist-only` settings
keep their behavior. Run `nab lock` to refresh existing locks with the new metadata-policy input.

## Trusting older source metadata

Set `trust-unverified-deps = true` to accept dependencies in a pre-PEP-643 sdist's `PKG-INFO`
without verification. The default is `false`.

```toml
[tool.nab.packages."legacy-package==1.0"]
dist-policy = { policy = "wheel-or-sdist", trust-unverified-deps = true }
```

Explicitly dynamic dependency fields still need another metadata source or a permitted build. To
reuse a same-version wheel's dependencies for a dynamic sdist, see
[assume-universal-deps](build-policy.md#assuming-dependencies-are-universal).
