# Lock with multiple indexes

> [!WARNING]
> Multi-index support is experimental. The lockfile records which index supplied each package.
> Support for that field varies between installers, and the lockfile schema may change.

Use PyPI for most dependencies and the PyTorch CPU index for `torch`.

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

## Run

```bash
nab lock pyproject.toml
```

## How nab routes the request

Nab checks the indexes in declaration order for `numpy` and uses the first one that lists it. The
`index = "torch-cpu"` setting restricts `torch` to that index. If `torch-cpu` has no `torch`
release, the lock fails.

## Index order and routing scope

To give several packages the same index, replace the `[tool.nab.packages.torch]` table with a
package rule.

```toml
[[tool.nab.package-rules]]
match = ["torch", "torchvision", "torchaudio"]
index = "torch-cpu"
```

Nab chooses the index before it selects a version. Use bare package names for rules that set
`index`, without version specifiers or environment markers. Each package can have one index for the
whole resolve. See [Package and index policy](../reference/package-policy.md) for other settings.

## Per-index policy

To require wheels for packages from PyPI, set `dist-policy` on that index.

```toml
[tool.nab.index.pypi]
dist-policy = "wheel-only"
```

Packages from `torch-cpu` still use the global default. For mirrors that require frequent cache
revalidation, see [Per-index policy](../reference/package-policy.md#index-overrides) for the
`assume-fresh-seconds` setting.

## Conflicting package and index policies

The following settings conflict because both set `dist-policy` for `torch`. To keep the index's
policy, remove `dist-policy` from the package table and keep `index = "torch-cpu"`. To keep the
package's policy, remove the whole `[tool.nab.index.torch-cpu]` table. Removing just its
`dist-policy` line would leave an empty table, which Nab rejects.

```toml
[tool.nab.packages.torch]
index = "torch-cpu"
dist-policy = "wheel-only"

[tool.nab.index.torch-cpu]
dist-policy = "sdist-only"
```
