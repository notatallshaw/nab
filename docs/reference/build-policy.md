# Build policy

`build-policy` controls where Nab may run a PEP 517 backend to obtain
[dependency metadata](https://packaging.python.org/en/latest/specifications/core-metadata/). Nab
first tries to read static metadata.

## Build permissions

A source's effective policy determines which metadata builds are allowed:

| `build-policy` | Sources whose backend may run                               |
| -------------- | ----------------------------------------------------------- |
| `never`        | None                                                        |
| `build-local`  | Local checkouts and workspace members                       |
| `build-remote` | Local sources, index sdists, VCS clones and direct archives |

The host-specific default is `build-local`. Declared platforms, including universal mode, default to
`never`; [host builds for declared targets](#declared-targets-and-host-builds) need an additional
opt-in.

To permit a metadata build for one package:

```toml
[tool.nab.packages.deepspeed]
build-policy = "build-remote"
```

A global `build-policy = "never"` can still be overridden per package or index. See
[Package and index policy](package-policy.md) for those forms and their conflict rules.

## Static metadata

Nab reads these sources before considering a backend:

| Source                                                        | Static metadata                                                                                                   |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Wheel                                                         | `METADATA`, from a sidecar, range request or full download                                                        |
| Index sdist                                                   | PEP 643 static dependency fields in `PKG-INFO`, or static dependency declarations in the bundled `pyproject.toml` |
| Local checkout, workspace member, VCS clone or direct archive | Static `[project]` fields in `pyproject.toml`                                                                     |

For PEP 643 sdists, `Requires-Dist` and `Provides-Extra` must not be listed in `Dynamic`.
Pre-PEP-643 `PKG-INFO` dependencies need
{ref}`an explicit trust setting <trusting-older-source-metadata>`.

Local and declared-source metadata may need a backend when `[project]` is missing or when `version`,
`requires-python`, `dependencies` or `optional-dependencies` are dynamic.

The default source archive format is `.tar.gz`. Legacy `.zip` sdists require
{ref}`allow-zip-sdists <legacy-zip-sdists>`. VCS repositories are cloned and direct archives are
hash-verified before their metadata is read.

### When metadata is unavailable

An index sdist is one candidate release. If its required build is forbidden or fails, Nab rejects
that release and can try another. If no release works, the resolution error includes the failures.

A declared local, VCS, archive or workspace source is the only source for its package. If metadata
cannot be obtained, the command fails; there is no alternate index release to try.

A [dependency override](metadata-overrides.md) can supply known dependencies without a build.
{ref}`assume-universal-deps <assuming-dependencies-are-universal>` can reuse same-version wheel
dependencies for a dynamic sdist.

## Build requirements

A backend runs in an isolated environment containing `[build-system].requires`. Nab installs these
requirements as wheels. A requirement available only as an sdist needs its own build first.

Set the permitted nesting depth with:

```toml
[tool.nab]
build-requires-depth = 0
```

The depth counts build environments opened beneath the first build:

| Depth         | Allowed nested builds                                                     |
| ------------- | ------------------------------------------------------------------------- |
| `0` (default) | None; report the requirement and build chain                              |
| `1`           | Build a requirement from source; that build's requirements must be wheels |
| `n`           | At most `n` nested build environments                                     |

Nab resolves the requirement as written. It can select a release needing a source build even when an
older release has wheels. The command fails if that build exceeds the depth limit.

A package or index `dist-policy = "sdist-only"` also requires a source build.
`metadata-policy = "prefer-wheel"` can supply wheel metadata, but installation still uses the source
archive. The refusal names the distribution policy when it excluded an available wheel.

### Policy inside build environments

The inner resolve inherits package and index overrides. A `build-policy` override may forbid a
metadata build there but cannot permit one. To require wheels for a build dependency:

```toml
[tool.nab.packages.meson]
dist-policy = "wheel-only"
```

`build-requires-depth` also applies to builds permitted by overrides, even when the global
`build-policy` is `never`.

### Build results and cycles

A built wheel must match the requested release. PEP 440 treats `1.0` and `1.0.0` as equal, but
`1.0+local` is a different version.

If its `Requires-Python` excludes the host, Nab records the restriction and resolves the build
requirements again. This can select an older allowed release. If none works, the command fails with
the Python incompatibility.

Nab refuses a build cycle such as:

```text
a 1.0 -> b 2.0 -> a 1.0
```

Two different versions of a package in one chain are allowed.

(declared-targets-and-host-builds)=

## Declared targets and host builds

A backend runs on the host and reports the host's metadata. Target declarations change the default
permission:

| Target                                                  | Metadata builds                                                          |
| ------------------------------------------------------- | ------------------------------------------------------------------------ |
| Host Python and platform                                | Apply the selected build policy                                          |
| Different Python on the host platform                   | Apply the policy and warn that the host interpreter supplies metadata    |
| Declared platform or implementation, including a matrix | Default to `never`; require `assume-universal-deps` and build permission |

The last row also applies when the declared platform matches the host, and to singleton matrices
resolved in `specific` mode. Without the dependency assumption, an explicit non-`never` build policy
for a declared target is a configuration error.

For a Python-only retarget, use `build-policy = "never"` to forbid host builds.

(assuming-dependencies-are-universal)=

## Assuming dependencies are universal

Enable `assume-universal-deps` only when a release's dependency declarations apply across the
selected targets:

```toml
[tool.nab.packages."watchdog==6.0.0"]
assume-universal-deps = true
```

For a dynamic sdist with no static `pyproject.toml` fallback, Nab can read `Requires-Dist` and
`Provides-Extra` from a same-version wheel. It evaluates their markers for each target. The wheel
need not be installable there; the sdist remains the install artifact.

The metadata source must pass upload cutoffs, distribution and metadata policies, yanking rules,
hash checks and release-identity checks. With source-only installation,
`metadata-policy = "prefer-wheel"` must also permit the wheel read.

The default is `false`. Set it globally, per package or per index, or use
`--project-assume-universal-deps`. A scoped `false` cancels a global `true`.

An incorrect assumption can omit target-specific dependencies from the lock. It does not make
incomplete dynamic `PKG-INFO` authoritative.

If no wheel supplies metadata, `build-policy = "build-remote"` permits a host metadata build.
Declared targets need both settings explicitly, and the host must be able to build the package. For
declared local, VCS and archive sources, use a bare package name in the override: their version is
unknown before metadata preparation.
